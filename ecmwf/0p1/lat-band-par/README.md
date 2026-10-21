# Latitude-band references for GIK pars

A GIK par chunk is `[url, offset, length]` for one whole GRIB message, so every
read today fetches the whole global field and cuts the box afterwards. Herbie
does the same. ECMWF packs every field as CCSDS:

- the packed data is a run of **RSIs**, 4,096 values each, which decode
  independently;
- they are stored north to south, a full row at a time.

So a **latitude band is one contiguous byte range**, once you know where each
RSI starts (`../README.md` §8). This folder adds that knowledge to the pars
**as a sidecar**, so it can be reused, and reads bands from it.

## What is here

| File | Role |
|---|---|
| `bandref.py` | `rsi_record(msg)`: decode one full message once and return its RSI bit offsets and Section 3/5 constants. `band_read(chunk, rec, lat_n, lat_s)`: fetch and decode only the band. `chunk_url()`: s3 → https, with `GIK_CHUNK_SOURCE=gcs` for `gs://ecmwf-open-data` |
| `augment_par.py` | par → **`<par>.rsi.parquet` sidecar** for the chosen variables, from one full read per message. It speeds up **re-reads**; for the realization's own read see below |
| `read_par_band.py` | par + sidecar → xarray band (optionally cut to a box after decoding); `--validate` compares every value with eccodes on the full message |
| `index_to_par.py` | stand-in par (chunk rows only, same key/value layout) built from ECMWF `.index`, for dates whose real pars are not readable from here |
| `example/` | the test inputs and sidecars below |

```bash
export GIK_CHUNK_SOURCE=gcs                              # byte-identical mirror, no S3 throttling
uv run augment_par.py <par.parquet> --vars 2t            # once: writes <par>.rsi.parquet
uv run read_par_band.py <par.parquet> --var 2t --lat 40,-40 --lon 15,80 --validate
```

## Test on the real pars: 20261019 00z, 0.25°, ens_01, all 85 steps

Read straight from `gs://gik-ecmwf-aws-tf/v20260623_run_par_ecmwf/2026/10/20261019/00z/2026101900z-ens_01.parquet`
with the `coiled-data@sewaa-416306` key (`coiled-data.json`, git-ignored),
bytes from the GCS mirror:

```bash
export GIK_CHUNK_SOURCE=gcs GOOGLE_APPLICATION_CREDENTIALS=../coiled-data.json
P=gs://gik-ecmwf-aws-tf/v20260623_run_par_ecmwf/2026/10/20261019/00z/2026101900z-ens_01.parquet
uv run augment_par.py $P --vars 2t,tp,msl,t/850,u/700 --threads 16
uv run read_par_band.py $P --side 2026101900z-ens_01.rsi.parquet --var t/850 --lat 40,-40 --validate
```

**The par refs are the `.index` triplets.** 656 of 656 chunk refs checked
(steps 0/24/240/360, every param and level) are exactly an `.index` record
`(_offset, _length)` of the same param, member and level. So results on
`.index` (`../lat-offset-test/`) carry over to the pars unchanged.

| | Result |
|---|---|
| Sidecar build | 425 messages (5 fields × 85 steps), 299 MB read once in 21 s with 16 threads → **372 KB** sidecar |

Band **40°N → 40°S**, every value checked against eccodes on the full message:

| Field | Fetched | Check (85 steps each) |
|---|---|---|
| `2t` | 27.95 of 56.14 MB (**49.8%**) | bit-exact |
| `tp` | 44.41 of 82.68 MB (**53.7%**) | bit-exact (0 h is a constant field: no bytes) |
| `msl` | 20.22 of 45.00 MB (**44.9%**) | bit-exact |
| `t/850` | 27.28 of 54.59 MB (**50.0%**) | bit-exact |
| `u/700` | 30.07 of 60.61 MB (**49.6%**) | bit-exact |

So **425 of 425** band reads are bit-exact on the real pars, at 0.6–0.74 s
per message from this machine. Cutting `t/850` to lon 15 → 80 after decoding
gives the realization's 321 × 261 box.

### Earlier stand-in tests

Before the key was available, the same code was tested on the public HF par
`2026040700z-ens_01` (`2t` at 0/24/120/240 h: bit-exact, 49.7%) and on a
stand-in par built from `.index` by `index_to_par.py` (`example/`):

| 20261019 00z `2t` ens01, 85 steps (stand-in par) | Result |
|---|---|
| Band **40°N → 40°S** | 27.95 of 56.14 MB (49.8%), bit-exact. Identical to the real par above |
| Band **24°N → 12°S** | **13.09 of 56.14 MB (23.3%)**, bit-exact |

Output is a `DataArray (step, latitude, longitude)` in float32, latitudes 40 →
−40, longitudes −180 → 179.75 in GRIB scan order (as in `ecmwf/grids.py`).
Validation runs in float64, as eccodes does.

## The sidecar

One row per chunk key of the par:

| column | meaning |
|---|---|
| `key` | the par chunk key, e.g. `step_024/2t/sfc/ens01/0.0.0` |
| `d0` | byte offset of the packed data inside the message (Section 7 + 5) |
| `ni`, `nj` | grid (1440 × 721 at 0.25°) |
| `bps`, `ref_value`, `bin_scale`, `dec_scale` | Section 5 unpacking: value = (R + X·2^E) / 10^D |
| `ccsds_flags`, `block_size`, `rsi` | CCSDS parameters (14, 32, 128 on every message seen) |
| `rsi_bit_offsets` | little-endian uint32 bit offset of every RSI from the first data byte (254 at 0.25°, 1,583 at 0.1°) |

The **full** offset table is stored, so any latitude band can be read later,
not only the one in use today. The par is left untouched, so existing
readers, including stage ② building the virtual Icechunk store, see no change.

**Size.** ~1 KB per message (0.25°), which is **much bigger than the par**:
- per run: 39 channels × 85 steps × 51 members ≈ 169 k messages ≈ **170 MB**,
  against ~7 MB of pars;
- uint16 *deltas* would halve it;
- keeping only every 8th RSI as a checkpoint (~22 rows at 0.25°) gives
  ~128 B per message (~21 MB per run), at the cost of up to 7 extra RSIs
  (~2.8% of a message) per read.

Pick by how many different bands will be read.

## Cutting the realization's own read

The point of the latitude subset is to make the **realization** (stage ③,
`../../ewc-realization/frisky_daily_dag.py`, `read_message`) fetch less GRIB
from the bucket. Today it fetches every global message in full and cuts the
`ea-sw-io40` box (40°N → 40°S, 15°E → 80°E) afterwards.

**Building the sidecar inside the realization does not do that.** It reuses
the same full read, so the realization still fetches 100%, and only *later*
reads get cheaper. The realization runs first on a new date, and no one
before it has the RSI offsets:

- ECMWF does not publish them;
- stage ② reads only `.index`;
- moving a full read upstream moves the cost, it does not remove it.

So the realization's read has to find the offsets **itself, from a partial
read**. That is route B (`../lat-offset-test/v5/`, tested on these pars):

```
template (once per field and step, from one member of an earlier run, same era):
    full read → RSI offset profile + values

realization, per message:
    par ref ─▶ header GET (~1 KB) ─▶ window GET around the predicted band
            ─▶ sync to an exact RSI start ─▶ name the RSI
               (count to the end of the message, or value match against the template)
            ─▶ decode the band ─▶ cut 15°E → 80°E ─▶ box
            └▶ the decode also gives the exact RSI offsets → sidecar row, no extra bytes
```

What the realization fetches per message (band 40°N → 40°S, template from
the previous day, every band checked against eccodes):

| Policy | Template | Correct | Fetched |
|---|---|---|---|
| today: full message | — | — | 100% |
| **count to the end, always** | any age in the era (5 months tested) | **336 / 336** | **~77%** |
| **`tp` counted; others value match, count if the score > 0.45** | previous run (1 day) | **188 / 188** on 2 fresh dates | **~62%** |
| same, older template | 7 days – 5 months | 15–28% silently wrong | — |
| exact band, only if a sidecar already exists | — | 425 / 425 | ~45–54% |

Counts are from `../lat-offset-test/v5/` (v5, v5c, v5d). The `tp` policy was
fixed before its v5c/v5d test. A window that does not sync (1 in 376) falls
back to a full read.

- **Template cost:**
  - the ~62% policy needs a template refreshed every run: one full read per
    (field, step), from one member, i.e. 1 in 51 messages;
  - the ~77% policy can use one fixed template per era, read once.
- **Narrower bands save more:** for 24°N → 12°S, count-to-end costs ~60–70%
  (`../lat-offset-test/out/v4_tail_cost.txt`), against 100% today.
- **Longitude is still the whole 360°.** A box narrower than the globe costs
  the same as its latitude band.
- **No sidecar is written by a value-match read:** it can be silently wrong.
  A count-to-end read writes the exact offsets from the band start south, so
  **re-reads** of that message (reprocessing, a new channel or box at the same
  latitudes, validation, source.coop users) fetch only the exact band, ~50%.

Where to write the sidecar: one `{date}{run}z-{member}.rsi.parquet` beside
the par (`gs://…/v20260623_run_par_ecmwf/{Y}/{MM}/{date}/{run}z/`), keyed by
the par chunk key. The par is not changed. A par is `key → [url, offset,
length]`, which stage ② turns into Icechunk virtual chunks, and neither
format has room for a bit offset or the Section 5 constants. So stage ② stays
as it is, and the sidecar can be dropped or rebuilt independently.

**Icechunk:** for the same reason, the realization reads a band through the
par reader path (`bandref.py`, as `read_par_band.py` does), not through the
current virtual store. Otherwise the chunk manifest would need extending.

## Limits

- **Latitude only.** Longitude is cut after decoding, because an RSI (4,096
  values) is longer than a row (1,440).
- **Par keys.** The `v20260623` pars key pressure levels as
  `step_SSS/{param}/pl/{level}/{member}/0.0.0` (164 chunk rows per step).
  `--vars t/850` takes one level, `--vars t` all of them. `sol` params (`sot`,
  `vsw`) have **one** row each, though the file holds 4 soil levels. Matched
  against `.index`, `step_024/sot/sol/ens01` points at soil level **2** and
  `vsw` at level **4**, and the key does not say which. This looks like one
  level overwriting the others when the par was written: a par-building issue
  to fix upstream, not a band-read one.
- **0.25° only for now**, as requested. `bandref.py` reads the grid from
  Section 3, so 0.1° needs no code change.
- **Pars exist for 00z only** (20261018, 20261019, 20261020 at the time of
  writing).
