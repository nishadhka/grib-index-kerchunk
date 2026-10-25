# Wide box at 0.1°: lat 50°N → 40°S, lon 30°W → 130°E

This is the pre-check before a realization of the new region (2026-10-25). It answers three questions:

1. Can a worker download less than the whole GRIB message for this box?
2. What would one date cost on the dask/frisky cluster and in storage?
3. Where would the result go?

Nothing has been realized or uploaded for this box yet.

## The box

| | `ea-sw-io40` (realized 2026-10-23) | **wide box** |
|---|---|---|
| latitude | 40°N → 40°S | **50°N → 40°S** |
| longitude | 15°E → 80°E | **30°W → 130°E** |
| 0.1° grid points | 801 × 651 = 521,451 | **901 × 1601 = 1,442,501** (2.77×) |
| rows of the 1801-row grid | 500 … 1300 | **400 … 1300** |
| columns (grid starts at −180°) | 1950 … 2600 | **1500 … 3100** |

The box does not cross the antimeridian, so it is one contiguous column slice.

## 1. Latitude subset before download (`measure_prefix.py`)

ECMWF packs every field with CCSDS (libaec, RSIs of 4,096 samples). It scans north to south, one full 360° row at a time (`../README.md` §8). That allows two partial reads:

- **Option C, the prefix.** One HTTP Range GET reads from the message start to the end of the RSI that holds row 40°S. The decode starts from bit 0 and needs no stored offsets and no search (`../lat-offset-test/c_prefix/`).
- **The exact band.** Read only from the RSI that holds 50°N to that same end. This needs the band start's RSI bit offset for every message, either stored or found by the Rust search (`../lat-offset-test/rust/`).

**What was measured:** 20261021 00z, 0.1°. All **46 realized channels** (CRMA's 39 plus the 7 cGAN fields), for members 0 (control, `oper`), 1, 25 and 50, at steps 3, 24, 120 and 240 h. That is **736 messages** and 1.73 GB.

**How each was checked:** every message was read in full once. Then exactly the prefix bytes were decoded, truncated, the way a worker would decode them.

| Read per message | Bytes (all 736) | Share of full | **Saving** |
|---|---|---|---|
| whole message (today; gribberish codec) | 1.727 GB | 100% | — |
| **option C prefix, 90°N → 40°S** | 1.351 GB | **78.2%** (68.3–97.2 per message) | **21.8%** |
| exact band, 50°N → 40°S | 1.046 GB | 60.5% | **39.5%** |

- **Correct everywhere.** The box decoded from the truncated prefix matched eccodes **736/736** and gribberish 1.6.0 (the workers' decoder) **736/736**, bit for bit.
- **Stable.** The prefix share is 78.1–78.4% at every step and 78.0–78.5% for every member, control included.
- **By channel** (prefix share of bytes):

  | Prefix share | Channels |
  |---|---|
  | 70–72% | `gh500` `msl` `tciw` |
  | 74–79% | most winds, `t`, `vo`, `d`, `r` |
  | 80–82% | `t2m` `skt` `d2m` `tp` |
  | 84–86% | `q*` `cp` `tcw` `tcwv` |
  | 88–97% | `tcrw` `cape` `lsm` `ro` |

  Fields that are mostly constant at the poles (`ro`, `lsm`, `cape`) pack almost all their bytes into the band.

**Verdict on the 25% target:**

- **Option C does not reach it at 0.1°: it saves 21.8%.**
  - The cost is set only by the south edge, 40°S. At 0.25° the same south edge gave 24.5% (`../lat-offset-test/c_prefix/`).
  - At 0.1° the tropical rows hold an even larger share of the bytes.
  - Moving the north edge from 40°N to 50°N changes nothing, because a prefix always starts at 90°N.
- **Only the exact band clears it (39.5%).** But it needs the bit offset of the RSI at 50°N in each message:
  - **Stored offsets:** these come only from a full read of that same message. They help re-reads, not the realization's first read.
  - **The Rust search** (`../lat-offset-test/rust/`, 30 ms – 2 s per message) finds a true RSI start inside a byte window. Naming *which* RSI it is relies on matching values against a template. In the tests that failed silently on 6 of 79 0.1° long-lead messages (v3a), and `tp`-like zero-run fields need the slow v5 path.
  - For a production store, a silent one-RSI shift (~1 row, misplaced) is not acceptable.
- **The decoder gap:** gribberish 1.6.0 cannot decode a truncated message. It raises `Data section not found when reading message data`. So option C in the workers needs:
  - either the libaec prefix decode inside `read_message` (libaec ≥ 1.1 ships in the imagecodecs wheel; ~88 ms per 0.1° message here);
  - or gribberish Issue 2 (`../gribberish-issue-drafts.md`) implemented.

```bash
uv run measure_prefix.py --date 20261021 --run 00      # → logs/measure_prefix_20261021_00z.{txt,json}
```

## 2. Cost of one date on dask/frisky (estimate, scaled from the 2026-10-23 trial)

The basis is the measured 20261021 00z run at 0.1°:

- the 39 channels took 113.2 min, and the 7-channel cGAN append took 18.3 min;
- 20 readers (5 VMs; `dask-worker-03` was down);
- 73.4 GB on Ceph for 46 channels;
- mirror at ~126 MB/s.

| | `ea-sw-io40`, measured | **wide box, estimate** |
|---|---|---|
| messages per date (46 ch × 51 × 65 steps) | 152,490 | 152,490 (the box does not change the message count) |
| GRIB bytes read | ~358 GB (2.35 MB/message) | ~358 GB whole messages, **~280 GB with option C** |
| block (1 step × 51 members × box), raw float32 | 106 MB | **294 MB** (per task, in worker memory) |
| stage ③ wall, 46 channels, 20 readers | **131.5 min** | **~2.0–2.5 h** **[est]** |
| stored on Ceph | **73.4 GB** | **~203 GB** **[est]** (2.77×, same compression ratio 0.23) |
| mirror to source.coop | 8.6 + 1.7 min | **~27 min** at 126 MB/s **[est]** |
| one year of 00z dates | ~27 TB | **~74 TB** **[est]** |

How the wall-time estimate was reached:

- **Reads.** A reader managed ~0.95 messages/s at 0.1°, bound by fetch and decode. Option C cuts the fetch by 22% and the decode to the 72% of rows kept. That alone would put the run at ~1.8 h.
- **Writes.** They grow 2.77× (~71 MB per block compressed instead of 26 MB), which pushes it back up. The 294 MB block also needs checking against worker memory before the run.
- **Measure, don't scale.** A short run (2 channels × 51 members × 65 steps) on the cluster would replace these scalings with measured numbers.

## 3. Where it would be stored (proposed, nothing written yet)

| | Path |
|---|---|
| Ceph sink (EWC) | `must-icechunk/wide-30w130e-50n40s/v0p1-20261021-00z` |
| source.coop | `s3://e4drr-project/forecasts/ecmwf-ifs-0p1-icechunk-realized-30w130e-50n40s` → https://source.coop/e4drr-project/forecasts/ecmwf-ifs-0p1-icechunk-realized-30w130e-50n40s |

The new paths are kept separate from `forecasts/ecmwf-ifs-0p1-icechunk-realized` (`ea-sw-io40`, 46 channels), which stays as it is. The extent is in the name, so another box never shares the path.
