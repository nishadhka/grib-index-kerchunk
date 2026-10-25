#!/bin/bash
# Wide box, full date: 46 channels x 51 members x 65 steps of 20261021 00z on
# the isolated frisky cluster (:8797, /tmp/frisky-wide), then manifest and
# member checks, then mirror to the new source.coop path. Run after
# run_trial.sh, with its workers still up.
#   bash run_full.sh > logs/full-20261021-00z.log 2>&1
set -u
D=$(cd "$(dirname "$0")" && pwd)
REFILL=/home/ubuntu/nishadhka-work/refill-crma-routine/medium-range-forecast
R=$REFILL/3-realize-frisky
P=$R/.venv/bin/python
SINK=wide-30w130e-50n40s/v0p1-20261021-00z
DEST=forecasts/ecmwf-ifs-0p1-icechunk-realized-30w130e-50n40s
VARS="tp tcwv msl sp ro t2m skt u10 v10 lsm u925 v925 u850 v850 u700 v700 u500 v500 u200 v200 \
      q925 q850 q700 q500 r850 r700 t850 t700 t500 gh500 vo850 vo700 vo500 d850 d700 d200 w700 cape d2m \
      cp mcc ssr tciw tclw tcrw tcw"
NOAWS="env -u AWS_ENDPOINT_URL -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN AWS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt"
cd $D

while pgrep -f 'run_2425.sh|3-realize-frisky/daily_run.sh' >/dev/null; do
  echo "=== CRMA daily run active, waiting $(date -u +%FT%TZ)"; sleep 300; done

# Fused read+write tasks (--fused-pool 4) at 6 per VM: 24 GETs/VM, ~1.8 GB of
# blocks/VM; measured 16 msg/s on d850 (vs 5.7 with separate read tasks).
timeout 400 /opt/mamba/envs/dask/bin/python deploy_workers.py --start --workers-per-vm 1 \
    --nthreads 6 --memory-limit 12GB 2>&1 | grep -E "alive"
$P mem_monitor.py --clear-stats 2>/dev/null
$P mem_monitor.py --every 15 > logs/mem-full.tsv 2>/dev/null &
MON=$!
echo "=== realize start $(date -u +%FT%TZ)"
t0=$(date +%s)
$NOAWS $P -u build_corpus.py --sink $SINK --era 0p1 --start 2026-10-21 --days 1 \
    --vars $VARS --members 51 --steps 65 --rounds 6 --pause 120 \
    --max-inflight 40 --fused-pool 4 \
    --env $REFILL/.env --scheduler 192.168.1.164:8797 ${EXTRA:-} 2>&1 | grep -v -E 'WARN|\.rs:'
rc=${PIPESTATUS[0]}
echo "=== realize exit=$rc wall=$(( $(date +%s)-t0 ))s $(date -u +%FT%TZ)"
sleep 20; kill $MON
$P mem_monitor.py --stats 2>/dev/null
echo "=== memory (MB): peak VM used / peak frisky worker RSS per VM"
awk -F'\t' 'NR==1{for(i=2;i<=NF;i++)h[i]=$i; next} {for(i=2;i<=NF;i++) if($i+0>m[i]) m[i]=$i+0}
            END{for(i=2;i<=NF;i++) printf "  %s %d\n", h[i], m[i]}' logs/mem-full.tsv
[ $rc = 0 ] || { echo "=== realize failed; not mirroring"; exit 1; }

echo "=== check_chunks $(date -u +%FT%TZ)"
$NOAWS timeout 1800 $P check_chunks.py --prefix $SINK --env $REFILL/.env 2>&1 | grep -v -E 'WARN|\.rs:|^\s*$' | tail -8
echo "=== check_members gh500 $(date -u +%FT%TZ)"
$NOAWS timeout 1800 $P check_members.py --prefix $SINK --var gh500 --env $REFILL/.env 2>&1 | grep -v -E 'WARN|\.rs:|^\s*$' | tail -8
$NOAWS $P count_sink.py $SINK 2>&1 | tail -2

echo "=== mirror -> s3://e4drr-project/$DEST $(date -u +%FT%TZ)"
t0=$(date +%s)
( set -a; . $R/.env.sourcecoop; set +a; eval "$(~/.local/bin/source-coop creds --format env)"
  AWS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt $P -u mirror_ceph_to_source_coop.py \
      --src-prefix $SINK --dest $DEST --env $REFILL/.env ) 2>&1 | grep -v -E '^\s+retry|WARN'
echo "=== mirror exit=${PIPESTATUS[0]} wall=$(( $(date +%s)-t0 ))s $(date -u +%FT%TZ)"
