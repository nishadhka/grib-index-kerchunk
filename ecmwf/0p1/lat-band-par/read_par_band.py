# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "pandas", "pyarrow", "imagecodecs", "gcsfs", "eccodes", "xarray"]
# ///
"""Read a latitude band from a GIK par + its .rsi.parquet sidecar.

Each chunk costs ONE range GET of just the band's bytes. With --validate, the
whole message is also fetched and decoded with eccodes, and the band is
compared value for value.

    uv run read_par_band.py <par.parquet> --var 2t --lat 40,-40 [--steps 24] [--validate]
"""
import argparse, os, re, sys, time

import numpy as np
import pandas as pd
import xarray as xr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from augment_par import CHUNK, open_par  # noqa: E402
from bandref import band_read, chunk_url, get_range, parse_ref  # noqa: E402


def lon_axis(ni):
    """GRIB scan order, -180 first (see ecmwf/grids.py)."""
    return -180.0 + np.arange(ni) * 360.0 / ni


def read_band(par, side, var, lat_n, lat_s, steps=None, validate=False):
    refs = {k: parse_ref(v) for k, v in zip(par["key"], par["value"]) if CHUNK.match(k)}
    rows = side[side.key.map(lambda k: CHUNK.match(k)[2] == var)]
    if steps is not None:
        rows = rows[rows.key.map(lambda k: int(CHUNK.match(k)[1]) in steps)]
    out, hours, stats = [], [], []
    for rec in rows.to_dict("records"):
        chunk = refs[rec["key"]]
        t = time.time()
        vals, lats, nb = band_read(chunk, rec, lat_n, lat_s)
        dt = time.time() - t
        err = None
        if validate:
            import eccodes
            msg = get_range(chunk_url(chunk[0]), chunk[1], chunk[2])
            h = eccodes.codes_new_from_message(msg)
            full = eccodes.codes_get_values(h).reshape(rec["nj"], rec["ni"])
            eccodes.codes_release(h)
            j0 = int(round((90 - lat_n) * (rec["nj"] - 1) / 180))
            err = float(np.abs(vals - full[j0:j0 + len(lats)]).max())
        out.append(vals); hours.append(int(CHUNK.match(rec["key"])[1]))
        stats.append(dict(step=hours[-1], fetched=nb, message=chunk[2], seconds=dt, max_abs_err=err))
    # float32, as the realized store holds it; validation above used float64
    da = xr.DataArray(np.stack(out).astype(np.float32), dims=("step", "latitude", "longitude"),
                      coords=dict(step=np.array(hours, "timedelta64[h]"), latitude=lats,
                                  longitude=lon_axis(rows.iloc[0]["ni"])), name=var)
    return da, pd.DataFrame(stats)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("par")
    ap.add_argument("--side", default=None, help="default: <par>.rsi.parquet")
    ap.add_argument("--var", default="2t")
    ap.add_argument("--lat", default="40,-40", help="north,south")
    ap.add_argument("--lon", default=None, help="optional west,east cut AFTER decoding, e.g. 15,80")
    ap.add_argument("--steps", default=None)
    ap.add_argument("--validate", action="store_true")
    a = ap.parse_args()
    par = open_par(a.par)
    side = pd.read_parquet(a.side or a.par.replace(".parquet", ".rsi.parquet"))
    ln, ls = map(float, a.lat.split(","))
    steps = None if a.steps is None else {int(s) for s in a.steps.split(",")}
    da, st = read_band(par, side, a.var, ln, ls, steps, a.validate)
    if a.lon:
        w, e = map(float, a.lon.split(","))
        da = da.sel(longitude=slice(w, e))
    print(da)
    share = st.fetched.sum() / st.message.sum()
    print(f"\n{len(st)} messages: fetched {st.fetched.sum() / 1e6:.2f} MB of {st.message.sum() / 1e6:.2f} MB "
          f"({100 * share:.1f}%); {st.seconds.mean():.2f} s per message")
    if a.validate:
        print(f"max |band - eccodes full decode| over all messages: {st.max_abs_err.max():.3g}  "
              f"({'BIT-EXACT' if st.max_abs_err.max() == 0 else 'MISMATCH'})")


if __name__ == "__main__":
    main()
