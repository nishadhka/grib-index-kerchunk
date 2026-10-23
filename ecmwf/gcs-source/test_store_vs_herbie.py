# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "icechunk>=2.1", "zarr>=3.2", "xarray>=2025.1", "gribberish>=1.4", "numpy",
#     "herbie-data", "cfgrib", "eccodes", "matplotlib", "cartopy",
# ]
# ///
"""Does the virtual store open and return the right fields?  Store vs Herbie.

One variable, a few forecast steps, one (date, run). The store side is read the
way a consumer reads it -- xr.open_zarr, then .sel by LABEL on time, step,
latitude, longitude -- through gs:// virtual refs decoded by gribberish. The
Herbie side fetches the same messages independently (its own index parse,
its own download, cfgrib/eccodes decode) and is reindexed onto the store's
coordinate labels. So a wrong axis, a permuted member, a step filed at the
wrong index, or a codec error all show up as a difference here.

  per member   members 1..50, element-wise. Same bytes, two decoders: must agree
               to float32 packing noise (max |diff| <= 1e-4 of the field range,
               r >= 0.9999). Herbie's enfo has no control, so number 0 is
               compared only when --control gets it from the oper stream.
  plot         per step: store ensemble mean, Herbie ensemble mean, difference,
               over East Africa.

  uv run test_store_vs_herbie.py --store /home/ubuntu/icechunk-local/ecmwf-ens-gcs-test \\
      --date 20261020 --run 06 --var tp --steps 24,72,144
  uv run test_store_vs_herbie.py ... --var t --level 850 --steps 0,48,96
  uv run test_store_vs_herbie.py ... --var lcc     # one of the 7 new 43-sfc params
  uv run test_store_vs_herbie.py ... --var sot --level 3   # soil layer (--soil-layers store)

Exit 1 if any step fails.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import warnings
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import cartopy.crs as ccrs
import numpy as np
import xarray as xr
import icechunk
import gribberish.zarr  # noqa: F401 -- registers the gribberish Zarr v3 codec

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import compare_icechunk_herbie as C  # noqa: E402 -- stats, map panel, name map

warnings.filterwarnings("ignore")
os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")

PER_MEMBER_RTOL = C.PER_MEMBER_RTOL
MIN_CORR = 0.9999


def open_store(store: str, era: str, run: str) -> xr.Dataset:
    st = (icechunk.gcs_storage(bucket=store[5:].partition("/")[0],
                               prefix=store[5:].partition("/")[2].rstrip("/"),
                               from_env=True)
          if store.startswith("gs://") else icechunk.local_filesystem_storage(store))
    auth = icechunk.containers_credentials({
        "s3://ecmwf-forecasts/": icechunk.s3_anonymous_credentials(),
        "gs://ecmwf-open-data/": icechunk.GcsCredentials.Anonymous()})
    repo = icechunk.Repository.open(st, authorize_virtual_chunk_access=auth)
    return xr.open_zarr(repo.readonly_session("main").store, group=f"{era}/{run}z",
                        zarr_format=3, consolidated=False, chunks=None)


def store_field(ds, var, date, run, step, level):
    """-> (values[number, lat, lon], lats, lons, numbers), all by label."""
    t = np.datetime64(f"{date[:4]}-{date[4:6]}-{date[6:]}T{run}:00", "ns")
    if t not in ds.time.values:
        raise SystemExit(f"{date} {run}z is not on this group's time axis "
                         f"{ds.time.values}")
    da = ds[var].sel(time=t, step=step)
    for dim, sel in (("isobaricInhPa", float), ("soilLayer", int)):
        if dim in da.dims:
            if level is None:
                raise SystemExit(f"{var} is on {dim}; pass --level")
            da = da.sel({dim: sel(level)})
    da = da.sel(latitude=slice(C.LAT_MAX, C.LAT_MIN),
                longitude=slice(C.LON_MIN, C.LON_MAX))
    return (da.values.astype(np.float32), da.latitude.values, da.longitude.values,
            da.number.values)


def herbie_open(date, run, step, product, search, priority):
    """Herbie picks a source by existence, then fails outright if that source
    throttles the download (AWS 503 Slow Down) -- so try each source in turn."""
    from herbie import Herbie
    errs = []
    for src in priority:
        try:
            H = Herbie(f"{date[:4]}-{date[4:6]}-{date[6:]} {run}:00", model="ifs",
                       product=product, fxx=step, priority=[src], verbose=False)
            if H.grib is None:
                raise RuntimeError("not found")
            # overwrite belongs to the download, not the constructor: a subset
            # left by a download that died mid-way (a throttled source) is
            # otherwise reused as if complete -- seen: 1 member of 50
            return H, H.xarray(search, verbose=False, overwrite=True), src
        except Exception as e:
            errs.append(f"{src}: {str(e).splitlines()[0][:70]}")
    raise RuntimeError("every Herbie source failed -- " + "; ".join(errs))


def herbie_field(date, run, step, var, level, lats, lons, product, priority,
                 levtype="pl"):
    """-> (values[number, lat, lon], numbers, source) on the store's own labels."""
    param = C.TO_GRIB.get(var, var)
    search = f":{param}:{level}:{levtype}:" if level is not None else f":{param}:sfc:"
    H, ds, src = herbie_open(date, run, step, product, search, priority)
    if isinstance(ds, list):
        cand = [d for d in ds if "number" in d.dims] or ds
        ds = cand[0]
    da = ds[param if param in ds.data_vars else list(ds.data_vars)[0]]
    if float(da.longitude.max()) > 180:
        da = da.assign_coords(longitude=((da.longitude + 180) % 360) - 180
                              ).sortby("longitude")
    if "number" not in da.dims:
        n = int(da.coords["number"]) if "number" in da.coords else 0
        da = da.expand_dims(number=[n])
    # by label, tolerance far under half a cell: an absent label raises
    da = da.sel(latitude=lats, longitude=lons, method="nearest", tolerance=0.01)
    return da.sortby("number").values.astype(np.float32), da.number.values, src


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--store", required=True, help="local path | gs://bucket/prefix")
    ap.add_argument("--era", default="50r1", choices=["0p4", "49r1", "50r1"])
    ap.add_argument("--date", required=True)
    ap.add_argument("--run", default="00", choices=["00", "06", "12", "18"])
    ap.add_argument("--var", default="tp", help="store array name (t2m, tp, lcc, t, ...)")
    ap.add_argument("--level", type=int, default=None, help="hPa, for pl vars")
    ap.add_argument("--steps", default="24,72,144")
    ap.add_argument("--priority", default="azure,ecmwf,aws,google",
                    help="Herbie sources, tried in order until one serves. Non-GCS "
                         "first keeps the bytes independent of what the store reads")
    ap.add_argument("--control", action="store_true",
                    help="also compare number 0 against Herbie product 'oper' (50r1)")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "plots"))
    args = ap.parse_args()
    steps = [int(s) for s in args.steps.split(",")]
    prio = args.priority.split(",")

    t0 = time.time()
    ds = open_store(args.store, args.era, args.run)
    print(f"store {args.store} {args.era}/{args.run}z opened in {time.time() - t0:.1f}s: "
          f"{dict(ds.sizes)}")
    if args.var not in ds:
        raise SystemExit(f"{args.var} not in group; have {sorted(ds.data_vars)}")
    missing = [s for s in steps if s not in ds.step.values]
    if missing:
        raise SystemExit(f"steps {missing} not on the axis {ds.step.values.tolist()}")

    levtype = "sol" if "soilLayer" in ds[args.var].dims else "pl"
    rows, report, failed = [], [], False
    for step in steps:
        ts = time.time()
        sv, lats, lons, snum = store_field(ds, args.var, args.date, args.run, step,
                                           args.level)
        t_store = time.time() - ts
        th = time.time()
        hv, hnum, src = herbie_field(args.date, args.run, step, args.var, args.level,
                                     lats, lons, "enfo", prio, levtype)
        if args.control:
            try:
                cv, cnum, _ = herbie_field(args.date, args.run, step, args.var,
                                           args.level, lats, lons, "oper", prio,
                                           levtype)
                hv, hnum = np.concatenate([cv, hv]), np.concatenate([[0], hnum])
            except Exception as e:
                print(f"  control via Herbie 'oper' unavailable: {str(e)[:80]}")
        t_herbie = time.time() - th
        pm = C.per_member_stats(sv, snum, hv, hnum)
        want = 50 + (1 if args.control and 0 in hnum else 0)
        ok = (pm["n_compared"] == want and pm["n_store_all_nan"] == 0
              and pm["max_rel_diff"] <= PER_MEMBER_RTOL and pm["min_corr"] >= MIN_CORR)
        failed |= not ok
        s_mean = np.nanmean(sv[np.isin(snum, hnum)], axis=0)
        h_mean = np.nanmean(hv, axis=0)
        ens = C.field_stats(s_mean, h_mean)
        print(f"  T+{step:>3}h  {'PASS' if ok else 'FAIL'}  members {pm['n_compared']}/{want}"
              f"  min r {pm['min_corr']:.6f}  max|diff| {pm['max_abs_diff']:.3g}"
              f" ({pm['max_rel_diff']:.1e} of range)  ens-mean r {ens['corr']:.6f}"
              f"  [store {t_store:.1f}s, herbie/{src} {t_herbie:.1f}s]")
        rows.append((step, s_mean, h_mean))
        report.append({"step": step, "pass": ok, "herbie_source": src,
                       "store_s": round(t_store, 2), "herbie_s": round(t_herbie, 2),
                       "ensemble_mean": ens,
                       **{k: v for k, v in pm.items() if k != "members"}})

    # plot: one row per step -- store mean | Herbie mean | difference
    units = C.VAR_UNITS.get(args.var, "")
    lev = ("" if args.level is None else f" layer {args.level}" if levtype == "sol"
           else f" {args.level} hPa")
    fig, axes = plt.subplots(len(rows), 3, figsize=(17, 5.2 * len(rows)),
                             subplot_kw={"projection": ccrs.PlateCarree()},
                             squeeze=False)
    for (step, s, h), ax in zip(rows, axes):
        vmin, vmax = float(np.nanmin([s, h])), float(np.nanmax([s, h]))
        cmap = "Blues" if args.var in ("tp", "cp", "sf", "ro") else "viridis"
        d = s - h
        dmax = float(np.nanmax(np.abs(d)))
        dext = max(dmax, 1e-12)            # colour scale only; the title is honest
        for a, data, title, cm, lo, hi in (
                (ax[0], s, f"Icechunk (gs://) T+{step}h", cmap, vmin, vmax),
                (ax[1], h, f"Herbie T+{step}h", cmap, vmin, vmax),
                (ax[2], d, f"Icechunk - Herbie  max|d| {dmax:.2g}", "RdBu_r", -dext, dext)):
            im = C._map(a, data, lons, lats, title, cm, lo, hi)
            fig.colorbar(im, ax=a, shrink=0.75, label=units)
    fig.suptitle(f"{args.var}{lev} ensemble mean (members 1-50) -- {args.era} "
                 f"{args.date} {args.run}z -- virtual store vs Herbie",
                 fontsize=14, fontweight="bold")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tag = f"{args.var}{'L' if levtype == 'sol' and args.level else ''}{args.level or ''}"
    png = out / f"store_vs_herbie_{tag}_{args.date}{args.run}z.png"
    fig.savefig(png, dpi=110, bbox_inches="tight", facecolor="white")
    (out / f"store_vs_herbie_{tag}_{args.date}{args.run}z.json").write_text(
        json.dumps(report, indent=1, default=float))
    print(f"\n{'FAIL' if failed else 'PASS'}: {args.var}{lev} {args.date} {args.run}z, "
          f"steps {steps} -> {png}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
