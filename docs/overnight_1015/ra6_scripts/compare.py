"""Paired comparison of local.ra_platform JSON outputs (same seeds): base vs new.

    python compare.py base.json new.json [more base/new pairs...]

Per game the score for A is 1 / 0.5 / 0. Prints per opponent and total: W-L-D for both, score with 95% Wilson CI,
paired mean difference, discordant pairs (new better / base better) and the exact two-sided sign test p on them
(McNemar), plus side-A health: INVALID_ACTION, rejected, runner errors/hangs, decision p99 max / slowest, first war
factory median, identical games.
"""
import json
import math
import statistics
import sys
from collections import defaultdict


def wilson(score, n, z=1.96):
    if n <= 0:
        return 0.0, 1.0
    p = score / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return max(0.0, c - h), min(1.0, c + h)


def sign_p(a, b):
    """Exact two-sided binomial test of a vs b successes at p=0.5."""
    n = a + b
    if n == 0:
        return 1.0
    k = min(a, b)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def score(r):
    return {"win": 1.0, "draw": 0.5}.get(r["result_a"], 0.0)


def key(r):
    return (r["seed"], r["map"], r["a_seat"], r.get("b_spec"))


def load(paths):
    rows = []
    for p in paths:
        rows += json.load(open(p))["rows"]
    return rows


def health(rows):
    a = [r["a"] for r in rows]
    weap = [s["first_weap_s"] for s in a if s["first_weap_s"] is not None]
    return (f"invalid {sum(s['invalid_actions'] for s in a)} rejected {sum(s['rejected'] for s in a)} "
            f"errors {sum(1 for s in a if s['error'] or s['hung'])} p99max {max((s['p99_ms'] for s in a), default=0):.1f}ms "
            f"p99med {statistics.median([s['p99_ms'] for s in a]) if a else 0:.1f}ms "
            f"slowest {max((s['slowest_ms'] for s in a), default=0):.0f}ms weap_med {statistics.median(weap) if weap else None} "
            f"weap_missing {sum(1 for s in a if s['first_weap_s'] is None)}")


def report(base, new, label=""):
    bm = {key(r): r for r in base}
    nm = {key(r): r for r in new}
    common = sorted(set(bm) & set(nm), key=str)
    groups = defaultdict(list)
    import os
    for k in common:
        groups[k[3]].append(k)
        if os.environ.get("BYMAP"):
            groups["map:" + k[1]].append(k)
    groups["ALL"] = common
    print(f"== {label} paired games {len(common)} (base only {len(set(bm) - set(nm))}, new only {len(set(nm) - set(bm))})")
    for g, ks in sorted(groups.items(), key=lambda kv: (kv[0] == "ALL", str(kv[0]))):
        sb = sum(score(bm[k]) for k in ks)
        sn = sum(score(nm[k]) for k in ks)
        better = sum(1 for k in ks if score(nm[k]) > score(bm[k]))
        worse = sum(1 for k in ks if score(nm[k]) < score(bm[k]))
        same = sum(1 for k in ks if bm[k]["result_a"] == nm[k]["result_a"] and bm[k]["game_s"] == nm[k]["game_s"])
        lb, hb = wilson(sb, len(ks))
        ln, hn = wilson(sn, len(ks))
        diffs = [score(nm[k]) - score(bm[k]) for k in ks]
        md = statistics.mean(diffs) if diffs else 0
        sd = statistics.stdev(diffs) if len(diffs) > 1 else 0
        ci = 1.96 * sd / math.sqrt(len(diffs)) if diffs else 0
        print(f"  {str(g):8s} n={len(ks):3d} base {sb:5.1f} ({100*sb/len(ks):5.1f}%, {100*lb:.1f}-{100*hb:.1f}) "
              f"new {sn:5.1f} ({100*sn/len(ks):5.1f}%, {100*ln:.1f}-{100*hn:.1f}) diff {100*md:+.1f}pp "
              f"(+-{100*ci:.1f}) better {better} worse {worse} p={sign_p(better, worse):.4f} identical {same}")
    print("  base health:", health([bm[k] for k in common]))
    print("  new  health:", health([nm[k] for k in common]))
    flips = [(k, bm[k]["result_a"], nm[k]["result_a"]) for k in common if score(nm[k]) != score(bm[k])]
    for k, a, b in flips[:40]:
        print(f"    flip {k}: {a} -> {b}  (base {bm[k]['game_s']/60:.1f}m K/D {bm[k]['a']['kills_cost']}/{bm[k]['a']['deaths_cost']}, "
              f"new {nm[k]['game_s']/60:.1f}m K/D {nm[k]['a']['kills_cost']}/{nm[k]['a']['deaths_cost']})")


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) % 2:
        sys.exit("pairs of base.json new.json")
    allb, alln = [], []
    for i in range(0, len(args), 2):
        b, n = load([args[i]]), load([args[i + 1]])
        report(b, n, f"{args[i]} vs {args[i + 1]}")
        allb += b
        alln += n
    if len(args) > 2:
        report(allb, alln, "POOLED")
