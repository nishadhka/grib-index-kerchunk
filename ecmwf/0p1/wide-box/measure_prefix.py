# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "eccodes", "imagecodecs", "gribberish==1.6.0"]
# ///
"""How much of each 0.1 deg message a wide box needs, measured on the 46
realized channels, every result checked bit for bit.

Box: lat 50 -> -40, lon -30 -> 130 (the wide CRMA region). ECMWF scans
north -> south, a full 360 deg row at a time, CCSDS-packed in RSIs of 4,096
samples (../README.md §8). Per message this records, from one full read:

  prefix   bytes from the message start to the end of the RSI holding row
           -40 deg  (option C: no offsets, no search; what a worker can fetch
           with one Range GET)
  band     bytes from the RSI holding row 50 deg to that same end (needs the
           RSI bit offset of the band start: a stored sidecar or the Rust
           search in ../lat-offset-test/rust)

and checks that decoding EXACTLY `prefix` bytes (truncated, libaec
AEC_NO_FLUSH from bit 0) gives the box bit-identical to eccodes and to
gribberish on the whole message. Writes the truncated prefixes for the Rust
check (rust_prefix/) when --dump is given.

    uv run measure_prefix.py --date 20261021 --run 00 [--members 0,1,25,50]
        [--steps 3,24,120,240] [--dump cases/]
"""
import argparse, ctypes, glob, json, os, sys, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
import numpy as np, eccodes, imagecodecs
from gribberish import parse_grib_message

G = "https://storage.googleapis.com/ecmwf-open-data/"
LAT_N, LAT_S, LON_W, LON_E = 50.0, -40.0, -30.0, 130.0
lib = ctypes.CDLL(glob.glob(os.path.join(os.path.dirname(imagecodecs.__file__),
                                         "..", "imagecodecs.libs", "libaec*"))[0])

# the 46 realized channels: (index param, level|None, out name)
SFC = [("tp", "tp"), ("tcwv", "tcwv"), ("msl", "msl"), ("sp", "sp"), ("ro", "ro"),
       ("2t", "t2m"), ("skt", "skt"), ("10u", "u10"), ("10v", "v10"), ("lsm", "lsm"),
       ("mucape", "cape"), ("2d", "d2m"), ("cp", "cp"), ("mcc", "mcc"), ("ssr", "ssr"),
       ("tciw", "tciw"), ("tclw", "tclw"), ("tcrw", "tcrw"), ("tcw", "tcw")]
PL = [("u", 925), ("v", 925), ("u", 850), ("v", 850), ("u", 700), ("v", 700),
      ("u", 500), ("v", 500), ("u", 200), ("v", 200), ("q", 925), ("q", 850),
      ("q", 700), ("q", 500), ("r", 850), ("r", 700), ("t", 850), ("t", 700),
      ("t", 500), ("gh", 500), ("vo", 850), ("vo", 700), ("vo", 500), ("d", 850),
      ("d", 700), ("d", 200), ("w", 700)]
CHANNELS = [(p, None, o) for p, o in SFC] + [(p, l, f"{p}{l}") for p, l in PL]
assert len(CHANNELS) == 46


class Strm(ctypes.Structure):
    _fields_ = [("next_in", ctypes.c_void_p), ("avail_in", ctypes.c_size_t),
                ("total_in", ctypes.c_size_t), ("next_out", ctypes.c_void_p),
                ("avail_out", ctypes.c_size_t), ("total_out", ctypes.c_size_t),
                ("bits_per_sample", ctypes.c_uint), ("block_size", ctypes.c_uint),
                ("rsi", ctypes.c_uint), ("flags", ctypes.c_uint),
                ("state", ctypes.c_void_p)]


def aec(data, K, nsamp, offsets=False, flush=1):
    """libaec decode of `data` from bit 0 -> (raw samples, RSI bit offsets|None)."""
    out = np.empty(nsamp * K["bps"], np.uint8)
    inb = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    s = Strm(); s.next_in, s.avail_in = ctypes.addressof(inb), len(data)
    s.next_out, s.avail_out = out.ctypes.data, out.nbytes
    s.bits_per_sample, s.block_size, s.rsi, s.flags = K["args"]
    assert lib.aec_decode_init(ctypes.byref(s)) == 0
    if offsets:
        lib.aec_decode_enable_offsets(ctypes.byref(s))
    rc = lib.aec_decode(ctypes.byref(s), flush)
    got = (out.nbytes - s.avail_out) // K["bps"]
    o = None
    if offsets:
        c = ctypes.c_size_t(); lib.aec_decode_count_offsets(ctypes.byref(s), ctypes.byref(c))
        a = (ctypes.c_size_t * c.value)(); lib.aec_decode_get_offsets(ctypes.byref(s), a, c)
        o = np.array(a, dtype=np.int64)
    lib.aec_decode_end(ctypes.byref(s))
    if rc != 0:
        raise ValueError(f"libaec rc {rc}")
    return out[:got * K["bps"]], o


def get(url, a=None, b=None):
    hdr = {} if a is None else {"Range": f"bytes={a}-{b}"}
    for i in range(8):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=120).read()
        except (urllib.error.URLError, TimeoutError) as e:
            if getattr(e, "code", 503) in (403, 404) or i == 7:
                raise
            time.sleep(min(2 ** i, 30))


def header(msg):
    h = eccodes.codes_new_from_message(msg)
    K = {k: eccodes.codes_get(h, k) for k in (
        "Ni", "Nj", "bitsPerValue", "ccsdsBlockSize", "ccsdsRsi", "ccsdsFlags",
        "referenceValue", "binaryScaleFactor", "decimalScaleFactor", "offsetSection7",
        "latitudeOfFirstGridPointInDegrees", "longitudeOfFirstGridPointInDegrees")}
    K["values"] = eccodes.codes_get_values(h) if K["bitsPerValue"] else None
    eccodes.codes_release(h)
    K["d0"] = K["offsetSection7"] + 5
    # libaec's output width: 1 byte to 8 bits, 2 to 16, 3 to 24 with AEC_DATA_3BYTE
    b = K["bitsPerValue"]
    K["bps"] = 1 if b <= 8 else 2 if b <= 16 else 3 if K["ccsdsFlags"] & 2 and b <= 24 else 4
    K["args"] = (K["bitsPerValue"], K["ccsdsBlockSize"], K["ccsdsRsi"], K["ccsdsFlags"])
    return K


def unpack(raw, K):
    if K["bps"] == 3:
        r = raw.reshape(-1, 3).astype(np.uint32)
        X = ((r[:, 0] << 16) | (r[:, 1] << 8) | r[:, 2]).astype(np.float64)
    else:
        X = raw.view({1: "u1", 2: ">u2", 4: ">u4"}[K["bps"]]).astype(np.float64)
    return (K["referenceValue"] + X * 2.0 ** K["binaryScaleFactor"]) / 10.0 ** K["decimalScaleFactor"]


def box_idx(K):
    ni, nj = K["Ni"], K["Nj"]
    d = 180.0 / (nj - 1)
    lon0 = K["longitudeOfFirstGridPointInDegrees"]
    lon0 = lon0 - 360 if lon0 >= 180 else lon0
    j0, j1 = round((90 - LAT_N) / d), round((90 - LAT_S) / d) + 1
    i0, i1 = round((LON_W - lon0) / d), round((LON_E - lon0) / d) + 1
    return j0, j1, i0, i1


def one(job, dump):
    tag, url, o, L = job
    msg = get(url, o, o + L - 1)
    K = header(msg)
    if not K["bitsPerValue"]:
        return dict(tag=tag, L=L, const=True)
    ni, nj = K["Ni"], K["Nj"]
    j0, j1, i0, i1 = box_idx(K)
    S = K["ccsdsBlockSize"] * K["ccsdsRsi"]
    data = msg[K["d0"]:L - 4]                       # Section 7 data, '7777' off
    raw, offs = aec(data, K, ni * nj, offsets=True)
    full = unpack(raw, K)[:ni * nj].reshape(nj, ni)   # libaec pads the last block
    k_end = (j1 * ni - 1) // S                      # RSI holding the last box sample
    end_bit = offs[k_end + 1] if k_end + 1 < len(offs) else 8 * len(data)
    prefix = K["d0"] + (end_bit + 7) // 8           # message bytes for option C
    k_beg = (j0 * ni) // S
    band = prefix - (K["d0"] + offs[k_beg] // 8)
    # option C, exactly `prefix` bytes, decoded as a worker would
    t = time.time()
    praw, _ = aec(msg[K["d0"]:prefix], K, j1 * ni, flush=0)
    t_dec = time.time() - t
    pbox = unpack(praw, K)[:j1 * ni].reshape(j1, ni)[j0:j1, i0:i1] if len(praw) >= j1 * ni * K["bps"] else None
    ecc = K["values"].reshape(nj, ni)[j0:j1, i0:i1]
    gm = parse_grib_message(msg, 0)
    grb = np.asarray(gm.data(), dtype=np.float64).reshape(nj, ni)[j0:j1, i0:i1]
    r = dict(tag=tag, L=L, prefix=int(prefix), band=int(band), d0=K["d0"],
             rows=j1, ni=ni, t_dec=t_dec,
             exact_eccodes=pbox is not None and np.array_equal(pbox, ecc),
             exact_gribberish=pbox is not None and np.array_equal(pbox.astype(np.float32), grb.astype(np.float32)),
             full_vs_eccodes=bool(np.array_equal(full[j0:j1, i0:i1], ecc)))
    if dump:
        n = tag.replace(" ", "_").replace("/", "-")
        with open(os.path.join(dump, n + ".prefix"), "wb") as f:
            f.write(msg[:prefix])
        np.save(os.path.join(dump, n + ".box.npy"), ecc.astype(np.float32))
        r["case"] = n
        r.update(bits=K["bitsPerValue"], bs=K["ccsdsBlockSize"], rsi=K["ccsdsRsi"],
                 ref=K["referenceValue"], E=K["binaryScaleFactor"], D=K["decimalScaleFactor"],
                 j0=j0, j1=j1, i0=i0, i1=i1)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="20261021"); ap.add_argument("--run", default="00")
    ap.add_argument("--members", default="0,1,25,50")
    ap.add_argument("--steps", default="3,24,120,240")
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--dump")
    ap.add_argument("--json", default="measure_prefix.json")
    a = ap.parse_args()
    if a.dump:
        os.makedirs(a.dump, exist_ok=True)
    d, r = a.date, a.run
    jobs = []
    for step in map(int, a.steps.split(",")):
        for m in map(int, a.members.split(",")):
            for kind in ("sfc", "pl"):
                stem = (f"{d}/{r}z/ifs/0p1/oper/{d}{r}0000-{step}h-oper-fc-{kind}" if m == 0 else
                        f"{d}/{r}z/ifs/0p1/enfo/{d}{r}0000-{step}h-enfo-ef-{kind}")
                recs = [json.loads(l) for l in get(G + stem + ".index").decode().splitlines() if l.strip()]
                for p, lev, out in CHANNELS:
                    if (lev is None) != (kind == "sfc"):
                        continue
                    hit = [x for x in recs if x["param"] == p and x.get("number", "0") == str(m)
                           and (lev is None or x.get("levelist") == str(lev))]
                    assert len(hit) == 1, (stem, p, lev, m, len(hit))
                    jobs.append((f"{out} m{m:02d} s{step:03d}", G + stem + ".grib2",
                                 hit[0]["_offset"], hit[0]["_length"]))
    print(f"{d} {r}z 0.1 deg, box lat {LAT_N}..{LAT_S} lon {LON_W}..{LON_E}: "
          f"{len(jobs)} messages", flush=True)
    t0 = time.time()
    rows = []
    with ThreadPoolExecutor(a.threads) as ex:
        for x in ex.map(lambda j: one(j, a.dump), jobs):
            rows.append(x)
            if x.get("const"):
                print(f"  {x['tag']:22} constant field, no packed data", flush=True)
                continue
            ok = x["exact_eccodes"] and x["exact_gribberish"]
            print(f"  {x['tag']:22} {'OK ' if ok else 'BAD'} {x['L']/1e6:5.2f} MB  "
                  f"prefix {100*x['prefix']/x['L']:5.1f}%  band {100*x['band']/x['L']:5.1f}%", flush=True)
    json.dump(rows, open(a.json, "w"), indent=0)
    v = [x for x in rows if not x.get("const")]
    L = sum(x["L"] for x in v); P = sum(x["prefix"] for x in v); B = sum(x["band"] for x in v)
    fr = np.array([x["prefix"] / x["L"] for x in v])
    print(f"\n{sum(x['exact_eccodes'] for x in v)}/{len(v)} prefix decodes == eccodes, "
          f"{sum(x['exact_gribberish'] for x in v)}/{len(v)} == gribberish, "
          f"{sum(x['full_vs_eccodes'] for x in v)}/{len(v)} full libaec == eccodes; "
          f"{len(rows)-len(v)} constant")
    print(f"bytes  full {L/1e9:.3f} GB  prefix {P/1e9:.3f} GB ({100*P/L:.1f}%, saves {100-100*P/L:.1f}%)  "
          f"band {B/1e9:.3f} GB ({100*B/L:.1f}%, saves {100-100*B/L:.1f}%)")
    print(f"prefix per message: mean {100*fr.mean():.1f}%  min {100*fr.min():.1f}%  max {100*fr.max():.1f}%")
    by = {}
    for x in v:
        by.setdefault(x["tag"].split()[0], []).append(x)
    print("by channel (prefix % of bytes, byte-weighted):")
    print("  " + "  ".join(f"{k} {100*sum(y['prefix'] for y in g)/sum(y['L'] for y in g):.0f}"
                           for k, g in sorted(by.items(), key=lambda kv: sum(y['prefix'] for y in kv[1])/sum(y['L'] for y in kv[1]))))
    print(f"wall {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
