# ECMWF ENS at 0.1°: virtual store, realization, publication (first run, 2026-10-23)

The first end-to-end run of the 0.1° IFS ENS stream (`ifs/0p1/`, published
from 20261020 06z; survey in [`../README.md`](../README.md)) through the same
chain the 0.25° daily routine uses:

```
.index on gs://ecmwf-open-data  ->  virtual Icechunk store (no pars, no template)
   -> source.coop  forecasts/ecmwf-ifs-0p1-icechunk-vd
   -> frisky realization, CRMA's 39 channels, ea-sw-io40 box at 0.1°
   -> EWC Ceph  must-icechunk/ea-swio/v0p1-20261021-00z
   -> source.coop  forecasts/ecmwf-ifs-0p1-icechunk-realized
```

Run on the EWC gateway (`jupyterhub-gateway`) against the frisky cluster that
the 0.25° daily routine uses. The daily routine was idle while it ran.

| Published | URL |
|---|---|
| virtual store | https://source.coop/e4drr-project/forecasts/ecmwf-ifs-0p1-icechunk-vd |
| realized 20261021 00z | https://source.coop/e4drr-project/forecasts/ecmwf-ifs-0p1-icechunk-realized |

## 1. Virtual store: 13 cycles, 20261020 06z → 20261023 06z

Built with [`../../gcs-source/index_to_icechunk.py`](../../gcs-source/index_to_icechunk.py)
`--resolution 0p1`. That script reads only the `.index` files. At 0.1° every
step is two files, `-pl` and `-sfc`, each with its own `.index`, so a run reads
twice as many indexes as at 0.25°. The control comes from `oper/fc`, as in
50r1. Groups are `0p1/{00,06,12,18}z` with dates appended on `time`.

```bash
cd ecmwf/gcs-source
uv run index_to_icechunk.py --date 20261021 --run 00 --resolution 0p1 \
    --store /home/ubuntu/icechunk-local/ecmwf-ifs-0p1-icechunk-vd --verify 12
```

| cycle | refs | `.index` files | read indexes | commit | wall incl. 12-chunk read-back |
|---|---|---|---|---|---|
| 00z / 12z (85 steps) | 742,478 | 340 | 23–25 s | 5.5–6.1 s | 51–55 s |
| 06z / 18z (49 steps) | 428,018 | 196 | 14 s | 3.3–3.5 s | 41–44 s |
| 20261020 06z (first 0.1° cycle) | 423,020 | 196 | 10.6 s | 3.3 s | 37 s |

- **Read-back:** 156 of 156 chunks across the 13 cycles equal a direct decode of the message.
- **Size and content:** 13 cycles take 164 MB and 847 objects (metadata and manifests only; the GRIB stays on GCS). There are 51 members and 14 pressure levels: 51 surface + 10 pressure-level variables in 00z/12z, 49 + 10 in 06z/18z.
- **First cycle:** 20261020 06z has no soil fields (`sot`, `vsw`). Every later cycle has them. None of the 39 CRMA channels uses them.
- **Upload:** done by [`mirror_local_to_source_coop.py`](mirror_local_to_source_coop.py), using boto3 copies with the `repo` pointer last.
  - The 170 MB took 1.4 min plus 1.1 min, because a source.coop **520** stopped the first pass (see issue 1).
  - Read back anonymously from source.coop: 4 groups, 13 dates, 14 snapshots.
- **20261023 12z and 18z** were not published by the time of the run; 0.1° appears about 2 h after 0.25°. [`append_when_published.sh`](append_when_published.sh) `2026102312 2026102318` was started at 22:01 UTC. For each cycle it waits for publication (checking every 5 min, up to 10 h), appends it with the 12-chunk read-back and re-uploads. Log: `logs/append-2026102312-18.log`.
  - 12z was published at 01:06 UTC on 10-24 and appended and uploaded a minute later.
  - 18z's last step appeared at 04:43 UTC, but one of its other `.index` files still returned 404 then. The build exited before committing anything. A rerun on 2026-10-25 at 12:00 UTC appended it (read-back 12/12) and uploaded 63 objects.
  - The store now holds all **15** requested cycles.

## 2. Realization of 20261021 00z, 39 channels

**The code is in this folder.** It is a copy of the live
`refill-crma-routine/.../3-realize-frisky` (`bc62385`), changed only in
`frisky_daily_dag.py`:

| change | why |
|---|---|
| `SRC_PREFIX = "forecasts/ecmwf-ifs-0p1-icechunk-vd"` | a constant, because the module is shipped to the worker VMs, where this machine's env is absent |
| `_GRID["0p1"] = (0.1, 15.0)` | box 40°N → 40°S, 15°E → 80°E at 0.1° is **801 × 651** (0.25°: 321 × 261) |
| `is_convective_era` includes `0p1`, `--era 0p1` allowed | the era has `cape`/`w700`/`d2m` |
| retry on source.coop 5xx (`no root element`, 520/502/504) | issue 2 |

**The daily routine's own copy is untouched.** Its RULE 1 restart before stage ③
redeploys its module to the workers.

```bash
cd ecmwf/0p1/realize-0p1
timeout 400 /opt/mamba/envs/dask/bin/python deploy_workers.py --scheduler-on 192.168.1.164 \
    --start --workers-per-vm 1 --nthreads 4 --memory-limit 12GB
VARS="tp tcwv msl sp ro t2m skt u10 v10 lsm u925 v925 u850 v850 u700 v700 u500 v500 u200 v200 \
      q925 q850 q700 q500 r850 r700 t850 t700 t500 gh500 vo850 vo700 vo500 d850 d700 d200 w700 cape d2m"
env -u AWS_ENDPOINT_URL -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
    AWS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
  $REFILL/3-realize-frisky/.venv/bin/python -u build_corpus.py --sink ea-swio/v0p1-20261021-00z \
    --era 0p1 --start 2026-10-21 --days 1 --vars $VARS --members 51 --steps 65 \
    --rounds 6 --pause 120 --env $REFILL/.env --scheduler 192.168.1.164:8796
```

### Measured (log: `logs/realize-20261021-00z-20261023T1953.log`)

| | **0.1°, this run** | 0.25°, same date (daily run, `crma/RUNBOOK_V4_20261021.md`) | ratio |
|---|---|---|---|
| work | 39 ch × 51 members × 65 steps = 2,535 blocks, 129,285 messages | the same | 1 |
| readers | **20** (5 VMs × 4; `dask-worker-03` down) | 24 | 0.83 |
| stage ③ wall | **113.2 min** (6,792.7 s; 19:53 → 21:46 UTC) | 51.0 min | **2.2×** |
| messages/s | 19.0 | ~42 | 0.45 |
| per reader | 0.95 msg/s | ~1.8 msg/s | |
| block (1, 1, 51, lat, lon) raw | 106.4 MB (51 × 801 × 651 × 4 B) | 17.1 MB | 6.2× |
| stored on Ceph | **65.0 GB** (~25.6 MB/block) | ~17.2 GB | **3.8×** |
| retried blocks | 8 (round 2, all recovered) | 15 on 2026-10-15 | |
| mirror ③ to source.coop | **8.6 min**, 2,588 objects, 65.02 GB at 126.1 MB/s (454 GB/h), 0 errors | 5.3 min | 1.6× |

- **Bytes read** were about 2 MB per message, so ~260 GB from GCS in 113 min, about 38 MB/s **[est]**. The `~0.10 TB of AWS egress` that `build_corpus.py` prints is its 0.25° constant, not a measurement.
- **Wall time grew less than the data did (2.2× against ~6×).**
  - A 0.1° message takes ~95 ms to decode, against ~16 ms at 0.25°, plus a 4–5× larger fetch.
  - So per-reader throughput halved: 0.95 against ~1.8 msg/s.
  - The 0.25° path is bounded by AWS/GCS request rate and small reads. At 0.1° the reads are larger and the readers spend more of their time decoding.
- **Readers:** 24 readers on a full cluster would make it ~95 min **[est]**. The AWS throttle that set the 24-reader rule (`../../ewc-realization/05_BOTTLENECKS_AND_ISSUES.md`) is not in this path: the refs point at `gs://`. More readers per VM is the obvious next test.
- **Fit in the daily routine:** stage ③ would grow from ~51 to ~113 min, and mirror ③ from ~5 to ~9 min. That adds about 1¼ h a day, and the run would also have to wait for 0.1°, which publishes ~2 h after 0.25°.
- **Whole chain for one date:**
  - virtual store append: ~1 min;
  - upload: ~1 min;
  - stage ③: 113 min;
  - verify: ~5 min;
  - mirror ③: 9 min;
  - **about 2 h 10 min**.

### Verification

| check | result |
|---|---|
| manifest (`check_chunks.py`) | all 39 channels 65/65 chunks, **total missing 0**, `VERDICT: complete` |
| members (`check_members.py --var gh500`) | spread grows 1.44 → 21.49 (0 → 240 h), finite 1.0000, `members are REAL` |
| independent decode | raw GRIB from `gs://` decoded by gribberish, box cut by grid index (rows 500–1300, cols 1950–2600). Compared with the realized store for `tp` m15 0 h (a retried block), `tp` m15 240 h, `t2m` control 24 h, `u850` m33 120 h, `d2m` m50 6 h: **max \|diff\| 0, no NaN** |
| coordinates | lat 40.0 → −40.0 (801), lon 15.0 → 80.0 (651) |
| published copy, anonymous | opens from `data.source.coop` in 4.3 s at snapshot `NX879XCFJ3WK`; `tp` 0 h and 240 h, `t2m` 24 h, `cape` 240 h, `d2m` 0 h all finite over 51 members. One 106 MB block takes 2–12 s, but one read took 45 s |

`check_geolocation.py` (`../../ewc-realization/`) did not run: its helper
`inspect_store.py` imports `dotenv`, which the refill venv lacks. The index-cut
comparison above covers the same question.

### 2b. The cGAN's 14 fields: 7 channels appended

**Which fields were missing.** The cGAN takes 14 forecast fields
(`cGAN_tutorial/RFE2_cGAN_RUNBOOK.md` §1, `all_fcst_fields`):
`cape cp mcc sp ssr t2m tciw tclw tcrw tcw tcwv tp u700 v700`. Seven are
already among CRMA's 39 (`cape sp t2m tcwv tp u700 v700`). The other seven are:

| field | meaning |
|---|---|
| `cp` | convective precipitation, accumulated |
| `mcc` | medium cloud cover |
| `ssr` | surface net solar radiation, accumulated |
| `tciw` | total column cloud ice water |
| `tclw` | total column cloud liquid water |
| `tcrw` | total column rain water |
| `tcw` | total column water |

All seven are in the 0.1° `-sfc` files. Five of them (`cp mcc tciw tclw tcrw`)
appear in the 0.25° stream only from 20261020 06z.

**How they were added.** [`append_channels.py`](append_channels.py) is a copy
of the production script. It does **not** rebuild the store:
- it adds the new arrays, metadata only, with `mode="a"` (never `"w"`);
- it then realizes only those channels, then re-mirrors.

`NEW_CHANNELS` is set to the seven fields, and era `0p1` is allowed.

```bash
env -u AWS_ENDPOINT_URL -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
    AWS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
  $REFILL/3-realize-frisky/.venv/bin/python -u append_channels.py --sink ea-swio/v0p1-20261021-00z \
    --era 0p1 --start 2026-10-21 --days 1 --env $REFILL/.env --rounds 6 --pause 120
```

| | measured |
|---|---|
| work | 7 ch × 51 members × 65 steps = 23,205 messages (18% of the 39-channel run) |
| append | **18.3 min** (1,099.7 s), 21.1 msg/s on 20 readers, 0 retries |
| added on Ceph | **8.4 GB** (65.0 → 73.4 GB). These fields compress better than the 39 average: ~1.2 GB/channel against ~1.7 |
| re-mirror | 474 new objects, 8.39 GB in 103 s (81.7 MB/s), 0 errors |
| manifest | 46 channels × 65/65 chunks, total missing 0 |
| independent decode | `cp` m4 72 h, `mcc` control 24 h, `ssr` m50 240 h, `tciw` m12 120 h, `tclw` m27 6 h, `tcrw` m33 48 h, `tcw` m45 0 h: **max \|diff\| 0, no NaN**. `t2m` re-checked: unchanged |
| published | snapshot `DC0D89EDAFQ83ARYA110`, 46 variables; all 14 cGAN fields present, read anonymously |

**What it costs.** A date with all 46 channels (39 + 7) takes about 113 + 18 =
**~131 min** of realization and **73.4 GB** stored at 0.1°. Building the 46 in
one pass would save the second schema commit and pass, but the read cost is the
same.

**What the cGAN still needs.** These are raw fields over the realized box.
- The cGAN takes ensemble **mean and sd** per field, over **24 h windows** (`accumulated_fields = ['cp','ssr','tp']`). That is the cGAN's own preprocessing, done downstream of this store.
- `cape` is `mucape` from 2025 on (`frisky_daily_dag._resolve_var`). The cGAN's training archive should be checked for which of the two it used.

## 3. Issues met

1. **source.coop 520 on upload.** One `PutObject` returned HTTP 520 (Cloudflare, transient), and the uploader stopped. It now retries each object 8 times with backoff. The second pass skipped what was already there.
2. **source.coop empty-body 5xx on read.** One `read_message` failed with `error parsing XML: no root element` while fetching a manifest that was present (HTTP 200, full size, on retry). The daily copy retries only `SlowDown`/503, so this would fail a task. This copy also retries these.
3. **Worker network errors during stage ③.** The worker logs show GCS connect timeouts (`storage.googleapis.com … operation timed out`) and `minimum throughput … 0 B/s` stalls. 8 blocks failed with `error fetching virtual reference` (e.g. `tp`, member 15, step 0). `build_corpus.py`'s round 2 re-read all of them, and the independent decode shows them exact.
4. **2 orphan chunks.** Ceph holds 2,537 chunk objects for 2,535 blocks. They are the first attempts of retried writes, not referenced by any snapshot. Harmless; icechunk garbage collection would remove them.
5. **`dask-worker-03` is down.** `deploy_workers.py` printed "6 frisky workers … (24 concurrent readers)", but only 5 VMs answered. The summary line counts configured VMs, not live ones, so check the per-VM `status` lines.
6. **Gateway disk is at 94%** (5 GB free). The local virtual store is small (164 MB), but the daily routine needs ≥2.5 GB free (its stage 10 gate).
7. **Soil levels.** `sot`/`vsw` keep one of their four layers, as at 0.25° (`../../gcs-source/README.md` §7).

## 4. Files

| file | role |
|---|---|
| `frisky_daily_dag.py` | task module, 0.1° era and source (§2) |
| `build_corpus.py`, `deploy_workers.py`, `bandwidth_probe.py`, `sink_icechunk.py`, `check_chunks.py`, `check_members.py`, `mirror_ceph_to_source_coop.py` | unchanged copies from `refill-crma-routine` `bc62385`, kept beside the changed module because `deploy_workers.py` ships the `frisky_daily_dag.py` next to it |
| `append_channels.py` | adds channels to a finished store (§2b); `NEW_CHANNELS` = the 7 cGAN fields |
| `append_when_published.sh` | waits for a cycle on GCS, appends it, re-uploads (§1) |
| `mirror_local_to_source_coop.py` | local virtual store → source.coop, `repo` last |
| `count_sink.py` | chunks written so far under a Ceph prefix (progress of a running build) |
| `logs/` | build, upload, realization and mirror logs of this run |
