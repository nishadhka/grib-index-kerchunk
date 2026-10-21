# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy"]
# ///
"""Bytes from band start to message end (count-to-end naming) vs the band alone."""
import sys, pickle, collections
import numpy as np
S = sys.argv[1]
for grid, (dlat, ni) in {"0p25": (0.25, 1440), "0p1": (0.1, 3600)}.items():
    rows = pickle.load(open(f"{S}/out/offsets_{grid}.pkl", "rb"))
    for band, (ln, ls) in {"40N..40S": (40, -40), "24N..12S": (24, -12)}.items():
        j0, j1 = round((90 - ln) / dlat), round((90 - ls) / dlat) + 1
        k0, k1e = j0 * ni // 4096, (j1 * ni - 1) // 4096 + 1
        b, t = collections.defaultdict(list), collections.defaultdict(list)
        for r in rows:
            bits = r["databytes"] * 8
            b[r["param"]].append((r["offs"][k1e] - r["offs"][k0]) / bits)
            t[r["param"]].append((bits - r["offs"][k0]) / bits)
        print(f"{grid} {band}: " + "  ".join(f"{p} band {100*np.mean(b[p]):.0f}% -> to end {100*np.mean(t[p]):.0f}%" for p in b))
