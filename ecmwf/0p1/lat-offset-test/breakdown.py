"""Tally sync outputs (several files = one run split by resumes) by what varies."""
import re, sys, collections
c = collections.defaultdict(collections.Counter); per = collections.defaultdict(collections.Counter)
fails, pe, t, fetched = [], [], [], []
for f in sys.argv[1:]:
    for l in open(f):
        m = re.match(r"\s+(\S+)\s+m(\d+)\s+s\s*(\d+)\s+(\d+)z\s+(\S+)", l)
        if not m or m[5] in ("skip", "s3fail"): continue
        p, mem, st, run, status = m.groups(); status = status.replace("-late", "")
        kind = "member (+24h, 12z)" if st == "24" and run == "12" else ("run (06z/18z, +24h)" if run != "12" else "step (m01, 12z)")
        c[kind][status] += 1; per[p][status] += 1
        if status != "OK": fails.append(l.strip()[:110])
        x = re.search(r"pred_err=([\d.]+)KB", l); pe.append(float(x[1])) if x else None
        x = re.search(r"([\d.]+)% of", l); fetched.append(float(x[1]))
        x = re.search(r"KB\s+([\d.]+)s", l); t.append(float(x[1]))
tot = sum(c.values(), collections.Counter())
print("total", dict(tot), f"n={sum(tot.values())}  fetched {min(fetched):.1f}-{max(fetched):.1f}% (mean {sum(fetched)/len(fetched):.1f})  time mean {sum(t)/len(t):.1f}s max {max(t):.1f}s")
for k, v in c.items(): print(f"  {k:22}", dict(v))
print("  per var:", {p: f"{v['OK']}/{sum(v.values())}" for p, v in per.items()})
if pe: print(f"  prediction error: median {sorted(pe)[len(pe)//2]:.1f} KB, max {max(pe):.1f} KB")
for l in fails: print("  FAIL", l)
