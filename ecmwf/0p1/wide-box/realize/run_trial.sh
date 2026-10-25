#!/bin/bash
# Wide-box trial: wait for the CRMA daily run to finish, then realize
# 2 channels x 51 members x 65 steps of 20261021 00z on an isolated frisky
# scheduler (:8797) and workers (/tmp/frisky-wide), sampling VM memory.
#   bash run_trial.sh > logs/trial-<stamp>.log 2>&1
set -u
D=$(cd "$(dirname "$0")" && pwd)
REFILL=/home/ubuntu/nishadhka-work/refill-crma-routine/medium-range-forecast
P=$REFILL/3-realize-frisky/.venv/bin/python
SINK=${SINK:-wide-30w130e-50n40s/trial-2ch-20261021-00z}
VARS=${VARS:-"t2m d850"}
STAMP=${STAMP:-trial}
cd $D

echo "=== waiting for the CRMA daily run to finish $(date -u +%FT%TZ)"
while pgrep -f 'run_2425.sh|3-realize-frisky/daily_run.sh' >/dev/null; do sleep 60; done
echo "=== cluster free $(date -u +%FT%TZ)"

timeout 400 /opt/mamba/envs/dask/bin/python deploy_workers.py --scheduler-on 192.168.1.164 \
    --start --workers-per-vm 1 --nthreads 4 --memory-limit 12GB 2>&1 | grep -E 'dask-worker|scheduler|readers'
$P mem_monitor.py --clear-stats 2>/dev/null
$P mem_monitor.py --every 15 > logs/mem-$STAMP.tsv 2>/dev/null &
MON=$!

t0=$(date +%s)
env -u AWS_ENDPOINT_URL -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
    AWS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
  $P -u build_corpus.py --sink $SINK --era 0p1 --start 2026-10-21 --days 1 \
    --vars $VARS --members 51 --steps 65 --rounds 6 --pause 120 \
    --env $REFILL/.env --scheduler 192.168.1.164:8797 ${EXTRA:-} 2>&1 | grep -v -E 'WARN|\.rs:'
rc=${PIPESTATUS[0]}
echo "=== build exit=$rc wall=$(( $(date +%s)-t0 ))s $(date -u +%FT%TZ)"
sleep 20
kill $MON
$P mem_monitor.py --stats 2>/dev/null
echo "=== memory (MB): peak VM used / peak frisky worker RSS per VM"
awk -F'\t' 'NR==1{for(i=2;i<=NF;i++)h[i]=$i; next} {for(i=2;i<=NF;i++) if($i+0>m[i]) m[i]=$i+0}
            END{for(i=2;i<=NF;i++) printf "  %s %d\n", h[i], m[i]}' logs/mem-$STAMP.tsv
env -u AWS_ENDPOINT_URL -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
    AWS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
  $P count_sink.py $SINK 2>&1 | tail -3
