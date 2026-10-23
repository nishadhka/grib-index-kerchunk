"""Local Icechunk store (directory) -> source.coop, the way the other mirrors do it.

    set -a; . ../.env.sourcecoop; set +a; eval "$(source-coop creds --format env)"
    python mirror_local_to_source_coop.py --src /home/ubuntu/icechunk-local/ecmwf-ifs-0p1-icechunk-vd \
        --dest forecasts/ecmwf-ifs-0p1-icechunk-vd

Same rules as mirror_ceph_to_source_coop.py: plain object copies (boto3
put_object), `overwritten/` skipped, the top-level `repo` pointer uploaded LAST
so a reader never sees a pointer to objects that are not there yet. Everything
except `repo` is immutable in icechunk, so an object already present at the
same size is skipped. `repo` is always uploaded: it changes on every commit and
can keep its size.
"""
import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
from botocore.config import Config


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="local icechunk store directory")
    ap.add_argument("--dest", required=True, help="prefix in the bucket")
    ap.add_argument("--bucket", default="e4drr-project")
    ap.add_argument("--threads", type=int, default=24)
    args = ap.parse_args()

    src = Path(args.src)
    files = [p for p in src.rglob("*") if p.is_file()
             and p.relative_to(src).parts[0] != "overwritten"]
    rels = {p.relative_to(src).as_posix(): p for p in files}
    if "repo" not in rels:
        raise SystemExit(f"{src}: no `repo` object -- not an icechunk store")
    sc = boto3.client("s3", endpoint_url=os.environ["AWS_ENDPOINT_URL"],
                      region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
                      config=Config(s3={"addressing_style": "path"},
                                    max_pool_connections=args.threads * 2,
                                    retries={"max_attempts": 10, "mode": "adaptive"}))
    have = {}
    for page in sc.get_paginator("list_objects_v2").paginate(
            Bucket=args.bucket, Prefix=args.dest.rstrip("/") + "/"):
        for o in page.get("Contents", []):
            have[o["Key"]] = o["Size"]
    todo = [(r, p) for r, p in rels.items() if r != "repo"
            and have.get(f"{args.dest}/{r}") != p.stat().st_size]
    nbytes = sum(p.stat().st_size for _, p in todo)
    print(f"{len(rels)} objects local, {len(have)} at dest, uploading {len(todo)} "
          f"({nbytes / 1e6:.1f} MB) + repo")

    def one(item):
        # source.coop sits behind Cloudflare: a 5xx (seen: 520) is transient
        r, p = item
        for attempt in range(8):
            try:
                return sc.put_object(Bucket=args.bucket, Key=f"{args.dest}/{r}",
                                     Body=p.read_bytes())
            except Exception as e:
                if attempt == 7:
                    raise
                print(f"  retry {attempt + 1} {r}: {str(e)[:80]}", flush=True)
                time.sleep(2 ** attempt)

    t0 = time.time()
    with ThreadPoolExecutor(args.threads) as ex:
        list(ex.map(one, todo))
    one(("repo", rels["repo"]))          # LAST
    dt = time.time() - t0
    print(f"uploaded {len(todo) + 1} objects, {nbytes / 1e6:.1f} MB in {dt:.1f}s; "
          f"`repo` pointer last -- MIRROR COMPLETE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
