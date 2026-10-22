# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "eccodes", "imagecodecs", "pandas", "pyarrow"]
# ///
"""Option C: read a latitude band from the message start, with no search.

The first RSI starts at bit 0 of the packed data, so decoding from the top
needs no bit search and no stored offsets. Per message:

    1. one GET from the message start to the template-predicted end of the
       band's last RSI, plus a margin;
    2. decode from bit 0 with libaec offset capture, stopping at the band's
       last row;
    3. if the bytes ran out first, GET the next piece (2% of the message +
       4 KiB) and decode again. Too short a guess costs bytes, never a wrong
       answer.

The decode also yields the exact RSI offsets from the top to the band end,
which is what a par row / sidecar would store for later exact-band reads.
Every message is checked against a full read: band values against eccodes,
captured offsets against the true ones.

    GIK_CHUNK_SOURCE=gcs uv run c_prefix/prefix_read.py <template par> <test par>... [--lat 40,-40]
"""
import argparse, json, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "v5"))
from sync_band5 import RS, VARS, full, par_refs, sec5  # noqa: E402
from aecx import ctypes, get, lib, np, stream  # noqa: E402

MARGIN, EXTEND = 0.015, 0.02          # of the message length, + 2 KiB / 4 KiB


def decode_prefix(data, K, nsamp):
    """Decode up to nsamp samples from bit 0 of `data` (it may end early).
    Returns (raw samples decoded, RSI bit offsets)."""
    out = np.empty(nsamp * K["bpsamp"], np.uint8)
    s = stream(data, *K["args"], out)
    lib.aec_decode_enable_offsets(ctypes.byref(s))
    rc = lib.aec_decode(ctypes.byref(s), 0)          # no flush: input may stop mid-block
    got = (out.nbytes - s.avail_out) // K["bpsamp"]
    c = ctypes.c_size_t(); lib.aec_decode_count_offsets(ctypes.byref(s), ctypes.byref(c))
    o = (ctypes.c_size_t * c.value)(); lib.aec_decode_get_offsets(ctypes.byref(s), o, c)
    lib.aec_decode_end(ctypes.byref(s))
    if rc != 0:
        raise ValueError(f"libaec error {rc}")
    return out[:got * K["bpsamp"]], np.array(o, dtype=np.int64)


def unpack(raw, K):
    X = raw.view(">u2" if K["bpsamp"] == 2 else ">u4").astype(np.float64)
    return (K["referenceValue"] + X * 2.0 ** K["binaryScaleFactor"]) / 10.0 ** K["decimalScaleFactor"]


def prefix_band(key, o, L, T, ln, ls):
    """Option C for one message. Returns (band values, lats, offsets, bytes fetched, GETs)."""
    ni, nj = T["ni"], T["nj"]
    dlat = 180.0 / (nj - 1)
    j0, j1 = round((90 - ln) / dlat), round((90 - ls) / dlat) + 1
    k1e = (j1 * ni - 1) // RS + 1                      # first RSI after the band
    # predicted end of the band's last RSI, in message bytes, from the template profile
    end = T["d0"] + (T["fr"][k1e] if k1e < len(T["fr"]) else 1.0) * (L - T["d0"])
    end = min(L, int(end + MARGIN * L) + 2048)
    msg = get(key, o, o + end - 1)
    gets = 1
    while True:
        K = sec5(msg)                                  # this message's own header and d0
        K.update(bpsamp=2 if K["bitsPerValue"] <= 16 else 4)
        K["args"] = (K["bitsPerValue"], K["ccsdsBlockSize"], K["ccsdsRsi"], K["ccsdsFlags"])
        if K["bitsPerValue"] == 0:
            return None
        data = msg[K["d0"]:L - 4] if len(msg) == L else msg[K["d0"]:]
        raw, offs = decode_prefix(data, K, j1 * ni)
        if len(raw) == j1 * ni * K["bpsamp"] or len(msg) == L:
            break
        more = min(L - len(msg), int(EXTEND * L) + 4096)  # ran out: fetch the next piece
        msg += get(key, o + len(msg), o + len(msg) + more - 1)
        gets += 1
    vals = unpack(raw, K)[j0 * ni:j1 * ni].reshape(j1 - j0, ni)
    lats = 90 - np.arange(j0, j1) * dlat
    return vals, lats, offs, len(msg), gets, K["d0"], k1e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ref"); ap.add_argument("tests", nargs="+")
    ap.add_argument("--lat", default="40,-40")
    ap.add_argument("--steps", default="0,6,24,48,72,120,144,168,240,300,360")
    ap.add_argument("--member-step", type=int, default=24)
    ap.add_argument("--save", action="append", default=[],
                    help="field:step:file.npz, e.g. 2t:24:out/c_2t_24.npz (first test par); repeatable")
    a = ap.parse_args()
    ln, ls = map(float, a.lat.split(","))
    ref = par_refs(a.ref)
    jobs = []
    for i, t in enumerate(a.tests):
        steps = [int(s) for s in a.steps.split(",")] if i == 0 else [a.member_step]
        refs = par_refs(t)
        jobs += [(os.path.basename(t), f, s, refs[(f, s)]) for f in VARS for s in steps]
    print(f"option C prefix read, band {ln}..{ls}, template {os.path.basename(a.ref)}, "
          f"margin {MARGIN:.1%}+2KiB, extend {EXTEND:.0%}+4KiB, {len(jobs)} messages", flush=True)
    tmpl, rows = {}, []
    for name, f, step, (key, o, L) in jobs:
        tag = f"{name.split('.')[0]:22} {f:5} s{step:3}"
        if (f, step) not in tmpl:
            R = full(*ref[(f, step)])
            tmpl[(f, step)] = None if R is None else dict(
                fr=R["offs"] / (R["databytes"] * 8), d0=R["K"]["d0"], ni=R["K"]["Ni"], nj=R["K"]["Nj"])
        T = tmpl[(f, step)]
        if T is None:
            T = dict(fr=np.linspace(0, 1, 255), d0=180, ni=1440, nj=721)   # constant template: grid only
        t = time.time()
        r = prefix_band(key, o, L, T, ln, ls)
        dt = time.time() - t
        if r is None:
            print(f"  {tag}  skip    constant field (no packed data: the header alone gives it)", flush=True)
            continue
        vals, lats, offs, nbytes, gets, d0, k1e = r
        truth = full(key, o, L)                            # verification only
        j0 = round((90 - lats[0]) / (180.0 / (T["nj"] - 1)))
        exact = np.array_equal(vals, truth["vals"].reshape(-1, T["ni"])[j0:j0 + len(lats)])
        n_off = min(len(offs), k1e)
        offs_ok = np.array_equal(offs[:n_off], truth["offs"][:n_off])
        ok = exact and offs_ok
        rows.append(dict(field=f, frac=nbytes / L, gets=gets, ok=ok, t=dt))
        print(f"  {tag}  {'OK' if ok else 'WRONG':5} {100 * nbytes / L:5.1f}% of {L / 1e3:5.0f} KB  "
              f"GETs {gets}  {dt:4.1f}s  values {'exact' if exact else 'DIFFER'}  "
              f"offsets 0..{n_off - 1} {'exact' if offs_ok else 'DIFFER'}", flush=True)
        for spec in a.save if name == os.path.basename(a.tests[0]) else []:
            sf, ss, path = spec.rsplit(":", 2)
            if sf == f and int(ss) == step:
                np.savez(path, vals=vals, lats=lats, lons=-180 + np.arange(T["ni"]) * 360 / T["ni"],
                         key=key, offset=o, length=L, fetched=nbytes)
    import pandas as pd
    df = pd.DataFrame(rows)
    print(f"{int(df.ok.sum())}/{len(df)} exact; fetched mean {100 * df.frac.mean():.1f}% "
          f"(min {100 * df.frac.min():.1f}, max {100 * df.frac.max():.1f}); "
          f"extra GETs in {int((df.gets > 1).sum())} messages")
    print("  by field: " + "  ".join(f"{k} {100 * g.frac.mean():.1f}%" for k, g in df.groupby("field")))


if __name__ == "__main__":
    main()
