"""Latitude-band references for GIK pars: build once, read a band later.

A GIK par chunk is `[url, offset, length]` for one whole GRIB message. ECMWF
packs every field as CCSDS (`grid_ccsds`), whose RSIs (reference sample
intervals, 4096 values each) decode independently and run north to south. So a
latitude band is one contiguous byte range -- if you know where each RSI starts.

`rsi_record(msg)` decodes one full message ONCE and returns everything needed
to read any latitude band of it later: the RSI bit offsets plus the Section 5
unpacking constants. `band_read(chunk, rec, lat_n, lat_s)` then fetches only
the band's bytes (one range GET) and decodes them. Longitude is always the full
360 deg: an RSI is longer than a row.

Byte source: the par says s3://ecmwf-forecasts/...; `chunk_url()` swaps the
prefix like stage 2 does. GIK_CHUNK_SOURCE=gcs reads gs://ecmwf-open-data (a
byte-identical mirror from 2023-07-12), default s3.
"""
from __future__ import annotations

import ctypes, glob, json, os, struct, time, urllib.error, urllib.request

import numpy as np
import imagecodecs

_lib = ctypes.CDLL(glob.glob(os.path.join(os.path.dirname(imagecodecs.__file__),
                                          "..", "imagecodecs.libs", "libaec*"))[0])
_HTTP = {"s3": "https://ecmwf-forecasts.s3.amazonaws.com/",
         "gcs": "https://storage.googleapis.com/ecmwf-open-data/"}
GCS_FIRST_DATE = "20230712"


class _Strm(ctypes.Structure):
    _fields_ = [("next_in", ctypes.c_void_p), ("avail_in", ctypes.c_size_t),
                ("total_in", ctypes.c_size_t), ("next_out", ctypes.c_void_p),
                ("avail_out", ctypes.c_size_t), ("total_out", ctypes.c_size_t),
                ("bits_per_sample", ctypes.c_uint), ("block_size", ctypes.c_uint),
                ("rsi", ctypes.c_uint), ("flags", ctypes.c_uint),
                ("state", ctypes.c_void_p)]


def _stream(buf, rec, out):
    s = _Strm(); inb = (ctypes.c_ubyte * len(buf)).from_buffer_copy(buf)
    s.next_in, s.avail_in = ctypes.addressof(inb), len(buf)
    s.next_out, s.avail_out = out.ctypes.data, out.nbytes
    s.bits_per_sample, s.block_size = rec["bps"], rec["block_size"]
    s.rsi, s.flags = rec["rsi"], rec["ccsds_flags"]
    s._keep = inb
    if _lib.aec_decode_init(ctypes.byref(s)) != 0:
        raise ValueError("libaec refused the stream parameters")
    return s


def chunk_url(url: str, source: str | None = None) -> str:
    """Par url -> HTTPS url of the GRIB file (pars omit the .grib2 suffix)."""
    source = source or os.environ.get("GIK_CHUNK_SOURCE", "s3")
    key = url.split("s3://ecmwf-forecasts/", 1)[1]
    if not key.endswith(".grib2"):
        key += ".grib2"
    if source == "gcs" and key[:8] < GCS_FIRST_DATE:
        source = "s3"                                  # GCS does not have it
    return _HTTP[source] + key


def get_range(url: str, start: int, length: int) -> bytes:
    for i in range(10):
        try:
            req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{start + length - 1}"})
            return urllib.request.urlopen(req, timeout=60).read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            if getattr(e, "code", 503) not in (429, 500, 503):
                raise
            time.sleep(min(2 ** i, 30))
    raise RuntimeError(f"source kept throttling: {url}")


def _section5(msg: bytes):
    """Walk GRIB2 sections: Section 5 constants, grid size, and the data start."""
    p, out = 16, {}
    while p < len(msg) - 4:
        L, num = struct.unpack(">IB", msg[p:p + 5])
        if num == 3:
            out["ni"], out["nj"] = struct.unpack(">II", msg[p + 30:p + 38])
        elif num == 5:
            s = msg[p:p + L]
            if struct.unpack(">H", s[9:11])[0] != 42:
                raise ValueError("not CCSDS packing (template 5.42)")
            sm = lambda b: -(b & 0x7FFF) if b & 0x8000 else b
            out.update(ref_value=struct.unpack(">f", s[11:15])[0],
                       bin_scale=sm(struct.unpack(">H", s[15:17])[0]),
                       dec_scale=sm(struct.unpack(">H", s[17:19])[0]),
                       bps=s[19], ccsds_flags=s[21], block_size=s[22],
                       rsi=struct.unpack(">H", s[23:25])[0])
        elif num == 7:
            out["d0"] = p + 5
            return out
        p += L
    raise ValueError("no Section 7")


def rsi_record(msg: bytes) -> dict:
    """ONE full decode -> what band_read needs, for any latitude band, forever."""
    rec = _section5(msg)
    n = rec["ni"] * rec["nj"]
    if rec["bps"] == 0:                                # constant field: nothing to index
        rec["rsi_bit_offsets"] = b""; return rec
    bpsamp = 2 if rec["bps"] <= 16 else 4
    out = np.empty(n * bpsamp, np.uint8)
    s = _stream(msg[rec["d0"]:], rec, out)
    _lib.aec_decode_enable_offsets(ctypes.byref(s))
    if _lib.aec_decode(ctypes.byref(s), 1) != 0:
        raise ValueError("libaec decode failed")
    c = ctypes.c_size_t(); _lib.aec_decode_count_offsets(ctypes.byref(s), ctypes.byref(c))
    o = (ctypes.c_size_t * c.value)(); _lib.aec_decode_get_offsets(ctypes.byref(s), o, c)
    _lib.aec_decode_end(ctypes.byref(s))
    offs = np.array(o, dtype=np.int64)
    assert offs[-1] < 2 ** 32
    rec["rsi_bit_offsets"] = offs.astype("<u4").tobytes()   # bits from the first data byte
    rec["_full_codes"] = out                           # for validation only; not stored
    return rec


def _unpack(raw, rec):
    X = raw.view(">u2" if rec["bps"] <= 16 else ">u4").astype(np.float64)
    return (rec["ref_value"] + X * 2.0 ** rec["bin_scale"]) / 10.0 ** rec["dec_scale"]   # float64, as eccodes


def band_rows(rec, lat_n: float, lat_s: float):
    """Row range of a latitude band on this message's grid (N -> S scan, +90 first)."""
    dlat = 180.0 / (rec["nj"] - 1)
    return round((90 - lat_n) / dlat), round((90 - lat_s) / dlat) + 1


def band_plan(chunk, rec, lat_n, lat_s):
    """Which bytes of the GRIB file hold the band: (file_start, length, k0, k1, j0, j1)."""
    url, msg_off, msg_len = chunk
    offs = np.frombuffer(rec["rsi_bit_offsets"], "<u4").astype(np.int64)
    rs = rec["block_size"] * rec["rsi"]
    j0, j1 = band_rows(rec, lat_n, lat_s)
    p0, p1 = j0 * rec["ni"], j1 * rec["ni"]
    k0, k1 = p0 // rs, (p1 - 1) // rs
    b0 = offs[k0] // 8
    b1 = (offs[k1 + 1] + 7) // 8 if k1 + 1 < len(offs) else msg_len - rec["d0"]
    return msg_off + rec["d0"] + int(b0), int(b1 - b0), k0, k1, j0, j1


def band_read(chunk, rec, lat_n: float, lat_s: float, source: str | None = None):
    """Fetch and decode ONLY the latitude band. Returns (values[rows, ni], lats, nbytes)."""
    start, length, k0, k1, j0, j1 = band_plan(chunk, rec, lat_n, lat_s)
    lats = 90 - np.arange(j0, j1) * 180.0 / (rec["nj"] - 1)
    if rec["bps"] == 0:
        return np.full((j1 - j0, rec["ni"]), rec["ref_value"], np.float64), lats, 0
    part = get_range(chunk_url(chunk[0], source), start, length)
    return decode_band(part, rec, k0, k1, j0, j1), lats, len(part)


def decode_band(part, rec, k0, k1, j0, j1):
    offs = np.frombuffer(rec["rsi_bit_offsets"], "<u4").astype(np.int64)
    base = (offs[k0] // 8) * 8
    rel = (ctypes.c_size_t * len(offs))(*np.maximum(offs - base, 0).tolist())
    rs, n = rec["block_size"] * rec["rsi"], rec["ni"] * rec["nj"]
    bpsamp = 2 if rec["bps"] <= 16 else 4
    raw = np.empty((k1 - k0 + 1) * rs * bpsamp, np.uint8)
    for i, k in enumerate(range(k0, k1 + 1)):
        take = min(rs, n - k * rs) * bpsamp
        s = _stream(part, rec, raw[i * rs * bpsamp:][:take])
        rc = _lib.aec_decode_range(ctypes.byref(s), rel, len(offs), k * rs * bpsamp, take)
        _lib.aec_decode_end(ctypes.byref(s))
        if rc != 0:
            raise ValueError(f"aec_decode_range failed on RSI {k}")
    vals = _unpack(raw, rec)
    a = j0 * rec["ni"] - k0 * rs
    return vals[a:a + (j1 - j0) * rec["ni"]].reshape(j1 - j0, rec["ni"])


# Sidecar columns. Everything but the offsets is a scalar from Section 3/5.
SIDE_COLS = ["key", "d0", "ni", "nj", "bps", "ref_value", "bin_scale", "dec_scale",
             "ccsds_flags", "block_size", "rsi", "rsi_bit_offsets"]


def parse_ref(v) -> list:
    """A par value is JSON bytes: [url, offset, length]."""
    return json.loads(v.decode() if isinstance(v, (bytes, bytearray)) else v)
