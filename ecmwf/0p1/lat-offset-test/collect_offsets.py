# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "eccodes", "imagecodecs"]
# ///
"""Collect RSI offset profiles for many messages -> out/offsets_<grid>.pkl"""
import sys, pickle
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, sys.argv[2] + "/lib")
from aecx import *

grid = sys.argv[1]
date = "20261020"
VARS = [("2t", None, "sfc"), ("tp", None, "sfc"), ("msl", None, "sfc"),
        ("t", 850, "pl"), ("u", 700, "pl")]
jobs = []
for p, lev, kind in VARS:
    for m in range(1, 51, 7):                          # 8 members, step 24, 12z
        jobs.append((date, "12", 24, p, lev, kind, m))
    for st in (0, 6, 72, 144, 240, 360):               # member 1, other steps
        jobs.append((date, "12", st, p, lev, kind, 1))
    for run in ("06", "18"):                           # other runs
        jobs.append((date, run, 24, p, lev, kind, 1))

def one(j):
    d, run, st, p, lev, kind, m = j
    key = path(d, run, grid, st, kind)
    r = find(index(key + ".index"), p, m, lev)
    msg = get(key + ".grib2", r["_offset"], r["_offset"] + r["_length"] - 1)
    K = header(msg)
    if K["bitsPerValue"] == 0:                         # constant field (e.g. tp at 0 h)
        print("skip constant", j); return None
    offs, _ = rsi_offsets(msg, K)
    return dict(run=run, step=st, param=p, level=lev, member=m,
                length=r["_length"], d0=K["d0"], databytes=len(msg) - K["d0"],
                offs=offs, bps=K["bitsPerValue"])

with ThreadPoolExecutor(4) as ex:
    out = [o for o in ex.map(one, jobs) if o]
pickle.dump(out, open(f"{sys.argv[2]}/out/offsets_{grid}.pkl", "wb"))
print(grid, len(out), "messages")
