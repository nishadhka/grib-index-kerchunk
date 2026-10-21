# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "eccodes", "imagecodecs"]
# ///
# Usage: uv run rsi_band_subset_poc.py <https-url> <msg_offset> <msg_length> <lat_north> <lat_south>
#   e.g. uv run rsi_band_subset_poc.py \
#     https://ecmwf-forecasts.s3.amazonaws.com/20261020/12z/ifs/0p1/enfo/20261020120000-24h-enfo-ef-sfc.grib2 \
#     1083426288 1864394 24 -12
"""PoC: latitude-band subset of a CCSDS GRIB2 message BEFORE download.

1. index:  decode one full message once with libaec offset capture -> bit
           offset of every RSI (4096-sample independently decodable segment).
2. fetch:  HTTP Range GET of only the bytes covering the band's RSIs.
3. decode: aec_decode_range per RSI on the partial buffer, unpack, compare
           with eccodes' decode of the full global field.
"""
import ctypes, glob, urllib.error, json, os, sys, time, urllib.request
import numpy as np, eccodes, imagecodecs

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

def get(url, a, b):
    for i in range(8):
        try:
            return urllib.request.urlopen(urllib.request.Request(
                url, headers={"Range": f"bytes={a}-{b}"})).read()
        except urllib.error.HTTPError as e:
            if e.code != 503: raise
            time.sleep(2 ** i)
    raise RuntimeError("S3 kept throttling")

url, off, length = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
lat_n, lat_s = float(sys.argv[4]), float(sys.argv[5])   # band, N -> S

# ---- 1. index (one full read; this is the cost paid once per message) -----
msg = get(url, off, off + length - 1)
h = eccodes.codes_new_from_message(msg)
K = {k: eccodes.codes_get(h, k) for k in (
    "Ni", "Nj", "bitsPerValue", "ccsdsBlockSize", "ccsdsRsi", "ccsdsFlags",
    "referenceValue", "binaryScaleFactor", "decimalScaleFactor",
    "offsetSection7", "latitudeOfFirstGridPointInDegrees",
    "jDirectionIncrementInDegrees")}
truth = eccodes.codes_get_values(h).reshape(K["Nj"], K["Ni"])
eccodes.codes_release(h)
d0 = K["offsetSection7"] + 5                      # first byte of packed data
data = msg[d0:]
n = K["Ni"] * K["Nj"]; bps = K["bitsPerValue"]; bpsamp = 2 if bps <= 16 else 4
args = (bps, K["ccsdsBlockSize"], K["ccsdsRsi"], K["ccsdsFlags"])
full = np.empty(n * bpsamp, np.uint8)
s = stream(data, *args, full)
lib.aec_decode_enable_offsets(ctypes.byref(s))
assert lib.aec_decode(ctypes.byref(s), 1) == 0
cnt = ctypes.c_size_t(); lib.aec_decode_count_offsets(ctypes.byref(s), ctypes.byref(cnt))
offs = (ctypes.c_size_t * cnt.value)()
lib.aec_decode_get_offsets(ctypes.byref(s), offs, cnt)
lib.aec_decode_end(ctypes.byref(s))
offs = np.array(offs, dtype=np.int64)            # bit offsets into `data`
rsi_samples = K["ccsdsBlockSize"] * K["ccsdsRsi"]
print(f"grid {K['Nj']}x{K['Ni']}  bps={bps}  RSI={rsi_samples} samples  "
      f"n_rsi={len(offs)}  index={len(offs)*4/1024:.1f} KiB as uint32")

# ---- 2. fetch only the band ------------------------------------------------
dlat, lat0 = K["jDirectionIncrementInDegrees"], K["latitudeOfFirstGridPointInDegrees"]
j0, j1 = round((lat0 - lat_n) / dlat), round((lat0 - lat_s) / dlat) + 1
p0, p1 = j0 * K["Ni"], j1 * K["Ni"]                   # sample range [p0, p1)
k0, k1 = p0 // rsi_samples, (p1 - 1) // rsi_samples   # RSIs touched
b0 = offs[k0] // 8
b1 = (offs[k1 + 1] + 7) // 8 if k1 + 1 < len(offs) else len(data)
t = time.time()
part = get(url, off + d0 + b0, off + d0 + b1 - 1)
dt = time.time() - t
print(f"band rows {j0}..{j1-1} ({lat_n}..{lat_s})  RSIs {k0}..{k1}  "
      f"fetched {len(part)/1e6:.3f} MB of {length/1e6:.3f} MB "
      f"({100*len(part)/length:.1f}%) in {dt*1000:.0f} ms")

# ---- 3. decode the partial buffer ----------------------------------------
rel = offs - b0 * 8                                 # offsets relative to `part`
rel_c = (ctypes.c_size_t * len(rel))(*np.maximum(rel, 0).tolist())
raw = np.empty((k1 - k0 + 1) * rsi_samples * bpsamp, np.uint8)
for i, k in enumerate(range(k0, k1 + 1)):
    take = min(rsi_samples, n - k * rsi_samples) * bpsamp
    seg = raw[i * rsi_samples * bpsamp:][:take]
    s = stream(part, *args, seg)
    rc = lib.aec_decode_range(ctypes.byref(s), rel_c, len(rel),
                              k * rsi_samples * bpsamp, take)
    lib.aec_decode_end(ctypes.byref(s))
    assert rc == 0, (k, rc)
X = raw.view(">u2" if bpsamp == 2 else ">u4").astype(np.float64)
vals = (K["referenceValue"] + X * 2.0 ** K["binaryScaleFactor"]) / 10.0 ** K["decimalScaleFactor"]
band = vals[p0 - k0 * rsi_samples: p1 - k0 * rsi_samples].reshape(j1 - j0, K["Ni"])
ref = truth[j0:j1]
print(f"band {band.shape}  max|partial - eccodes full| = {np.abs(band - ref).max():.3g}  "
      f"range {band.min():.4g}..{band.max():.4g}")
print(json.dumps({"rsi_bit_start": int(offs[k0]), "byte_offset": int(off + d0 + b0),
                  "byte_length": len(part), "first_sample": int(k0 * rsi_samples)}))
