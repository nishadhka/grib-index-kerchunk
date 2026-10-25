"""Sample worker-VM memory every --every s until killed; or collect prefix stats.

Read-only on the VMs: /proc/meminfo, the RSS of every frisky worker process
(both /tmp/frisky-wide and the daily /tmp/frisky-ea), and the
prefix-stats-*.json files read_prefix writes. Uses the Dask cluster only as a
remote-exec channel, as deploy_workers.py does.

    $P mem_monitor.py > logs/mem-<run>.tsv            # sample (background)
    $P mem_monitor.py --stats                          # sum prefix-read counters
    $P mem_monitor.py --clear-stats                    # before a run
"""
import argparse, sys, time
from distributed import Client


def probe():
    import socket, subprocess
    m = {l.split(":")[0]: int(l.split()[1]) // 1024 for l in open("/proc/meminfo")}
    r = subprocess.run(["pgrep", "-f", "frisky worker"], capture_output=True, text=True)
    rss = []
    for pid in r.stdout.split():
        try:
            for l in open(f"/proc/{pid}/status"):
                if l.startswith("VmRSS"):
                    rss.append(int(l.split()[1]) // 1024)
        except OSError:
            pass
    return dict(host=socket.gethostname(), used=m["MemTotal"] - m["MemAvailable"],
                avail=m["MemAvailable"], rss=max(rss or [0]))


def stats(clear=False):
    import glob, json, os, socket
    out = []
    for f in glob.glob("/tmp/frisky-wide/prefix-stats-*.json"):
        if clear:
            os.remove(f)
        else:
            out.append(json.load(open(f)))
    return dict(host=socket.gethostname(), stats=out)


ap = argparse.ArgumentParser()
ap.add_argument("--every", type=float, default=15)
ap.add_argument("--stats", action="store_true")
ap.add_argument("--clear-stats", action="store_true")
a = ap.parse_args()
c = Client("tcp://127.0.0.1:8786", timeout=30)
if a.stats or a.clear_stats:
    seen, tot = set(), {}
    for v in c.run(stats, a.clear_stats).values():
        if v["host"] in seen:
            continue
        seen.add(v["host"])
        for st in v["stats"]:
            for k in ("msgs", "bytes_fetched", "bytes_full", "gets", "topups", "t_get", "t_decode"):
                tot[k] = tot.get(k, 0) + st[k]
            for k in ("src_aws", "src_gcs"):
                tot[k] = tot.get(k, 0) + st.get(k, 0)
    if a.stats and tot.get("msgs"):
        print(f"prefix reads {tot['msgs']:,} msgs: fetched {tot['bytes_fetched']/1e9:.2f} of "
              f"{tot['bytes_full']/1e9:.2f} GB ({100*tot['bytes_fetched']/tot['bytes_full']:.1f}%, "
              f"saved {100-100*tot['bytes_fetched']/tot['bytes_full']:.1f}%), "
              f"{tot['topups']} top-up GETs, mean GET {tot['t_get']/tot['msgs']:.2f}s, "
              f"decode {1000*tot['t_decode']/tot['msgs']:.0f} ms, "
              f"GETs served by AWS {tot['src_aws']:,} / GCS {tot['src_gcs']:,}")
        print("(counters are flushed every 200 messages per process, so the last <200 per process are missing)")
    elif a.stats:
        print("no prefix stats on the VMs")
    sys.exit(0)
hosts = None
while True:
    seen, row = set(), {}
    for v in c.run(probe).values():
        if v["host"] in seen:
            continue
        seen.add(v["host"]); row[v["host"]] = v
    if hosts is None:
        hosts = sorted(row)
        print("utc\t" + "\t".join(f"{h[-2:]}_used\t{h[-2:]}_rss" for h in hosts), flush=True)
    print(time.strftime("%H:%M:%S", time.gmtime()) + "\t" +
          "\t".join(f"{row[h]['used']}\t{row[h]['rss']}" if h in row else "-\t-" for h in hosts),
          flush=True)
    time.sleep(a.every)
