"""Paired comparison of local.panel runs played on the same games (McNemar), plus the engine search's latency.

    python scripts/search_report.py A.jsonl B.jsonl         # A (e.g. ARENA_BATTLE=search) vs B (classic), same games
    python scripts/search_report.py A.jsonl                 # one run: win rate, Wilson CI, search counters, latency

Games are matched on (opponent, g) (same --seed and --same-team setting give the same draft pool, seats and
match seed). McNemar's exact test on the discordant pairs (A won / B lost vs the reverse); the paired difference of
win rates with a normal-approximation 95% CI. Latency: per-game maximum decision time on the candidate's clock
(tmax_cand, platform VGC timer charged in real wall time) and the engine search's own round trips (srch_*).
"""
from __future__ import annotations

import json
import math
import sys


def load(path: str) -> dict:
    rows = {}
    for line in open(path):
        r = json.loads(line)
        rows[(r["opp"], r["g"])] = r
    return rows


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - h) / d, (c + h) / d


def binom_two_sided(b: int, c: int) -> float:
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * p)


def pct(xs: list, q: float) -> float:
    xs = sorted(xs)
    if not xs:
        return 0.0
    return xs[min(len(xs) - 1, max(0, int(math.ceil(q * len(xs))) - 1))]


def summary(name: str, rows: dict) -> None:
    n = len(rows)
    w = sum(1 for r in rows.values() if r.get("win"))
    lo, hi = wilson(w, n)
    err = sum(1 for r in rows.values() if r.get("error"))
    inv = sum(r.get("inv_cand", 0) for r in rows.values())
    timer = sum(r.get("timer_cand", 0) for r in rows.values())
    forf = sum(1 for r in rows.values() if r.get("forfeit") == "cand")
    print(f"{name}: {w}/{n} = {100 * w / max(1, n):.1f}% [{100 * lo:.1f}, {100 * hi:.1f}] | errors {err}, "
          f"invalid {inv}, timer defaults {timer}, forfeits {forf}")
    tmax = [r["tmax_cand"] for r in rows.values() if "tmax_cand" in r]
    if tmax:
        print(f"  candidate max decision per game: p50 {pct(tmax, .5):.2f}s p99 {pct(tmax, .99):.2f}s "
              f"max {max(tmax):.2f}s; bank left min {min(r.get('bank_cand', 0) for r in rows.values()):.0f}s")
    s = [r for r in rows.values() if "srch_dec" in r]
    if s:
        dec = sum(r["srch_dec"] for r in s)
        used = sum(r["srch_used"] for r in s)
        same = sum(r["srch_same"] for r in s)
        fb = sum(r["srch_fb"] for r in s)
        skip = sum(r.get("srch_skip", 0) for r in s)
        nms = sum(r.get("srch_n", 0) for r in s)
        mean = sum(r.get("srch_ms_sum", 0) for r in s) / max(1, nms)
        mx = [r.get("srch_ms_max", 0) for r in s]
        reasons: dict = {}
        for r in s:
            for k, v in (r.get("srch_reasons") or {}).items():
                reasons[k] = reasons.get(k, 0) + v
        print(f"  engine search: {dec} decisions, used {used} ({100 * used / max(1, dec):.1f}%; same as classic "
              f"{same}), classic fallback {fb}, skipped for time {skip} {reasons or ''}")
        print(f"  search round trip: mean {mean:.0f} ms; per-game max p50 {pct(mx, .5):.0f} p99 {pct(mx, .99):.0f} "
              f"max {max(mx):.0f} ms")


def paired(a: dict, b: dict, na: str, nb: str) -> None:
    keys = sorted(set(a) & set(b))
    if not keys:
        print("no common games")
        return
    wa = sum(1 for k in keys if a[k].get("win"))
    wb = sum(1 for k in keys if b[k].get("win"))
    bb = sum(1 for k in keys if a[k].get("win") and not b[k].get("win"))
    cc = sum(1 for k in keys if b[k].get("win") and not a[k].get("win"))
    n = len(keys)
    d = (wa - wb) / n
    se = math.sqrt(max(0.0, (bb + cc) / n - d * d) / n) if n else 0.0
    print(f"paired on {n} games: {na} {wa} vs {nb} {wb} wins; diff {100 * d:+.1f} pts "
          f"[{100 * (d - 1.96 * se):+.1f}, {100 * (d + 1.96 * se):+.1f}]; discordant {bb} / {cc}, "
          f"McNemar exact p = {binom_two_sided(bb, cc):.3f}")
    by = {}
    for k in keys:
        by.setdefault(k[0], [0, 0, 0])
        by[k[0]][0] += 1
        by[k[0]][1] += bool(a[k].get("win"))
        by[k[0]][2] += bool(b[k].get("win"))
    if len(by) > 1:
        for opp, (m, x, y) in sorted(by.items()):
            print(f"  {opp:18s} {x}/{m} vs {y}/{m}  ({100 * (x - y) / m:+.1f})")


def main() -> None:
    args = [x for x in sys.argv[1:] if not x.startswith("-")]
    if not args:
        sys.exit(__doc__)
    runs = [(p.rsplit("/", 1)[-1].replace(".jsonl", ""), load(p)) for p in args]
    for name, rows in runs:
        summary(name, rows)
    if len(runs) == 2:
        paired(runs[0][1], runs[1][1], runs[0][0], runs[1][0])


if __name__ == "__main__":
    main()
