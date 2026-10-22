# Draft issues for mpiannucci/gribberish

Not posted. Two separate issues, since they touch different layers:

1. the VirtualiZarr parser: cross-file template reuse (what the VirtualiZarr
   maintainers asked to have raised here);
2. the core decoder and codec: decode the first N rows of a CCSDS message
   whose bytes stop early, which answers the open question in #51 for the
   northern part of a grid with a plain virtual chunk.

---

## Issue 1: GribberishParser: reuse a per-stream template so later cycles resolve from the index alone

**Problem**

`GribberishParser(use_index=...)` (#154) uses the sidecar index to find
messages. It then fetches the header window (sections 0–5) of every selected
message, **for every file of every cycle**. For a forecast stream, those
headers are the same from cycle to cycle, apart from dates and byte ranges.

For ECMWF open-data ENS at 0.25°, one 00z run is:

- **85 files** (steps);
- about **8,500 messages per file** (one 24 h file of 20261019: 50 members ×
  ~170 fields and levels);
- so about **700,000 header windows** to read per run, even when the index
  already gives every message's `(param, levtype, levelist, number, step)`
  and `(_offset, _length)`.

**What we do instead.** The Grib-Index-Kerchunk (GIK) method is used
operationally at ICPAC for East Africa (E4DRR / SEWAA), for GEFS and ECMWF
ENS. It works like this:

1. **Once per stream and schema era**, scan one reference cycle's headers to
   build a *template*: for each index key, the zarr variable, dims, coords,
   attrs and codec config.
2. **Every other cycle:** read only the index files (85 small text GETs per
   run), join each entry to the template on its index key, and emit the
   manifest `[url, offset, length]`. No GRIB bytes are read.

**Results for ECMWF ENS:**

- Stage 2 takes ~5–15 min per date on Cloud Run, and is fully stateless.
- Template rebuilds are only needed when the schema changes. ECMWF open data
  has had 5 such eras since 2023: grid, pl-level count, control stream and
  surface param set changed. A template also fails safe: new params appear as
  unmatched index keys rather than wrong data.

**Suggested shape** (open to whatever fits the parser):

```python
tmpl = GribberishParser(use_index=True).build_template(ref_url)
# serialisable: index key -> (group, variable, dims, coords, attrs, codec config)
tmpl.save("ecmwf-enfo-0p25-50r1.template.json")

parser = GribberishParser(use_index="only", template=tmpl)   # no GRIB reads
store = parser(url, registry)                                 # index + template -> ManifestStore
```

- `use_index="only"` would require the index. It would raise, rather than
  fall back to a scan, when an entry has no template key. That is how a
  schema change gets detected.
- **The template key needs to be stable across cycles.** For ECMWF that is
  `(param, levtype, levelist, number)` plus a step slot. For NOAA `.idx`,
  the variable and level strings. This connects to the group-path
  stability work in #165 and #178.

**Questions**

- Would you take this in the parser, or as a separate helper that builds a
  `ManifestStore` from (index, template)?
- Is there a preferred serialisation for the template (zarr metadata JSON, or
  a pickled `ManifestStore` with empty manifests)?

---

## Issue 2: CCSDS (template 5.42): decode the first N rows of a truncated message

This follows up #51, which asks whether a message could be "chopped into
equal sections along the slowest-varying dimension". For **CCSDS-packed**
messages (template 5.42, used by ECMWF open data, Météo-France and others) a
simpler version of that already works, and needs only a small change here.

**The idea**

- Samples are in scan order (north → south, a full row at a time), so rows
  0..N of the grid are the **first** samples of the data section.
- AEC data is a sequence of RSIs (reference sample intervals: `block_size ×
  rsi` = 32 × 128 = 4,096 samples for ECMWF). The first RSI starts at bit 0
  of Section 7, so decoding from the top needs no offsets and no search.
- So a byte range **from the message start** to the end of row N's RSI holds
  everything needed: Sections 0–6 (grid and packing parameters) and the
  packed data for rows 0..N. Nothing else has to be stored.
- That range is an ordinary `[url, offset, length]` virtual chunk, with the
  same offset as the whole message and a shorter length.

**What we measured** (ECMWF ENS 0.25°, 20261013 and 20261017 00z; 5 fields
`2t tp msl t/850 u/700`; 11 steps 0–360 h plus 8 members at +24 h; every
band checked value for value against eccodes on the full message):

| Rows kept | Messages | Bytes fetched per message | Correct |
|---|---|---|---|
| 90°N → 40°S | 282 | **~77%** (72–82) | 282 / 282 bit-exact |
| 90°N → 12°S | 94 | **~61%** (58–64) | 94 / 94 bit-exact |

Against Herbie (whole message, cfgrib) on the same message, `2t` and `tp` in
the box 15–80°E, 40°N–40°S came out **identical** from 79% and 82% of the
bytes; `2t` at 24°N–12°S, identical from 63%.

How we read it (Python + libaec ≥ 1.1 through ctypes):

1. GET from the message offset to a predicted end. The prediction comes from
   the RSI offsets of the same field on an earlier cycle, plus a 1.5% + 2 KiB
   margin.
2. `aec_decode` with `AEC_NO_FLUSH` on the truncated Section 7, asking for
   `N × Ni` samples. libaec returns the samples it could decode.
3. If fewer than `N × Ni` came back, GET the next piece and decode again. A
   short guess costs bytes, never a wrong value.
4. With `aec_decode_enable_offsets`, the same decode returns the exact RSI
   offsets, so the next read of this message can store the exact length.

**The ask**

1. **Decode with a row (or sample) limit, tolerating a short Section 7.**
   Something like `decode(message_bytes, max_rows=N)` → an `N × Ni` array,
   which succeeds when the bytes end after row N's RSI even though
   Section 7's declared length is larger. Today the decoder expects the full
   message.
2. **The same option on the zarr codec**, so a virtual chunk can point at
   the prefix:

   ```python
   # array lat 90N..40S, chunk shape (N, Ni); one chunk per message
   codecs = [GribberishCodec(var="2t", rows=N)]
   manifest[...] = {"path": url, "offset": msg_offset, "length": prefix_len}
   ```

   The codec reads the grid and packing parameters from the prefix's own
   Sections 3 and 5, so no per-chunk codec parameters are needed. That is
   what makes this fit VirtualiZarr / Icechunk as they are.
3. **(Optional) return the RSI offsets on decode** when the libaec feature
   (#79) is on (`aec_decode_enable_offsets`). We measured libaec decode plus
   offsets at ~9 ms per 0.25° message, against ~19 ms for the current
   decode. A pipeline can then write the exact `prefix_len` per message
   into the manifest.

`prefix_len` must cover row N's RSI completely. If it is too short, the
codec should raise rather than return a partial chunk.

**Limits**

- Only the northern part of a grid can be kept this way: rows 0..N, not a
  band in the middle. For 40°N → 40°S the rows north of 40°N (~28% of the
  samples, fewer of the bytes) are still fetched and decoded.
- Longitude cannot be cut: an RSI (4,096 samples) is longer than a row
  (1,440 at 0.25°, 3,600 at 0.1°).
- Only template 5.42; simple and complex packing would need other work.

**A middle band, for reference.** Fetching only the band itself is possible
too: ~50% of the bytes for 40°N → 40°S and ~23% for 24°N → 12°S, 425/425
bit-exact. It needs the bit offset of the band's first RSI and the Section 5
constants, which do not fit a virtual chunk, so we keep them in a sidecar.
We also have a Rust search (~250 lines, 30 ms – 2 s per message) that finds
those offsets in a byte window without a stored copy. Those are a larger
change; we'd rather start with the prefix decode above, and can share them
if useful.

**Question:** would you accept (1) and (2), and (3) if it fits? We'd open a PR.
