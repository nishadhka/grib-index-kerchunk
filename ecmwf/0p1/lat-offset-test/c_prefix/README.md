# Option C: read the band from the message start, with no search

The first RSI starts at bit 0 of the packed data. Reading from the **message
start** down to the end of the band therefore needs no bit search, no stored
offsets and no value match. Per message (`prefix_read.py`):

1. **One GET** from the message start to the template-predicted end of the
   band's last RSI, plus 1.5% of the message and 2 KiB.
2. **Decode from bit 0** with libaec offset capture, stopping at the band's
   last row.
3. **If the bytes ran out** first, GET the next piece (2% + 4 KiB) and decode
   again. A short guess costs bytes, never a wrong answer.

The same decode returns the **exact RSI offsets** from the top to the band
end. That is the par row or sidecar a later exact-band read (~50%) needs, at
no extra cost.

The template only sets the first GET's length. A missing or stale template
means a second GET, not an error.

## Result on the real pars

Every message was checked against a full read: the band values against
eccodes, and the captured offsets against the true ones.

| Run | Band | Template | Exact | Fetched (mean, min–max) | Extra GETs |
|---|---|---|---|---|---|
| test 20261013 | 40°N → 40°S | 20261012 (1 day) | **94 / 94** | **77.3%** (73.1–81.7) | 0 |
| test 20261017 | 40°N → 40°S | 20261016 (1 day) | **94 / 94** | 77.5% (73.0–81.8) | 0 |
| test 20261013 | 40°N → 40°S | **20260513 (5 months)** | **94 / 94** | 76.4% (71.7–81.0) | 2 |
| test 20261013 | **24°N → 12°S** | 20261012 | **94 / 94** | **60.5%** (57.7–63.7) | 0 |

**376 / 376 exact.** Each run covers 5 fields (`2t tp msl t/850 u/700`):

- ens_01 at 11 steps from 0 to 360 h;
- 7 other members and the control at 24 h.

`tp` at 0 h is a constant field, which needs no packed data.

By field (40°N → 40°S, 1-day template): `msl` 73%, `t/850` 77%, `u/700` 77%,
`2t` 79%, `tp` 81%.

Outputs: `../out/c_prefix_*.txt`.

```bash
GIK_CHUNK_SOURCE=gcs uv run c_prefix/prefix_read.py <template par> <test par>... [--lat 40,-40] \
    [--save 2t:24:c_2t_24.npz]
```

## Compared with Herbie, same GRIB message

`plot_compare.py` downloads the same message with Herbie: same `.index`
offset and length, decoded by cfgrib. Both are cut to the same box and
compared value for value (float32).

| Field, member 1, 20261013 00z +24 h | Box | Option C fetched | Herbie fetched | Difference |
|---|---|---|---|---|
| `2t` | 15–80°E, 40°N–40°S (`ea-sw-io40`) | 519 KB (78.6%) | 660 KB (whole message) | **identical** |
| `tp` | same | 684 KB (81.7%) | 838 KB | **identical** |
| `2t` | 15–80°E, 24°N–12°S | 413 KB (62.6%) | 660 KB | **identical** |

![2t](../out/c_prefix_vs_herbie_2t_24h.png)
![tp](../out/c_prefix_vs_herbie_tp_24h.png)
![2t East Africa band](../out/c_prefix_vs_herbie_2t_24h_24N12S.png)

```bash
uv run c_prefix/plot_compare.py c_2t_24.npz "2026-10-13 00:00" 24 2t 1 out/c_prefix_vs_herbie_2t_24h.png
```

## Option C against the other reads

| Read per message | Search | Exact here | 40°N → 40°S | 24°N → 12°S |
|---|---|---|---|---|
| full message (today, Herbie, GIK) | no | — | 100% | 100% |
| **C: message start → band end** | **no** | **376 / 376** | **~77%** | **~61%** |
| v5: window + count to the end | yes (Rust 30 ms – 2 s) | 376 / 376 | ~77% | ~64% |
| v5: `tp` counted, others value match | yes | 188 / 188 (1-day template only) | ~62% | — |
| exact band (offsets already stored) | no | 425 / 425 | ~50% | ~23% |

- **C costs the same as count-to-end** for 40°N → 40°S, and less for East
  Africa's latitudes.
- **C has no search, no value match and no reliance on template age.** It
  should be the realization's first read.
- **Only the value-match policy fetches less without stored offsets** (~62%).
  It needs a 1-day template and the search, and its errors are silent.
- **C's offsets go into the par.** Every later read of that message is then
  the exact band.
- **C fits a standard Icechunk virtual chunk:** `[url, message offset,
  length to the band end]`. It needs a codec that decodes only the first N
  rows of a cut-off message (`../../lat-band-par/README.md`).
