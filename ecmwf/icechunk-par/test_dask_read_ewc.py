# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "icechunk>=2.1", "zarr>=3.2", "xarray>=2025.1", "dask[distributed]",
#   "pandas", "gribberish>=1.4", "psutil",
# ]
# ///
"""Dask read test for a GIK -> Icechunk store on the EWC cluster.

Companion to test_dask_read.py, which uses a LocalCluster. This variant targets
the *distributed* cluster on ECMWF's European Weather Cloud, where workers are
separate VMs:

    gateway (JupyterHub + scheduler, 192.168.1.129:8786)
      -> dask-worker-01..06  (4 vCPU / 16 GB each)

Two things differ from the LocalCluster case, and both are the point of this test:

  1. PEP 723 / `uv run` does NOT provision the workers. On a LocalCluster the
     script's own interpreter is the worker, so inline dependencies suffice. Here
     each worker VM has a pre-built environment (micromamba, from
     cloud-init-dask-worker.yaml). If that environment drifts from the client's,
     failures appear as opaque deserialization errors rather than ImportError -
     hence the explicit version-parity check below.

  2. Virtual chunk fetches leave the VM. The upstream GRIB (s3://ecmwf-forecasts)
     is read from each worker directly, so throughput reflects EWC egress rather
     than a local disk cache.

Usage, from a JupyterHub notebook terminal or the gateway:

    python test_dask_read_ewc.py --store s3://e4drr-project/forecasts/... --steps 3

    # write probe against the EWC object store (Ceph RGW):
    source /etc/dask/s3.env && python test_dask_read_ewc.py --probe-write

Exit code 0 = all PASS, 1 = any FAIL.
"""
import argparse
import os
import time

import numpy as np

CONTAINER_PREFIX = "s3://ecmwf-forecasts/"
EWC_ENDPOINT = "https://object-store.os-api.cci1.ecmwf.int"
EWC_REGION = "RegionOne"
FAILURES = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def resolve_storage(store: str):
    import icechunk
    if store.startswith("s3://"):
        bucket, _, prefix = store[5:].partition("/")
        anon = "AWS_ACCESS_KEY_ID" not in os.environ
        return icechunk.s3_storage(
            bucket=bucket, prefix=prefix.rstrip("/"),
            region=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
            endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),
            anonymous=anon, from_env=not anon, force_path_style=True)
    return icechunk.local_filesystem_storage(store)


def local_versions():
    """Version fingerprint of the packages that must match across the cluster."""
    import importlib.metadata as md
    out = {}
    for pkg in ("dask", "distributed", "zarr", "xarray", "numpy", "icechunk",
                "gribberish"):
        try:
            out[pkg] = md.version(pkg)
        except md.PackageNotFoundError:
            out[pkg] = "MISSING"
    return out


def worker_peak_rss_mb():
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def probe_write(bucket, prefix):
    """Does Icechunk's commit protocol work on this S3 implementation?

    Icechunk relies on conditional writes (If-None-Match) to make concurrent
    commits safe. AWS S3 supports them; Ceph RadosGW support is version
    dependent. Verify before building a pipeline on it - a store that cannot
    commit safely is worse than no store.
    """
    import icechunk as ic
    import zarr

    print(f"\nwrite probe -> s3://{bucket}/{prefix}  ({EWC_ENDPOINT})")
    storage = ic.s3_storage(
        bucket=bucket, prefix=prefix,
        region=os.environ.get("AWS_DEFAULT_REGION", EWC_REGION),
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL", EWC_ENDPOINT),
        from_env=True, force_path_style=True)

    repo = ic.Repository.open_or_create(storage)
    check("repository create/open on EWC object store", True)

    session = repo.writable_session("main")
    root = zarr.create_group(store=session.store, overwrite=True)
    arr = root.create_array("probe", shape=(64, 64), chunks=(32, 32), dtype="f4")
    arr[:] = np.arange(64 * 64, dtype="f4").reshape(64, 64)
    snap = session.commit("write probe")
    check("commit succeeded (conditional write supported)", bool(snap), f"snapshot {snap}")

    ro = repo.readonly_session("main")
    back = zarr.open_group(store=ro.store, mode="r")["probe"][:]
    check("round-trip readback matches", bool(np.allclose(back[0, :4], [0, 1, 2, 3])))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", help="icechunk store (s3://... or local path)")
    ap.add_argument("--steps", type=int, default=3, help="lead times to include")
    ap.add_argument("--scheduler", default=os.environ.get(
        "DASK_SCHEDULER_ADDRESS", "tcp://127.0.0.1:8786"))
    ap.add_argument("--probe-write", action="store_true",
                    help="test icechunk commits against the EWC object store")
    ap.add_argument("--bucket", default="must-icechunk")
    ap.add_argument("--prefix", default="probe/conditional-write")
    args = ap.parse_args()

    from dask.distributed import Client
    client = Client(args.scheduler)
    info = client.scheduler_info()["workers"]
    print(f"cluster: {client}\n"
          f"workers: {len(info)}  threads: {sum(w['nthreads'] for w in info.values())}")
    check("workers registered", len(info) > 0, f"{len(info)} workers")

    # --- version parity ------------------------------------------------------
    # The failure this guards against is silent: mismatched dask/zarr versions
    # surface as unpickling errors deep in a task, not as a clear message.
    mine = local_versions()
    theirs = client.run(local_versions)
    print("\nclient versions: " + ", ".join(f"{k}={v}" for k, v in mine.items()))
    mismatched = {
        w: {k: (mine[k], v[k]) for k in mine if mine[k] != v[k]}
        for w, v in theirs.items()
    }
    bad = {w: d for w, d in mismatched.items() if d}
    check("client/worker versions identical", not bad,
          "" if not bad else f"{len(bad)} worker(s) differ: {list(bad.values())[0]}")

    missing = {w: [k for k, val in v.items() if val == "MISSING"]
               for w, v in theirs.items()}
    missing = {w: m for w, m in missing.items() if m}
    check("all required packages present on workers", not missing,
          "" if not missing else str(list(missing.values())[0]))

    if args.probe_write:
        probe_write(args.bucket, args.prefix)

    if args.store:
        import icechunk
        import xarray as xr
        import gribberish.zarr  # noqa: F401 -- registers the Zarr v3 codec

        auth = icechunk.containers_credentials(
            {CONTAINER_PREFIX: icechunk.s3_anonymous_credentials()})
        repo = icechunk.Repository.open(resolve_storage(args.store),
                                        authorize_virtual_chunk_access=auth)
        ds = xr.open_zarr(repo.readonly_session("main").store,
                          consolidated=False, zarr_format=3, chunks={})
        sub = ds["t2m"].isel(step=slice(0, args.steps))
        n_chunks = int(np.prod([sub.sizes[d] for d in ("time", "number", "step")]))
        print(f"\nt2m subset {dict(sub.sizes)} -> {n_chunks} virtual chunks "
              f"({n_chunks * 451 * 900 * 4 / 1e6:.0f} MB decoded)")
        check("dataset opens lazily as dask arrays", sub.chunks is not None,
              f"dask chunksize {sub.data.chunksize}")

        t0 = time.time()
        emean_v, estd_v = client.compute([sub.mean("number"), sub.std("number")],
                                         sync=True)
        wall = time.time() - t0
        print(f"ensemble mean+std computed in {wall:.1f}s "
              f"({n_chunks / wall:.0f} chunks/s incl. S3 fetch + GRIB decode)")

        check("results finite", bool(np.isfinite(emean_v.values).all()
                                     and np.isfinite(estd_v.values).all()))
        check("ens-mean t2m plausible (180-340 K)",
              180 < float(emean_v.mean()) < 340,
              f"mean={float(emean_v.mean()):.2f} K")
        check("ens spread positive", float(estd_v.mean()) > 0,
              f"mean std={float(estd_v.mean()):.3f} K")

        peaks = client.run(worker_peak_rss_mb)
        print("  per-worker peak RSS (MB): "
              + ", ".join(f"{v:.0f}" for v in peaks.values()))
        # Workers are 16 GB VMs; dask reserves ~2 GB for the OS.
        check("worker peak RSS bounded", max(peaks.values()) < 12_000,
              f"max {max(peaks.values()):.0f} MB")
        check("all workers alive (no OOM kills/restarts)",
              len(client.scheduler_info()["workers"]) == len(info))

    client.close()
    print(f"\n{'ALL TESTS PASSED' if not FAILURES else 'FAILED: ' + ', '.join(FAILURES)}")
    raise SystemExit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
