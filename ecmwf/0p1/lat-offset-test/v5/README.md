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

## Template age and the `tp` policy (v5c, v5d)

**Question:** how old can the template be, and does "count `tp` always,
value-match the rest" hold on a fresh date? Policy fixed before the run:
`--count tp` (other fields: v5 rule, count when the score is above 0.45).

**v5c**, test **20261017** 00z (not used before): ens_01 at 11 steps,
ens_04…ens_46 and control at 24 h, 5 fields, 94 messages.

| Template (`ens_01`, 00z) | Age | `tp` policy: OK / **silent wrong** / no sync | Fetched |
|---|---|---|---|
| 20261016 | 1 day | **93 / 0 / 1** | 61% |
| 20261010 | 7 days | 74 / **19** / 1 | 65% |
| 20260917 | 30 days | 68 / **26** / 0 | 63% |
| 20260617 | 4 months | 79 / **15** / 0 | 73% |
| 20260513 | 5 months | 77 / **17** / 0 | 69% |
| 20260513, count-only | 5 months | 93 / 1 / 0 | 77% |

- **Value match needs a fresh template.** At 7 days or more it picks a
  wrong RSI (typically 1–5 RSIs off) in 15–28% of messages. Those scores
  (0.27–0.44) sit under the gate, so nothing flags them. Failures are spread
  over `2t`, `msl` and `t/850` at every lead.
- **Count-to-end does not depend on template age.** It uses the template
  only to place the window, and it names the RSI from the message itself.
- **Two sync faults showed up**, both in `tp`, and neither depends on age:
  - 2 counts named the right RSI from a start a few bits off. This is the
    false-start effect already fixed for the head GET.
  - 1 message had no sync in its window.

**Fixes** (mechanism, not rule):

- the count path now starts the band from the chain's offsets 2+ RSIs past
  the sync point (at RSI k0, or via the head GET);
- a window with no sync falls back to a full read, which is exact and flagged
  as path `full`.

**v5d** reran both settings, on 20261017 (where the faults were seen) and on
a second fresh date, **20261013** (test members ens_01, ens_05…ens_47,
control):

| Run | Template | Result | Fetched |
|---|---|---|---|
| `tp` policy, test 20261017 | 1 day | **94 / 94** (1 via full read) | 62.6% |
| `tp` policy, test 20261013 | 1 day | **94 / 94** | 62.8% |
| count-only, test 20261017 | 5 months | **94 / 94** | 77.5% |
| count-only, test 20261013 | 5 months | **94 / 94** | 77.8% |

**376 of 376 are exact**, every band checked against eccodes on the full
message. The no-sync message (`tp` 24 h, ens_32, 20261017) is the only full
read. Its window was searched and then fetched again in full, so it cost 158%
of that message. Why it would not sync is not yet known.

### What the realization can use

| Template | Policy | Fetched per message |
|---|---|---|
| **previous run** (1 member, ~1 in 51 messages if refreshed each run) | `tp` counted, others value match + gate | **~62%** |
| any age in the era (a fixed per-era template, no refresh) | count to the end | **~77%** |

Both were exact on every test date here. Value match with an old template is
**not** safe.
