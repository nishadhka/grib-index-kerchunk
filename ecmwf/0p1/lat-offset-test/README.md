# Latitude-band offsets without a full read

ECMWF packs each field as CCSDS (`grid_ccsds`). The packed data is a sequence
of RSIs of 4,096 values each, and each RSI can be decoded on its own, so a
latitude band is one contiguous byte range (`../README.md` §8). GRIB does not
record where each RSI starts, though. `../rsi_band_subset_poc.py` finds the
starts by decoding the whole message once.

The tests here ask whether the starts can be found **without** reading the
whole message. The idea is to work like GIK: use the `.index` plus one
reference message per variable, read once when the template is built.

## Files

| File | What it does |
|---|---|
| `lib/aecx.py` | shared helpers: S3 range GET with retry, `.index` lookup, libaec via ctypes, RSI offsets of a full message |
| `collect_offsets.py` | **ground truth**: full read of 79 messages per grid (5 variables × members, steps and runs), saving the true RSI offsets to `out/offsets_<grid>.pkl` (git-ignored, regenerate with this script) |
| `predict_offsets.py` | how well one reference's offset profile, scaled by each message's `.index` length, predicts the others → `out/predict_offsets.txt` |
| [`v1/`](v1/README.md) | predict → fetch window → bit-sync → pick by smoothness |
| [`v2/`](v2/README.md) | offset bug fixed; joint (start, RSI number) choice using a 12-RSI strip |

Run from this folder:

```bash
uv run collect_offsets.py 0p25 . && uv run collect_offsets.py 0p1 .
uv run predict_offsets.py .
uv run v2/sync_band2.py 0p25 40,-40 .      # and 0p1
```

## Findings so far

1. **The offsets follow a stable pattern.** Scale the reference's offset
   profile by each message's length from `.index`, and the band start is
   predicted to within ~0.5–1% of the message for `2t`, `t`, `u` and `msl`,
   and 3–4% for `tp`. Members barely differ; forecast steps differ the most.
2. **The exact bit can be recovered from a window around the prediction.**
   - A start decodes smoothly and forms a self-consistent RSI chain.
   - The decoder falls back into step within one RSI after a slightly wrong
     start, so hopping to the next reported start lands on a true one.
   - In v2, 147 of 148 messages synced to the exact bit, and none failed to sync.
3. **Naming the RSI is the remaining problem, and only across forecast steps.**
   Whenever a message was at the reference's own step it was exact: 90 of 90,
   across members and runs, at both grids. At other steps 25 of 58 were right.
   The fix to try next is one reference per step, which GIK templates already
   provide.
4. **Cost per message:** the `.index` record, a ~250 B header GET, and one
   GET of ~56–73% of the message for a 40°N → 40°S band (the exact band is
   52–62%; the rest is margin). The bit search takes ~8 s per message in
   Python + ctypes. That is a PoC cost, not a floor: a C or Rust scan would
   be far faster.
5. **Longitude is still the whole 360°**, at every stage (`../README.md` §8).
