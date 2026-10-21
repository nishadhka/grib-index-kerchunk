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
| `augment_par.py` | par → **`<par>.rsi.parquet` sidecar** for the chosen variables. This is the step the realization would run, since it already reads every message in full |
| `read_par_band.py` | par + sidecar → xarray band (optionally cut to a box after decoding); `--validate` compares every value with eccodes on the full message |
| `index_to_par.py` | stand-in par (chunk rows only, same key/value layout) built from ECMWF `.index`, for dates whose real pars are not readable from here |
| `example/` | the test inputs and sidecars below |

```bash
export GIK_CHUNK_SOURCE=gcs                              # byte-identical mirror, no S3 throttling
uv run augment_par.py <par.parquet> --vars 2t            # once: writes <par>.rsi.parquet
uv run read_par_band.py <par.parquet> --var 2t --lat 40,-40 --lon 15,80 --validate
```

## Test: 20261019 00z, 0.25°, `2t`, member 1, all 85 steps

**The pars themselves were not readable.** The provided key,
`coiled-data-e4drr@e4drr-crafd`, is denied list *and* read on
`gs://gik-ecmwf-aws-tf/v20260623_run_par_ecmwf/` (as
`daily-route-crma/.../make_run_date_lists.py` already notes). The public HF
mirror stops at 2026-04-07.

So the test has two parts:

1. **Real par format:** the HF par `2026040700z-ens_01.parquet`, `2t` at steps
   0/24/120/240. Sidecar built, band read, validated: **bit-exact**, 49.7% of
   the bytes. This checks the key/value parsing, the url without `.grib2`,
   and the step keys against a genuine par.
2. **The requested date:** `index_to_par.py` built the 20261019 00z `ens01`
   chunk rows for `2t` from `.index`. These are the same triplets GIK Stage 2
   writes from the same `.index`.

| 20261019 00z `2t` ens01, 85 steps | Result |
|---|---|
| Sidecar build (one full read of each message) | 56.1 MB read once, 9.2 s with 8 threads → **84 KB** sidecar (254 RSI offsets per message) |
| Band **40°N → 40°S** (`ea-sw-io40` latitudes) | **27.95 of 56.14 MB (49.8%)**, every one of 85 steps **bit-exact** against eccodes on the full message |
| Same band, cut to **lon 15 → 80** after decoding | 321 × 261: the realization's box, from 49.6% of the bytes |
| Band **24°N → 12°S** | **13.09 of 56.14 MB (23.3%)**, 85 steps bit-exact |
| Read time | 0.5–0.7 s per message (one range GET plus decode, from this machine) |

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

## Using the realization to build it

The realization (stage ③, `../../ewc-realization/frisky_daily_dag.py`,
`read_message`) already reads **every global message in full**, once, to cut
the `ea-sw-io40` box. The band sidecar is a by-product of that read, if the
read is made from the bytes:

```
today:     virtual Icechunk chunk ──gribberish codec──▶ global field ──.sel(box)──▶ box
proposed:  par ref (url, off, len) ──range GET──▶ msg ──rsi_record()──▶ global codes + RSI offsets
                                                        │                    │
                                                        ▼                    ▼
                                                   box (as today)   sidecar row ─▶ {date}{run}z-{member}.rsi.parquet
```

- **No extra bytes**: the same one read per message.
- **Less CPU**: libaec decode plus offset capture takes **9.4 ms** per 0.25°
  message, against **19.0 ms** for the gribberish decode used now (measured
  here on the same message).
- Rows are gathered per (date, run, member) and written beside the par
  (`gs://…/v20260623_run_par_ecmwf/{Y}/{MM}/{date}/{run}z/{date}{run}z-{member}.rsi.parquet`),
  or into a parallel prefix.
- **Every later read is ~50%** (40°N–40°S) or ~23% (24°N–12°S): reprocessing,
  a new channel or box at the same latitudes, validation, and source.coop
  users. That covers any longitude: West Africa, South Asia and the SW Indian
  Ocean all sit inside the same band.
- The realization's **own** first read cannot benefit, because it is the read
  that builds the sidecar. For first reads without a full read, see
  `../lat-offset-test/` (sync from a per-step template, ~56–60%).
- **Icechunk:** a virtual chunk can only be `[url, offset, length]`, with no
  room for a bit offset or unpacking constants. A band chunk therefore needs
  the parquet reader path (`read_par_band.py`), or an extension to the chunk
  manifest, not the current virtual store.

## Limits

- **Latitude only.** Longitude is cut after decoding, because an RSI (4,096
  values) is longer than a row (1,440).
- **Tested on one variable.** `2t` was tested; other single-level params
  share the code path. Pressure-level keys must match
  `step_SSS/{param}/pl/{member}/0.0.0`. The legacy HF par has only one `pl`
  row per param (47 chunk rows per step, so one level of 13). Check how the
  `v20260623` pars key their levels before running `--vars t,u,...`.
- **0.25° only for now**, as requested. `bandref.py` reads the grid from
  Section 3, so 0.1° needs no code change.
- **Real 20261019+ pars** need a key with read on `gik-ecmwf-aws-tf`
  (`coiled-data@sewaa-416306` is the one the daily route documents). Then:

  ```
  GOOGLE_APPLICATION_CREDENTIALS=<key> uv run augment_par.py gs://gik-ecmwf-aws-tf/v20260623_run_par_ecmwf/2026/10/20261019/00z/2026101900z-ens_01.parquet --vars 2t
  ```

  The HF mirror names member files `ens_01`; check the bucket's naming.
