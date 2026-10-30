"""Visual check of the GIK box subset against Herbie, 7 fields, one message each.

For each field, the same GRIB message (cycle, step, member) is read four ways
and cut to the box 25N..15S, 19..55E (config.BOX, 401 x 361):

  H  Herbie: whole message by its .index byte range, decoded by cfgrib (eccodes),
     then .sel() to the box -- the independent reference
  P  GIK prefix read (cgan_dag.read_prefix): Range GET of the message start up
     to the box's last row, CCSDS-decoded with libaec -- what step4 uses
  V  the virtual Icechunk store (public, source.coop): whole message through
     its virtual reference, decoded by gribberish (cgan_dag.read_virtual)
  R  the realized store step4 wrote (if given)

    python herbie_vs_gik.py \
        --date 20261026 --run 00 --step 54 --member 1 \
        --realized /srv/state/ifs0p1/icechunk/.../20261026T00z-jurre-6h+mvua-7d \
        --out out/herbie_vs_gik_20261026T00z_54h_m1.png | tee out/herbie_vs_gik_20261026T00z_54h_m1.txt

Needs requirements.txt in this folder. The GIK code is in gik/ (copied from
cGAN_tutorial ifs-0p1-gik @ bc94087); set IFS0P1_GIK to test a checkout instead.
"""
import argparse
import os
import sys
import time

import numpy as np

# The GIK reader under test: cgan_dag.py, config.py, stores.py copied verbatim
# into gik/ from cGAN_tutorial ifs-0p1-gik @ bc94087 (gik/PROVENANCE.md).
# IFS0P1_GIK=<a cGAN_tutorial/ifs-0p1-gik checkout> tests that checkout instead.
HERE = os.path.dirname(os.path.abspath(__file__))
GIK = os.environ.get("IFS0P1_GIK") or os.path.join(HERE, "gik")
sys.path.insert(0, GIK)
import cgan_dag  # noqa: E402  (strips AWS_*)
import config as C  # noqa: E402

# cgan name -> (Herbie levtype, Herbie search, display scale/offset, unit, cmap)
SHOW = {
    "tp":   ("sfc", ":tp:sfc:{m}:",      1000.0, 0.0,     "mm (accum. 0-step)", "Blues"),
    "t2m":  ("sfc", ":2t:sfc:{m}:",      1.0,    -273.15, "°C",            "RdYlBu_r"),
    "tcwv": ("sfc", ":tcwv:sfc:{m}:",    1.0,    0.0,     "kg m⁻²",        "GnBu"),
    "sp":   ("sfc", ":sp:sfc:{m}:",      0.01,   0.0,     "hPa",           "viridis"),
    "cape": ("sfc", ":mucape:sfc:{m}:",  1.0,    0.0,     "J kg⁻¹ (mucape)", "magma_r"),
    "u700": ("pl",  ":u:700:pl:{m}:",    1.0,    0.0,     "m s⁻¹",         "RdBu_r"),
    "v700": ("pl",  ":v:700:pl:{m}:",    1.0,    0.0,     "m s⁻¹",         "RdBu_r"),
}


def herbie_box(date8, run, step, member, levtype, search, save_dir):
    from herbie import Herbie
    when = f"{date8[:4]}-{date8[4:6]}-{date8[6:]} {run}:00"
    for attempt in range(6):
        try:
            # Herbie falls back to GCS only when a file is MISSING, not on S3's
            # 503 Slow Down: alternate the sources (byte-identical copies)
            src = ["aws", "google"] if attempt % 2 == 0 else ["google", "aws"]
            H = Herbie(when, model="ifs", product="enfo", fxx=step, levtype=levtype,
                       priority=src, save_dir=save_dir, verbose=False)
            inv = H.inventory(search)
            assert len(inv) == 1, (search, len(inv))
            row = inv.iloc[0]
            ds = H.xarray(search, remove_grib=False)
            break
        except Exception as exc:                       # noqa: BLE001  503 Slow Down
            if attempt == 5:
                raise
            print(f"    herbie retry {attempt + 1} ({src[0]} failed): {str(exc)[:60]}")
            time.sleep(2 ** attempt)
    da = ds[list(ds.data_vars)[0]]
    lon = da.longitude.values
    da = da.assign_coords(longitude=np.where(lon >= 180, lon - 360, lon)).sortby("longitude")
    da = da.sortby("latitude", ascending=False)
    b = C.BOX
    # explicit target points, nearest: cfgrib's float coords (24.9999...) drop
    # an edge row/column from a plain label slice
    ny, nx = C.box_shape(b)
    tl = np.round(b["lat_max"] - b["deg"] * np.arange(ny), 4)
    tn = np.round(b["lon_min"] + b["deg"] * np.arange(nx), 4)
    sub = da.sel(latitude=tl, longitude=tn, method="nearest", tolerance=b["deg"] / 4)
    sub = sub.assign_coords(latitude=tl, longitude=tn)
    return (sub.values.astype(np.float32), sub.latitude.values, sub.longitude.values,
            int(row.end_byte) - int(row.start_byte), H.grib)


def realized_box(spec, field, step, member):
    import stores as S
    ds, _ = S.open_realized_dataset(spec, None)
    da = ds[field].isel(time=0).sel(number=member, step=np.timedelta64(step, "h"))
    da = da.sortby("latitude", ascending=False)
    return da.values.astype(np.float32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="20261026")
    ap.add_argument("--run", default="00")
    ap.add_argument("--step", type=int, default=54)
    ap.add_argument("--member", type=int, default=1, help="1..50 (enfo); 0 is in oper")
    ap.add_argument("--fields", nargs="+", default=list(SHOW))
    ap.add_argument("--realized", default=None, help="local realized store dir")
    ap.add_argument("--save-dir", default="/srv/state/ifs0p1/herbie")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    vd = C.vd_spec("sourcecoop")
    res, rows = {}, []
    for f in a.fields:
        levtype, search, *_ = SHOW[f]
        param, level, var = C.FIELDS[f]
        t0 = time.time()
        h, lat, lon, hbytes, url = herbie_box(a.date, a.run, a.step, a.member, levtype,
                                              search.format(m=a.member), a.save_dir)
        acc = {}
        p = cgan_dag.read_prefix(param, level, a.date, a.run, a.member, a.step, C.BOX, acc)
        v = cgan_dag.read_virtual(vd, var, level, a.date, a.run, a.member, a.step, C.BOX)
        r = realized_box("local:" + a.realized, f, a.step, a.member) if a.realized else None
        assert h.shape == p.shape == v.shape, (h.shape, p.shape, v.shape)
        d = {"P-H": np.abs(p - h).max(), "V-H": np.abs(v - h).max()}
        if r is not None:
            d["R-H"] = np.abs(r - h).max()
        res[f] = dict(h=h, p=p, lat=lat, lon=lon, d=d,
                      share=acc["bytes_fetched"] / acc["bytes_full"], hbytes=hbytes)
        rows.append((f, h.shape, d, acc["bytes_fetched"], hbytes))
        print(f"{f:5s} {h.shape}  " + "  ".join(f"max|{k}|={x:.3g}" for k, x in d.items())
              + f"  prefix {acc['bytes_fetched'] / 1e6:.2f} of {hbytes / 1e6:.2f} MB "
              f"({100 * acc['bytes_fetched'] / hbytes:.0f}%)  {time.time() - t0:.0f}s", flush=True)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    n = len(a.fields)
    fig, ax = plt.subplots(n, 3, figsize=(15, 4.1 * n),
                           subplot_kw=dict(projection=ccrs.PlateCarree()))
    ax = np.atleast_2d(ax)
    ext = [C.BOX["lon_min"], C.BOX["lon_max"], C.BOX["lat_min"], C.BOX["lat_max"]]
    for k, f in enumerate(a.fields):
        _, _, sc, off, unit, cmap = SHOW[f]
        R = res[f]
        H, P = R["h"] * sc + off, R["p"] * sc + off
        vmin, vmax = np.nanpercentile(H, [1, 99])
        if cmap == "RdBu_r":
            vmax = max(abs(vmin), abs(vmax)); vmin = -vmax
        exact = all(x == 0 for x in R["d"].values())
        diff = P - H
        titles = [f"{f}  Herbie + cfgrib, whole message ({R['hbytes'] / 1e6:.1f} MB)",
                  f"{f}  GIK prefix read via the VS index ({100 * R['share']:.0f}% of bytes)",
                  f"GIK − Herbie: " + ("identical, max |diff| = 0" if exact else
                                       ", ".join(f"{kk} {x:.2g}" for kk, x in R["d"].items()))]
        for j, (fld, t) in enumerate(zip([H, P, diff], titles)):
            kw = (dict(vmin=vmin, vmax=vmax, cmap=cmap) if j < 2 else
                  dict(vmin=-1e-6, vmax=1e-6, cmap="RdBu_r") if exact else dict(cmap="RdBu_r"))
            m = ax[k, j].pcolormesh(R["lon"], R["lat"], fld, transform=ccrs.PlateCarree(),
                                    shading="auto", **kw)
            ax[k, j].add_feature(cfeature.COASTLINE, linewidth=0.6)
            ax[k, j].add_feature(cfeature.BORDERS, linewidth=0.3)
            ax[k, j].set_extent(ext, crs=ccrs.PlateCarree())
            ax[k, j].set_title(t, fontsize=9)
            fig.colorbar(m, ax=ax[k, j], orientation="horizontal", pad=0.03, shrink=0.8,
                         label=unit if j < 2 else unit + " (difference)")
    fig.suptitle(f"ECMWF IFS ENS 0.1°  {a.date} {a.run}z +{a.step} h, member {a.member}:  "
                 f"box 25N–15S, 19–55E (401×361).  P = GIK prefix, V = virtual store "
                 f"(gribberish), R = realized store, H = Herbie", fontsize=11, y=0.999)
    fig.tight_layout()
    fig.savefig(a.out, dpi=90)
    print("wrote", a.out)
    print("gik code:", GIK)


if __name__ == "__main__":
    main()
