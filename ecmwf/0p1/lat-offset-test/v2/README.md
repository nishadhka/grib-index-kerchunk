# v2 — fixed offsets, joint (start, RSI number) choice

Same question, data and band as [v1](../v1/README.md): 40°N → 40°S, five
variables, 74 messages per grid from 20261020. There is one reference message
per variable (member 1, +24 h, 12z), and it is the only message read in full.

**Changes from v1** (`sync_band2.py`):

1. **Offset fix.** `try_decode` returns absolute bit positions within the
   fetched window. libaec counts from the seek byte and, after a non-zero bit
   seek, reports 8 bits short. That was measured on 25 true starts: +8 at
   shifts 1–7, +0 at shift 0. Its entry 0 is a seeded 0 rather than a
   position, so it is replaced with the start itself. In v1 this bug blocked
   every non-byte-aligned start.
2. **Joint choice.**
   - Collect every candidate start (smooth over 3 RSIs, plus the hop and chain
     check from v1) from the first one found until one RSI length later.
   - Decode a **12-RSI strip** (49,152 values: ~34 rows at 0.25°, ~14 at 0.1°)
     from each candidate.
   - Score it against the reference field at **every** RSI number the margin
     allows, which is margin / mean RSI size + 3 (tens to ~100 at 0.1°), not ±6.
   - Keep the best (start, RSI number) pair by mean |difference| /
     std(reference).

Per message the cost is unchanged: the `.index` record, one ~250 B header
GET, and one GET of the predicted band ± (5% of the message + 2 KiB).

Run from `lat-offset-test/`, after `collect_offsets.py`:
`uv run v2/sync_band2.py 0p25 40,-40 .`. An optional 4th argument such as
`"2t:5,tp:4"` runs only the first few messages of the listed variables.

## Result

"OK" means the exact bit **and** the right RSI number. Both were checked
against the true offsets.

| Grid | OK | Wrong | No sync | Fetched (band + margin) | Search time per message |
|---|---|---|---|---|---|
| 0.25° | **63 / 74** | 11 | 0 | 56–72% (mean 63.0%) | mean 8.1 s, max 97 s |
| 0.1° | **52 / 74** | 22 | 0 | 56–73% (mean 63.3%) | mean 8.3 s, max 21 s |

By what differs from the reference:

| Message differs from the reference in… | 0.25° | 0.1° |
|---|---|---|
| member only (7 members, +24 h, 12z) | **35 / 35** | **35 / 35** |
| run only (06z, 18z, +24 h) | **10 / 10** | **10 / 10** |
| forecast step (member 1 at 0, 6, 72, 144, 240, 360 h) | 18 / 29 | 7 / 29 |

By variable at 0.25°: `2t` 15/15, `tp` 12/14, `t` 13/15, `u` 12/15,
`msl` 11/15. At 0.1°: `2t` 15/15, `u` 10/15, `tp`, `msl` and `t` 9/15 each
(`tp` 9/14).

The fetched share by variable was `msl` ~56%, `u` ~61%, `2t` ~63%, `t` ~64%
and `tp` ~71–73%. That is the exact band (52–62%, see `../../README.md` §8)
plus the margin on both sides.

Raw output: `../out/v2_sync_0p25.txt`, `../out/v2_sync_0p1.txt`. The first v2
run, made before the offset fix, was stopped after 20 messages. It is kept as
`../out/v2_offset-bug_sync_0p25_partial.txt`: 2 no-syncs and 2 `tp` wrong in
those 20.

## What the failures are

- **Sync is solved.** 32 of the 33 failures found the **exact bit** of a true
  RSI start and only named the wrong RSI. The one exception is `tp` at 6 h,
  0.25°, which was 688 bits off.
- **Naming fails only across forecast steps.** A different step looks too
  little like the +24 h reference for a 12-RSI strip to tell neighbouring
  RSIs apart. This is worse at 0.1°, where 12 RSIs are only ~14 rows. And
  for `tp` and `msl` the field itself changes the most between steps.
  Members and runs at the reference's own step were never wrong.

## Next step (v3, not run yet)

**One reference per forecast step.** GIK templates are already built per
(member, step) from one reference date, so this fits the existing design:
read one message per (variable, step) in full when the template is built,
and match each message against the reference for its own step. v2 shows that
same-step matching was correct in 90 of 90 cases. To confirm it across
steps, v3 needs a second member (or date) at each of the six steps as its
reference.

Other options, if a per-step reference is not enough:

- a longer alignment strip at 0.1° (e.g. 32 RSIs ≈ 36 rows);
- a narrower prediction margin, using the per-step offset profile, so that
  there are fewer RSI numbers to choose between;
- matching the sequence of RSI *lengths* (which reflect geography) rather
  than values.
