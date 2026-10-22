# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "eccodes", "imagecodecs", "pandas", "pyarrow"]
# ///
"""How long must a prefix chunk be, if its length is written into the manifest
before anyone reads the message?

For each test message and each band south edge, from a full read:

    needed  = the first byte after the band's last RSI (message start = 0);
    predicted = the template's band end (same field and step, earlier run),
                scaled to this message's length, as prefix_read.py predicts it.

The margin a manifest needs is (needed - predicted) / length. Each `needed`
is also checked by decoding exactly that many bytes (the prefix codec's case).

    GIK_CHUNK_SOURCE=gcs uv run c_prefix/manifest_margin.py <test par>... \
        --templates <par>... [--south 40,20,0,-12,-20,-40,-60]
"""
import argparse, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from prefix_read import decode_prefix, sec5  # noqa: E402
from sync_band5 import RS, VARS, full, par_refs  # noqa: E402
from aecx import get, np  # noqa: E402
import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tests", nargs="+")
    ap.add_argument("--templates", nargs="+", required=True)
    ap.add_argument("--south", default="40,20,0,-12,-20,-40,-60")
    ap.add_argument("--steps", default="0,6,24,48,72,120,144,168,240,300,360")
    ap.add_argument("--member-step", type=int, default=24)
    ap.add_argument("--check", default="-12,-40", help="south edges verified by decoding exactly `needed` bytes")
    a = ap.parse_args()
    souths = [float(s) for s in a.south.split(",")]
    checks = {float(s) for s in a.check.split(",")}
    jobs = []
    for i, t in enumerate(a.tests):
        steps = [int(s) for s in a.steps.split(",")] if i == 0 else [a.member_step]
        refs = par_refs(t)
        jobs += [(os.path.basename(t).split(".")[0], f, s, refs[(f, s)]) for f in VARS for s in steps]
    tref = {os.path.basename(p).split(".")[0][:10]: par_refs(p) for p in a.templates}
    tprof = {}
    rows = []
    for name, f, step, (key, o, L) in jobs:
        R = full(key, o, L)
        if R is None:
            continue
        ni, nj, d0 = R["K"]["Ni"], R["K"]["Nj"], R["K"]["d0"]
        msg = get(key, o, o + L - 1)
        K = sec5(msg); K.update(bpsamp=2 if K["bitsPerValue"] <= 16 else 4)
        K["args"] = (K["bitsPerValue"], K["ccsdsBlockSize"], K["ccsdsRsi"], K["ccsdsFlags"])
        dlat = 180.0 / (nj - 1)
        for s in souths:
            j1 = round((90 - s) / dlat) + 1
            k1e = (j1 * ni - 1) // RS + 1
            needed = d0 + (-(-int(R["offs"][k1e]) // 8) if k1e < len(R["offs"]) else L - 4 - d0)
            ok = None
            if s in checks:   # decode exactly `needed` bytes, as a prefix codec would get them
                raw, _ = decode_prefix(msg[d0:needed], K, j1 * ni)
                ok = len(raw) == j1 * ni * K["bpsamp"]
            row = dict(test=name, field=f, step=step, south=s, L=L, needed=needed, frac=needed / L, ok=ok)
            for tdate, refs in tref.items():
                if (tdate, f, step) not in tprof:
                    T = full(*refs[(f, step)])
                    tprof[(tdate, f, step)] = None if T is None else (T["offs"] / (T["databytes"] * 8), T["K"]["d0"])
                P = tprof[(tdate, f, step)]
                if P is None:
                    continue
                fr, td0 = P
                pred = td0 + (fr[k1e] if k1e < len(fr) else 1.0) * (L - td0)
                row[f"m_{tdate}"] = (needed - pred) / L
            rows.append(row)
        print(f"  {name:20} {f:5} s{step:3}  needed " +
              " ".join(f"{r['south']:+.0f}:{100 * r['frac']:.1f}%" for r in rows[-len(souths):]), flush=True)
    df = pd.DataFrame(rows)
    print(f"\n{df.test.nunique()} test pars, {len(df) // len(souths)} messages")
    print("\nbytes needed (message start -> band end), % of message, by south edge:")
    for s, g in df.groupby("south", sort=False):
        print(f"  south {s:+5.0f}  mean {100 * g.frac.mean():5.1f}  min {100 * g.frac.min():5.1f}  max {100 * g.frac.max():5.1f}"
              + ("" if g.ok.isna().all() else f"   exact-length decode ok {int(g.ok.sum())}/{g.ok.notna().sum()}"))
    print("\nmargin needed over the template prediction, % of message (positive = prediction short):")
    for c in [c for c in df.columns if c.startswith("m_")]:
        m = 100 * df[c].dropna()
        print(f"  template {c[2:]}:  max {m.max():+.2f}  p99 {m.quantile(.99):+.2f}  p95 {m.quantile(.95):+.2f}  "
              f"median {m.median():+.2f}   short at 1.5%+2KiB: {int((df[c] * df.L > .015 * df.L + 2048).sum())}"
              f"   at 3%+4KiB: {int((df[c] * df.L > .03 * df.L + 4096).sum())}")
        for f, g in df.groupby("field"):
            print(f"      {f:6} max {100 * g[c].max():+.2f}")


if __name__ == "__main__":
    main()
