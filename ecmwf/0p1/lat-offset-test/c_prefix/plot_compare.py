# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "xarray", "herbie-data", "cfgrib", "eccodes", "matplotlib", "cartopy"]
# ///
"""Plot an option C band read beside Herbie's read of the same GRIB message.

The band comes from prefix_read.py --save (message start -> band end, decoded
from bit 0). Herbie downloads the whole message by its .index byte range and
decodes it with cfgrib. Both are cut to the same box and compared value by
value (as float32, cfgrib's dtype).

    uv run c_prefix/plot_compare.py <c.npz> <date> <step> <param> <member> <out.png> [--box 15,80,40,-40]
"""
import argparse

import cartopy.crs as ccrs
import cartopy.feature as cfeature
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from herbie import Herbie

ap = argparse.ArgumentParser()
ap.add_argument("npz"); ap.add_argument("date"); ap.add_argument("step", type=int)
ap.add_argument("param"); ap.add_argument("member", type=int); ap.add_argument("out")
ap.add_argument("--box", default="15,80,40,-40", help="west,east,north,south (ea-sw-io40)")
ap.add_argument("--save-dir", default="/tmp/herbie")
a = ap.parse_args()
w, e, n, s = map(float, a.box.split(","))

# ---- option C band read (saved by prefix_read.py) ----
c = np.load(a.npz)
lats, lons, vals = c["lats"], c["lons"], c["vals"]
ci = (lats <= n) & (lats >= s)
cj = (lons >= w) & (lons <= e)
cbox = vals[np.ix_(ci, cj)].astype(np.float32)

# ---- Herbie: the same message from the same file ----
H = Herbie(a.date, model="ifs", product="enfo", fxx=a.step, priority=["aws"], save_dir=a.save_dir)
search = f":{a.param}:sfc:{a.member}:g:"
inv = H.inventory(search)
assert len(inv) == 1, inv
row = inv.iloc[0]
# Herbie's end_byte is start + length (exclusive)
same_bytes = int(row.start_byte) == int(c["offset"]) and int(row.end_byte) - int(row.start_byte) == int(c["length"])
ds = H.xarray(search, remove_grib=False)
da = ds[list(ds.data_vars)[0]]
hl = da.longitude.values
hl = np.where(hl >= 180, hl - 360, hl)
da = da.assign_coords(longitude=hl).sortby("longitude")
hbox = da.sel(latitude=slice(n, s), longitude=slice(w, e)).values.astype(np.float32)
assert hbox.shape == cbox.shape, (hbox.shape, cbox.shape)
diff = cbox - hbox
exact = np.array_equal(cbox, hbox)
herbie_bytes = int(row.end_byte) - int(row.start_byte)   # the whole message

scale, unit = (1000.0, "mm") if a.param == "tp" else (1.0, da.attrs.get("units", ""))
lon_b, lat_b = lons[cj], lats[ci]
fig, ax = plt.subplots(1, 3, figsize=(16, 6.2), subplot_kw=dict(projection=ccrs.PlateCarree()))
vmin, vmax = np.nanpercentile(hbox * scale, [1, 99])
titles = [f"Option C (band read)\n{c['fetched'] / 1e3:.0f} KB of {int(c['length']) / 1e3:.0f} KB fetched "
          f"({100 * c['fetched'] / c['length']:.0f}%)",
          f"Herbie + cfgrib\n{herbie_bytes / 1e3:.0f} KB fetched (whole message)",
          f"C − Herbie\n{'identical (max |diff| = 0)' if exact else f'max |diff| = {np.abs(diff).max():.3g}'}"]
for k, (fld, t) in enumerate(zip([cbox * scale, hbox * scale, diff * scale], titles)):
    kw = dict(vmin=vmin, vmax=vmax, cmap="Blues" if a.param == "tp" else "RdYlBu_r") if k < 2 else \
        dict(vmin=-1e-6 if exact else None, vmax=1e-6 if exact else None, cmap="RdBu_r")
    m = ax[k].pcolormesh(lon_b, lat_b, fld, transform=ccrs.PlateCarree(), shading="auto", **kw)
    ax[k].add_feature(cfeature.COASTLINE, linewidth=0.6)
    ax[k].add_feature(cfeature.BORDERS, linewidth=0.3)
    ax[k].set_extent([w, e, s, n], crs=ccrs.PlateCarree())
    ax[k].set_title(t, fontsize=10)
    fig.colorbar(m, ax=ax[k], orientation="horizontal", pad=0.04, shrink=0.85, label=unit)
fig.suptitle(f"ECMWF ENS {a.date} +{a.step} h, {a.param}, member {a.member}: box {w:g}–{e:g}°E, "
             f"{n:g}°N–{abs(s):g}°{'S' if s < 0 else 'N'}  |  same GRIB message: {'yes' if same_bytes else 'NO'}",
             fontsize=11)
fig.tight_layout()
fig.savefig(a.out, dpi=110)
print(f"{a.param} +{a.step}h m{a.member}: box {cbox.shape}, identical={exact}, max|diff|={np.abs(diff).max():.3g}, "
      f"same message bytes={same_bytes}, C fetched {int(c['fetched'])} B vs Herbie {herbie_bytes} B "
      f"({100 * int(c['fetched']) / herbie_bytes:.1f}%) -> {a.out}")
