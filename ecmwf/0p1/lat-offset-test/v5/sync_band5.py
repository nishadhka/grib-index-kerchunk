# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "eccodes", "imagecodecs", "pandas", "pyarrow"]
# ///
"""Index-free band read on real GIK pars, v5: the v4 search plus a confidence
gate that falls back to counting RSIs to the end of the message.

Message locations come ONLY from the par chunk refs [url, offset, length],
which are the .index triplets GIK Stage 2 writes. The reference for a test
message is the same key (param, level, step) in a reference par from another
date, read in full once (template time) and reused for every test message at
that step.

Rule, fixed before the run (proposed in ../v4/README.md):
    window  = predicted band +- (1.5% of the message + 2 KiB)
    choice  = argmin score/best + LAMBDA * (dist / (SIGMA * message))**2,
              SIGMA = 1%, LAMBDA = 0.25
    if the chosen score > GATE (0.45): fetch on to the end of the message,
              decode from the synced start, and name the RSI by counting
              RSIs to the end (exact; no reference values involved).
Every test message is also read in full, for verification only: the chosen
start must be the true start of the chosen RSI, and the band decoded from the
fetched bytes must equal eccodes on the full message.

    uv run v5/sync_band5.py <ref.par> <test.par>... --lat 40,-40 > out/v5_sync_0p25.txt
"""
import argparse, collections, json, os, re, struct, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from aecx import *
import pandas as pd

RS, NR = 4096, 12           # samples per RSI; RSIs per alignment strip
SMOOTH = 0.02               # max mean |adjacent diff| / code range
MARGIN, SIGMA, LAMBDA, GATE = 0.015, 0.01, 0.25, 0.45
CHUNK = re.compile(r"^step_(\d{3})/([^/]+)/(sfc|pl|sol)/(?:(\d+)/)?([^/]+)/0\.0\.0$")
VARS = ["2t", "tp", "msl", "t/850", "u/700"]


def par_refs(path):
    """{(field, step): (key, offset, length)} for the chunk rows of VARS."""
    df = pd.read_parquet(path); out = {}
    for k, v in zip(df["key"], df["value"]):
        m = CHUNK.match(k)
        if not m: continue
        f = m[2] if m[4] is None else f"{m[2]}/{m[4]}"
        if f in VARS:
            url, o, L = json.loads(v.decode() if isinstance(v, bytes) else v)
            out[(f, int(m[1]))] = (url.split("s3://ecmwf-forecasts/", 1)[1] + ".grib2", o, L)
    return out


def sec5(hdr):
    """Walk GRIB2 sections in the header bytes -> unpacking constants and d0,
    the first packed-data byte. d0 is read from THIS message: the control
    (oper/fc file) has other section sizes than the perturbed members."""
    p, K = 16, None
    while p < len(hdr) - 5:
        L, num = struct.unpack(">IB", hdr[p:p + 5])
        if num == 5:
            s = hdr[p:p + L]
            sm = lambda b: -(b & 0x7FFF) if b & 0x8000 else b
            K = dict(referenceValue=struct.unpack(">f", s[11:15])[0],
                     binaryScaleFactor=sm(struct.unpack(">H", s[15:17])[0]),
                     decimalScaleFactor=sm(struct.unpack(">H", s[17:19])[0]),
                     bitsPerValue=s[19], ccsdsFlags=s[21], ccsdsBlockSize=s[22],
                     ccsdsRsi=struct.unpack(">H", s[23:25])[0])
        if num == 7:
            if K is None: raise ValueError("no section 5")
            K["d0"] = p + 5
            return K
        p += L
    raise ValueError("header GET too short to reach section 7")


def try_decode(buf, bit, nsamp, K, want_offsets=False, to_end=False):
    """Decode nsamp values from an exact bit of buf (to_end: as many as there
    are, stopping at the end of buf). None on error."""
    if bit < 0 or bit // 8 >= len(buf): return None
    b0, sh = bit // 8, bit % 8
    out = np.empty(nsamp * K["bpsamp"], np.uint8)
    s = stream(buf[b0:], *K["args"], out)
    if want_offsets:
        lib.aec_decode_enable_offsets(ctypes.byref(s))
    if sh and lib.aec_buffer_seek(ctypes.byref(s), sh) != 0:
        lib.aec_decode_end(ctypes.byref(s)); return None
    rc = lib.aec_decode(ctypes.byref(s), 1 if to_end else 0)
    ok = rc == 0 and (to_end or s.avail_out == 0)
    got = (out.nbytes - s.avail_out) // K["bpsamp"]
    offs = None
    if ok and want_offsets:
        c = ctypes.c_size_t(); lib.aec_decode_count_offsets(ctypes.byref(s), ctypes.byref(c))
        o = (ctypes.c_size_t * c.value)(); lib.aec_decode_get_offsets(ctypes.byref(s), o, c)
        # libaec reports 8 bits short after a non-zero bit seek (v2 finding);
        # entry 0 is its seeded 0, so replace it with the start itself.
        offs = np.array(o, dtype=np.int64) + b0 * 8 + (8 if sh else 0)
        offs[0] = bit
    lib.aec_decode_end(ctypes.byref(s))
    if not ok: return None
    X = out[:got * K["bpsamp"]].view(">u2" if K["bpsamp"] == 2 else ">u4").astype(np.int64)
    return (X, offs) if want_offsets else X


def rough(X, bps):
    return np.abs(np.diff(X)).mean() / 2 ** bps


def candidate(buf, bit, K):
    """Start of a smooth, self-consistent RSI chain near `bit`, or None (as v4)."""
    bps = K["bitsPerValue"]
    X = try_decode(buf, bit, 64, K)
    if X is None or rough(X, bps) > SMOOTH or X.max() >= 2 ** bps: return None
    r = try_decode(buf, bit, 3 * RS, K, want_offsets=True)
    if r is None or len(r[1]) < 3: return None
    X3, offs = r
    if not all(rough(X3[i * RS:(i + 1) * RS], bps) < SMOOTH for i in range(3)): return None
    nb = int(offs[1])
    r2 = try_decode(buf, nb, 3 * RS, K, want_offsets=True)
    if r2 is None or len(r2[1]) < 2 or int(r2[1][1]) != int(offs[2]): return None
    return nb, int(offs[2] - offs[1])


def candidates(buf, K, max_bits):
    for bit in range(0, max_bits):
        c = candidate(buf, bit, K)
        if c: break
    else:
        return []
    found = {c[0]}
    for b2 in range(bit + 1, bit + 2 * c[1] + 1):
        c2 = candidate(buf, b2, K)
        if c2: found.add(c2[0])
    return sorted(found)


def score_all(buf, starts, K, kpred, kspan, reffield):
    out = []
    scale = np.nanstd(reffield) or 1.0
    for st in starts:
        X = try_decode(buf, st, NR * RS, K)
        if X is None: continue
        v = (K["referenceValue"] + X * 2.0 ** K["binaryScaleFactor"]) / 10 ** K["decimalScaleFactor"]
        for k in range(max(0, kpred - kspan), kpred + kspan + 1):
            ref = reffield[k * RS:(k + NR) * RS]
            if len(ref) < len(v): break
            out.append((st, k, float(np.abs(v - ref).mean() / scale)))
    return out


def pick(cands, fr, bits, w0, L):
    best_sc = min(c[2] for c in cands) or 1e-12
    sig = SIGMA * L * 8
    cost = lambda c: c[2] / best_sc + LAMBDA * ((fr[c[1]] * bits - (w0 * 8 + c[0])) / sig) ** 2
    return min(cands, key=cost)


def head(key, o, K, fr, bits, buf, w0, st, k, k0):
    """Fetch the bytes before the window, back to about one RSI before RSI k0
    (template estimate to 3 RSIs before k0, +30% + 4 KiB), sync at their start
    and decode forward to the known start st of RSI k. The chain's first
    offsets may be off (after a false start in a tp zero run the decoder can
    take more than one RSI to fall into step), so take the entry AT RSI k0,
    counted back from st, and require at least 2 RSIs before it. Returns the new
    (buf, w0, start, k, bytes fetched), or None."""
    extra = int((fr[k] - fr[max(k0 - 3, 0)]) * bits / 8 * 1.3) + 4096
    h0 = max(0, w0 - extra)
    hb = get(key, o + K["d0"] + h0, o + K["d0"] + w0 - 1)
    b2, target = hb + buf, (w0 - h0) * 8 + st
    for bit in range(0, min(len(hb), 2 * 4096) * 8):
        c = candidate(b2, bit, K)
        if not c: continue
        # decode by bytes, to just past the target, so the RSI estimate cannot cut it short
        r = try_decode(b2[:target // 8 + 8192], c[0], (k + 2) * RS, K, want_offsets=True, to_end=True)
        if r is None or target not in r[1].tolist(): continue   # a false start (tp: long zero runs)
        chain = r[1].tolist()
        i = chain.index(target)
        j = i - (k - k0)                                       # chain index of RSI k0
        if j < 2: continue                                     # too close to a possibly-false start
        return b2, h0, chain[j], k0, len(hb)
    return None


def full(key, o, L):
    """Full read: header constants, RSI offsets, eccodes values (truth / template)."""
    msg = get(key, o, o + L - 1)
    K = header(msg)
    if K["bitsPerValue"] == 0: return None
    offs, _ = rsi_offsets(msg, K)
    h = eccodes.codes_new_from_message(msg); vals = eccodes.codes_get_values(h); eccodes.codes_release(h)
    return dict(K=K, offs=offs, vals=vals, databytes=len(msg) - K["d0"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ref"); ap.add_argument("tests", nargs="+")
    ap.add_argument("--lat", default="40,-40")
    ap.add_argument("--steps", default="0,6,24,48,72,120,144,168,240,300,360",
                    help="steps for the first test par; the others are tested at --member-step")
    ap.add_argument("--member-step", type=int, default=24)
    ap.add_argument("--gate", type=float, default=GATE, help="-1 forces count-to-end on every message")
    ap.add_argument("--count", default="", help="fields always counted to the end, e.g. tp (v5c policy)")
    a = ap.parse_args()
    ln, ls = map(float, a.lat.split(","))
    ref = par_refs(a.ref)
    jobs = []
    for i, t in enumerate(a.tests):
        steps = [int(s) for s in a.steps.split(",")] if i == 0 else [a.member_step]
        refs = par_refs(t)
        jobs += [(os.path.basename(t), f, s, refs[(f, s)]) for f in VARS for s in steps]
    print(f"v5 reference {os.path.basename(a.ref)}  band {ln}..{ls}  margin {MARGIN:.1%}+2KiB  "
          f"sigma {SIGMA:.0%} lambda {LAMBDA}  gate {a.gate}  always count [{a.count}]  {len(jobs)} messages", flush=True)
    tmpl = {}
    tot = collections.Counter(); rows = []
    for name, f, step, (key, o, L) in jobs:
        tag = f"{name.split('.')[0]:22} {f:5} s{step:3}"
        t = time.time()
        if (f, step) not in tmpl:                                  # template time, once per (field, step)
            R = full(*ref[(f, step)])
            tmpl[(f, step)] = None if R is None else dict(
                fr=R["offs"] / (R["databytes"] * 8), d0=R["K"]["d0"], rsi_bytes=R["databytes"] / len(R["offs"]),
                field=R["vals"], ni=R["K"]["Ni"], nj=R["K"]["Nj"])
        T = tmpl[(f, step)]
        if T is None:
            print(f"  {tag}  skip    reference is a constant field", flush=True); continue
        truth = full(key, o, L)                                     # verification only
        if truth is None:
            print(f"  {tag}  skip    constant field", flush=True); continue
        t = time.time()
        fr, ni, nj = T["fr"], T["ni"], T["nj"]
        dlat = 180.0 / (nj - 1); n = ni * nj; nrsi = -(-n // RS)
        j0, j1 = round((90 - ln) / dlat), round((90 - ls) / dlat) + 1
        k0, k1e = j0 * ni // RS, (j1 * ni - 1) // RS + 1
        K = sec5(get(key, o, o + T["d0"] + 1023))                   # header GET (~1 KB)
        K.update(bpsamp=2 if K["bitsPerValue"] <= 16 else 4)
        K["args"] = (K["bitsPerValue"], K["ccsdsBlockSize"], K["ccsdsRsi"], K["ccsdsFlags"])
        databytes = L - K["d0"]; bits = databytes * 8
        margin = int(MARGIN * L) + 2048
        w0 = max(0, int(fr[k0] * bits / 8) - margin)
        w1 = min(databytes, int(fr[k1e] * bits / 8) + margin) if k1e < len(fr) else databytes
        buf = get(key, o + K["d0"] + w0, o + K["d0"] + w1 - 1)      # band GET
        fetched = 1024 + len(buf)
        starts = candidates(buf, K, max_bits=(2 * margin + 4096) * 8)
        status, info, path = "nosync", "", "-"
        if starts:
            kpred = int(np.argmin(np.abs(fr * bits - (w0 * 8 + starts[0]))))
            kspan = int(margin / T["rsi_bytes"]) + 3
            cands = score_all(buf, starts, K, kpred, kspan, T["field"])
            if cands:
                st, k, sc = pick(cands, fr, bits, w0, L)
                path = "match"
                if sc > a.gate or f in a.count.split(","):                                       # not confident: count to the end
                    path = "count"
                    if w1 < databytes:
                        buf += get(key, o + K["d0"] + w1, o + L - 1)   # tail GET
                        fetched += L - K["d0"] - w1
                    # stop at the end of the packed data: the message ends with Section 8
                    # ("7777"), which would otherwise decode as more blocks
                    r = try_decode(buf[:databytes - 4 - w0], st, nrsi * RS, K, want_offsets=True, to_end=True)
                    k = nrsi - len(r[1]) if r else -1
                    if r is not None and len(r[1]) > 2:
                        # st may be a few bits off a true start (tp zero runs) and still
                        # count right; offsets 2+ RSIs into the chain are exact, so start
                        # the band from there: at RSI k0 if it is that far in, else at
                        # chain[2] and let the head GET reach back to k0
                        chain = r[1].tolist()
                        st, k = (chain[k0 - k], k0) if k0 - k >= 2 else (chain[2], k + 2)
                if 0 < k0 < k:                                      # window opened after the band start
                    h = head(key, o, K, fr, bits, buf, w0, st, k, k0)
                    if h:
                        buf, w0, st, k, nb = h; fetched += nb; path += "+head"
                absbit = w0 * 8 + st
                # end-to-end: decode the band from the fetched bytes at the chosen (start, k)
                band_ok = False
                if k <= k0 and 0 <= k:
                    need = j1 * ni - k * RS
                    X = try_decode(buf, st, min(need, n - k * RS), K)
                    if X is not None and len(X) >= need:
                        v = (K["referenceValue"] + X * 2.0 ** K["binaryScaleFactor"]) / 10 ** K["decimalScaleFactor"]
                        band_ok = np.array_equal(v[j0 * ni - k * RS:need], truth["vals"][j0 * ni:j1 * ni])
                right = 0 <= k < len(truth["offs"]) and truth["offs"][k] == absbit
                status = ("OK" if band_ok else "OK-late" if k > k0 else "OK-band?") if right else "WRONG"
                ti = int(np.argmin(np.abs(truth["offs"] - absbit)))
                info = f"{path:5} k={k} true={ti} score={sc:.3f}"
        if status == "nosync" or (status != "WRONG" and path.startswith("count") and k < 0):
            # no sync in the window: read the whole message (exact, and flagged)
            msg = get(key, o, o + L - 1); fetched += L; path = "full"
            h = eccodes.codes_new_from_message(msg); v = eccodes.codes_get_values(h); eccodes.codes_release(h)
            status = "OK" if np.array_equal(v[j0 * ni:j1 * ni], truth["vals"][j0 * ni:j1 * ni]) else "WRONG"
            info = "full  no sync in the window"
        dt = time.time() - t
        tot[status] += 1; rows.append(dict(path=path, status=status, frac=fetched / L))
        print(f"  {tag}  {status:7} {100 * fetched / L:5.1f}% of {L / 1e3:5.0f} KB  {dt:5.1f}s  {info}", flush=True)
    df = pd.DataFrame(rows)
    print(dict(tot), f"fetched mean {100 * df.frac.mean():.1f}%")
    for p, g in df.groupby("path"):
        print(f"  path {p}: {len(g)} messages, {(g.status == 'OK').sum()} OK, fetched mean {100 * g.frac.mean():.1f}%")


if __name__ == "__main__":
    main()
