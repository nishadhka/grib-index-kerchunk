"""One way to name and open every store in this workflow.

A spec is a string:
    /abs/path, local:/abs/path   directory on this machine
    ceph:<prefix>                EWC Ceph, bucket must-icechunk (AK/SK from --env)
    sourcecoop:<prefix>          source.coop, bucket e4drr-project, read anonymous

Writing to source.coop is never done through icechunk here: stores are built
locally or on Ceph and COPIED up (publish_*.py), `repo` pointer last.
"""
from __future__ import annotations

import os

SC_ENDPOINT = "https://data.source.coop"
SC_BUCKET = "e4drr-project"
CEPH_ENDPOINT = "https://object-store.os-api.cci1.ecmwf.int"
CEPH_BUCKET = "must-icechunk"
S3_CONTAINER = "s3://ecmwf-forecasts/"
GCS_CONTAINER = "gs://ecmwf-open-data/"


def parse(spec: str):
    if spec.startswith("ceph:"):
        return "ceph", spec[5:].strip("/")
    if spec.startswith("sourcecoop:"):
        return "sourcecoop", spec[11:].strip("/")
    if spec.startswith("local:"):
        return "local", spec[6:]
    if spec.startswith("/"):
        return "local", spec
    raise SystemExit(f"store spec {spec!r}: use /path, local:/path, ceph:<prefix> "
                     f"or sourcecoop:<prefix>")


def load_env(path):
    """AK/SK for Ceph from a gitignored KEY=VALUE file. Values never printed."""
    creds = {}
    with open(os.path.expanduser(path)) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                creds[k.strip().replace("export ", "")] = v.strip().strip('"\'')
    missing = [k for k in ("AK", "SK") if k not in creds]
    if missing:
        raise SystemExit(f"{path} is missing {missing}")
    return creds["AK"], creds["SK"]


def storage(spec: str, env_file: str | None = None):
    import icechunk as ic
    kind, where = parse(spec)
    if kind == "local":
        return ic.local_filesystem_storage(where)
    if kind == "sourcecoop":
        return ic.s3_storage(bucket=SC_BUCKET, prefix=where, endpoint_url=SC_ENDPOINT,
                             region="us-east-1", anonymous=True, from_env=False,
                             force_path_style=True)
    if not env_file:
        raise SystemExit("a ceph: store needs --env pointing at the AK/SK file")
    ak, sk = load_env(env_file)
    return ic.s3_storage(bucket=CEPH_BUCKET, prefix=where, region="RegionOne",
                         endpoint_url=CEPH_ENDPOINT, access_key_id=ak,
                         secret_access_key=sk, force_path_style=True, from_env=False)


def open_virtual(spec: str):
    """Repository of the virtual store, chunk containers authorised anonymously.

    Manifest preload off: source.coop returns sporadic 5xx, and an eager
    preload turns one of those into a failed open.
    """
    import icechunk as ic
    auth = ic.containers_credentials({S3_CONTAINER: ic.s3_anonymous_credentials(),
                                      GCS_CONTAINER: ic.GcsCredentials.Anonymous()})
    cfg = ic.RepositoryConfig.default()
    cfg.manifest = ic.ManifestConfig(preload=ic.ManifestPreloadConfig(
        max_total_refs=0, max_arrays_to_scan=0))
    return ic.Repository.open(storage(spec), config=cfg,
                              authorize_virtual_chunk_access=auth)


def open_virtual_dataset(spec: str, run: str = "00", era: str = "0p1"):
    """(xarray Dataset of group <era>/<run>z, snapshot id). Lazy, numpy-backed."""
    import xarray as xr
    import gribberish.zarr  # noqa: F401 -- registers the Zarr v3 codec
    sess = open_virtual(spec).readonly_session("main")
    ds = xr.open_zarr(sess.store, group=f"{era}/{run}z", consolidated=False,
                      zarr_format=3, decode_timedelta=True, chunks=None)
    return ds, sess.snapshot_id


def open_realized(spec: str, env_file: str | None = None, create: bool = False):
    """-> (Repository, created?)."""
    import icechunk as ic
    st = storage(spec, env_file)
    if ic.Repository.exists(st):
        return ic.Repository.open(st), False
    if not create:
        raise SystemExit(f"no realized store at {spec}")
    if parse(spec)[0] == "sourcecoop":
        raise SystemExit("source.coop is read-only here: realize to local or ceph, "
                         "then publish")
    return ic.Repository.create(st), True


def open_realized_dataset(spec: str, env_file: str | None = None):
    import xarray as xr
    repo, _ = open_realized(spec, env_file)
    sess = repo.readonly_session("main")
    return xr.open_zarr(sess.store, consolidated=False, zarr_format=3,
                        decode_timedelta=True), sess.snapshot_id
