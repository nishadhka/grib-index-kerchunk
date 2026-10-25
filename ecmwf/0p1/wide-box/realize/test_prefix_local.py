"""Local check before any worker sees the module: read_prefix (the prefix read)
against read_message_virtual (the whole message through the virtual store and
gribberish) for every channel, plus the box coordinates the schema will get.

    uv run --with frisky --with icechunk==2.1.1 --with zarr==3.2.1 \
        --with xarray==2026.7.0 --with gribberish==1.6.0 --with numpy==2.5.1 \
        --with imagecodecs python test_prefix_local.py [--members 0,1,50] [--steps 0,24,240]
"""
import argparse
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import frisky_daily_dag as dag

ap = argparse.ArgumentParser()
ap.add_argument("--date", default="2026-10-21")
ap.add_argument("--members", default="0,1,50")
ap.add_argument("--steps", default="0,24,240")
ap.add_argument("--threads", type=int, default=8)
a = ap.parse_args()

c = dag.subset_coords("0p1", a.date, [0], [0])
lat, lon = c["latitude"], c["longitude"]
print(f"box coords: lat {lat[0]}..{lat[-1]} ({len(lat)}), lon {lon[0]}..{lon[-1]} ({len(lon)}); "
      f"grid_for -> {dag.grid_for('0p1')}")

jobs = [(v, l, n, m, s) for v, l, n in dag.CHANNELS
        for m in map(int, a.members.split(",")) for s in map(int, a.steps.split(","))]


def one(j):
    v, l, n, m, s = j
    t = time.time()
    p = dag.read_prefix(v, l, a.date, m, s)
    tp = time.time() - t
    w = dag.read_message_virtual("0p1", v, l, a.date, m, s)
    return n, m, s, p.shape, np.array_equal(p, w, equal_nan=True), tp


ok = 0
with ThreadPoolExecutor(a.threads) as ex:
    for n, m, s, shape, eq, tp in ex.map(one, jobs):
        ok += eq
        if not eq or s == 0:
            print(f"  {n:6} m{m:02d} s{s:03d} {shape} {'EQUAL' if eq else 'DIFFER'} {tp:.2f}s")
st = dag.prefix_stats()
print(f"\n{ok}/{len(jobs)} prefix reads equal the whole-message virtual read")
print(f"fetched {st['bytes_fetched']/1e9:.3f} of {st['bytes_full']/1e9:.3f} GB "
      f"({100*st['bytes_fetched']/st['bytes_full']:.1f}%), {st['gets']} GETs for {st['msgs']} "
      f"messages ({st['topups']} top-ups), index files {st['idx_files']} "
      f"({st['idx_bytes']/1e6:.1f} MB), decode {1000*st['t_decode']/st['msgs']:.0f} ms/msg")
