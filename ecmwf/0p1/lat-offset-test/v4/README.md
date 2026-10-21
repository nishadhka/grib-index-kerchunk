# v4 — weighting the choice by distance from the predicted RSI

Same band, variables, messages and per-step references as
[v3](../v3/README.md): 40°N → 40°S, 79 messages per grid. Prediction,
bit-sync and the 12-RSI value match are unchanged. The only change is how the
(start, RSI number) pair is chosen.

**Rule, fixed before the run** (`sync_band4.py`):

```
cost = score / best_score_of_message + ((pred_bit(k) - start_bit) / σ)²,   σ = 1% of the message
```

`score` is v3's value-match score, so v3's rule is the first term alone.

Every scored (start, k) pair was also saved, so other weightings could be
compared **offline** without fetching again (`rescore_v4.py`). Rules picked
from that sweep were tuned on these same messages, so they read as
optimistic.

| Run | Grid | Reference at the message's own step |
|---|---|---|
| **v4b** | 0.25° | `20261021:00:1`: next date, other run (the template case, as in v3b) |
| **v4a** | 0.1° | `20261020:same:2`: same date, member 2 (as in v3a). The 0.1° 20261021 00z run was still unpublished |

Both read through the GCS mirror (`GIK_CHUNK_SOURCE=gcs`) with no throttling.
Search time was a mean of 4.9 s (0.25°) and 7.9 s (0.1°) per message.

```bash
GIK_CHUNK_SOURCE=gcs uv run v4/sync_band4.py 0p25 40,-40 . 20261021:00:1   # writes out/v4_cands_*.pkl
uv run v4/rescore_v4.py out/v4_cands_0p25_20261021-00-1.pkl out/v4b_sync_0p25.txt
uv run v4/tail_cost.py .
```

## Result

**The fixed rule did not help.**

| Grid | v3 rule (value only) | **v4 rule (fixed before the run)** |
|---|---|---|
| 0.25° (v4b) | 73 / 79 | **73 / 79** |
| 0.1° (v4a) | 73 / 79 | **72 / 79** |

The failures moved rather than shrank. At 0.25° they became near-misses
(1–4 RSIs off), including 3 `tp` messages that v3 had right. The prior pulls
toward the prediction, and the prediction is itself often an RSI or two off.

**The offline sweep** (`../out/v4b_rescore_0p25.txt`, `../out/v4a_rescore_0p1.txt`):

| Rule | 0.25° | 0.1° |
|---|---|---|
| value only (v3) | 73 | 73 |
| nearest predicted RSI, no value match | 40 | 19 |
| best value within ±2 RSI of the prediction | 75 | 50 |
| best value within ±3 RSI of the prediction | 77 | 58 |
| combined, σ = 1%, λ = 1 (**the fixed rule**) | 73 | 72 |
| combined, σ = 1–2%, λ = 0.25 (best; *tuned here*) | **78** | **74** |

- **A weak prior helps a little at 0.25°**: 78/79 at best, the one miss being
  `tp` at 6 h. This was tuned on these 79 messages, so it is not an expected
  score on new data.
- **The prior barely helps at 0.1°** (74/79 at best). The 0.1° failures are
  long-lead `msl`, `t`, `u` and `tp`, whose prediction is off by **20–27
  RSIs** (28–40 KB). There is nothing reliable to pull toward.
- **The prediction cannot name the RSI on its own.** Nearest-predicted is
  right only 40/79 and 19/79 times. It narrows the search but does not decide it.
- **Sync stays solved**: in every message the true start was among the
  candidates, and every failure picked an exact start but the wrong number.

## The fetch margin can shrink

From the prediction errors at the band start:

| Margin | Band start inside the window (0.25° / 0.1°) | Fetched (0.25° / 0.1°) |
|---|---|---|
| 5% + 2 KiB (v2–v4) | 100% / 100% | 62.5% / 63.4% |
| 2% + 2 KiB | 100% / 100% | ~56.5% / ~57.4% |
| **1.5% + 2 KiB** | **100% / 100%** | **~55.5% / ~56.4%** |
| 1% + 2 KiB | 100% / 96.2% | ~54.5% / ~55.4% |

With 1.5%, the cost approaches the exact band, which is 45–62% depending on
the variable.

## The exact alternative: count RSIs to the end

The last RSI ends exactly at the end of the message, and the number of RSIs
is fixed by the grid (254 at 0.25°, 1,583 at 0.1°). So decode from the synced
start to the end of the message, count the RSIs, and the RSI number follows
**exactly**, with no reference values and no matching.

The cost is fetching the part south of the band as well
(`../out/v4_tail_cost.txt`, from the ground-truth offsets):

| Band | Band alone | Band start → end of message |
|---|---|---|
| 40°N → 40°S | 45–62% | **72–80%** |
| 24°N → 12°S | 19–34% | **60–70%** |

That suggests a hybrid, **v5 (not run)**:

- use the value match (with a weak prior and a 1.5% margin, ~56%) when its
  score is confident;
- fall back to counting to the end (~72–80%) when it is not.

The score separates the two cases well, though not perfectly:

| Grid | Right choices | Wrong choices | Right choices at or above the lowest wrong score |
|---|---|---|---|
| 0.25° (v4b) | 0.09–0.79 | 0.52–1.03 | 4 / 73 |
| 0.1° (v4a) | 0.02–0.62 | 0.47–0.90 | 1 / 72 |

A threshold just below the lowest wrong score (~0.45) would send every
failure, and only 5 of 145 right choices, to the exact fallback. The
threshold was read off these same runs, so it needs checking on other dates.

Both paths are still well below the 100% of a full read, and neither needs
anything beyond `.index`, the per-step template and range GETs.
