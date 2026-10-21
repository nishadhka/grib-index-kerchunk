# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "pandas", "pyarrow", "imagecodecs", "gcsfs"]
# ///
"""Write a latitude-band sidecar for a GIK par: <par>.rsi.parquet.

For every chunk of the chosen variables, read the whole GRIB message ONCE (the
read the realization already does), record its RSI bit offsets and unpacking
constants, and store them next to the par. The par itself is not modified, so
every existing reader keeps working; band readers open the sidecar as well.

    uv run augment_par.py <par.parquet | gs://...par.parquet> --vars 2t [--steps 0,24]
    GIK_CHUNK_SOURCE=gcs uv run augment_par.py ...      # read bytes from gs://ecmwf-open-data

Pars on GCS are read with GOOGLE_APPLICATION_CREDENTIALS (needs object read
on the pars bucket).
"""
import argparse, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bandref import SIDE_COLS, chunk_url, get_range, parse_ref, rsi_record  # noqa: E402

# step_024/2t/sfc/ens01/0.0.0, and step_024/t/pl/850/ens01/0.0.0 in the v20260623 pars
CHUNK = re.compile(r"^step_(\d{3})/([^/]+)/(sfc|pl|sol)/(?:(\d+)/)?([^/]+)/0\.0\.0$")


def wanted(m, want):
    """--vars entry 't' takes every level of t; 't/850' takes one."""
    return m[2] in want or (m[4] is not None and f"{m[2]}/{m[4]}" in want)


def open_par(path):
    if path.startswith("gs://"):
        import gcsfs
        return pd.read_parquet(gcsfs.GCSFileSystem(token=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")).open(path))
    return pd.read_parquet(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("par")
    ap.add_argument("--vars", default="2t", help="comma list of par param names, e.g. 2t,tp,t/850 (t = all levels)")
    ap.add_argument("--steps", default=None, help="comma list of hours (default: all)")
    ap.add_argument("--out", default=None, help="default: <par basename>.rsi.parquet beside a local par, or ./")
    ap.add_argument("--threads", type=int, default=8)
    a = ap.parse_args()

    df = open_par(a.par)
    want = set(a.vars.split(","))
    steps = None if a.steps is None else {int(s) for s in a.steps.split(",")}
    jobs = []
    for k, v in zip(df["key"], df["value"]):
        m = CHUNK.match(k)
        if m and wanted(m, want) and (steps is None or int(m[1]) in steps):
            jobs.append((k, parse_ref(v)))
    if not jobs:
        sys.exit(f"no chunks for {sorted(want)} in {a.par}")

    def one(job):
        key, (url, off, length) = job
        msg = get_range(chunk_url(url), off, length)       # the one full read
        rec = rsi_record(msg)
        rec.pop("_full_codes", None)
        rec["key"] = key
        return rec

    t = time.time()
    with ThreadPoolExecutor(a.threads) as ex:
        recs = list(ex.map(one, jobs))
    side = pd.DataFrame(recs)[SIDE_COLS].sort_values("key")
    name = os.path.basename(a.par).replace(".parquet", ".rsi.parquet")
    out = a.out or (os.path.join(os.path.dirname(a.par), name) if not a.par.startswith("gs://") else name)
    side.to_parquet(out, index=False, compression="zstd")
    nbytes = sum(j[1][2] for j in jobs)
    print(f"{len(side)} chunks of {sorted(want)}: read {nbytes / 1e6:.1f} MB once in {time.time() - t:.1f} s "
          f"-> {out} ({os.path.getsize(out) / 1e3:.1f} KB, "
          f"{side.rsi_bit_offsets.map(len).mean() / 4:.0f} RSI offsets per message)")


if __name__ == "__main__":
    main()
