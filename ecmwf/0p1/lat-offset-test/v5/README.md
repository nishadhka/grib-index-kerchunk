# v5: on the real GIK pars, with a confidence gate and count-to-end

v1–v4 took message locations from ECMWF's `.index` on 20261020. v5 takes
them **only from the real pars** (`gs://gik-ecmwf-aws-tf/v20260623_run_par_ecmwf/`,
read with the `coiled-data@sewaa-416306` key). The par refs are the `.index`
triplets: 656 of 656 checked match exactly (`../../lat-band-par/README.md`).

The template is the same par key (field, level, step) in a par from **another
date**, read in full once per step. Every test message costs:

- a ~1 KB header GET;
- one GET of the predicted band ± margin;
- sometimes a tail GET (count path) or a head GET (window opened late).

Band 40°N → 40°S, 0.25°, 00z (pars exist for 00z only).

## Rule, fixed before the run (from `../v4/README.md`)

```
window = predicted band ± (1.5% of the message + 2 KiB)
choice = argmin  score/best + 0.25 · (distance / 1% of the message)²
if score > 0.45: fetch on to the end of the message and count RSIs → exact RSI number
if the chosen RSI is after the band start: fetch the head and chain back to it
```

**Verification** reads every test message in full. A message counts as `OK`
only when two things hold:

- the chosen start is the true start of the chosen RSI;
- the band decoded **from the fetched bytes** equals eccodes on the full
  message, value for value.

| Run | Template | Test |
|---|---|---|
| **v5a** | `2026101800z-ens_01` | `20261019` 00z: ens_01 at 11 steps (0–360 h) + ens_08…ens_50 and control at 24 h; `2t tp msl t/850 u/700` → 94 messages |
| **v5b** | `2026101900z-ens_01` | `20261020` 00z ens_01 at 11 steps → 54 messages |

```bash
export GIK_CHUNK_SOURCE=gcs
uv run v5/sync_band5.py <template par> <test par>...            # the v5 rule
uv run v5/sync_band5.py <template par> <test par>... --gate -1  # count-to-end on every message
```

## Result

| Run | v5 rule | Fetched | Count-to-end only | Fetched |
|---|---|---|---|---|
| v5a (94) | **94 / 94** | 57.8% | **94 / 94** | 76.6% |
| v5b (54) | **53 / 54** | 58.9% | **54 / 54** | 76.3% |

By path, under the v5 rule (both runs together):

| Path | Messages | OK | Fetched |
|---|---|---|---|
| value match | 100 | 99 | 53% |
| value match + head GET | 19 | 19 | 56% |
| count to end | 26 | 26 | 77% |
| count to end + head GET | 3 | 3 | 80% |

Mean fetched by field (v5a, v5 rule): `msl` 50%, `2t` 54%, `t/850` 55%,
`u/700` 65%, `tp` 67%. The exact band alone (sidecar) is 45–54%.

- **Count-to-end was exact every time it ran:** 29 of 29 under the gate,
  and **148 of 148** when forced on every message. It needs no reference values.
  The RSI count is fixed by the grid (254 at 0.25°).
- **The one failure is silent:** `tp` at 6 h in v5b scored 0.422, under the
  0.45 gate. The value match chose RSI 70 where the true RSI was 73, so the
  band it returned is 3 RSIs (about 8 rows) too far north. `tp` at 6 h also
  failed in v3 and v4.
- **The head GET is new and needed:** with a 1.5% margin and a template from
  another date, the window opened after the band start in 22 of 148 messages.
  It is cheap: about 3–4% of the message.

## Bugs found and fixed on the way (the rule did not change)

The first full run (`../out/v5*_run1.txt`) scored 87/94 and 53/54 because of
two mechanism bugs:

1. **Control.** The control comes from `oper/fc`, whose headers are a
   different size, so its data starts at another byte. The script took that
   start from the template (a perturbed member), which put all 5 control
   messages off by a fixed number of bits. Now `d0` comes from each
   message's own header GET.
2. **The head chain** trusted the first offset after a sync. After a false
   start in a `tp` zero run, the decoder can take more than one RSI to fall
   back into step. Now the head reaches 3 RSIs before the band, and the
   chain's offset is taken **at** the band's RSI.

Before the full run, a 10-message check (in both test sets) found that
count-to-end must stop before Section 8 (`7777`). Otherwise those 4 bytes
decode as extra blocks and can add one RSI.

## What it means for the pars

- **The latitude subset works on the real pars without a full read.**
  - Count-to-end: exact (148/148), at ~77% of the bytes.
  - v5 rule: ~58% of the bytes, with 1 silent error in 148.
- **For a production default, use count-to-end** (or a stricter gate for `tp`).
  - Its errors could only come from sync, and sync never failed in v2–v5.
  - The value match saves ~20% of the bytes but can pick the wrong RSI
    without knowing it.
- **A count-to-end read also yields the exact RSI offsets** from the band
  start to the end of the message. So it can write the
  `../../lat-band-par/` sidecar row for that message (valid for any band
  starting at or south of 40°N) with no extra bytes. Every later read is then
  the exact band, ~50%.
