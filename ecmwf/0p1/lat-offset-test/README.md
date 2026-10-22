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
| `lib/aecx.py` | shared helpers: range GET with retry (S3, or the byte-identical `gs://ecmwf-open-data` with `GIK_CHUNK_SOURCE=gcs`), `.index` lookup, libaec via ctypes, RSI offsets of a full message |
| `collect_offsets.py` | **ground truth**: full read of 79 messages per grid (5 variables × members, steps and runs), saving the true RSI offsets to `out/offsets_<grid>.pkl` (git-ignored, regenerate with this script) |
| `predict_offsets.py` | how well one reference's offset profile, scaled by each message's `.index` length, predicts the others → `out/predict_offsets.txt` |
| [`v1/`](v1/README.md) | predict → fetch window → bit-sync → pick by smoothness |
| [`v2/`](v2/README.md) | offset bug fixed; joint (start, RSI number) choice using a 12-RSI strip |
| [`v3/`](v3/README.md) | one reference per forecast step, from the same date (v3a) or the next date (v3b); resumable |
| [`v4/`](v4/README.md) | weighting the choice by distance from the predicted RSI; offline rule sweep; margin and count-to-end costs |
| [`v5/`](v5/README.md) | **on the real GIK pars**: confidence gate, count-to-end fallback, head GET; every band checked against eccodes |
| [`c_prefix/`](c_prefix/README.md) | **option C**: read from the message start to the band end, decode from bit 0, no search: **376/376 exact**, ~77% (40°N–40°S) and ~61% (24°N–12°S); identical to Herbie's full-message read |
| [`rust/`](rust/README.md) | Rust port of the search and a benchmark on the same bytes: v5 search **0.50 s/msg** (15×, same answers as Python 54/54); chain search **30–53 ms** on smooth fields |
| `breakdown.py` | tally one or more sync outputs by what differs from the reference |

Run from this folder:

```bash
uv run collect_offsets.py 0p25 . && uv run collect_offsets.py 0p1 .
uv run predict_offsets.py .
uv run v3/sync_band3.py 0p25 40,-40 . 20261021:00:1      # and 0p1; ref = date:run:member
uv run breakdown.py out/v3b_sync_0p25.txt
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
3. **Naming the RSI: one reference per step fixes most of it (v3).**
   - With a single +24 h reference (v2), messages at other steps were right
     only 25 of 58 times.
   - With a reference at each message's own step (v3), the result was
     **75/79** (v3a) and **73/79** (v3b) at 0.25°, and **73/79** at 0.1°
     (v3a). v3b takes its reference from **another date and run**, as a GIK
     template would.
   - Every member and run case is exact: 150 of 150.
   - The remaining failures are long leads (144–360 h) of `msl`, `t` and
     `u`, where ensemble spread makes the reference look different.
   - **Distance weighting (v4) did not fix this.** The rule fixed before the
     run scored 73/79 (0.25°) and 72/79 (0.1°). The best weak prior from the
     offline sweep reaches 78/79 and 74/79, but it was tuned on this data.
     The prediction alone names the RSI only 40/79 and 19/79 times.
   - **Exact naming is possible** by counting RSIs from the synced start to
     the end of the message. That costs 72–80% of the message for 40°N–40°S.
     A score threshold catches every failure and only 5 of 145 right choices,
     so the next step (v5) is a hybrid: value match when the score is
     confident, count-to-end otherwise.
   - **v5, on the real pars** (template from another date):
     - The hybrid rule scored **94/94** and **53/54**, at ~58% of the bytes.
     - The one miss is `tp` at 6 h. It is silent, because its score was just
       under the gate.
     - **Count-to-end alone is exact on 148/148**, at ~77%. Every band
       decoded from the fetched bytes was checked against eccodes on the full
       message.
   - **Template age (v5c/v5d):**
     - value match is safe only with a template from the previous run
       (15–28% silent errors at 7 days to 5 months);
     - count-to-end is not affected by template age.
     - After two sync fixes: "`tp` counted, rest value match" with a 1-day
       template is **188/188** at ~62%, and count-only with a **5-month**
       template is **188/188** at ~77%, on two fresh dates.
4. **The GCS mirror avoids S3 throttling.** `gs://ecmwf-open-data` is
   byte-identical (`ifs/0p1/` included). A v3 run throttled out on S3 was
   finished there with no retries (`v3/README.md`).
5. **Cost per message:** the `.index` record, a ~250 B header GET, and one
   GET of ~56–73% of the message for a 40°N → 40°S band (the exact band is
   45–62%; the rest is margin). The margin can drop from 5% to **1.5%**
   with the band start still inside the window in 100% of messages, which
   brings the fetch to ~56% (v4). The bit search takes ~8 s per message in
   Python + ctypes. In Rust (`rust/`) it takes 0.50 s with the same answers,
   or 30–53 ms with the chain search on smooth fields.
6. **Option C needs no search at all** (`c_prefix/`): read from the
   message start to the band end and decode from bit 0. 376/376 exact,
   ~76% for 40°S and ~59% for 12°S. Its cost depends only on the south
   edge, and a manifest length of template prediction + 3% + 4 KiB was
   never short with templates up to 4 months old, so it can be written
   into an Icechunk virtual store without reading GRIB.
7. **Longitude is still the whole 360°**, at every stage (`../README.md` §8).
