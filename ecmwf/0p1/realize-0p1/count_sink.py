"""Chunks written so far under a Ceph sink prefix (progress of a running build).

    AWS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt python count_sink.py ea-swio/v0p1-20261021-00z
"""
import os, sys, boto3

ENV = "/home/ubuntu/nishadhka-work/refill-crma-routine/medium-range-forecast/.env"
env = {}
for line in open(ENV):
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env[k.replace("export ", "").strip()] = v.strip().strip("'\"")
s3 = boto3.client("s3", endpoint_url="https://object-store.os-api.cci1.ecmwf.int",
                  aws_access_key_id=env["AK"], aws_secret_access_key=env["SK"],
                  region_name="RegionOne")
n = b = 0
for p in s3.get_paginator("list_objects_v2").paginate(Bucket="must-icechunk",
                                                      Prefix=sys.argv[1].rstrip("/") + "/"):
    for o in p.get("Contents", []):
        b += o["Size"]
        n += o["Key"].split("/")[2] == "chunks"
print(f"chunks {n}/2535 ({100 * n / 2535:.0f}%)  {b / 1e9:.1f} GB")
