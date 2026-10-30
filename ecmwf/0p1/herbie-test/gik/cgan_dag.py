"""Worker-side tasks: read 0.1 deg IFS messages for a box, write realized blocks.

Shipped AS ONE FILE to every Frisky worker (step3_deploy_workers.py) and also
used in-process by the local cluster. Frisky pickles task functions by
reference, so this module must import nothing from this folder: the box and
field table arrive as task arguments.

The unit of work is a BLOCK = one (field, step): 51 members x box, one Zarr
chunk. `read_write_block` reads its members with a small thread pool into one
preallocated array and writes it from the same worker. (Separate read tasks
plus a gather made every member's box cross the LAN to the writing worker --
2.6-5.7 msg/s instead of 16 on the wide box. grib-index-kerchunk
ecmwf/0p1/wide-box/README.md section 4.)

Two ways to read a message
--------------------------
read_prefix   (default) one HTTP Range GET of the message PREFIX that holds the
              box's rows, decoded with libaec. ECMWF packs 0.1 deg fields with
              CCSDS, north -> south, rows whole, and the first RSI starts at
              bit 0 of Section 7 -- so rows 90N..15S are a prefix of the
              message. For this box that is ~60% of the bytes instead of 100%.
              gribberish 1.6.0 rejects a truncated message, so this is a
              stand-in until its codec takes a row limit; it is bit-exact
              against gribberish and eccodes (736/736 messages, wide-box
              README section 1; step5_check.py re-checks every run).
read_virtual  the whole message through the virtual Icechunk store, decoded by
              gribberish. Slower, used for checks and as `--full-read`.

Byte ranges for read_prefix come from the GCS .index files -- the same ones
the virtual store was built from (step1) -- because icechunk 2.1 has no public
call returning a chunk's virtual reference. Data GETs go to AWS first and fall
back to the byte-identical GCS copy on 503 Slow Down or any error.
"""
from __future__ import annotations

import os
import threading
import time

import numpy as np

# The AWS_* trap: icechunk caches object-store config process-wide, and a Ceph
# AWS_ENDPOINT_URL in the environment would redirect the anonymous reads of
# s3://ecmwf-forecasts. Gone before anything opens a store, here and in every
# worker spawned from this process.
for _k in [k for k in os.environ if k.startswith("AWS_")]:
    os.environ.pop(_k)

GCS_HTTP = "https://storage.googleapis.com/ecmwf-open-data/"
AWS_HTTP = "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com/"
HOME = os.environ.get("CGAN_DAG_HOME", "/tmp/frisky-cgan-ea")

_IDX: dict = {}
_IDX_LOCK = threading.Lock()
_NEED: dict = {}               # (param, level) -> largest prefix share needed so far
_LIBAEC: list = []
_VD: dict = {}
_STATS = {"msgs": 0, "bytes_fetched": 0, "bytes_full": 0, "gets": 0, "topups": 0,
          "t_get": 0.0, "t_decode": 0.0, "src_aws": 0, "src_gcs": 0, "idx_files": 0}


# ── HTTP ────────────────────────────────────────────────────────────────────
def _http(url, a=None, b=None):
    """GET (optionally a byte range). A data URL tries AWS, then GCS."""
    import random
    import urllib.request
    hdr = {} if a is None else {"Range": f"bytes={a}-{b}"}
    urls = [url]
    if url.startswith(GCS_HTTP) and url.endswith(".grib2"):
        urls = [AWS_HTTP + url[len(GCS_HTTP):], url]
    delay = 0.5
    for attempt in range(8):
        for u in urls:
            try:
                out = urllib.request.urlopen(urllib.request.Request(u, headers=hdr),
                                             timeout=120).read()
                if len(urls) > 1:
                    _STATS["src_aws" if u.startswith(AWS_HTTP) else "src_gcs"] += 1
                return out
            except Exception as exc:                          # noqa: BLE001
                if getattr(exc, "code", 503) in (400, 403, 404) and u == urls[-1]:
                    raise
        if attempt == 7:
            raise RuntimeError(f"GET {url} [{a}-{b}] failed on every source")
        time.sleep(delay + random.random() * delay)
        delay = min(delay * 2, 30)


def _index(date8, run, step_h, stream, kind):
    """{(param, level, number): (offset, length)} of one .index, cached per process.

    A duplicate key keeps the LAST line, as the virtual store does.
    """
    import json
    key = (date8, run, step_h, stream, kind)
    if key in _IDX:
        return _IDX[key]
    with _IDX_LOCK:
        if key in _IDX:
            return _IDX[key]
        stem = (f"{date8}/{run}z/ifs/0p1/{stream}/{date8}{run}0000-{step_h}h-"
                f"{'oper-fc' if stream == 'oper' else 'enfo-ef'}-{kind}")
        refs = {}
        for line in _http(GCS_HTTP + stem + ".index").decode().splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            if d["_length"] == -1:
                continue
            lev = int(d["levelist"]) if d.get("levtype") == "pl" else None
            refs[(d["param"], lev, int(d.get("number", 0)))] = (int(d["_offset"]),
                                                                int(d["_length"]))
        _IDX[key] = (GCS_HTTP + stem + ".grib2", refs)
        _STATS["idx_files"] += 1
        return _IDX[key]


# ── GRIB2, just enough of it ────────────────────────────────────────────────
def _sm(b):
    """GRIB sign-and-magnitude integer."""
    v = int.from_bytes(b, "big")
    top = 1 << (8 * len(b) - 1)
    return -(v & (top - 1)) if v & top else v


def _header(msg):
    """Grid (template 3.0) and CCSDS packing (template 5.42) constants."""
    import struct
    pos, sec = 16, {}
    while pos + 5 <= len(msg) and msg[pos:pos + 4] != b"7777":
        n = msg[pos + 4]
        sec[n] = (pos, int.from_bytes(msg[pos:pos + 4], "big"))
        if n == 7:
            break
        pos += sec[n][1]
    if 7 not in sec:
        raise ValueError("prefix too short to reach Section 7")
    s3, s5 = msg[sec[3][0]:], msg[sec[5][0]:]
    if int.from_bytes(s3[12:14], "big") != 0 or int.from_bytes(s5[9:11], "big") != 42:
        raise ValueError("not grid 3.0 + packing 5.42")
    if msg[sec[6][0] + 5] != 255 or s3[71] != 0:
        raise ValueError("bitmap present or scan mode not N->S, W->E")
    K = dict(ni=int.from_bytes(s3[30:34], "big"), nj=int.from_bytes(s3[34:38], "big"),
             la1=_sm(s3[46:50]) / 1e6, lo1=_sm(s3[50:54]) / 1e6,
             R=struct.unpack(">f", s5[11:15])[0], E=_sm(s5[15:17]), D=_sm(s5[17:19]),
             bits=s5[19], flags=s5[21], bs=s5[22], rsi=int.from_bytes(s5[23:25], "big"),
             d0=sec[7][0] + 5)
    b = K["bits"]
    K["bps"] = 1 if b <= 8 else 2 if b <= 16 else 3 if (K["flags"] & 2 and b <= 24) else 4
    return K


def _aec(data, K, nsamp):
    """libaec with AEC_NO_FLUSH: decode up to nsamp samples from bit 0.

    Returns (raw bytes, samples decoded, input bytes consumed).
    """
    import ctypes
    import glob
    import imagecodecs
    if not _LIBAEC:
        lib = ctypes.CDLL(glob.glob(os.path.join(os.path.dirname(imagecodecs.__file__),
                                                 "..", "imagecodecs.libs", "libaec*"))[0])

        class Strm(ctypes.Structure):
            _fields_ = [("next_in", ctypes.c_void_p), ("avail_in", ctypes.c_size_t),
                        ("total_in", ctypes.c_size_t), ("next_out", ctypes.c_void_p),
                        ("avail_out", ctypes.c_size_t), ("total_out", ctypes.c_size_t),
                        ("bits_per_sample", ctypes.c_uint), ("block_size", ctypes.c_uint),
                        ("rsi", ctypes.c_uint), ("flags", ctypes.c_uint),
                        ("state", ctypes.c_void_p)]
        _LIBAEC[:] = [lib, Strm]
    lib, Strm = _LIBAEC
    out = np.empty(nsamp * K["bps"], np.uint8)
    inb = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    s = Strm()
    s.next_in, s.avail_in = ctypes.addressof(inb), len(data)
    s.next_out, s.avail_out = out.ctypes.data, out.nbytes
    s.bits_per_sample, s.block_size, s.rsi, s.flags = K["bits"], K["bs"], K["rsi"], K["flags"]
    if lib.aec_decode_init(ctypes.byref(s)) != 0:
        raise ValueError("aec_decode_init failed")
    rc = lib.aec_decode(ctypes.byref(s), 0)
    got, used = (out.nbytes - s.avail_out) // K["bps"], int(s.total_in)
    lib.aec_decode_end(ctypes.byref(s))
    if rc != 0:
        raise ValueError(f"libaec error {rc}")
    return out[:got * K["bps"]], got, used


# ── the two readers ─────────────────────────────────────────────────────────
def read_prefix(param, level, date8, run, member, step_h, box, acc=None):
    """ONE message -> the box (float32, lat descending), from a message prefix.

    `acc`, if given, is a dict that receives this message's byte/GET/time counts
    (read_write_block passes one per block and returns it to the client).
    """
    url, refs = _index(date8, run, step_h, "oper" if member == 0 else "enfo",
                       "sfc" if level is None else "pl")
    off, length = refs[(param, level, member)]
    deg = box["deg"]
    ny = int(round((box["lat_max"] - box["lat_min"]) / deg)) + 1
    nx = int(round((box["lon_max"] - box["lon_min"]) / deg)) + 1
    rows_share = (90.0 - box["lat_min"]) / 180.0
    # First GET: the largest share this process has needed for the field, plus
    # 1.5%; before any is known, the row share with generous headroom. A short
    # guess costs a top-up GET, never a wrong value.
    need = _NEED.get((param, level))
    frac = need + 0.015 if need else min(1.0, rows_share * 1.35 + 0.03)
    end = min(length, int(frac * length) + 4096)
    t = time.time()
    msg = _http(url, off, off + end - 1)
    gets, tdec = 1, 0.0
    while True:
        K = _header(msg)
        if K["bits"] == 0:                       # constant field: R is every value
            box_arr = np.full((ny, nx), K["R"] / 10.0 ** K["D"], np.float32)
            break
        dl = 180.0 / (K["nj"] - 1)
        lo1 = K["lo1"] - 360 if K["lo1"] >= 180 else K["lo1"]
        j0 = round((K["la1"] - box["lat_max"]) / dl)
        i0 = round((box["lon_min"] - lo1) / dl)
        j1, ni = j0 + ny, K["ni"]
        data = msg[K["d0"]:length - 4] if len(msg) == length else msg[K["d0"]:]
        t1 = time.time()
        raw, got, used = _aec(data, K, j1 * ni)
        tdec += time.time() - t1
        if got >= j1 * ni:
            share = (K["d0"] + used) / length
            if share > _NEED.get((param, level), 0.0):
                _NEED[(param, level)] = share
            if K["bps"] == 3:
                r3 = raw.reshape(-1, 3).astype(np.uint32)
                X = (r3[:, 0] << 16) | (r3[:, 1] << 8) | r3[:, 2]
            else:
                X = raw.view({1: "u1", 2: ">u2", 4: ">u4"}[K["bps"]])
            X = X[:j1 * ni].reshape(j1, ni)[j0:j1, i0:i0 + nx].astype(np.float64)
            box_arr = ((K["R"] + X * 2.0 ** K["E"]) / 10.0 ** K["D"]).astype(np.float32)
            break
        if len(msg) >= length:
            raise ValueError(f"{param} {level} m{member} s{step_h}: whole message "
                             f"decoded to {got} samples, need {j1 * ni}")
        more = min(length - len(msg), int(0.05 * length) + 4096)
        msg += _http(url, off + len(msg), off + len(msg) + more - 1)
        gets += 1
    this = dict(msgs=1, bytes_fetched=len(msg), bytes_full=length, gets=gets,
                topups=gets - 1, t_decode=tdec, t_get=time.time() - t - tdec)
    with _IDX_LOCK:
        for k, v in this.items():
            _STATS[k] += v
            if acc is not None:
                acc[k] = acc.get(k, 0) + v
    if _STATS["msgs"] % 200 == 0:
        flush_stats()
    return box_arr


def _vd_dataset(vd_spec, run):
    """The virtual store's <0p1>/<run>z group, opened once per process."""
    key = (vd_spec, run)
    if key in _VD:
        return _VD[key]
    import icechunk as ic
    import xarray as xr
    import gribberish.zarr  # noqa: F401
    if vd_spec.startswith("sourcecoop:"):
        st = ic.s3_storage(bucket="e4drr-project", prefix=vd_spec[11:].strip("/"),
                           endpoint_url="https://data.source.coop", region="us-east-1",
                           anonymous=True, from_env=False, force_path_style=True)
    else:
        st = ic.local_filesystem_storage(vd_spec.split(":", 1)[-1])
    auth = ic.containers_credentials({"s3://ecmwf-forecasts/": ic.s3_anonymous_credentials(),
                                      "gs://ecmwf-open-data/": ic.GcsCredentials.Anonymous()})
    cfg = ic.RepositoryConfig.default()
    cfg.manifest = ic.ManifestConfig(preload=ic.ManifestPreloadConfig(
        max_total_refs=0, max_arrays_to_scan=0))
    repo = ic.Repository.open(st, config=cfg, authorize_virtual_chunk_access=auth)
    ds = xr.open_zarr(repo.readonly_session("main").store, group=f"0p1/{run}z",
                      consolidated=False, zarr_format=3, decode_timedelta=True, chunks=None)
    _VD[key] = ds
    return ds


def read_virtual(vd_spec, var, level, date8, run, member, step_h, box):
    """ONE whole message through the virtual store -> the box (float32)."""
    import random
    ds = _vd_dataset(vd_spec, run)
    da = ds[var]
    if level is not None:
        da = da.sel(isobaricInhPa=float(level))
    t = np.datetime64(f"{date8[:4]}-{date8[4:6]}-{date8[6:]}T{run}:00")
    da = da.sel(time=t, number=member, step=np.timedelta64(step_h, "h"),
                latitude=slice(box["lat_max"], box["lat_min"]),
                longitude=slice(box["lon_min"], box["lon_max"]))
    delay = 0.5
    for attempt in range(8):
        try:
            return np.asarray(da.values, dtype=np.float32)
        except Exception as exc:                              # noqa: BLE001
            m = str(exc)
            if attempt == 7 or not any(s in m for s in ("SlowDown", "503", "520", "502",
                                                        "504", "no root element")):
                raise
            time.sleep(delay + random.random() * delay)
            delay = min(delay * 2, 30)


# ── the task ────────────────────────────────────────────────────────────────
def read_write_block(fork, name, si, field, date8, run, members, step_h, box,
                     pool=4, vd_spec=None, full_read=False):
    """Read one (field, step) for all members and write it as one chunk.

    `field` = (index param, level, virtual-store var). Returns (fork, counts):
    the client merges the fork's changeset and sums the counts. A running task
    holds one block (51 x 401 x 331 float32 = 27 MB here) plus the messages in
    flight.
    """
    import concurrent.futures as cf
    import zarr
    param, level, var = field
    deg = box["deg"]
    ny = int(round((box["lat_max"] - box["lat_min"]) / deg)) + 1
    nx = int(round((box["lon_max"] - box["lon_min"]) / deg)) + 1
    block = np.empty((len(members), ny, nx), np.float32)
    acc = {}
    t0 = time.time()

    def one(i):
        if full_read:
            block[i] = read_virtual(vd_spec, var, level, date8, run, members[i], step_h, box)
        else:
            block[i] = read_prefix(param, level, date8, run, members[i], step_h, box, acc)

    with cf.ThreadPoolExecutor(pool) as ex:
        list(ex.map(one, range(len(members))))
    t1 = time.time()
    zarr.open_array(fork.store, path=name, mode="r+")[0, si] = block
    acc.update(t_read_block=t1 - t0, t_write_block=time.time() - t1)
    return fork, acc


def stats():
    import socket
    return dict(_STATS, need={f"{p}{'' if l is None else l}": round(v, 4)
                              for (p, l), v in _NEED.items()},
                host=socket.gethostname(), pid=os.getpid())


def flush_stats():
    """Counters to HOME/prefix-stats-<pid>.json, summed by mem_monitor.py --stats."""
    import json
    try:
        os.makedirs(HOME, exist_ok=True)
        with open(f"{HOME}/prefix-stats-{os.getpid()}.json", "w") as f:
            json.dump(stats(), f)
    except OSError:
        pass
    return True
