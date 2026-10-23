# ECMWF ENS: `.index` on GCS → local virtual Icechunk store, no template

This folder builds a **virtual Icechunk store** of one ECMWF ENS cycle straight
from the `.index` files ECMWF publishes on `gs://ecmwf-open-data`. It needs no
stage-1 pars, no HuggingFace template, no `scan_grib` and no GRIB bytes. It
then checks the store against Herbie, member by member.

| File | Role |
|---|---|
| `index_to_icechunk.py` | `.index` (GCS, HTTPS) → refs → virtual Icechunk store; built-in read-back check |
| `test_store_vs_herbie.py` | opens the store like a consumer, compares with Herbie, writes `plots/*.png` + `*.json` |
| `plots/` | Herbie comparison maps and reports |

Status (2026-10-23): three stores built locally, every check exact. The way
forward is to realize them with the latitude prefix read (§6).

---

## 1. Why no template is needed

The pipeline so far was

```
.index (s3) → stage 1 pars (+ template metadata) → build_ecmwf_icechunk.py → icechunk
```

The Icechunk builder only uses the `step_*` refs from a par, and every ref
comes from the `.index`. The template adds only `.zarray`/`.zattrs` keys.
Checked on 2026-10-22, `gik-fmrc-v2ecmwf_fmrc.tar.gz` `ens_01` rt000.par has
2,774 keys, 0 of them `step_*` and 0 refs. The parquet/kerchunk readers need
those keys. The Icechunk store does not:

| Store part | Comes from |
|---|---|
| grid, pl levels, step axis, era | `icechunk-par/build_ecmwf_icechunk.py` (`ERAS`, `STEPS_BY_RUN`), `grids.era_for` |
| array names | `.index` `param` (+ level for pl channels, as the builder names them) |
| decoding | the gribberish Zarr v3 codec, one chunk = one GRIB message |
| `[url, offset, length]` | `.index` `_offset`, `_length` |

So the route is

```
gs://ecmwf-open-data/{date}/{run}z/ifs/0p25/{enfo,oper}/*.index
   → refs (gs://…grib2, offset, length)
   → same group layout as build_ecmwf_icechunk.py: {era}/{run}z
```

**Same refs as stage 1, proven.** `--parity-pars` on 20261019 00z:
712,133 = 712,133 refs, 0 present on only one side, 0 differing. Reading
takes 170 GETs (about 11 s); stage 1 needs 4,335 (51 members × 85 steps).

**Why GCS.** The GCS objects are byte-identical to `s3://ecmwf-forecasts`.
One full run per era was compared object by object (same list, same sizes,
identical `.index` at every step). The comparison is in
`daily-route-crma/medium-range-forecast/2-icechunk-virtual/GCS_AS_CHUNK_SOURCE.md`.
So refs point at `gs://` and the AWS throttle (`503 Slow Down`) is out of the
path, both for reading the index and for reading chunks later. GCS holds
2023-07-12 onward only.

Rules carried over from stage 1 on purpose:

- **Control:** in 50r1 it comes from `oper/fc`; before 50r1, from `enfo`
  `number=0`. The control has 15,133 refs per 00z cycle; each perturbed member
  has 13,940.
- **Duplicate keys** `(step, member, param, levtype[, level])`: the last
  index line wins, as stage 1's dict does. That is wrong for the soil
  variables. Pass `--soil-layers` to keep all four layers (§7).
- **Containers:** both `s3://ecmwf-forecasts/` and `gs://ecmwf-open-data/` are
  registered. On an existing store, `gs://` is merged into the stored config.
  A fresh config that names only `gs://` would *replace* `s3://` on save.

## 2. Building a store

```bash
cd ecmwf/gcs-source
uv run index_to_icechunk.py --date 20261021 --run 00 \
    --store /home/ubuntu/icechunk-local/ecmwf-ens-20261021-00z
```

| Flag | Meaning |
|---|---|
| `--era` | default `grids.era_for(date, run)` |
| `--store` | local path or `gs://bucket/prefix` (`--sa-key` for a service account) |
| `--verify N` | read N random chunks back through the store and compare with a direct decode (default 24) |
| `--verify-only` | check an existing store only |
| `--parity-pars DIR` | compare the refs with a date's stage-1 pars |
| `--overwrite-date` | rewrite a date already in a preallocated group |
| `--soil-layers` | key `sot`/`vsw` by layer and write them on a `soilLayer` axis (1–4); see §7. Not combinable with `--parity-pars` |

One commit per `(era, run, date)`. Several cycles can go into one store: each
run hour is its own group, and dates append on `time`.

## 3. The three local stores (built 2026-10-23)

One folder per cycle under `/home/ubuntu/icechunk-local/`. Each was run one
after another on this machine (gateway VM, ECMWF cloud). The table is the
first build. The same day, all three were **rebuilt with `--soil-layers`**:
443,012 / 768,488 / 768,488 refs, 32/32 read-back each, same timings within
a second, 9 / 17 / 17 MB.

| Store | Group | `.index` files | Refs | Read indexes | Commit | **Build** | Read-back | Size |
|---|---|---|---|---|---|---|---|---|
| `ecmwf-ens-20261020-06z` | `50r1/06z` | 98 | 428,018 | 8.3 s | 3.4 s | **11.7 s** | 24/24 equal, 30.9 s | 8.6 MB |
| `ecmwf-ens-20261021-00z` | `50r1/00z` | 170 | 742,478 | 13.1 s | 5.7 s | **18.8 s** | 24/24 equal, 29.5 s | 17 MB |
| `ecmwf-ens-20261022-00z` | `50r1/00z` | 170 | 742,478 | 11.6 s | 5.6 s | **17.2 s** | 24/24 equal, 30.4 s | 17 MB |

- **Build time** is reading the `.index` files plus committing the refs:
  12–19 s per cycle. With the 24-chunk read-back the whole command takes
  about 48 s.
- **Contents:** 51 members, 0.25° global (721 × 1440, lon −180…179.75,
  lat 90…−90), 14 pl levels, 4 soil layers.
  - 00z: 85 steps (0–360 h), 49 sfc + 2 soil (`sot`, `vsw`) + 10 pl variables.
  - 06z: 49 steps (0–144 h), 47 sfc + 2 soil + 10 pl. The two missing are
    `mx2t6`/`mn2t6`, which exist only in the long runs past 144 h.
- **The 7 new 43-sfc parameters** `cp hcc lcc mcc tciw tclw tcrw` are in all
  three stores. That regime starts exactly at 20261020 06z: 00z that day has
  47 params, 06z has 54. These parameters are what a template from before
  that date would orphan, and here they need nothing.
- Each store holds only metadata and manifests. The GRIB stays on GCS.

## 4. Streaming from a store

```python
import icechunk, xarray as xr, gribberish.zarr  # registers the codec

repo = icechunk.Repository.open(
    icechunk.local_filesystem_storage("/home/ubuntu/icechunk-local/ecmwf-ens-20261021-00z"),
    authorize_virtual_chunk_access=icechunk.containers_credentials({
        "s3://ecmwf-forecasts/": icechunk.s3_anonymous_credentials(),
        "gs://ecmwf-open-data/": icechunk.GcsCredentials.Anonymous()}))
ds = xr.open_zarr(repo.readonly_session("main").store, group="50r1/00z",
                  zarr_format=3, consolidated=False, chunks=None)
tp = ds.tp.isel(time=0).sel(step=[24, 48, 72],
                            latitude=slice(15, -12), longitude=slice(21, 52))
```

`test_store_vs_herbie.open_store(path, era, run)` does the same thing.

Measured on each store from a fresh process: the open took 0.1 s. Reading
`tp` over East Africa for 51 members × 3 steps took 7–8 s, about 150 GRIB
messages fetched whole and decoded by gribberish. There were no NaNs. The
ensemble mean at T+72 h was 7.07, 7.25 and 8.51 mm.

## 5. Herbie verification (`test_store_vs_herbie.py`)

The store side reads the way a consumer reads: `xr.open_zarr`, then `.sel`
**by label** on time, step, latitude and longitude, through `gs://` refs
decoded by gribberish. Herbie works independently: its own index parse, its
own download (Azure or data.ecmwf.int first, not GCS), and a cfgrib/eccodes
decode. Its result is put onto the store's labels. A wrong axis, a permuted
member, a step filed at the wrong index or a codec error would each show up
as a difference.

```bash
uv run test_store_vs_herbie.py --store /home/ubuntu/icechunk-local/ecmwf-ens-20261020-06z \
    --date 20261020 --run 06 --var tp --steps 24,72,144
uv run test_store_vs_herbie.py ... --var t --level 850 --steps 0,48,96 --control
```

Pass criteria per member: max |diff| ≤ 1e-4 of the field range and r ≥ 0.9999.
The script exits 1 on any failure.

| Store | Variable | Steps | Members | max \|diff\| | min r |
|---|---|---|---|---|---|
| 20261020 06z | `tp` | 24, 72, 144 | 50/50 | **0** | 1.000000 |
| 20261020 06z | `t` 850 hPa | 0, 48, 96 | 51/51 (control from `oper`) | **0** | 1.000000 |
| 20261020 06z | `lcc` (new 43-sfc param) | 24, 72, 144 | 50/50 | **0** | 1.000000 |
| 20261022 00z | `tp` | 24, 120, **240** (6-hourly part) | 51/51 | **0** | 1.000000 |
| 20261021 00z, `--soil-layers` | `sot` layer 1, layer 4 | 24, 240 | 51/51 each | **0** | 1.000000 |
| 20261021 00z, `--soil-layers` | `vsw` layer 1, layer 4 | 24, 240 | 51/51 each | **0** | 1.000000 |

All results are bit-exact. Maps: `plots/store_vs_herbie_{var}_{date}{run}z.png`
(store ensemble mean | Herbie ensemble mean | difference, East Africa). Reports
are in the matching `.json`. Store reads took 2–3 s per step; Herbie took
7–24 s.

Two Herbie behaviours the script works around:

- **No fallback after a failed download.** Herbie picks a source by
  existence and fails outright if that source throttles the download (AWS
  `503 Slow Down`). `--priority azure,ecmwf,aws,google` is tried one source
  at a time.
- **Partial download reused.** A download that died midway left a 1-message
  subset, and Herbie reused it as if it were complete (1 member of 50).
  `H.xarray(..., overwrite=True)` fixes it.

## 6. Way forward: realizing the store with the latitude prefix read

The realization (stage ③, `ewc-realization/frisky_daily_dag.py`) opens the
`{era}/00z` group and, per message, does `.sel(latitude=slice(40, -40))`. Each
chunk is a **whole** GRIB message, so the 50 % of the globe it drops is still
fetched and decoded.

**Option C, the prefix read** (`../0p1/lat-offset-test/c_prefix/README.md`).
CCSDS data (template 5.42) is a run of RSIs, and the first RSI starts at bit
0 of Section 7. Samples are in scan order, north → south. So the bytes from
the **message start** to the end of the band's last RSI hold everything
needed: the headers, plus rows 90°N → the south edge. That needs no search,
no stored offsets and no value match.

| Measured (20261013, 20261017 00z; `2t tp msl t/850 u/700`) | Result |
|---|---|
| band values vs eccodes on the full message | **376/376 exact** |
| fetched, 90°N → 40°S | ~77 % of the message |
| fetched, 90°N → 12°S | ~61 % |
| vs Herbie, same message, box 15–80°E | identical |
| cost by south edge | 40°N 27 %, 0° 52 %, 12°S 59 %, 40°S 76 %, 60°S 87 % |
| manifest margin needed over the template prediction | **3 % + 4 KiB**: 0 short in 658 cases, templates up to 4 months old |

The Rust tool (`../0p1/lat-offset-test/rust/`) is the search for an RSI start
inside an arbitrary window. It is needed only for reads that start mid-message,
such as the exact-band read. In Rust it takes 0.50 s per message, against
7.6 s in Python, with the same 54/54 answers. Its allocation-free CCSDS
decoder (`fastdec.rs`) decodes from bit 0 with an early stop, which is what
the prefix read needs. The prefix read itself needs no search.

**How it fits this store.** Stage ② (this folder) writes a **second group**
per south edge next to the whole-message one, and still reads no GRIB:

```
{era}/00z          [url, offset, length]        whole message (as now)
{era}/00z-s40      [url, offset, prefix_len]    rows 90°N → 40°S
    prefix_len = d0 + profile[k_end] × (length − d0) + 3 % × length + 4 KiB
    latitude 90 → −40 (521 rows at 0.25°), chunk (…, 521, 1440)
    codec: GribberishCodec(var=…, rows=521)
```

`profile` is the template's **RSI-end profile**: the fraction of the data
section at each RSI end, per (field, step). There are about 255 floats per
field and step at 0.25°. It comes from one full read per (field, level,
step) of one member, about 1 in 51 messages of one run, refreshed monthly.
This is a small sidecar file. It is not the HuggingFace template, which this
route still does not need.

The realization then opens `{era}/00z-s40` with `read_message` unchanged. On a
short chunk the codec raises and the realization re-reads that message from
`{era}/00z`.

**Steps, in order:**

| # | Step | State |
|---|---|---|
| 1 | Local virtual store from `.index` only, `gs://` refs | **done** (§3) |
| 2 | Herbie verification routine, bit-exact | **done** (§5) |
| 3 | Prefix read exact from a truncated message, decoded from bit 0 | **done** in Python + libaec: 376/376, and 188/188 at the exact minimum length |
| 4 | Rust CCSDS decoder / band search | **done** as a benchmark (`rust/`), not packaged |
| 5 | gribberish `decode(..., max_rows=N)` tolerant of a short Section 7, plus a `rows=N` codec option that raises if bytes stop before row N | **not built**: drafted as Issue 2 in `../0p1/gribberish-issue-drafts.md`, not posted |
| 6 | RSI-end profile builder (one full read per field/step) | logic exists in `c_prefix/prefix_read.py`; needs a standalone script and storage format |
| 7 | `index_to_icechunk.py --prefix-south 40` writing `{era}/{run}z-s40` | not built: same refs, `length` → `prefix_len`, a shorter latitude axis |
| 8 | Realization fallback to the whole-message group | not built: a `try/except` around `.values` |
| 9 | Herbie check on the prefix group (`test_store_vs_herbie.py --group …-s40`) | not built: the same comparison, by label, on the band |
| 10 | Repeat the margin table on more dates, and at 0.1° | owed: one test date so far |

**Before the codec exists**, `read_message` can do the prefix read itself:
one GET of `prefix_len` from the store's ref, a libaec decode from bit 0 and
a top-up GET if short. That is `prefix_read.py`'s logic. It costs one
dependency (libaec ≥ 1.1, already in the imagecodecs wheel) and no store
change.

## 7. Soil variables: the stored field mixes soil depths

**The defect.** Since the 13-level era (2025-01-14 06z), every cycle has two
soil variables on levtype `sol`, `sot` (soil temperature) and `vsw`
(volumetric soil moisture), each at four layers (`levelist` 1–4, 1 = top).
Stage 1 keys a ref as `{var}/{levtype}/{member}`. Pressure levels got the
level in the key in `4ca1c21` ("per-pressure-level keys … fix collapse
bug"), but `sol` was left out on purpose. The comment at
`../ecmwf_index_processor.py:160` reads *"sfc/sol keep their old shape"*, and
production stage 1 has the same line
(`crma/medium-range-forecast/1-pars-lithops/run_lithops_ecmwf.py:365`). The
four layers share one key, and the last `.index` line wins.

**Why it is worse than "one layer kept".** The `.index` lists the four
layers in **no fixed order**. Member 1 at T+24 has `vsw` as 2, 3, 4, 1 and
`sot` as 3, 2, 4, 1. So the layer that survives changes from member to member
and from step to step. Over 51 members × 85 steps of 20261021 00z:

| Variable | Layer kept: 1 | 2 | 3 | 4 |
|---|---|---|---|---|
| `sot` | 1,986 | 741 | 859 | 664 |
| `vsw` | 850 | 0 | 850 | 2,550 |

Comparing the old store with the layer-aware one (members 0–50, T+24 h),
each member's field is **bit-identical** to one real layer:

```
sot  314312434213124431311121131214124131244321131211314
vsw  314344434443441434413441434414441434414341434414341
```

The data is valid GRIB with the wrong label, so nothing fails. Over East
Africa at T+24 the old `sot` ensemble mean is 300.48 K. The true layer means
are 298.98 / 301.72 / 301.55 / 301.32 K, so the old mean matches no layer.

**History.**
- `3eaca85` fixed the *drop* of soil fields: `sol` had been an unknown
  levtype, and it is now treated as surface.
- `415aa5e` noticed the collapse ("ens01 sot points at level 2, vsw at
  level 4 … a par-building issue to fix upstream").
- Nothing has fixed it since, in either repo. No consumer reads `sot`/`vsw`
  today: the cGAN, the realization and frisky do not use them. So the harm is
  latent, in the published `ecmwf-ens-v4` store and its pars.

**The fix in this builder (`--soil-layers`).** Soil is treated like pressure
levels. The `.index` `levelist` goes into the key, and `sot`/`vsw` are
written as `(time, number, step, soilLayer, latitude, longitude)`, with
chunk `(1, 1, 1, 1, 721, 1440)` and a `soilLayer` coordinate `[1, 2, 3, 4]`.
On 20261021 00z:

- the collapse is gone: 768,488 refs, which is the 742,478 before plus
  exactly the 26,010 lines that were collapsed;
- 34,680 soil refs, which is 2 variables × 4 layers × 85 steps × 51 members;
- read-back is 32/32 equal, every layer for the control and member 50;
- Herbie matches bit-exactly, 51/51, for `sot` and `vsw` at layers 1 and 4,
  T+24 and T+240 (`test_store_vs_herbie.py --var sot --level 1`; the script
  now searches `:sot:1:sol:` when the array has a `soilLayer` axis).

It is a flag because the default proves itself against stage-1 pars
(`--parity-pars`), and those pars carry the collapse. An existing store with
5-D `sot`/`vsw` refuses a `--soil-layers` date on the shape check, so the two
layouts never mix in one group.

**What production needs (not done):**

1. **Stage 1** (`run_lithops_ecmwf.py`, `ecmwf_index_processor.py`): key `sol`
   like `pl`, as `{var}/sol/{levelist}/{member}/0.0.0`.
2. **`build_ecmwf_icechunk.py`**: parse the layer segment, add the `soilLayer`
   axis, and route `sol` through a `soil_specs` like this builder's.
3. **`ecmwf-ens-v4`**: the existing `sot`/`vsw` arrays cannot be repaired in
   place, because the pars no longer say which layer each ref was. Either
   rebuild those two arrays from the `.index` (this builder's route: 170 GETs
   per cycle, the GRIB is untouched), or mark them as mixed-layer and drop
   them.
4. Repeat the check on a 49r1 13-level date, where `sol` first appears.

## 8. Known limits

- **0.1°: `--resolution 0p1`.** `ifs/0p1/` files are split `-pl`/`-sfc`, each
  with its own `.index`, so a 0.1° run reads both (340 indexes per 00z/12z, 196
  per 06z/18z). The groups are `0p1/{run}z` on the 1801 × 3600 grid of
  `grids.ERAS["0p1"]`. First built and published 2026-10-23, then realized for
  CRMA's 39 channels: [`../0p1/realize-0p1/README.md`](../0p1/realize-0p1/README.md).
- **Local filesystem storage** is not safe for concurrent commits. Build one
  cycle at a time per store, or use `gs://` storage.
- **Longitude** is the full 360° in every route, including the prefix read.
- **Before 2023-07-12** the data is on AWS only. Use the `s3://` route.
