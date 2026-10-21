# v1 — predict, fetch a window, bit-sync, pick by smoothness

**Question.** Can a latitude band be read without first reading the whole
message, using only the `.index` and one reference message per variable?

**Method** (`sync_band.py`)

1. **Template time (once per variable):** read one reference message in full
   (member 1, +24 h, 20261020 12z). Keep its RSI offset profile as a fraction
   of its packed length, plus its decoded field.
2. **Per message:**
   - Take the length from `.index`.
   - Predict the band's start and end as reference fraction × this message's length.
   - Make one header GET (~250 B) for the unpacking constants, then one GET of
     the predicted band ± a margin of 5% of the message + 2 KiB.
3. **Bit-sync:**
   - Scan the window bit by bit. A *candidate* is a position where 2 blocks,
     then 3 whole RSIs, decode with mean |adjacent difference| < 2% of the
     code range.
   - Hop to the second RSI start that the candidate reports, and keep it only
     if decoding from there reports the same third start.
   - Collect every candidate within one RSI length of the first and keep the
     smoothest.
4. **Which RSI:**
   - Take the RSI nearest the predicted position, ±6.
   - Decode 3 RSIs and pick the number whose values best match the reference
     field there.
5. **Check:** compare the chosen bit with the true RSI offsets collected
   by `../collect_offsets.py` (full reads, used only for checking).

Band 40°N → 40°S (the `ea-sw-io40` latitudes). Five variables (`2t`, `tp`,
`msl`, `t850`, `u700`). For each: 8 members at +24 h, member 1 at 6 other
steps (0–360 h), and the 06z and 18z runs. That is 15 messages per variable
(14 for `tp`, whose 0 h field is constant), all from 20261020.

Run: `uv run v1/sync_band.py 0p25 40,-40 .` from `lat-offset-test/`.
`collect_offsets.py` must have been run first.

## Result

| Grid | Messages | OK (exact bit, right RSI) | Wrong | No sync | Time per message |
|---|---|---|---|---|---|
| 0.25° | 74 (complete) | **54** | 18 | 2 | mean 13.7 s, max 70 s |
| 0.1° | 37 of 74 (stopped) | **24** | 13 | 0 | mean 12.6 s |

Per variable at 0.25°: `2t` 13/15 (2 no-sync), `t` 13/15, `msl` 10/15,
`u` 10/15, `tp` 8/14. At 0.1°, `tp` failed 9 of 14.

The fetched window was 52–64% of the message, which is the band plus the margin.

Raw output: `../out/v1_sync_0p25.txt`, `../out/v1_sync_0p1_partial.txt`.

## What went wrong

The prediction was never the problem: the window always contained the band.

**First, a bug in this script, found during v2.** `try_decode` adds the
bit shift (`bit % 8`) to the offsets libaec reports. In fact libaec counts
from the seek byte and, after a non-zero shift, reports **8 bits short**.
That was measured on 25 true starts: +8 at every shift 1–7, +0 at shift 0.

So every hop from a non-byte-aligned candidate landed 1–14 bits off and
failed the chain check. Only starts that happened to be byte-aligned could
pass. The bug is left in `sync_band.py` so that the numbers above can be
reproduced; v2 fixes it. It explains the 2 no-syncs: the true start of
`2t` m29 is at shift 1. It probably also explains much of failure type 2
below.

The two failure types as observed:

1. **Exact bit found, wrong RSI number.** This was 11 of 18 failures at 0.25°
   and 8 of 13 at 0.1°.
   - Sync locked onto a true RSI start, but step 4 named the wrong RSI.
   - Within ±6 the true number was often not even a choice. The prediction
     error for `tp` and for distant steps is tens of RSIs at 0.25° and up to
     ~100 at 0.1° (`../out/predict_offsets.txt`).
   - A 3-RSI strip (~8.5 rows at 0.25°, ~3.4 at 0.1°) is also too short to
     tell neighbours apart.
2. **False sync.** This was 7 of 18 at 0.25° and 5 of 13 at 0.1°.
   - Starts a few hundred to a few thousand bits early also decode
     smoothly, because the decoder falls back into step.
   - The hop fixes most of them, but a chain can stay out of step and still
     be smooth. This is worst for `tp`, which is mostly zeros: its true
     roughness is ~0.0002–0.002, so a slightly rougher wrong chain still
     passes and can win.

## Steps on the way to this version

These were run on a few messages; their output is not kept.

- First, **without the hop**, the first candidate scanning from the window
  start was accepted. Most were wrong: 60 of 74 at 0.25°. All of those sat
  3–1,500 bits *before* a true start.
- Adding the **hop** to the second reported start fixed 5 of the 6
  diagnostic cases (exact bit). This is the self-resync behaviour that
  makes the method possible at all.

Fixes for both failure types are in [`../v2/`](../v2/).
