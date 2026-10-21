# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "eccodes", "imagecodecs"]
# ///
"""Index-only band read, v2: predict -> fetch window -> bit-sync -> joint
(start, RSI number) choice by aligning a 12-RSI strip with the reference.

Per param, ONE reference message is read in full (template time). Every other
message costs: its .index record, a ~250 B header GET, and one GET of the
predicted band +- margin. Results are checked against the true offsets from
collect_offsets.py (full reads, used for verification only).
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
RS, NR = 4096, 12           # samples per RSI; RSIs per alignment strip
SMOOTH = 0.02               # max mean |adjacent diff| / code range


def sec5(hdr):
    """Walk GRIB2 sections in the header bytes -> unpacking constants."""
    p = 16
    while True:
        L, num = struct.unpack(">IB", hdr[p:p + 5])
        if num == 5:
            s = hdr[p:p + L]
            sm = lambda b: -(b & 0x7FFF) if b & 0x8000 else b
            return dict(referenceValue=struct.unpack(">f", s[11:15])[0],
                        binaryScaleFactor=sm(struct.unpack(">H", s[15:17])[0]),
                        decimalScaleFactor=sm(struct.unpack(">H", s[17:19])[0]),
                        bitsPerValue=s[19], ccsdsFlags=s[21], ccsdsBlockSize=s[22],
                        ccsdsRsi=struct.unpack(">H", s[23:25])[0])
        if num == 7:
            raise ValueError("no section 5")
        p += L


def try_decode(buf, bit, nsamp, K, want_offsets=False):
    """Decode nsamp values starting at an exact bit of buf. None on error."""
    if bit < 0 or bit // 8 >= len(buf): return None
    b0, sh = bit // 8, bit % 8
    out = np.empty(nsamp * K["bpsamp"], np.uint8)
    s = stream(buf[b0:], *K["args"], out)
    if want_offsets:
        lib.aec_decode_enable_offsets(ctypes.byref(s))
    if sh and lib.aec_buffer_seek(ctypes.byref(s), sh) != 0:
        lib.aec_decode_end(ctypes.byref(s)); return None
    ok = lib.aec_decode(ctypes.byref(s), 0) == 0 and s.avail_out == 0
    offs = None
    if ok and want_offsets:
        c = ctypes.c_size_t(); lib.aec_decode_count_offsets(ctypes.byref(s), ctypes.byref(c))
        o = (ctypes.c_size_t * c.value)(); lib.aec_decode_get_offsets(ctypes.byref(s), o, c)
        # Absolute bit positions in buf. libaec counts from buf[b0:], and after
        # a non-zero bit seek it reports 8 bits short (the seek pulls one byte
        # into its accumulator without counting it) -- measured: +8 at every
        # shift 1..7, +0 at shift 0. Entry 0 is libaec's seeded 0, not a
        # position, so replace it with the start itself.
        offs = np.array(o, dtype=np.int64) + b0 * 8 + (8 if sh else 0)
        offs[0] = bit
    lib.aec_decode_end(ctypes.byref(s))
    if not ok: return None
    X = out.view(">u2" if K["bpsamp"] == 2 else ">u4").astype(np.int64)
    return (X, offs) if want_offsets else X


def rough(X, bps):
    return np.abs(np.diff(X)).mean() / 2 ** bps


def candidate(buf, bit, K):
    """Start of a smooth, self-consistent RSI chain near `bit`, or None."""
    bps = K["bitsPerValue"]
    X = try_decode(buf, bit, 64, K)                   # cheap prefilter: 2 blocks
    if X is None or rough(X, bps) > SMOOTH or X.max() >= 2 ** bps: return None
    r = try_decode(buf, bit, 3 * RS, K, want_offsets=True)
    if r is None or len(r[1]) < 3: return None
    X3, offs = r
    if not all(rough(X3[i * RS:(i + 1) * RS], bps) < SMOOTH for i in range(3)): return None
    # A start a few bits early can still decode smoothly: the decoder falls
    # back into step within the first RSI. Hop to the second start it reports
    # and keep it only if the chain from there agrees on the third start.
    nb = int(offs[1])
    r2 = try_decode(buf, nb, 3 * RS, K, want_offsets=True)
    if r2 is None or len(r2[1]) < 2 or int(r2[1][1]) != int(offs[2]): return None
    return nb, int(offs[2] - offs[1])                 # start, length of that RSI


def candidates(buf, K, max_bits):
    """Every distinct candidate start from the first one found to one RSI later."""
    for bit in range(0, max_bits):
        c = candidate(buf, bit, K)
        if c: break
    else:
        return []
    found = {c[0]}
    for b2 in range(bit + 1, bit + 2 * c[1] + 1):     # ~1 RSI past the first start
        c2 = candidate(buf, b2, K)
        if c2: found.add(c2[0])
    return sorted(found)


def choose(buf, starts, K, kpred, kspan, reffield):
    """Joint (start, k): align a 12-RSI decoded strip with the reference field."""
    best = None
    scale = np.nanstd(reffield) or 1.0
    for st in starts:
        X = try_decode(buf, st, NR * RS, K)
        if X is None: continue
        v = (K["referenceValue"] + X * 2.0 ** K["binaryScaleFactor"]) / 10 ** K["decimalScaleFactor"]
        for k in range(max(0, kpred - kspan), kpred + kspan + 1):
            ref = reffield[k * RS:(k + NR) * RS]
            if len(ref) < len(v): break
            sc = np.abs(v - ref).mean() / scale
            if best is None or sc < best[0]:
                best = (sc, st, k)
    return best


rows = collections.defaultdict(list)
for r in truth:
    rows[r["param"]].append(r)
j0, j1 = round((90 - ln) / dlat), round((90 - ls) / dlat) + 1
k0, k1e = j0 * ni // RS, (j1 * ni - 1) // RS + 1
print(f"{grid} band {ln}..{ls}: RSI {k0}..{k1e - 1}", flush=True)
tot = collections.Counter(); fetched = []; times = []
# optional 4th arg: "param:n,param:n" -> only the first n messages of those params
only = dict(x.split(":") for x in sys.argv[4].split(",")) if len(sys.argv) > 4 else None
for p, rs in rows.items():
    if only is not None:
        if p not in only: continue
        rs = rs[:int(only[p]) + 1]
    ref = next(r for r in rs if r["member"] == 1 and r["step"] == 24 and r["run"] == "12")
    fr = ref["offs"] / (ref["databytes"] * 8)
    rsi_bytes = ref["databytes"] / len(ref["offs"])
    # template time: the ONE full read of the reference, kept as a decoded field
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
        K = sec5(get(key + ".grib2", o, o + ref["d0"] + 63))          # header GET
        K.update(d0=ref["d0"], bpsamp=2 if K["bitsPerValue"] <= 16 else 4)
        K["args"] = (K["bitsPerValue"], K["ccsdsBlockSize"], K["ccsdsRsi"], K["ccsdsFlags"])
        databytes = L - K["d0"]; bits = databytes * 8
        margin = int(0.05 * L) + 2048
        w0 = max(0, int(fr[k0] * bits / 8) - margin)
        w1 = min(databytes, int(fr[k1e] * bits / 8) + margin) if k1e < len(fr) else databytes
        buf = get(key + ".grib2", o + K["d0"] + w0, o + K["d0"] + w1 - 1)   # band GET
        starts = candidates(buf, K, max_bits=(2 * margin + 4096) * 8)
        status, info = "nosync", ""
        if starts:
            kpred = int(np.argmin(np.abs(fr * bits - (w0 * 8 + starts[0]))))
            kspan = int(margin / rsi_bytes) + 3
            best = choose(buf, starts, K, kpred, kspan, reffield)
            if best:
                sc, st, k = best
                absbit = w0 * 8 + st
                if r["offs"][k] == absbit:
                    status = "OK" if k <= k0 else "OK-late"     # late: window began inside band
                else:
                    ti = int(np.argmin(np.abs(r["offs"] - absbit)))
                    status = "WRONG"
                    info = f"k={k} true={ti} d={absbit - r['offs'][ti]:+d}b"
                info += f" cands={len(starts)} score={sc:.3f}"
        dt = time.time() - t
        tot[status] += 1; fetched.append(len(buf) / L); times.append(dt)
        print(f"  {p:4} m{r['member']:02} s{r['step']:3} {r['run']}z  {status:7} "
              f"{100 * len(buf) / L:4.1f}% of {L / 1e3:6.0f} KB  {dt:5.1f}s  {info}", flush=True)
print(dict(tot), f"fetched mean {100 * np.mean(fetched):.1f}%  time mean {np.mean(times):.1f}s max {max(times):.1f}s")
