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

## Two ways to generate the band references

Both were tested on the real `v20260623` pars, and both write the same
sidecar rows.

| | A. From a full read | B. Without a full read (`../lat-offset-test/v5/`) |
|---|---|---|
| Input | par ref, then the whole message | par ref + a template par from another date (one full read per step, once) |
| Bytes per message | 100%, once | ~77%: band start to the end of the message (count-to-end) |
| Offsets known | every RSI | every RSI from the band start to the end (valid for any band starting at or south of it) |
| Tested | 425/425 band reads bit-exact (5 fields × 85 steps) | 148/148 exact (2 dates, members, control, 0–360 h) |
| Fits | the realization, which reads every message in full anyway | a reader that has not seen the message yet |

After either one, every later band read is the exact band (~45–54% for
40°N → 40°S).

**Add it as a sidecar, not inside the par.** A par is `key → [url, offset,
length]`, which stage ② turns into Icechunk virtual chunks. A band read needs
a bit offset and the Section 5 constants as well, which neither format has a
place for. A separate `<par>.rsi.parquet` keyed by the same chunk key:

- leaves the par and stage ② unchanged;
- can be written later, by whichever process reads the message first;
- can be dropped or rebuilt without touching the pars.

The v5 rule's cheaper path (value match, ~58%) can return a wrong band
silently (1 in 148), so it should not write sidecar rows. Count-to-end should.

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
