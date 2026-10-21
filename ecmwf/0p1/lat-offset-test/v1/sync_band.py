# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "eccodes", "imagecodecs"]
# ///
"""Index-only band read: predict -> fetch window -> bit-sync -> identify RSI.

Per param, ONE reference message is read in full (template time). Every other
message is read with: its .index record, a ~200 B header GET, and one GET of
the predicted band +- margin. The exact RSI start is found by scanning bit
positions in the window, then checked against the true offsets collected by
collect_offsets.py (which did full reads, for verification only).
"""
import sys, pickle, struct, time, collections
sys.path.insert(0, sys.argv[3] + "/lib")
from aecx import *

grid, (ln, ls) = sys.argv[1], map(float, sys.argv[2].split(","))
S = sys.argv[3]
dlat, ni = {"0p25": (0.25, 1440), "0p1": (0.1, 3600)}[grid]
truth = pickle.load(open(f"{S}/out/offsets_{grid}.pkl", "rb"))
date = "20261020"
KIND = {"2t": "sfc", "tp": "sfc", "msl": "sfc", "t": "pl", "u": "pl"}
RS = 4096


def sec5(hdr):
    """Walk GRIB2 sections in the header bytes -> unpacking constants."""
    p = 16
    while True:
        L, num = struct.unpack(">IB", hdr[p:p + 5])
        if num == 5:
            s = hdr[p:p + L]
            R = struct.unpack(">f", s[11:15])[0]
            sm = lambda b: -(b & 0x7FFF) if b & 0x8000 else b
            E, D = sm(struct.unpack(">H", s[15:17])[0]), sm(struct.unpack(">H", s[17:19])[0])
            return dict(referenceValue=R, binaryScaleFactor=E, decimalScaleFactor=D,
                        bitsPerValue=s[19], ccsdsFlags=s[21], ccsdsBlockSize=s[22],
                        ccsdsRsi=struct.unpack(">H", s[23:25])[0])
        if num == 7:
            raise ValueError("no section 5")
        p += L


def try_decode(buf, bit, nsamp, K, want_offsets=False):
    """Decode nsamp values starting at an exact bit of buf. None on error."""
    b0, sh = bit // 8, bit % 8
    out = np.empty(nsamp * K["bpsamp"], np.uint8)
    s = stream(buf[b0:], *K["args"], out)
    if want_offsets:
        lib.aec_decode_enable_offsets(ctypes.byref(s))
    if sh and lib.aec_buffer_seek(ctypes.byref(s), sh) != 0:
        lib.aec_decode_end(ctypes.byref(s)); return None
    rc = lib.aec_decode(ctypes.byref(s), 0)
    ok = rc == 0 and s.avail_out == 0
    offs = None
    if ok and want_offsets:
        c = ctypes.c_size_t(); lib.aec_decode_count_offsets(ctypes.byref(s), ctypes.byref(c))
        o = (ctypes.c_size_t * c.value)(); lib.aec_decode_get_offsets(ctypes.byref(s), o, c)
        offs = np.array(o, dtype=np.int64) + sh
    lib.aec_decode_end(ctypes.byref(s))
    if not ok: return None
    X = out.view(">u2" if K["bpsamp"] == 2 else ">u4").astype(np.int64)
    return (X, offs) if want_offsets else X


def rough(X, bps):
    """Mean |adjacent difference| as a fraction of the code range."""
    return np.abs(np.diff(X)).mean() / 2 ** bps


def candidate(buf, bit, K):
    """Smooth, self-consistent RSI chain starting near bit -> (start, X3, offs, rough)."""
    bps = K["bitsPerValue"]
    X = try_decode(buf, bit, 64, K)                   # cheap prefilter: 2 blocks
    if X is None or rough(X, bps) > 0.02 or X.max() >= 2 ** bps: return None
    r = try_decode(buf, bit, 3 * RS, K, want_offsets=True)
    if r is None: return None
    X3, offs = r
    if not all(rough(X3[i * RS:(i + 1) * RS], bps) < 0.02 for i in range(3)): return None
    # A start a few bits early can still decode smoothly: the decoder falls
    # back into step within the first RSI. Hop to the second start it reports
    # and keep it only if the chain from there agrees on the third start.
    nb = bit + int(offs[1])
    r2 = try_decode(buf, nb, 3 * RS, K, want_offsets=True)
    if r2 is None or nb + int(r2[1][1]) != bit + int(offs[2]): return None
    return nb, r2[0], r2[1], rough(r2[0], bps)


def sync(buf, K, max_bits):
    """Scan to the first candidate, then keep scanning one RSI length further
    and return the smoothest candidate: the true start is always among them,
    while out-of-step chains decode measurably rougher."""
    for bit in range(0, max_bits):
        c = candidate(buf, bit, K)
        if c: break
    else:
        return None, None, None
    span = int(c[2][1])                               # one RSI length in bits
    found = {c[0]: c}
    for b2 in range(bit + 1, bit + span + 1):
        c2 = candidate(buf, b2, K)
        if c2: found.setdefault(c2[0], c2)
    best = min(found.values(), key=lambda c: c[3])
    return best[0], best[1], best[2]


rows = collections.defaultdict(list)
for r in truth:
    rows[r["param"]].append(r)
j0, j1 = round((90 - ln) / dlat), round((90 - ls) / dlat) + 1
k0, k1e = j0 * ni // RS, (j1 * ni - 1) // RS + 1
print(f"{grid} band {ln}..{ls}: RSI {k0}..{k1e - 1}")
tot = collections.Counter()
for p, rs in rows.items():
    ref = next(r for r in rs if r["member"] == 1 and r["step"] == 24 and r["run"] == "12")
    fr = ref["offs"] / (ref["databytes"] * 8)
    # template-time: the ONE full read of the reference, kept as a decoded field
    rkey = path(date, "12", grid, 24, KIND[p])
    rr = find(index(rkey + ".index"), p, 1, ref["level"])
    rmsg = get(rkey + ".grib2", rr["_offset"], rr["_offset"] + rr["_length"] - 1)
    RK = header(rmsg); _, rraw = rsi_offsets(rmsg, RK); reffield = unpack(rraw, RK)
    for r in rs:
        if r is ref: continue
        t = time.time()
        key = path(date, r["run"], grid, r["step"], KIND[p])
        rec = find(index(key + ".index"), p, r["member"], r["level"])
        o, L = rec["_offset"], rec["_length"]
        hdr = get(key + ".grib2", o, o + ref["d0"] + 63)          # header GET
        K = sec5(hdr)
        K.update(d0=ref["d0"], bpsamp=2 if K["bitsPerValue"] <= 16 else 4,
                 n=RK["n"])
        K["args"] = (K["bitsPerValue"], K["ccsdsBlockSize"], K["ccsdsRsi"], K["ccsdsFlags"])
        bits = (L - K["d0"]) * 8
        margin = int(0.05 * L) + 2048                               # bytes
        w0 = max(0, int(fr[k0] * bits / 8) - margin)
        w1 = min(L - K["d0"], int(fr[k1e] * bits / 8) + margin if k1e < len(fr) else L - K["d0"])
        buf = get(key + ".grib2", o + K["d0"] + w0, o + K["d0"] + w1 - 1)   # band GET
        bit, X3, offs3 = sync(buf, K, max_bits=(margin * 2 + 4096) * 8)
        status = "nosync"
        if bit is not None:
            absbit = w0 * 8 + bit
            kn = int(np.argmin(np.abs(fr * bits - absbit)))
            vals = (K["referenceValue"] + X3 * 2.0 ** K["binaryScaleFactor"]) / 10 ** K["decimalScaleFactor"]
            cands = range(max(0, kn - 6), min(len(fr) - 3, kn + 7))
            score = {k: np.abs(vals - reffield[k * RS:k * RS + 3 * RS]).mean() for k in cands}
            k = min(score, key=score.get)
            ti = int(np.argmin(np.abs(r["offs"] - absbit)))
            tX = try_decode(buf, int(r["offs"][ti] - w0 * 8), 3 * RS, K) if r["offs"][ti] >= w0 * 8 else None
            dbg = f"d={absbit - r['offs'][ti]:+d}b found_rough={rough(X3, K['bitsPerValue']):.4f} true_rough={rough(tX, K['bitsPerValue']) if tX is not None else -1:.4f} offs3={list(offs3[:3]) if offs3 is not None else None}"
            status = ("OK" if r["offs"][k] == absbit else f"WRONG(k={k},true_at={ti})") + " " + dbg
            if k > k0: status += " late"                            # window started inside band
        tot[status.split("(")[0].split()[0]] += 1
        print(f"  {p:4} m{r['member']:02} s{r['step']:3} {r['run']}z  {status:8} "
              f"fetched {len(buf)/1e3:7.1f} KB + hdr of {L/1e3:7.1f} KB ({100*len(buf)/L:4.1f}%)  "
              f"sync {time.time()-t:5.2f}s")
print(dict(tot))
