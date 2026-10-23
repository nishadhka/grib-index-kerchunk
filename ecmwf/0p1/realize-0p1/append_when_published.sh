#!/bin/bash
# Wait for a 0.1 deg cycle to be published on GCS, append it to the local
# virtual store, then upload the store to source.coop. One cycle per argument.
#   nohup bash append_when_published.sh 2026102312 2026102318 > logs/append-....log 2>&1 &
set -u
D=$(cd "$(dirname "$0")" && pwd)
S=/home/ubuntu/icechunk-local/ecmwf-ifs-0p1-icechunk-vd
R=/home/ubuntu/nishadhka-work/refill-crma-routine/medium-range-forecast/3-realize-frisky
G=https://storage.googleapis.com/ecmwf-open-data
for c in "$@"; do
  d=${c:0:8}; r=${c:8:2}; last=$([ $r = 00 -o $r = 12 ] && echo 360 || echo 144)
  echo "=== $c waiting $(date -u +%FT%TZ)"
  for i in $(seq 1 120); do          # up to 10 h, every 5 min
    n=0
    for f in enfo/${d}${r}0000-${last}h-enfo-ef-pl enfo/${d}${r}0000-${last}h-enfo-ef-sfc \
             oper/${d}${r}0000-${last}h-oper-fc-pl oper/${d}${r}0000-${last}h-oper-fc-sfc; do
      [ "$(curl -s -o /dev/null -w '%{http_code}' -I $G/$d/${r}z/ifs/0p1/$f.index)" = 200 ] && n=$((n+1))
    done
    [ $n = 4 ] && break
    sleep 300
  done
  [ $n = 4 ] || { echo "=== $c NOT PUBLISHED after 10 h"; continue; }
  echo "=== $c published $(date -u +%FT%TZ)"
  t0=$(date +%s)
  (cd $D/../../gcs-source && uv run -q index_to_icechunk.py --date $d --run $r --resolution 0p1 \
      --store $S --verify 12) 2>&1 | grep -v -E 'WARN|\.rs:|^\s*$|  equal$'
  rc=${PIPESTATUS[0]}
  echo "=== $c build exit=$rc wall=$(( $(date +%s)-t0 ))s"
  [ $rc = 0 ] || continue
  ( set -a; . $R/.env.sourcecoop; set +a; eval "$(~/.local/bin/source-coop creds --format env)"
    $R/.venv/bin/python -u $D/mirror_local_to_source_coop.py --src $S \
        --dest forecasts/ecmwf-ifs-0p1-icechunk-vd --threads 12 ) 2>&1 | grep -v '^  retry'
  echo "=== $c done $(date -u +%FT%TZ)"
done
