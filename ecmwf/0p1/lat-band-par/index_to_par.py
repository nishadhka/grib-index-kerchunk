# /// script
# requires-python = ">=3.11"
# dependencies = ["pandas", "pyarrow"]
# ///
"""Stand-in par from ECMWF .index, in the GIK par chunk layout.

Writes only the chunk rows of a par -- `step_SSS/{param}/{sfc|pl|sol}/ens{NN}/0.0.0`
-> `[url-without-.grib2, offset, length]` -- for the chosen params, one member,
all steps of a 0.25 deg ENS run. These are the same triplets a GIK par carries
(GIK Stage 2 reads the same .index). It exists so the band tools can be tested
on dates whose real pars are not readable from here; point the tools at real
pars as soon as they are.

    uv run index_to_par.py 20261019 00 --member 1 --vars 2t --out 2026101900z-ens01.parquet
"""
import argparse, json, urllib.request

import pandas as pd

LONG = list(range(0, 145, 3)) + list(range(150, 361, 6))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("date"); ap.add_argument("run")
    ap.add_argument("--member", type=int, default=1)
    ap.add_argument("--vars", default="2t")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    steps = LONG if a.run in ("00", "12") else LONG[:49]
    want = set(a.vars.split(","))
    rows = []
    for st in steps:
        stem = f"{a.date}/{a.run}z/ifs/0p25/enfo/{a.date}{a.run}0000-{st}h-enfo-ef"
        idx = urllib.request.urlopen(f"https://storage.googleapis.com/ecmwf-open-data/{stem}.index").read()
        for line in idx.decode().splitlines():
            r = json.loads(line)
            if r["param"] in want and r.get("number") == str(a.member) and r["levtype"] != "pl":
                key = f"step_{st:03d}/{r['param']}/{r['levtype']}/ens{a.member:02d}/0.0.0"
                rows.append((key, json.dumps([f"s3://ecmwf-forecasts/{stem}", r["_offset"], r["_length"]]).encode()))
    pd.DataFrame(rows, columns=["key", "value"]).to_parquet(a.out, index=False)
    print(f"{len(rows)} chunk rows -> {a.out}")


if __name__ == "__main__":
    main()
