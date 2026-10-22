# Rust port of the band search, and benchmark

The v5 search (`../v5/sync_band5.py`) finds an exact RSI start inside a
fetched byte window. In Python it took **7.6 s per message**, too slow for the
realization. This folder ports it to Rust and measures it against the Python on
**the same cached bytes**.

## Files

| File | Role |
|---|---|
| `dump_cases.py` | fetches the v5 inputs once (window → end of packed data, template values) and times the Python search on them → `<cases>/manifest.json`, `case_NNN.bin`, `ref_NNN.f64` |
| `truth_cases.py` | true RSI offsets from a full read of each case (verification only) → `<cases>/truth.json` |
| `fix_tails.py` | one-off: cases dumped before a fix to `dump_cases.py` stopped `d0` bytes short of the end of the data; this appends the missing bytes |
| `src/aec.rs` | libaec through `libloading`, the **same** `libaec.so` the Python uses (imagecodecs wheel) |
| `src/fastdec.rs` | allocation-free CCSDS decoder from any bit (~250 lines): ID, reference sample, zero-block / second-extension / split / uncompressed, RSI boundaries, inverse preprocessor. `smooth_rsi` stops as soon as an RSI is certainly too rough |
| `src/sync.rs` | the v5 search (`candidates`), the chain search (`chain_sync`), value match, count-to-end |
| `src/main.rs` | the benchmark and its checks |

```bash
# cases (Python side, once): template 20261012 00z ens_01, test 20261013 00z ens_01
GIK_CHUNK_SOURCE=gcs uv run rust/dump_cases.py  <template par> <test par> <cases dir>
GIK_CHUNK_SOURCE=gcs uv run rust/truth_cases.py <test par> <cases dir>
# Rust side; no system C toolchain is needed: cargo-zigbuild, or `cc` pointing at `zig cc`
cargo build --release
AECSYNC_LIBAEC=<.../imagecodecs.libs/libaec-*.so> target/release/aecsync <cases dir> v5-fast 3
```

## Benchmark

These numbers come from the following setup:

- **Messages:** 54, the 20261013 00z ens_01 test message for each of `2t`, `tp`, `msl`, `t/850` and `u/700` at 0–360 h. `tp` at 0 h is a constant field, so it is not searched.
- **Band and template:** 40°N → 40°S, with the template from the previous day.
- **Machine:** AMD EPYC 7713, single thread, best of 3 runs, CPU only (the bytes are cached).

| Search | Decoder | Time per message | Faster than Python | Result |
|---|---|---|---|---|
| Python v5 | libaec (ctypes) | 7.62 s | — | — |
| Rust v5 | libaec, one init per try | 2.47 s | 3× | **same answers as Python 54/54** |
| Rust v5 | `fastdec`, early exit per RSI | 1.00 s | 8× | same 54/54 |
| **Rust v5** | `fastdec`, streaming early exit | **0.50 s** | **15×** | **same 54/54, every count exact** |
| **Rust chain** | `fastdec` | **0.060 s** | **128×** | exact 52/54 (all 45 non-`tp`; 2 of 9 `tp` wrong) |

Per field, median (max):

| Field | Rust v5 | Rust chain |
|---|---|---|
| `2t` | 218 ms (241) | 53 ms (84) |
| `msl` | 179 ms (232) | 40 ms (73) |
| `t/850` | 182 ms (230) | 30 ms (81) |
| `u/700` | 220 ms (252) | 43 ms (92) |
| `tp` | 2,042 ms (2,474) | 82 ms (481); **2 wrong** |

Outputs are in `../out/rust_bench_*.txt`.

**Equality vs correctness:**

- *Same answers* means the same candidate starts, the same number of scored
  pairs, the same best (start, RSI, score), and the same count start as the
  Python.
- The counts are checked against the true RSI offsets, because the Python
  counts in the dump were taken on the short tails (see `fix_tails.py`).
- The chain search is a different algorithm, so it is judged only against
  the truth.

## What made it fast

1. **Not the language.** The direct port is only 3× faster. Each try costs a
   libaec init and free plus decodes: the same work as in the Python.
2. **The search does a lot of decoding.** About **36%** of bit positions pass
   the 64-sample test, and each of those decodes up to 3 RSIs (12,288
   samples). `fastdec` passes exactly the same 35.94% as libaec over 384k
   tries, so the decoder port is exact.
3. **Stop early, with the same answer.** An RSI fails when the mean
   |adjacent difference| exceeds 2% of the code range. The running sum only
   grows, so decoding can stop as soon as it crosses the limit. 2.47 s →
   0.50 s, still identical.
4. **Search less (chain).** 80% of the v5 tries collect *alternative* starts
   after the first one, which the value match then chooses from. The chain
   search instead:
   - stops at the first verified start;
   - decodes its RSI chain through the window;
   - uses the starts from 2 RSIs on.

   That takes 60 ms, and every smooth field was exact.

## Where the chain search fails: zero-run fields

In `tp` (mostly zeros), a false start can pass the 3-RSI test and decode
cleanly through the whole window. It then falls into step on the true RSI
track 1–3 RSIs later than the 2 that are skipped. Two attempts that did not
work:

- **Skipping a fixed depth** is a guess.
- **Convergence of two chains** (`../out/rust_bench_converge_rejected.txt`,
  47/54): false tracks converge with each other too.

So for now:

| Field type | Search | Time | Exact here |
|---|---|---|---|
| smooth (`2t`, `msl`, `t`, `u`, …) | chain | 30–53 ms median | 45 / 45 |
| zero-run (`tp`, and likely `cp`, `sf`, `ro`, …) | v5 | ~2 s | 9 / 9 |

The chain search was tested on 45 smooth messages from one date. It needs the
v5c/v5d style test (other dates, members, the control) before use.

## For comparison

| | Per message |
|---|---|
| gribberish full decode, today's read | ~19 ms |
| libaec full decode + offsets (sidecar build) | ~9.4 ms |
| Range GET of a 0.25° message from GCS (this machine) | ~0.6 s |

At 30–50 ms per message, the chain search costs CPU comparable to a full
decode. It is well below the network time it saves (~23–38% of the bytes).
