# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "icechunk>=2.1", "zarr>=3.2", "xarray>=2025.1",
#   "pandas", "pyarrow", "gribberish>=1.4",
# ]
# ///
"""ECMWF ENS .index files on GCS -> virtual Icechunk store. No pars, no template.

The pipeline so far has been

    .index (s3) -> stage 1 pars (+ template metadata) -> build_ecmwf_icechunk.py

but the icechunk builder only ever reads the `step_*` refs out of a par, and
every one of those comes from the .index. The template contributes .zarray /
.zattrs keys alone -- 2,774 of them per member, zero refs (checked 2026-10-22)
-- which kerchunk readers of the parquet need and the icechunk store does not:
its grid, levels and axes come from grids.py, its array names from the index
`param`, its decoding from the gribberish codec. So this goes straight from the
.index to icechunk:

    gs://ecmwf-open-data/.../*.index  ->  refs (gs://...grib2, offset, length)
                                       ->  same group layout as build_ecmwf_icechunk.py

The GCS objects are byte-identical to s3://ecmwf-forecasts (every step's .index
of one run per era compared, 2026-10-21; gcs-source/2026-10-21-*.txt), so refs
point at gs:// and the AWS throttle is out of the path entirely -- for reading
the index here and for reading chunks later. GCS holds 2023-07-12 onward only.

Stage 1 reads each index once per member (51 x 85 GETs); this reads each once.

Ref semantics are stage 1's, deliberately, so a store built either way holds
the same refs (`--parity-pars` proves it against a date's stage-1 pars):
  * key (step, member, param, levtype[, level]); pl level from `levelist`
  * the control comes from oper/fc in 50r1, from enfo number=0 before it
  * a duplicate key keeps the LAST index line -- stage 1 builds a dict. This
    collapses the levelists of levtype `sol` (soil layers) onto one; the count
    is reported, not fixed, because the builder has no soil-level axis either.

  uv run index_to_icechunk.py --date 20261020 --run 06 --store /path/to/store
  uv run index_to_icechunk.py --date 20261019 --run 00 --store /tmp/x \\
      --parity-pars .../pars/20261019          # refs == stage-1 pars?
  uv run index_to_icechunk.py --date 20261020 --run 06 --store /path --verify-only
"""
import argparse
import json
import random
import sys
import time as _time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import icechunk
import zarr
import gribberish

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "icechunk-par"))
import build_ecmwf_icechunk as B  # noqa: E402  -- group layout, axes, ref specs
from gribberish.zarr.codec import GribberishCodec  # noqa: E402

S3_PREFIX = "s3://ecmwf-forecasts/"
GCS_PREFIX = "gs://ecmwf-open-data/"
GCS_HTTP = "https://storage.googleapis.com/ecmwf-open-data/"
GCS_FIRST_DATE = "20230712"
STREAM_PATH = {"0p4": "0p4-beta", "49r1": "ifs/0p25", "50r1": "ifs/0p25",
               "0p1": "ifs/0p1"}
# ifs/0p1 splits every step into two files by level type, each with its own
# .index: -pl (levtype pl) and -sfc (levtype sfc and sol). 0p1/README.md §1.
SPLIT_ERAS = {"0p1": ("pl", "sfc")}
OPER_CONTROL_ERAS = ("50r1", "0p1")   # control in oper/fc, not enfo number=0


def get(url, rng=None, tries=6):
    h = {"Range": f"bytes={rng[0]}-{rng[0] + rng[1] - 1}"} if rng else {}
    for i in range(tries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                          timeout=120).read()
        except Exception as e:
            err = e
            _time.sleep(2 ** i)
    raise err


def grib_key(era, date, run, hour, stream, part=None):
    suffix = "oper-fc" if stream == "oper" else "enfo-ef"
    if part:
        suffix += f"-{part}"
    return (f"{date}/{run}z/{STREAM_PATH[era]}/{stream}/"
            f"{date}{run}0000-{hour}h-{suffix}")


def index_refs(era, date, run, control_stream, threads=8):
    """Every ref of one (date, run), straight from the .index files on GCS.

    Returns (refs, n_dupes) with the columns build_ecmwf_icechunk.parse_par
    produces plus `number`.
    """
    hours = B.STEPS_BY_RUN[run]
    streams = ["enfo"] + (["oper"] if control_stream == "oper" else [])
    jobs = [(h, s, part) for h in hours for s in streams
            for part in SPLIT_ERAS.get(era, (None,))]

    def fetch(job):
        h, stream, part = job
        key = grib_key(era, date, run, h, stream, part)
        return h, stream, key, get(GCS_HTTP + key + ".index").decode()

    rows = []
    with ThreadPoolExecutor(threads) as ex:
        for h, stream, key, text in ex.map(fetch, jobs):
            url = GCS_PREFIX + key + ".grib2"
            for line in text.splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                if d["_length"] == -1:
                    continue
                number = int(d.get("number", 0))
                # the control lives in exactly one stream: oper in 50r1, else enfo
                if (stream == "oper") != (number == 0 and control_stream == "oper"):
                    continue
                levtype = d.get("levtype", "")
                rows.append((h, d.get("param", "").lower().replace(" ", "_"), levtype,
                             float(d["levelist"]) if levtype == "pl" else np.nan,
                             url, int(d["_offset"]), int(d["_length"]), number))
    refs = pd.DataFrame(rows, columns=["step_h", "var", "levtype", "level", "url",
                                       "offset", "length", "number"])
    key = ["step_h", "number", "var", "levtype", "level"]
    n_dupes = int(refs.duplicated(key, keep="last").sum())
    refs = refs.drop_duplicates(key, keep="last").reset_index(drop=True)
    return refs, n_dupes, len(jobs)


def open_or_create(storage):
    """Both chunk containers. An existing store gets gs:// MERGED into its stored
    config -- a fresh config naming only gs:// would replace s3:// on save."""
    conts = {S3_PREFIX: (icechunk.s3_store(region="eu-central-1", anonymous=True),
                         icechunk.s3_anonymous_credentials()),
             GCS_PREFIX: (icechunk.gcs_store(), icechunk.GcsCredentials.Anonymous())}
    auth = icechunk.containers_credentials({p: c for p, (_, c) in conts.items()})
    if icechunk.Repository.exists(storage):
        repo = icechunk.Repository.open(storage, authorize_virtual_chunk_access=auth)
        have = set(repo.config.virtual_chunk_containers or {})
        if GCS_PREFIX in have:
            return repo
        cfg = repo.config
        cfg.set_virtual_chunk_container(icechunk.VirtualChunkContainer(GCS_PREFIX,
                                                                       conts[GCS_PREFIX][0]))
        repo = icechunk.Repository.open(storage, config=cfg,
                                        authorize_virtual_chunk_access=auth)
        if not have | {GCS_PREFIX} <= set(repo.config.virtual_chunk_containers or {}):
            raise SystemExit(f"REFUSING to save config: would lose {sorted(have)}")
        repo.save_config()
        print(f"  registered {GCS_PREFIX} alongside {sorted(have)}")
        return repo
    cfg = icechunk.RepositoryConfig.default()
    for p, (st, _) in conts.items():
        cfg.set_virtual_chunk_container(icechunk.VirtualChunkContainer(p, st))
    cfg.manifest = icechunk.ManifestConfig(
        splitting=icechunk.ManifestSplittingConfig.from_dict({
            icechunk.ManifestSplitCondition.AnyArray(): {
                icechunk.ManifestSplitDimCondition.DimensionName("time"):
                    B.MANIFEST_SPLIT_TIME}}))
    return icechunk.Repository.create(storage, cfg, authorize_virtual_chunk_access=auth)


def write_date(repo, era_name, run, date, refs, overwrite=False):
    """One commit for one (era, run, date) -- build_ecmwf_icechunk.main's write
    path, fed from refs instead of a pars directory."""
    era = B.ERAS[era_name]
    grp = f"{era_name}/{run}z"
    lev_idx = {float(v): i for i, v in enumerate(era["levels"])}
    steps = np.array(sorted(refs.step_h.unique()), dtype="int32")
    if list(steps) != B.STEPS_BY_RUN[run]:
        raise SystemExit(f"{date} {run}z: {len(steps)} steps in the index, "
                         f"expected {len(B.STEPS_BY_RUN[run])}")
    step_idx = {h: i for i, h in enumerate(steps)}
    bad = set(refs.loc[refs.levtype == "pl", "level"].dropna()) - set(lev_idx)
    if bad:
        raise SystemExit(f"levels {bad} outside the {era_name} superset")
    sfc_vars, pl_vars = B.split_levtypes(refs)
    time_val = B.time_value(date, run)

    session = repo.writable_session("main")
    store = session.store
    created = B.ensure_group(store, grp, era, steps, era_name)
    g = zarr.open_group(store=store, path=grp, mode="r+", zarr_format=3)
    mode = B.group_mode(g)
    tarr = g["time"]
    existing = tarr[:] if tarr.shape[0] else np.array([], dtype="int64")
    if not created:
        B.check_coords(g, era_name)
        assert np.array_equal(g["step"][:], steps), "step axis mismatch"
    if mode == B.PREALLOC:
        ti = int(np.searchsorted(existing, time_val))
        if ti >= existing.size or existing[ti] != time_val:
            raise SystemExit(f"{date} {run}z is not on {grp}'s preallocated axis")
        n_time = int(existing.size)
        if not overwrite and B.date_written(session, grp, g, ti):
            raise SystemExit(f"{date} {run}z already written in {grp}")
    else:
        if time_val in existing:
            raise SystemExit(f"{date} {run}z already in group {grp}")
        ti = int(tarr.shape[0])
        n_time = ti + 1
        tarr.resize((n_time,))
        tarr[ti] = time_val
        for name in list(g.array_keys()):
            if name not in B.COORD_NAMES and g[name].shape[0] != n_time:
                g[name].resize((n_time,) + g[name].shape[1:])

    n_set, new_arrays = 0, []
    for var, is_pl in [(v, False) for v in sfc_vars] + [(v, True) for v in pl_vars]:
        zname = B.channel_name(var, is_pl, pl_vars)
        path = f"{grp}/{zname}"
        full, chunks, dims = B.array_geometry(is_pl, n_time, len(steps),
                                              len(era["levels"]), era["ny"], era["nx"])
        if zname in g:
            assert g[zname].shape == full, f"{path}: shape {g[zname].shape} != {full}"
        else:
            new_arrays.append(zname)
            zarr.create_array(store, name=path, shape=full, chunks=chunks,
                              dtype="float32", fill_value=float("nan"),
                              serializer=GribberishCodec(var=zname),
                              compressors=None, filters=None, dimension_names=dims,
                              attributes={"grib_shortName": var}, overwrite=True)
        specs = B.ref_specs(refs, var, is_pl, ti, step_idx, lev_idx)
        rejected = store.set_virtual_refs(path, specs)
        assert not rejected, f"{path}: rejected refs {rejected}"
        n_set += len(specs)
    snap = session.commit(f"{grp} {date}: 51 members, {n_set} refs from .index "
                          f"on GCS (time index {ti})")
    return dict(grp=grp, ti=ti, n_set=n_set, snap=snap, new_arrays=new_arrays,
                sfc=sfc_vars, pl=pl_vars, created=created)


def parity(refs, pars_dir):
    """Do these refs equal what stage 1 + build_ecmwf_icechunk would write?"""
    p = B.load_date_refs(Path(pars_dir))
    k = ["step_h", "number", "var", "levtype", "level"]
    mine = refs.assign(key=refs.url.str.replace(GCS_PREFIX, "", regex=False))
    theirs = p.assign(key=p.url.str.replace(S3_PREFIX, "", regex=False))
    m = mine.merge(theirs, on=k, how="outer", suffixes=("", "_p"), indicator=True)
    both = m[m._merge == "both"]
    same = ((both.key == both.key_p) & (both.offset == both.offset_p)
            & (both.length == both.length_p))
    return dict(index=len(refs), pars=len(p), only_index=int((m._merge == "left_only").sum()),
                only_pars=int((m._merge == "right_only").sum()),
                differ=int((~same).sum()))


def verify(storage, era_name, run, date, refs, n, seed):
    """Read n chunks through the store and compare with a direct decode."""
    era = B.ERAS[era_name]
    auth = icechunk.containers_credentials({
        S3_PREFIX: icechunk.s3_anonymous_credentials(),
        GCS_PREFIX: icechunk.GcsCredentials.Anonymous()})
    repo = icechunk.Repository.open(storage, authorize_virtual_chunk_access=auth)
    g = zarr.open_group(store=repo.readonly_session("main").store,
                        path=f"{era_name}/{run}z", mode="r", zarr_format=3)
    ti = [int(t) for t in g["time"][:]].index(B.time_value(date, run))
    steps = [int(s) for s in g["step"][:]]
    levels = [float(v) for v in era["levels"]]
    pl_vars = sorted(refs.loc[refs.levtype == "pl", "var"].unique())
    rng = random.Random(seed)
    picks = [refs[(refs.levtype == lt) & (refs.number == m) & (refs.step_h == s)]
             for lt in ("sfc", "pl") for m in (0, 50) for s in (steps[0], steps[-1])]
    sample = pd.concat([c.iloc[[rng.randrange(len(c))]] for c in picks if len(c)]
                       + [refs.sample(max(0, n - len(picks)), random_state=seed)])
    ok, bad = 0, []
    for _, r in sample.iterrows():
        is_pl = r.levtype == "pl"
        a = g[B.channel_name(r["var"], is_pl, pl_vars)]
        si = steps.index(r.step_h)
        idx = (ti, int(r.number), si) + ((levels.index(r.level),) if is_pl else ())
        tag = f"{r['var']}{int(r.level) if is_pl else ''} m{int(r.number)} s{r.step_h}"
        try:
            got = np.asarray(a[idx])
            raw = get(GCS_HTTP + r.url[len(GCS_PREFIX):], (int(r.offset), int(r.length)))
            want = np.asarray(gribberish.parse_grib_message(raw, 0).data(),
                              dtype="float32").reshape(got.shape)
            same = np.array_equal(got, want, equal_nan=True)
        except Exception as e:
            same, tag = False, f"{tag}: {str(e).splitlines()[0][:70]}"
        ok += same
        if not same:
            bad.append(tag)
        print(f"      {tag:28s} {'equal' if same else 'DIFF'}")
    return ok, len(sample), bad


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True, help="YYYYMMDD")
    ap.add_argument("--run", default="00", choices=["00", "06", "12", "18"])
    ap.add_argument("--era", default=None, help="default: grids.era_for(date, run)")
    ap.add_argument("--resolution", default=None, choices=["0p25", "0p1"],
                    help="0p1: the parallel 0.1 deg stream (ifs/0p1, from 20261020 06z)")
    ap.add_argument("--store", required=True, help="local path | gs://bucket/prefix")
    ap.add_argument("--sa-key", default=None)
    ap.add_argument("--parity-pars", default=None,
                    help="stage-1 pars dir for this date: compare refs, then exit")
    ap.add_argument("--verify", type=int, default=24,
                    help="chunks to read back and compare after writing (0: skip)")
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--seed", type=int, default=1020)
    ap.add_argument("--overwrite-date", action="store_true")
    args = ap.parse_args()
    if args.date < GCS_FIRST_DATE:
        raise SystemExit(f"{args.date} predates gs://ecmwf-open-data ({GCS_FIRST_DATE})"
                         f" -- this date exists only on AWS")
    from grids import era_for
    era_name = args.era or era_for(args.date, args.run, args.resolution)
    control = "oper" if era_name in OPER_CONTROL_ERAS else "enfo"

    t0 = _time.time()
    refs, n_dupes, n_files = index_refs(era_name, args.date, args.run, control)
    t_idx = _time.time() - t0
    sfc, pl = B.split_levtypes(refs)
    print(f"{era_name}/{args.run}z {args.date}: {len(refs)} refs from "
          f"{n_files} .index "
          f"files on GCS in {t_idx:.1f}s -- {len(sfc)} sfc + {len(pl)} pl vars, "
          f"{refs.number.nunique()} members, control from {control}")
    if n_dupes:
        dup_vars = sorted(set(refs.loc[refs.levtype == "sol", "var"]))
        print(f"  {n_dupes} index lines share a key and were collapsed, last wins "
              f"(stage-1 behaviour; soil levels of {dup_vars})")
    # perturbed members must match each other; the control may carry more
    # (50r1 oper/fc has fields enfo does not, e.g. 15,133 vs 13,940 refs)
    per = refs.groupby("number").size()
    if refs.number.nunique() != B.N_MEMBERS or per.drop(0).nunique() != 1:
        raise SystemExit(f"members uneven: {per.value_counts().to_dict()}")

    if args.parity_pars:
        r = parity(refs, args.parity_pars)
        print(f"  parity vs stage-1 pars: {r}")
        return 0 if r["only_index"] == r["only_pars"] == r["differ"] == 0 else 1

    storage = B.resolve_storage(args.store, args.sa_key)
    if not args.verify_only:
        t0 = _time.time()
        out = write_date(open_or_create(storage), era_name, args.run, args.date,
                         refs, args.overwrite_date)
        print(f"  committed {out['n_set']} refs at {out['grp']}[time={out['ti']}] -> "
              f"{out['snap']} ({_time.time() - t0:.1f}s)"
              + (" -- new group" if out["created"] else ""))
        if out["new_arrays"] and not out["created"]:
            print(f"  arrays first seen on this date: {out['new_arrays']}")
    if args.verify:
        t0 = _time.time()
        ok, n, bad = verify(storage, era_name, args.run, args.date, refs,
                            args.verify, args.seed)
        print(f"  read-back: {ok}/{n} chunks equal their direct decode "
              f"({_time.time() - t0:.1f}s)" + (f" -- {bad[:3]}" if bad else ""))
        return 0 if ok == n else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
