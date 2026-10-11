"""Card strengths under the v4 engine-search pilot: a residual on top of the v3-fitted draft model.

    python scripts/fit_draft_resid.py ~/exp14 ~/exp15 ~/exp16 ~/exp19 ~/exp21              # report (5-fold CV)
    python scripts/fit_draft_resid.py ... --write                                           # agent/pokemon/draft_resid.json

Why: agent/pokemon/draft_model.json was fitted on v3 self-play (classic battle pilot). Since v4 both seats battle with
the engine search, which plays some cards better (Fake Out / Intimidate / Parting Shot users, redirection, speed
control: everything the engine simulates exactly) and others worse. The real contestants draft exactly the cards this
residual favours (Incineroar, Flutter Mane, Landorus-T, Amoonguss), see scripts/fit_meta_drafter.py.

Data: every local.panel game against `champion` whose per-game log (<run>.jsonl.logs/*.json, --save-logs) has both
rosters, from runs where both seats used the engine search (the champion is v4 or later: runs started after the v4
merge, 2026-09-29), excluding the gate seeds of docs/v5_standard.md (--exclude, default 7, 11, 31, 45, 73, 75, 83) so
the gates stay out of sample. Same-team games are skipped (no draft difference). --rows adds local.panel result
files whose rows carry both sixes (six_cand / six_opp: the meta* opponents); the shipped file was fitted without them
(adding 400 games against the meta drafter, seed 103, left almost no out-of-sample gain: +0.0007 nats/game).

Model:  P(win) = sigmoid(a0 + a1 * L + sum_{c in ours} d[species c] - sum_{c in theirs} d[species c]),  L = the shipped
model's log-odds of our six against theirs; d is L2-regularised (--lam, chosen by grouped 5-fold cross-validation over
distinct pools: games replayed on the same seed and seat share a fold). The drafter adds d to a card's strength on
both sides (agent/pokemon/drafter.py, ARENA_DRAFT_RESID=1; off by default).
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import math
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["ARENA_RECORD"] = "0"

from agent.pokemon.dex import to_id  # noqa: E402

GATE_SEEDS = (7, 11, 31, 45, 73, 75, 83)
V4_FROM = "2026-09-29 05:00:00"          # runs started before this had a v3.2 (classic) champion


def load(dirs, exclude) -> list[dict]:
    from agent.pokemon.dex import card_catalog
    by_sp: dict[str, dict] = {}
    for c in card_catalog().values():
        by_sp.setdefault(to_id(c["species"]), c)
    rows = []
    for base in dirs:
        for logs in sorted(glob.glob(os.path.join(os.path.expanduser(base), "**", "*.jsonl.logs"), recursive=True)):
            try:
                meta = json.loads(Path(logs[:-5] + ".meta.json").read_text())
            except (OSError, ValueError):
                continue
            if meta.get("opps") != ["champion"] or str(meta.get("started") or "") < V4_FROM or meta.get("same_team"):
                continue
            for f in sorted(glob.glob(os.path.join(logs, "*.json"))):
                try:
                    g = json.loads(Path(f).read_text())
                except (OSError, ValueError):
                    continue
                ro = g.get("rosters")
                if not ro or g.get("winner") is None or g.get("same_team") or g.get("seed") in exclude:
                    continue
                seat = g["seat"]
                try:
                    ours = [by_sp[to_id(s)] for s in ro[seat]]
                    theirs = [by_sp[to_id(s)] for s in ro[1 - seat]]
                except KeyError:
                    continue
                rows.append({"ours": ours, "theirs": theirs, "win": 1 if g["win"] else 0,
                             "pool": (g["seed"], g["g"]), "run": logs})
    return rows


def load_rows(paths, exclude) -> list[dict]:
    """local.panel result rows that carry both sixes (six_cand / six_opp: the meta* opponents, whose pilot is the
    champion's engine search). Only pass runs whose candidate also battles with the engine search."""
    from agent.pokemon.dex import card_catalog
    by_sp: dict[str, dict] = {}
    for c in card_catalog().values():
        by_sp.setdefault(to_id(c["species"]), c)
    rows = []
    for pat in paths:
        for p in sorted(glob.glob(os.path.expanduser(pat))):
            for line in open(p):
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if "error" in r or "six_cand" not in r or r.get("tie") or r.get("seed") in exclude:
                    continue
                try:
                    ours = [by_sp[to_id(s)] for s in r["six_cand"]]
                    theirs = [by_sp[to_id(s)] for s in r["six_opp"]]
                except KeyError:
                    continue
                rows.append({"ours": ours, "theirs": theirs, "win": 1 if r["win"] else 0,
                             "pool": (r["seed"], r["g"]), "run": p})
    return rows


def model_logodds(rows) -> None:
    from agent.pokemon.drafter import Drafter
    d = Drafter(opp_mode="off", resid=False)
    cache = {}
    for r in rows:
        key = (tuple(c["card_id"] for c in r["ours"]), tuple(c["card_id"] for c in r["theirs"]))
        if key not in cache:
            m, P, S = d._tables(r["ours"] + r["theirs"])
            mo = d.m_opp
            a, b = range(6), range(6, 12)
            cache[key] = sum(m[i] for i in a) - sum(mo[j] for j in b) + sum(P[i][j] for i in a for j in b) \
                + sum(S[x][y] for x in a for y in a if x < y) - sum(S[x][y] for x in b for y in b if x < y)
        r["L"] = cache[key]
        r["A"] = [to_id(c["species"]) for c in r["ours"]]
        r["B"] = [to_id(c["species"]) for c in r["theirs"]]


def fit(rows, species, lam, iters=600, lr=1.0):
    a0, a1 = 0.0, 1.0
    d = dict.fromkeys(species, 0.0)
    n = len(rows)
    for _ in range(iters):
        g0 = g1 = 0.0
        gd = dict.fromkeys(species, 0.0)
        for r in rows:
            z = a0 + a1 * r["L"] + sum(d[s] for s in r["A"]) - sum(d[s] for s in r["B"])
            e = r["win"] - 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))
            g0 += e
            g1 += e * r["L"]
            for s in r["A"]:
                gd[s] += e
            for s in r["B"]:
                gd[s] -= e
        a0 += lr * g0 / n
        a1 += lr * g1 / n
        if lam is not None:
            for s in species:
                d[s] += lr * (gd[s] - lam * d[s]) / n
    return a0, a1, d


def loglik(rows, a0, a1, d) -> float:
    t = 0.0
    for r in rows:
        z = a0 + a1 * r["L"] + sum(d.get(s, 0.0) for s in r["A"]) - sum(d.get(s, 0.0) for s in r["B"])
        p = 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))
        t += math.log(p if r["win"] else 1 - p)
    return t / len(rows)


def cv(rows, species, lam, folds=5, seed=0) -> float:
    pools = sorted({r["pool"] for r in rows})
    random.Random(seed).shuffle(pools)
    tot = 0.0
    for f in range(folds):
        test = set(pools[f::folds])
        tr = [r for r in rows if r["pool"] not in test]
        te = [r for r in rows if r["pool"] in test]
        tot += loglik(te, *fit(tr, species, lam)) * len(te)
    return tot / len(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="*", help="experiment directories searched for local.panel runs with --save-logs")
    ap.add_argument("--rows", nargs="*", default=[], help="local.panel result files (globs) with six_cand / six_opp")
    ap.add_argument("--exclude", default=",".join(map(str, GATE_SEEDS)), help="match seeds left out (gate seeds)")
    ap.add_argument("--lams", default="80,300,1000", help="L2 strengths tried by cross-validation")
    ap.add_argument("--lam", type=float, default=None, help="use this L2 strength (default: the best by CV)")
    ap.add_argument("--write", action="store_true", help="write agent/pokemon/draft_resid.json")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    exclude = {int(x) for x in a.exclude.split(",") if x.strip()}
    rows = load(a.dirs, exclude) + load_rows(a.rows, exclude)
    if not rows:
        raise SystemExit("no usable games")
    model_logodds(rows)
    species = sorted({s for r in rows for s in r["A"] + r["B"]})
    seeds = collections.Counter(r["pool"][0] for r in rows)
    print(f"{len(rows)} search-vs-search games, {len({r['pool'] for r in rows})} distinct pools; seeds {dict(seeds)}")
    base = cv(rows, species, None)
    print(f"  CV log-lik/game without residuals {base:.4f}")
    best = (base, None)
    for lam in [float(x) for x in a.lams.split(",") if x]:
        v = cv(rows, species, lam)
        print(f"  CV log-lik/game lambda {lam:g}: {v:.4f} ({v - base:+.4f})")
        if v > best[0]:
            best = (v, lam)
    lam = a.lam if a.lam is not None else best[1]
    if lam is None:
        print("residuals do not beat the model out of sample: nothing to write")
        return
    a0, a1, d = fit(rows, species, lam)
    cnt = collections.Counter(s for r in rows for s in r["A"] + r["B"])
    print(f"fit lambda {lam:g}: a0 {a0:+.3f}, a1 {a1:.3f} (slope on the model's log-odds)")
    for s in sorted(species, key=lambda s: -d[s]):
        print(f"  {s:22s} {d[s]:+.3f}  ({cnt[s]} appearances)")
    out = a.out or (str(ROOT / "agent" / "pokemon" / "draft_resid.json") if a.write else "")
    if out:
        Path(out).write_text(json.dumps({
            "about": "v4 engine-search pilot residual card strengths (species id -> logit), added to the draft model's "
                     "strength on both sides when ARENA_DRAFT_RESID=1 (scripts/fit_draft_resid.py)",
            "lambda": lam, "a0": round(a0, 4), "a1": round(a1, 4), "games": len(rows),
            "pools": len({r["pool"] for r in rows}), "seeds": sorted(seeds), "excluded_seeds": sorted(exclude),
            "cv_loglik": round(best[0], 5), "cv_loglik_without": round(base, 5),
            "d": {s: round(v, 4) for s, v in sorted(d.items())}}, indent=1) + "\n")
        print("wrote", out)


if __name__ == "__main__":
    main()
