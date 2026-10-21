# ECMWF IFS ENS at 0.1° (`ifs/0p1/`) — survey for GIK

ECMWF open data started publishing a **0.1° IFS stream** on
`s3://ecmwf-forecasts/` from **2026-10-20 06z**. It sits next to the
existing `ifs/0p25/` path, which is still published. This page records
what the new path contains and how it differs from the 0.25° stream the
current pipeline (`../README.md`) is built for.

Surveyed 2026-10-21 by listing 20261019 00z → 20261020 18z and reading
`.index` files plus a few byte-range GRIB messages (eccodes + gribberish).
At survey time 20261021 had nothing published yet, so the data covers
three runs: 06z, 12z and 18z of 20261020. 00z has not been seen yet.

---

## 1. Where it lives

```
s3://ecmwf-forecasts/{YYYYMMDD}/{HH}z/ifs/0p1/{stream}/{YYYYMMDDHH}0000-{H}h-{stream}-{type}-{pl|sfc}.{grib2|index}
```

| `ifs/` sub-path | 20261019 all runs | 20261020 00z | 20261020 06z onward |
|---|---|---|---|
| `0p25/` | ✓ | ✓ | ✓ |
| `0p1/`  | — | — | ✓ |

Streams under `ifs/0p1/` are the same four as `0p25/`: `enfo`, `oper`,
`waef`, `wave`. The `aifs-ens/` and `aifs-single/` sub-paths have no 0.1° stream.

**The big structural change: each step is split into two files by level type.**

| | 0.25° (current) | 0.1° (new) |
|---|---|---|
| ENS perturbed | `…-{H}h-enfo-ef.grib2` (one file) | `…-{H}h-enfo-ef-pl.grib2` + `…-{H}h-enfo-ef-sfc.grib2` |
| Control | `oper/…-{H}h-oper-fc.grib2` | `oper/…-{H}h-oper-fc-pl.grib2` + `…-oper-fc-sfc.grib2` |
| ENS products (12z, 240/360h only) | `enfo-ep.grib2` | `enfo-ep-pl.grib2` + `enfo-ep-sfc.grib2` |
| Wave | `waef-ef`, `wave-fc` | `waef-ef-sfc`, `wave-fc-sfc` (sfc only) |
| BUFR tropical-cyclone tracks (`*-tf.bufr`) | present | **absent** |

`sfc` files also carry the soil (`levtype=sol`) fields. Every `.grib2` has a
`.index` beside it, in the same JSON-lines format (`_offset`, `_length`,
`param`, `number`, `levtype`, `levelist`, `step`, …). The existing `.index`
parser works on it unchanged.

## 2. Grid and encoding

Checked with eccodes on the `2t`, `t850` and `tp` messages of member 1, 12z, +24h:

| | 0.25° | 0.1° |
|---|---|---|
| Grid | 721 × 1440 | **1801 × 3600** (6,483,600 points, 6.24× more) |
| Lat | 90 → -90 | 90 → -90 |
| Lon | 180 (=-180) → 179.75 | 180 (=-180) → **179.9** |
| Packing | `grid_ccsds` | `grid_ccsds` |
| bitsPerValue (`2t`, `t`) | 9 (12 before 20261020 06z) | 9 |
| bitsPerValue (`tp`) | 12 (16 before 20261020 06z) | 12 |

The longitude origin is the same -180 convention that `../grids.py` records,
so a `"0p1": dict(ny=1801, nx=3600, dlat=0.1, dlon=0.1)` entry there is all
the coordinate code needs.

**gribberish decodes the 0.1° messages correctly**: values match eccodes. It
returns a flat array of 6,483,600 values; reshape it to `(1801, 3600)`.
Decoding takes **~95 ms per field**, against ~16 ms for the same field at 0.25°.

## 3. Variables

The 0.1° files and the post-cutover 0.25° file carry **the same variable set**
and the same total message count per step (8,850 for 50 members).

| File | levtype | Params | Levels | Fields per member |
|---|---|---|---|---|
| `enfo-ef-pl` | pl | 9: `d gh q r t u v vo w` | 14: 10 50 100 150 200 250 300 400 500 600 700 850 925 1000 | 126 |
| `enfo-ef-sfc` | sfc | 43 (list below) | — | 43 |
| `enfo-ef-sfc` | sol | 2: `sot vsw` | 4: 1 2 3 4 | 8 |
| `oper-fc-pl` | pl | 10: the 9 above + `z` | 14 | 140 (control, `number=0`) |
| `oper-fc-sfc` | sfc + sol | same 43 + 2 | — | 51 |
| `waef-ef-sfc` / `wave-fc-sfc` | sfc | 13: `cdww h1012 h1214 h1417 h1721 h2125 h2530 mp2 mwd mwp pp1d swh wmb` | — | 13 |
| `enfo-ep-pl` | pl | `gh t ws`, `ptsa_{gt,lt}_{1,1p5,2}stdev` (types `em/es/ep`) | 250 300 500 850 1000 | — |
| `enfo-ep-sfc` | sfc | `msl`, `10fgg{10,15,25}`, `tpg{1,5,10,20,25,50,100}` | — | — |

The 43 surface params:
`100u 100v 10fg 10u 10v 2d 2t asn cp ewss hcc lcc lsm mcc mn2t3 msl mucape
mx2t3 nsss ptype ro rsn sd sf sithick skt sp ssr ssrd str strd sve svn tcc
tciw tclw tcrw tcw tcwv tp tprate ttr zos`.

**7 of them are new** compared with the 50r1 0.25° stream up to 20261020 00z,
which had 36: `cp hcc lcc mcc tciw tclw tcrw`. The pl set (9 params × 14
levels) and the sol set did not change.

Members follow the 50r1 dual-stream layout: `enfo-ef-*` holds `type=pf`
members 1–50 only, and the control (`number=0`, `type=fc`) is in `oper/`.

The 12 cGAN variables (`tp 2t sp ssr ssrd tcw tcwv tcc sf ro` + `u/v 700`)
are all present. Ten are in `-sfc` and `u700`/`v700` are in `-pl`, so every
step has to read **both** files.

At step 0, constant fields such as `10fg` are written as ~227-byte messages,
just as in 0.25°.

## 4. Step axis

The step axis is the same as at 0.25°: **85 steps** for 00z/12z (0–144 h at
3 h, then 150–360 h at 6 h) and **49 steps** for 06z/18z (0–144 h). Checked on
all four streams for 06z, 12z and 18z. `enfo-ep-*` exists only at 240 h and 360 h on 12z.

## 5. Sizes

### Single file, one step

| File | 0.25° (20261019, old packing) | 0.25° (20261020 06z+) | **0.1°** |
|---|---|---|---|
| `enfo-ef` all levtypes | 6.17 – 6.77 GB | 4.13 – 4.48 GB | — |
| `enfo-ef-pl` | — | — | **14.8 – 15.8 GB** |
| `enfo-ef-sfc` | — | — | **2.9 – 4.8 GB** (step 0 is smallest) |
| `oper-fc` (control) | 135 – 149 MB | 96 – 103 MB | pl 350 – 387 MB + sfc 65 – 97 MB |
| `waef-ef` | 534 – 548 MB | 263 – 274 MB | 1.30 – 1.35 GB |
| `wave-fc` | 11 MB | 5–6 MB | 26 – 27 MB |
| `.index` (enfo) | 2.0 MB | 2.1 MB | pl 1.5 MB + sfc 0.48–0.58 MB |

### One GRIB message, which is what GIK actually fetches

| Field (member 1, +24 h) | 0.25° old | 0.25° new | 0.1° |
|---|---|---|---|
| `tp` | 822 KB | 399 KB | **1.86 MB** |
| `2t` | 663 KB | 325 KB | **1.40 MB** |
| `t` 850 | 601 KB | 269 KB | **1.21 MB** |
| median, pl file | — | — | 2.43 MB (max 5.2 MB) |
| median, sfc file | — | — | 1.59 MB (max 4.5 MB) |

### Whole run (all steps)

| Stream | 0.25° 12z (20261019) | 0.25° 12z (20261020) | **0.1° 12z** | **0.1° 06z / 18z** |
|---|---|---|---|---|
| `enfo` (ef + ep) | 570 GB | 375 GB | **1,701 GB** (pl 1,299 + sfc 398 + ep 4) | 945 / 970 GB |
| `oper` | 12.4 GB | 8.5 GB | 39.3 GB | 22.2 / 22.7 GB |
| `waef` | 46 GB | 22.8 GB | 112.5 GB | 65 GB |
| `wave` | 0.9 GB | 0.5 GB | 2.2 GB | 1.3 GB |

So one 0.1° 00z/12z ENS run is **~1.7 TB**. That is about 3× the old 0.25°
run and 4.5× the new one. Of it, 76% is pressure-level data.

### Publication latency

The 0.1° `enfo` files appear about **2 h after** the 0.25° ones. For 06z,
0.25° `enfo` was written around 13:04 UTC and 0.1° around 15:04 UTC. For 12z
it was 19:40 against 21:40–22:02 UTC. Daily jobs that need 0.1° data must be
scheduled after this.

---

## 6. The 0.25° stream changed at the same cycle

The same cycle that introduced 0.1° also changed 0.25°, starting at
**20261020 06z**:

- `bitsPerValue` went down (`2t`/`t` 12 → 9, `tp` 16 → 12), which is why
  `enfo-ef` shrank from ~6.5 GB to ~4.3 GB per step.
- 7 surface params were added (`cp hcc lcc mcc tciw tclw tcrw`).
- Path, file name, grid, members, levels and step axis are unchanged.

For GIK this works like the 2025-01-14 level change. The 50r1 template built
from `20260513` will still map the 36 old params correctly on new dates. The
packing is read from each message, so decoding needs nothing new. But the 7
new params are **orphaned** until a template is built from a date on or after
20261020 06z. That makes it a fifth ECMWF schema era for the 0.25° table in
`CLAUDE.md`. I did not check whether this is also an IFS cycle change.

---

## 7. What has to change to add 0.1° to the current GIK pipeline

| Area | Current (0.25°) | Needed for 0.1° |
|---|---|---|
| URL builder (`ecmwf_util.py:129`, `ecmwf_index_processor.py:221,553`, `ecmwf_index_preprocessing.py:66`, Lithops inline) | one `…-enfo-ef.{grib2,index}` per step | **two** per step, `-pl` and `-sfc`, chosen by `levtype` (`sol` is in `sfc`). That is 2 `.index` reads per step: 170 per 00z/12z run instead of 85 |
| Reference values | every chunk points to the same URL for a given step | the URL depends on the variable. Kerchunk refs are per-chunk, so one parquet per member still works |
| Template (Stage 1) | `20260513` (50r1, 0.25°) | new template from a 0.1° date (≥ `20261021`, preferably a 00z/12z run so it has all 85 steps) |
| Grid (`grids.py`) | `0p25`, `0p4` | add `0p1`: 1801 × 3600, lon -180 → 179.9 |
| `ECMWF_RESOLUTION` env (Lithops) | `0p25` \| `0p4` | add `0p1` |
| Control | `oper/…-oper-fc.grib2` | `oper/…-oper-fc-{pl,sfc}.grib2` |
| Streaming (Stage 4) | ~16 ms decode, ~0.3–0.7 MB per chunk | ~95 ms decode, ~1.2–2.5 MB per chunk. That is ~6× the CPU and ~4.5× the bytes. A regional subset still has to fetch and decode the whole global field, because each GRIB message is one chunk |
| Parquet size | ~140 KB per member | about the same, since the number of refs per member is unchanged (8,850 messages per step / 50) |

Rough cost of the cGAN stream (12 vars × 9 steps × 51 members ≈ 5,500
messages): ~2 MB × 5,500 ≈ **11 GB fetched per date**, against ~2.5 GB at the
new 0.25° packing. Decode is ~9 CPU-minutes, against ~1.5.

---

## 8. Fetching only a latitude band (subsetting before download)

Today GIK, like Herbie, fetches the **whole GRIB message**, which is one
global field. It cuts out East Africa only after decoding. The `.index`
gives byte ranges for whole messages, never for part of one. For a long time
we assumed nothing better was possible, because a packed field is one
compressed bitstream.

**For these files that assumption does not hold.** ECMWF packs every field as
`grid_ccsds`, which is CCSDS 121.0 / libaec. On every message checked
(0.1° and 0.25°, before and after the 2026-10-20 change) the settings are
`ccsdsFlags=14`, `ccsdsBlockSize=32` and `ccsdsRsi=128`:

- The bitstream is a sequence of **RSIs** (reference sample intervals) of
  32 × 128 = **4,096 values** each. With preprocessing on (flag bit 8), each
  RSI starts from its own reference sample, so **any RSI can be decoded on
  its own**. You only need to know the bit position where it starts.
- Values are stored in scan order: north to south, a full 360° row at a time.
  So **a latitude band is one contiguous range of RSIs**, which means one
  contiguous byte range inside the message.
- GRIB does not record where each RSI starts. Padding is off (flag 32 is not
  set), so RSIs are not byte-aligned, and the coded lengths depend on the data.
  The offsets are different for every message and every date. To find them you
  have to decode the message **once**. libaec ≥ 1.1 does this as a side effect
  (`aec_decode_enable_offsets` / `aec_decode_get_offsets`), and
  `aec_decode_range` then decodes starting from a bit offset.

`rsi_band_subset_poc.py` (in this folder) indexes a message, then fetches
**only the band's bytes** from S3 with an HTTP Range request, decodes them and
compares the result with eccodes' decode of the full field. For
20261020 12z +24h, member 1, band 24°N → 12°S (the ICPAC domain's latitudes):

| Field | Grid | Full message | Band fetched | Share | max \|band − eccodes\| |
|---|---|---|---|---|---|
| `2t` | 0.1° | 1.400 MB | 0.349 MB | 24.9% | **0** |
| `t` 850 | 0.1° | 1.210 MB | 0.290 MB | 24.0% | **0** |
| `tp` | 0.1° | 1.864 MB | 0.653 MB | 35.0% | **0** |
| `2t` | 0.25° | 0.325 MB | 0.080 MB | 24.5% | **0** |
| `t` 850 | 0.25° | 0.269 MB | 0.063 MB | 23.3% | **0** |
| `tp` | 0.25° | 0.399 MB | 0.131 MB | 32.8% | **0** |

The band is 20% of the rows. It costs 24–35% of the bytes because tropical
fields compress worse than polar ones, and `tp` most of all.

For the realized `ea-sw-io40` domain (lat 40 → −40, lon 15 → 80) the band is
**51.9–62.5%** of the bytes at both grids (all bit-exact). The box itself is
only 8.1% of the grid points, so a latitude band still fetches ~7× more than
the box needs. The rest is the longitudes outside 15–80°E.

**This is not a copy of the data.** There are three ways to read a box, and
every one of them needs one full read of each message. They differ in what is
kept afterwards:

| | What is kept | Every later read of the box costs |
|---|---|---|
| Virtual GIK today | `[url, offset, length]` | the full global message |
| Virtual + RSI band refs | the same, plus ~40 B per message | the latitude band (~25–60%), all longitudes |
| Realized (EWC `ea-sw-io40`, dynamical.org) | a decoded, re-chunked copy | the box only, in any chunk shape |

Building the band refs costs nothing extra if it rides on a decode that
already happens. Stage ③ of `../ewc-realization/` already decodes every global
message once. Capturing the RSI offsets there (with libaec, or gribberish if
it exposes them) yields band references for the whole latitude belt, which
covers West Africa or South Asia as well, at no extra reads.

A **longitude cut is not practical without realizing.** Inside an RSI the
decode is sequential: the predictor carries the previous sample, and block
lengths vary. Cutting longitudes would need a checkpoint index every 32
values, plus one HTTP range per row: 321 GETs per message at 0.25° instead of
1, each a few hundred bytes. S3 has no multi-range GET, and this archive
already throttles on request rate (`../ewc-realization/05_BOTTLENECKS_AND_ISSUES.md`).

**Fitting band refs into Icechunk.** A virtual Icechunk chunk can only be
`[url, offset, length]`, and Zarr codecs are configured per array, not per
chunk. A band chunk would also need its bit shift (0–7) and its unpacking
constants. Latitude slabs that start exactly on an RSI boundary exist:
lcm(1440, 4096) / 1440 = **128 rows** at 0.25° and lcm(3600, 4096) / 3600 =
**256 rows** at 0.1°. Even so, they need a custom reader or an extension to
the chunk manifest. The parquet + gribberish reader path is the easier place
to add this.

**Limits:**

- **Latitude only.** An RSI holds 4,096 values, which is more than one row
  (3,600 values at 0.1°, 1,440 at 0.25°). Every row covers the full 360°, so
  a longitude cut still has to decode whole rows. The bytes fetched depend on
  the latitude band alone.
- **Exact offsets need a full read of the message.** If they are built that
  way, a single consumer reading each field once gains nothing, because the
  index costs a full read. The gain comes when the offsets are built **once,
  close to the data**, for example by an AWS `us-east-1` job, where reads are
  fastest. Reads from this open-data bucket cost the reader nothing from
  anywhere, so the real cost of the full read is time and throttling
  headroom, not money. The offsets are then published GIK-style for every
  downstream read (ICPAC on GCP, cGAN runs, reprocessing, other users). Each
  later read then costs ~25–35% of the message. *Finding them without a full
  read is tested below.*
- **Size of the index.** All RSI offsets take 1,583 × 4 B = 6.2 KiB per 0.1°
  message (1.0 KiB at 0.25°). For one fixed region you only need the band's
  start and end, so the GIK reference becomes
  `[url, byte_offset, byte_length, rsi_bit_start, first_sample]` plus the
  Section 5 unpacking constants (R, E, D): about 40 bytes per message.
- **Decoder.** gribberish cannot decode a partial message. This needs a small
  libaec-based codec (≈ the PoC's step 3, `aec_decode_range` per RSI) that
  the zarr/kerchunk reader calls for a "band" reference.

### Finding the band offsets without a full read ([`lat-offset-test/`](lat-offset-test/README.md))

This tests the GIK-style alternative: read **one reference message per
variable in full**, once, when the template is built. For every other
message use only:

- its `.index` record;
- a ~250 B header GET for the unpacking constants;
- one GET of the predicted band ± a margin.

The exact RSI start is then recovered from that window. Results for 40°N →
40°S, five variables (`2t tp msl t850 u700`) and 74 messages per grid
(20261020; members, steps 0–360 h, and the 06z/18z runs), checked against true
offsets from full reads:

1. **Prediction works.** Take the reference's offset profile as a fraction of
   its length and scale it by each message's `.index` length. That predicts
   the band start to within ~0.5–1% of the message (3–4% for `tp`).
2. **Exact sync works: 147 of 148 messages.** A true RSI start decodes
   smoothly and forms a self-consistent RSI chain. A slightly wrong start
   falls back into step within one RSI, so hopping to the next reported start
   lands exactly on a true one.
3. **Naming the RSI fails across forecast steps.** v2 picks the RSI number by
   aligning a 12-RSI decoded strip with the reference field:

   | Message differs from the reference in… | 0.25° | 0.1° |
   |---|---|---|
   | member (+24 h) | **35 / 35** | **35 / 35** |
   | run (06z/18z, +24 h) | **10 / 10** | **10 / 10** |
   | forecast step (0, 6, 72, 144, 240, 360 h vs +24 h) | 18 / 29 | 7 / 29 |
   | **total** | **63 / 74** | **52 / 74** |

   At its own step it is exact every time (90 / 90). The next test (v3) is one
   reference per forecast step, which is what GIK templates already provide:
   one per (member, step).
4. **Cost:** ~56–73% of the message is fetched (band + margin, against 52–62%
   for the exact band). The bit search takes ~8 s per message in
   Python + ctypes, which is a PoC figure, not a floor.

This replaces the full read with about 60% of it, which matters most for
consumers that read a field only once. It is still latitude-only.

Herbie (and wgrib2 `-small_grib`) have the same limit as GIK today. They
download the full message by `.idx` byte range and subset after decoding.
Server-side area cropping exists only in ECMWF's own services (MARS /
Polytope), not for the S3 open data.

---

## 9. Reproduce

```bash
A="uvx --from awscli aws s3 ls --no-sign-request"
$A s3://ecmwf-forecasts/20261020/06z/ifs/              # 0p1/ and 0p25/
$A --recursive s3://ecmwf-forecasts/20261020/12z/ifs/0p1/enfo/ | head
uvx --from awscli aws s3 cp --no-sign-request \
  s3://ecmwf-forecasts/20261020/12z/ifs/0p1/enfo/20261020120000-24h-enfo-ef-sfc.index .
```

Decode one message: byte-range GET `_offset` to `_offset+_length-1` from
`https://ecmwf-forecasts.s3.amazonaws.com/<key>`, then
`gribberish.parse_grib_array(buf, 0).reshape(1801, 3600)`.
