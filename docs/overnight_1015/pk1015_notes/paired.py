"""Paired comparison of two panel runs (same deals) + Sucker Punch counts from the saved logs.
usage: paired.py CAND.jsonl BASE.jsonl [more CAND BASE pairs...]  (pairs pooled at the end)"""
import json, math, sys, glob, os
from math import comb


def load(p):
    rows = {}
    for l in open(p):
        r = json.loads(l)
        if r.get("error"):
            continue
        rows[(r["opp"], r["g"])] = r
    return rows


def sucker(path_jsonl):
    d = path_jsonl + ".logs"
    use = fail = games = gused = 0
    other = {}
    for f in glob.glob(d + "/*.json"):
        g = json.load(open(f))
        cand = "p1" if g.get("seat", 0) == 0 else "p2"
        pend = None
        u = 0
        for l in g.get("log") or []:
            p = l.split("|")
            if l.startswith("|turn|"):
                pend = None
                continue
            if len(p) < 3:
                continue
            if p[1] == "move" and p[2].startswith(cand):
                if p[3] == "Sucker Punch":
                    use += 1; u += 1; pend = p[2][:3]
                else:
                    pend = None if pend == p[2][:3] else pend
            elif p[1] == "-fail" and pend and p[2][:3] == pend:
                fail += 1; pend = None
        games += 1
        gused += u > 0
    return use, fail, games, gused


def mcnemar(b, c):
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    return min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n)


def report(label, pairs):
    n = len(pairs)
    cw = sum(a for a, _ in pairs); bw = sum(b for _, b in pairs)
    b = sum(1 for a, x in pairs if a and not x); c = sum(1 for a, x in pairs if x and not a)
    d = (b - c) / n if n else 0
    se = math.sqrt(max(0.0, b + c - (b - c) ** 2 / n)) / n if n else 0
    print(f"{label:28s} n={n:4d} cand {cw:4d} ({100*cw/max(1,n):5.1f}%) base {bw:4d} ({100*bw/max(1,n):5.1f}%) "
          f"diff {100*d:+5.1f} [{100*(d-1.96*se):+5.1f}, {100*(d+1.96*se):+5.1f}] only c/b {b}/{c} p={mcnemar(b, c):.3f}")


args = sys.argv[1:]
allp = []
for i in range(0, len(args), 2):
    C, B = load(args[i]), load(args[i + 1])
    keys = sorted(set(C) & set(B))
    pairs = [(bool(C[k]["win"]), bool(B[k]["win"])) for k in keys]
    allp += pairs
    report(os.path.basename(args[i])[:28], pairs)
    for p in (args[i], args[i + 1]):
        if os.path.isdir(p + ".logs"):
            u, f, g, gu = sucker(p)
            bad = sum(1 for r in load(p).values() if r.get("inv_cand") or r.get("timer_cand") or r.get("forfeit") == "cand")
            tmax = max((r.get("tmax_cand", 0) for r in load(p).values()), default=0)
            print(f"     {os.path.basename(p)[:30]:30s} sucker use {u} fail {f} in {gu}/{g} games; unclean {bad}; tmax {tmax:.2f}s")
if len(args) > 2:
    report("POOLED", allp)
