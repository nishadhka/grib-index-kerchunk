# Draft issues for mpiannucci/gribberish

Not posted. Two separate issues, since they touch different layers:

1. the VirtualiZarr parser: cross-file template reuse (what the VirtualiZarr
   maintainers asked to have raised here);
2. the core decoder: latitude-band reads of CCSDS messages, which answers
   the open question in #51.

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

## Issue 2: CCSDS (template 5.42): read a latitude band without decoding (or fetching) the whole message

This follows up #51. That issue asks whether a message buffer could be "chopped
into equal sections along the slowest-varying dimension" so each section can
be read on its own. For **CCSDS-packed** messages (template 5.42, used by ECMWF
open data, Météo-France and others) it can, and we have measured it.

**Why it works**

- AEC data is a sequence of **RSIs** (reference sample intervals:
  `block_size × rsi` = 32 × 128 = 4,096 samples for ECMWF). Each RSI starts
  with a reference sample and **decodes independently**.
- libaec ≥ 1.1 exposes this (`aec_decode_enable_offsets`,
  `aec_decode_get_offsets`, `aec_decode_range`).
- Samples are in scan order (north → south, a full row at a time), so a
  **latitude band is one contiguous byte range**. Which RSIs cover a given
  latitude is fixed by the grid. *Where* they start differs per message.
- **Longitude cannot be cut this way:** an RSI (4,096 samples) is longer
  than a row (1,440 at 0.25°, 3,600 at 0.1°).

**What we measured** (ECMWF ENS 0.25°, 40°N → 40°S, every band checked
against eccodes on the full message):

| How the RSI offsets are known | Bytes fetched per message | Correct |
|---|---|---|
| stored once from a full decode (a small sidecar, ~1 KB/message) | **45–54%** | 425 / 425 bit-exact |
| found from a byte window, using a template profile from an earlier cycle | 62–77% | 376 / 376 (two policies) |

**Finding the offsets without a full read** is a bit-level search:

1. Fetch a window around the predicted band start.
2. Find a bit where a smooth, self-consistent RSI chain begins.
3. Name the RSI, either by counting RSIs to the end of the message or by
   matching values against a template.

**A Rust implementation** of the search is about 250 lines:

- an allocation-free CCSDS decoder that starts from any bit, with early exit;
- it gives the same answers as the Python reference on 54/54 messages.

| Search | Time per message (0.25°) |
|---|---|
| the Python reference | 7.6 s |
| Rust, full search | **0.50 s** |
| Rust, chain search (smooth fields) | **30–53 ms** |

**What could live in gribberish** (suggestions):

1. **Offsets on decode.** The libaec feature (#79) could enable offset
   capture and return the RSI bit offsets with the values. That's nearly free
   (we measured libaec decode plus offsets at ~9 ms per 0.25° message,
   against ~19 ms for the current decode).
2. **Range decode.**
   `decode_rsi_range(data_section, template_5_42_params, rsi_offsets, k0, k1)`
   → values for RSIs k0..k1, given the bytes of that range only.
3. **(Optional) the search**, for callers without stored offsets: a function
   that, given a byte window and the Section 5 parameters, returns verified
   RSI start bits. We'd be happy to contribute ours.

**Zarr / Icechunk note.** A band chunk needs a bit shift and the message's
Section 5 constants, neither of which fits in a virtual chunk's
`[url, offset, length]`. Today it needs a reader that also has the offsets
(we use a sidecar parquet next to the reference set).

Two upstream things would make it a plain virtual chunk:

- **ECMWF** could publish RSI offsets in `.index`, which libaec can capture
  at encode time, or pad each RSI to a byte (`AEC_PAD_RSI`);
- **Icechunk** could allow per-chunk codec parameters or multi-range refs.

**Question:** would you accept (1) and (2)? We'd open a PR.
