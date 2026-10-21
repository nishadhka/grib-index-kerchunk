# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy"]
# ///
"""How well does one reference message's RSI-offset profile predict others?"""
import sys, pickle, collections
import numpy as np

S = sys.argv[1]
BANDS = {"40N..40S": (40, -40), "24N..12S": (24, -12)}
GRID = {"0p25": (0.25, 1440), "0p1": (0.1, 3600)}

for grid, (dlat, ni) in GRID.items():
    rows = pickle.load(open(f"{S}/out/offsets_{grid}.pkl", "rb"))
    by = collections.defaultdict(list)
    for r in rows:
        by[r["param"]].append(r)
    print(f"\n===== {grid}  (errors in KB; 'unif' = assume bytes spread evenly over RSIs)")
    for band, (ln, ls) in BANDS.items():
        j0, j1 = round((90 - ln) / dlat), round((90 - ls) / dlat) + 1
        k0, k1e = j0 * ni // 4096, (j1 * ni - 1) // 4096 + 1
        print(f"-- band {band}: RSI {k0} .. {k1e}")
        for p, rs in by.items():
            ref = next(r for r in rs if r["member"] == 1 and r["step"] == 24 and r["run"] == "12")
            fr = ref["offs"] / (ref["databytes"] * 8)
            e0, e1, u0, rel = [], [], [], []
            for r in rs:
                if r is ref: continue
                bits = r["databytes"] * 8
                n_rsi = len(r["offs"])
                e0.append((fr[k0] * bits - r["offs"][k0]) / 8 / 1e3)
                e1.append((fr[k1e] * bits - r["offs"][k1e]) / 8 / 1e3 if k1e < n_rsi else 0)
                u0.append((k0 / n_rsi * bits - r["offs"][k0]) / 8 / 1e3)
                rel.append(abs(e0[-1]) * 1e3 / r["length"] * 100)
            e0, e1, u0 = map(np.abs, map(np.array, (e0, e1, u0)))
            print(f"   {p:4} n={len(rs)-1:2}  start err: median {np.median(e0):6.1f} max {e0.max():6.1f} KB"
                  f" ({max(rel):.1f}% of msg) | end err max {e1.max():6.1f} KB"
                  f" | unif start err max {u0.max():6.1f} KB  | msg {np.mean([r['length'] for r in rs])/1e3:.0f} KB")
    # worst cases by kind of variation, for one band
    ln, ls = BANDS["40N..40S"]; j0 = round((90 - ln) / dlat); k0 = j0 * ni // 4096
    print("-- 40N start error by what varies (max KB):")
    for p, rs in by.items():
        ref = next(r for r in rs if r["member"] == 1 and r["step"] == 24 and r["run"] == "12")
        fr = ref["offs"] / (ref["databytes"] * 8)
        def err(r): return abs(fr[k0] * r["databytes"] * 8 - r["offs"][k0]) / 8 / 1e3
        mem = [err(r) for r in rs if r["step"] == 24 and r["run"] == "12" and r is not ref]
        stp = [err(r) for r in rs if r["member"] == 1 and r["run"] == "12" and r["step"] != 24]
        run = [err(r) for r in rs if r["run"] != "12"]
        print(f"   {p:4} members {max(mem):6.1f} | steps {max(stp):6.1f} | runs {max(run):6.1f}")
