"""Summary of the final measurement (~/wt/_ra6_out): per set and pooled, base (fb_) vs candidate (fin_) and vs split
(fs_): wins, paired diff, better / worse, sign-test p, identical games; per opponent and per map for the pooled core;
health (invalid, rejected, errors, p99) per arm."""
import json
import math
import os
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compare import key, score, sign_p  # noqa: E402

O = "/home/linmengfdm/wt/_ra6_out"
CORE = ["tom1551", "tomsf1552", "nb1553", "nash1554", "pro1555", "strong1556", "main1557", "rusher1558", "sfmix1559"]
STRESS = ["fault1560", "lost1561", "win1562", "cash1563", "lat1564", "lat1567"]


def load(p):
    p = os.path.join(O, p)
    return json.load(open(p))["rows"] if os.path.exists(p) and os.path.getsize(p) > 0 else None


def line(label, base, new):
    bm = {key(r): r for r in base}
    nm = {key(r): r for r in new}
    ks = sorted(set(bm) & set(nm), key=str)
    if not ks:
        return f"{label}: no paired games"
    sb = sum(score(bm[k]) for k in ks)
    sn = sum(score(nm[k]) for k in ks)
    bt = sum(1 for k in ks if score(nm[k]) > score(bm[k]))
    wr = sum(1 for k in ks if score(nm[k]) < score(bm[k]))
    same = sum(1 for k in ks if bm[k]["result_a"] == nm[k]["result_a"] and bm[k]["game_s"] == nm[k]["game_s"])
    d = [score(nm[k]) - score(bm[k]) for k in ks]
    ci = 1.96 * statistics.stdev(d) / math.sqrt(len(d)) if len(d) > 1 else 0
    return (f"{label:34s} n={len(ks):4d} base {sb:6.1f} ({100*sb/len(ks):5.1f}%) new {sn:6.1f} ({100*sn/len(ks):5.1f}%) "
            f"diff {100*(sn-sb)/len(ks):+5.1f}pp (+-{100*ci:.1f}) {bt}/{wr} p={sign_p(bt, wr):.3f} identical {same}")


def health(rows):
    a = [r["a"] for r in rows]
    return (f"invalid {sum(s['invalid_actions'] for s in a)} rejected {sum(s['rejected'] for s in a)} "
            f"errors {sum(1 for s in a if s['error'] or s['hung'])} p99max {max(s['p99_ms'] for s in a):.1f}ms "
            f"slowest {max(s['slowest_ms'] for s in a):.0f}ms")


pool = {"fin": ([], []), "fs": ([], [])}
for name in CORE + STRESS + ["h2h1565"]:
    fb = load(f"fb_{name}.json")
    if fb is None:
        print(f"{name}: (not done)")
        continue
    for arm in ("fin", "fs"):
        new = load(f"{arm}_{name}.json")
        if new is None:
            continue
        print(line(f"{name} {arm}", fb, new))
        if name in CORE:
            pool[arm][0].extend(fb)
            pool[arm][1].extend(new)
for arm in ("fin", "fs"):
    base, new = pool[arm]
    if not base:
        continue
    print(f"== POOLED core ({arm})")
    print(line("ALL", base, new))
    by = defaultdict(lambda: ([], []))
    bm = {key(r): r for r in base}
    for r in new:
        k = key(r)
        if k in bm:
            by["opp:" + str(r.get("b_spec"))][0].append(bm[k])
            by["opp:" + str(r.get("b_spec"))][1].append(r)
            by["map:" + r["map"]][0].append(bm[k])
            by["map:" + r["map"]][1].append(r)
    for g in sorted(by):
        print(line("  " + g, *by[g]))
    print("  health base:", health(base))
    print("  health new: ", health(new))
for name in STRESS + ["h2h1565"]:
    for arm in ("fb", "fin"):
        rows = load(f"{arm}_{name}.json")
        if rows:
            print(f"{name} {arm} health: {health(rows)}")
for name in ("strong1556", "main1557", "nash1554"):
    v47, fb = load(f"v47_{name}.json"), load(f"fb_{name}.json")
    if v47 and fb:
        print(line(f"ident v47 vs fb {name}", v47, fb))
