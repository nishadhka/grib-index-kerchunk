# v3 — one reference per forecast step

Same band, variables and messages as [v2](../v2/README.md): 40°N → 40°S;
`2t tp msl t850 u700`; 8 members at +24 h, member 1 at 0/6/72/144/240/360 h,
and the 06z/18z runs, all from 20261020. Now **79 messages per grid**,
because member 1 at +24 h is no longer the reference and is tested too. `tp`
at 0 h is a constant field and was never collected.

**The one change from v2.** v2 used a single +24 h reference for every
message. v3 uses a reference **at the test message's own step**, taken from
`REF = date:run:member`, so it is read in full once per (variable, step).
This is how GIK templates are already organised: one per (member, step).
Prediction, bit-sync and the 12-RSI joint choice are unchanged from v2.

Two reference sources were tried:

| Run | Reference | What it stands for |
|---|---|---|
| **v3a** | `20261020:same:2`: member 2 (not tested), same date, same run | a template from the same date |
| **v3b** | `20261021:00:1`: member 1, **next date, different run** (00z vs 12z/06z/18z) | the realistic case: a template from another date |

Run from `lat-offset-test/`, after `collect_offsets.py`:

```bash
uv run v3/sync_band3.py 0p25 40,-40 . 20261020:same:2               # v3a
uv run v3/sync_band3.py 0p25 40,-40 . 20261021:00:1                 # v3b
GIK_CHUNK_SOURCE=gcs uv run v3/sync_band3.py 0p25 40,-40 . 20261020:same:2 out/<earlier>.txt   # resume, via GCS
uv run breakdown.py out/v3a_sync_0p25_s3_partial.txt out/v3a_sync_0p25_resume_gcs.txt          # tally
```

## Result

"OK" means the exact bit **and** the right RSI number, both checked against
the true offsets.

| Run | Grid | OK | Wrong | Members | Runs | Steps | Fetched | Prediction error (median / max) |
|---|---|---|---|---|---|---|---|---|
| v2 (for comparison) | 0.25° | 63 / 74 | 11 | 35/35 | 10/10 | 18/29 | mean 63.0% | — |
| **v3a** | 0.25° | **75 / 79** | 4 | 40/40 | 10/10 | 25/29 | 55.9–72.5% (mean 63.1%) | 0.4 / 7.2 KB |
| **v3b** | 0.25° | **73 / 79** | 6 | 40/40 | 10/10 | 23/29 | 54.6–71.7% (mean 62.5%) | 1.5 / 4.6 KB |
| v2 (for comparison) | 0.1° | 52 / 74 | 22 | 35/35 | 10/10 | 7/29 | mean 63.3% | — |
| **v3a** | 0.1° | **73 / 79** | 6 | 40/40 | 10/10 | 23/29 | 55.0–74.3% (mean 63.4%) | 1.9 / 39.9 KB |
| v3b | 0.1° | *not run*: 0.1° 20261021 00z was not yet published on S3 or GCS | | | | | | |

By variable:

| Run | `2t` | `tp` | `t` | `msl` | `u` |
|---|---|---|---|---|---|
| v3a 0.25° | 16/16 | 15/15 | 16/16 | 14/16 | 14/16 |
| v3b 0.25° | 16/16 | 14/15 | 15/16 | 14/16 | 14/16 |
| v3a 0.1° | 16/16 | 15/15 | 15/16 | 14/16 | 13/16 |

Search time was a mean of 11–16 s per message (max 69–125 s) in Python +
ctypes. A per-step reference cut the median prediction error from KB-scale
to ~0.4–1.9 KB.

Raw output, in `../out/`:

- `v3a_sync_0p25_s3_partial.txt` and `v3a_sync_0p25_resume_gcs.txt`: one run
  in two parts (see below);
- `v3a_sync_0p1.txt`;
- `v3b_sync_0p25.txt`.

## What the failures are

- **Every member and every run is exact** in all three runs: 150 of 150.
  That includes a reference from another date and run (v3b).
- **All 16 failures are member 1 at long leads**, mostly 240/360 h and
  once 144 h, for `msl`, `t` and `u`. The one exception is v3b `tp` at 6 h.
  In 15 of the 16 the **bit was exact** and only the RSI number was wrong;
  v3b `tp` at 6 h was 1,980 bits off, among 17 candidates.
- **Why.** At long leads the reference member (or date) no longer looks like
  the test member, because ensemble spread grows with lead time. The match
  score rises to 0.4–0.8, against 0.03–0.2 at +24 h, and the value match
  then picks an RSI far from the prediction. In several failures the
  prediction itself was within one RSI:
  - v3a 0.1° `u` 240 h: prediction error 0.2 KB, yet it picked RSI 316
    instead of 374.
  - v3b 0.25° `msl` 360 h: error 0.7 KB, picked 77 instead of 61.

  v3 searches every RSI number the 5% margin allows and ignores how close
  each one is to the prediction.

## Next step (v4, not run yet)

- **Weight the choice by distance from the predicted RSI.** With a per-step
  reference, the band start is predicted to a median of 0.4–1.9 KB. A typical
  RSI is ~2 KB at 0.25° and ~1.5 KB at 0.1°, so the predicted RSI number is
  usually right or one off. Combine the value score with a penalty on
  |k − k_pred|, or search only k_pred ± a few RSIs. The margin can then also
  be cut from 5% to ~1–2%, which reduces the bytes fetched.
- Run v3b at 0.1° once 20261021 00z is published.

## Throttling, and the GCS mirror

The v3a 0.25° run over S3 stopped after 63 of 79 messages: `RuntimeError: S3
kept throttling` after 10 retries with backoff. v3 now records a failed fetch
as `s3fail` and keeps going, and takes an earlier output file as a 5th
argument to skip messages already done.

The remaining 16 were resumed through **`gs://ecmwf-open-data`**
(`GIK_CHUNK_SOURCE=gcs` in `../lib/aecx.py`; the default is still S3). That
bucket mirrors `s3://ecmwf-forecasts` key for key, and it carries `ifs/0p1/`
too:

- file sizes are identical;
- the `2t` message bytes (20261020 12z +24 h) have the same md5 on both;
- for 0.25°, the same was already measured in
  `daily-route-crma/.../GCS_AS_CHUNK_SOURCE.md`.

So `.index` offsets carry over unchanged. The 16 messages ran with no
throttling at ~8 s each. GCS starts at 2023-07-12.
