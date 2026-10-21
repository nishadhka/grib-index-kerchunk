"""Shared helpers: S3 range GET with retry, .index lookup, libaec RSI offsets."""
import ctypes, glob, json, os, time, urllib.error, urllib.request
import numpy as np, eccodes, imagecodecs

H = "https://ecmwf-forecasts.s3.amazonaws.com/"
lib = ctypes.CDLL(glob.glob(os.path.join(os.path.dirname(imagecodecs.__file__),
                                         "..", "imagecodecs.libs", "libaec*"))[0])


class Strm(ctypes.Structure):
    _fields_ = [("next_in", ctypes.c_void_p), ("avail_in", ctypes.c_size_t),
                ("total_in", ctypes.c_size_t), ("next_out", ctypes.c_void_p),
                ("avail_out", ctypes.c_size_t), ("total_out", ctypes.c_size_t),
                ("bits_per_sample", ctypes.c_uint), ("block_size", ctypes.c_uint),
                ("rsi", ctypes.c_uint), ("flags", ctypes.c_uint),
                ("state", ctypes.c_void_p)]


def stream(buf, bps, bs, rsi, flags, out):
    s = Strm(); inb = (ctypes.c_ubyte * len(buf)).from_buffer_copy(buf)
    s.next_in, s.avail_in = ctypes.addressof(inb), len(buf)
    s.next_out, s.avail_out = out.ctypes.data, out.nbytes
    s.bits_per_sample, s.block_size, s.rsi, s.flags = bps, bs, rsi, flags
    s._keep = inb
    assert lib.aec_decode_init(ctypes.byref(s)) == 0
    return s


def get(key, a=None, b=None):
    hdr = {} if a is None else {"Range": f"bytes={a}-{b}"}
    for i in range(10):
        try:
            return urllib.request.urlopen(urllib.request.Request(H + key, headers=hdr),
                                          timeout=60).read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            if getattr(e, "code", 503) not in (503, 500): raise
            time.sleep(min(2 ** i, 30))
    raise RuntimeError("S3 kept throttling")


_idx = {}
def index(key):
    if key not in _idx:
        _idx[key] = [json.loads(l) for l in get(key).decode().splitlines() if l.strip()]
    return _idx[key]


def find(recs, param, number, level=None):
    for r in recs:
        if r["param"] == param and r.get("number", "0") == str(number) and \
           (level is None or r.get("levelist") == str(level)):
            return r
    raise KeyError((param, number, level))


def header(msg):
    h = eccodes.codes_new_from_message(msg)
    K = {k: eccodes.codes_get(h, k) for k in (
        "Ni", "Nj", "bitsPerValue", "ccsdsBlockSize", "ccsdsRsi", "ccsdsFlags",
        "referenceValue", "binaryScaleFactor", "decimalScaleFactor", "offsetSection7")}
    eccodes.codes_release(h)
    K["d0"] = K["offsetSection7"] + 5
    K["n"] = K["Ni"] * K["Nj"]
    K["bpsamp"] = 2 if K["bitsPerValue"] <= 16 else 4
    K["args"] = (K["bitsPerValue"], K["ccsdsBlockSize"], K["ccsdsRsi"], K["ccsdsFlags"])
    K["rsi_samples"] = K["ccsdsBlockSize"] * K["ccsdsRsi"]
    return K


def rsi_offsets(msg, K):
    """Bit offset of every RSI, relative to the first packed-data byte."""
    data = msg[K["d0"]:]
    out = np.empty(K["n"] * K["bpsamp"], np.uint8)
    s = stream(data, *K["args"], out)
    lib.aec_decode_enable_offsets(ctypes.byref(s))
    assert lib.aec_decode(ctypes.byref(s), 1) == 0
    c = ctypes.c_size_t(); lib.aec_decode_count_offsets(ctypes.byref(s), ctypes.byref(c))
    o = (ctypes.c_size_t * c.value)(); lib.aec_decode_get_offsets(ctypes.byref(s), o, c)
    lib.aec_decode_end(ctypes.byref(s))
    return np.array(o, dtype=np.int64), out


def unpack(raw, K):
    X = raw.view(">u2" if K["bpsamp"] == 2 else ">u4").astype(np.float64)
    return (K["referenceValue"] + X * 2.0 ** K["binaryScaleFactor"]) / 10.0 ** K["decimalScaleFactor"]


def path(date, run, grid, step, kind):
    """kind: 'sfc'|'pl' for 0p1, ignored for 0p25."""
    stem = f"{date}/{run}z/ifs/{grid}/enfo/{date}{run}0000-{step}h-enfo-ef"
    return stem + (f"-{kind}" if grid == "0p1" else "")
