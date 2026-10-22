# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "eccodes", "imagecodecs", "pandas", "pyarrow"]
# ///
"""Fetch v5 search inputs once and time the Python search on them, for the
Rust benchmark (`cargo run --release -- <out dir>`).

For each message: the window (predicted band +- margin) through to the end of
the packed data, so the search, the value match and count-to-end all run on
the same cached bytes, offline. Writes <out>/manifest.json, <out>/case_NNN.bin
(window..data end) and <out>/ref_NNN.f64 (template values the score needs).

    GIK_CHUNK_SOURCE=gcs uv run rust/dump_cases.py <template par> <test par> <out dir>
"""
import json, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "v5"))
import sync_band5 as v5          # noqa: E402  (main() is not run on import)
from sync_band5 import *         # noqa: E402,F401

STEPS = [0, 6, 24, 48, 72, 120, 144, 168, 240, 300, 360]
ref_par, test_par, out = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(out, exist_ok=True)
ref, test = par_refs(ref_par), par_refs(test_par)
ln, ls = 40.0, -40.0
cases = []
for f in VARS:
    for step in STEPS:
        R = full(*ref[(f, step)])
        if R is None:
            continue                                       # constant template (tp 0 h)
        key, o, L = test[(f, step)]
        fr, ni, nj = R["offs"] / (R["databytes"] * 8), R["K"]["Ni"], R["K"]["Nj"]
        n = ni * nj; nrsi = -(-n // RS); dlat = 180.0 / (nj - 1)
        j0, j1 = round((90 - ln) / dlat), round((90 - ls) / dlat) + 1
        k0, k1e = j0 * ni // RS, (j1 * ni - 1) // RS + 1
        K = sec5(get(key, o, o + R["K"]["d0"] + 1023))
        if K["bitsPerValue"] == 0:
            continue
        K.update(bpsamp=2 if K["bitsPerValue"] <= 16 else 4)
        K["args"] = (K["bitsPerValue"], K["ccsdsBlockSize"], K["ccsdsRsi"], K["ccsdsFlags"])
        databytes = L - K["d0"]; bits = databytes * 8
        margin = int(MARGIN * L) + 2048
        w0 = max(0, int(fr[k0] * bits / 8) - margin)
        w1 = min(databytes, int(fr[k1e] * bits / 8) + margin) if k1e < len(fr) else databytes
        tail = get(key, o + K["d0"] + w0, o + K["d0"] + databytes - 4 - 1)   # window .. end of packed data
        buf = tail[:w1 - w0]
        max_bits = (2 * margin + 4096) * 8
        # --- the Python search, timed on the cached bytes (CPU only) ---
        t0 = time.perf_counter()
        starts = candidates(buf, K, max_bits)
        t1 = time.perf_counter()
        kpred = int(np.argmin(np.abs(fr * bits - (w0 * 8 + starts[0])))) if starts else -1
        kspan = int(margin / (R["databytes"] / len(R["offs"]))) + 3
        cands = score_all(buf, starts, K, kpred, kspan, R["vals"]) if starts else []
        t2 = time.perf_counter()
        st = min(cands, key=lambda c: c[2])[0] if cands else (starts[0] if starts else 0)
        r = try_decode(tail, st, nrsi * RS, K, want_offsets=True, to_end=True)
        count = len(r[1]) if r else -1
        t3 = time.perf_counter()
        i = len(cases)
        open(f"{out}/case_{i:03}.bin", "wb").write(tail)
        lo = max(0, kpred - kspan) * RS
        hi = min(len(R["vals"]), (kpred + kspan + NR + 1) * RS)
        np.asarray(R["vals"][lo:hi], "<f8").tofile(f"{out}/ref_{i:03}.f64")
        cases.append(dict(
            field=f, step=step, win_len=len(buf), tail_len=len(tail), max_bits=max_bits,
            bps=K["bitsPerValue"], block_size=K["ccsdsBlockSize"], rsi=K["ccsdsRsi"], flags=K["ccsdsFlags"],
            ref_value=K["referenceValue"], bin_scale=K["binaryScaleFactor"], dec_scale=K["decimalScaleFactor"],
            nrsi=nrsi, w0=w0, pred_bits=[int(x) for x in fr * bits], kspan=kspan, ref_k0=lo // RS,
            ref_std=float(np.nanstd(R["vals"]) or 1.0),
            py=dict(starts=starts, kpred=kpred, n_cands=len(cands),
                    best=min(cands, key=lambda c: c[2]) if cands else None, count_start=st, count=count,
                    t_sync=t1 - t0, t_score=t2 - t1, t_count=t3 - t2)))
        print(f"{i:3} {f:5} s{step:3}  starts={len(starts)}  sync {t1 - t0:5.2f}s  score {t2 - t1:5.2f}s  "
              f"count {1e3 * (t3 - t2):5.1f}ms", flush=True)
json.dump(cases, open(f"{out}/manifest.json", "w"))
print(len(cases), "cases ->", out)
