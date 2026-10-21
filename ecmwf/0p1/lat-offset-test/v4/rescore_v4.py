# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy"]
# ///
"""Re-score v4 candidate dumps under other (start, RSI) choice rules, offline.

A choice is right when the chosen start is a true RSI start AND the chosen k
is that start's true RSI number. Usage: rescore_v4.py <v4_cands.pkl> <v4 log>
"""
import re, sys, pickle, collections
import numpy as np

dump = pickle.load(open(sys.argv[1], "rb"))


def dist(m, c):            # bits between the start and where k was predicted
    return abs(int(m["pred_bits"][c[1]]) - (m["w0"] * 8 + c[0]))


def value_only(m):
    return min(m["cands"], key=lambda c: c[2])


def nearest(m):
    return min(m["cands"], key=lambda c: (dist(m, c), c[2]))


def window(nrsi):
    def rule(m):
        rsi_bits = m["bits"] / len(m["pred_bits"])
        near = [c for c in m["cands"] if dist(m, c) <= nrsi * rsi_bits]
        return min(near or m["cands"], key=lambda c: c[2])
    return rule


def combined(sigma, lam):
    def rule(m):
        best = min(c[2] for c in m["cands"]) or 1e-12
        sig = sigma * m["L"] * 8
        return min(m["cands"], key=lambda c: c[2] / best + lam * (dist(m, c) / sig) ** 2)
    return rule


def right(m, c):
    return m["true_k"].get(c[0]) == c[1]


def kind(m):
    if m["step"] == 24 and m["run"] == "12": return "member"
    return "run" if m["run"] != "12" else "step"


rules = {"value only (v3 rule)": value_only, "nearest predicted": nearest}
for w in (1, 2, 3, 5):
    rules[f"value within +-{w} RSI of prediction"] = window(w)
for sg in (0.0025, 0.005, 0.01, 0.02):
    for lam in (0.25, 1, 4):
        tag = " <- v4 default (fixed before the run)" if (sg, lam) == (0.01, 1) else ""
        rules[f"combined sigma={sg * 100:g}% lambda={lam:g}{tag}"] = combined(sg, lam)

ms = [m for m in dump if m["cands"]]
print(f"{sys.argv[1].split('/')[-1]}: {len(ms)} messages with candidates "
      f"({sum(bool(m['true_k']) for m in ms)} have a true start among them)")
for name, rule in rules.items():
    ok = collections.Counter(); n = collections.Counter(); bad = []
    for m in ms:
        c = rule(m); n[kind(m)] += 1
        if right(m, c): ok[kind(m)] += 1
        else: bad.append(f"{m['param']} s{m['step']}")
    tot = sum(ok.values())
    print(f"  {tot:2}/{len(ms)}  member {ok['member']}/{n['member']} run {ok['run']}/{n['run']} "
          f"step {ok['step']}/{n['step']}   {name}" + (f"   fails: {', '.join(bad)}" if 0 < len(bad) <= 6 else ""))

# would a narrower fetch margin still have contained the band start?
pe, L, fetched = [], [], []
for l in open(sys.argv[2]):
    x = re.search(r"([\d.]+)% of\s+(\d+) KB.*pred_err=([\d.]+)KB", l)
    if x: fetched.append(float(x[1])); L.append(float(x[2])); pe.append(float(x[3]))
pe, L, fetched = map(np.array, (pe, L, fetched))
print(f"  prediction error at the band start: median {np.median(pe):.1f} KB, "
      f"max {pe.max():.1f} KB = {100 * (pe / L).max():.2f}% of the message")
for m in (0.05, 0.03, 0.02, 0.015, 0.01):
    inside = (pe < m * L + 2.048).mean()
    print(f"  margin {m * 100:4.1f}% + 2 KiB: band start inside window for {100 * inside:5.1f}% of messages, "
          f"fetched ~{np.mean(fetched - 2 * (5 - m * 100)):.1f}% of the message (was {fetched.mean():.1f}%)")
