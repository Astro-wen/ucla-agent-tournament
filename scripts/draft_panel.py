"""Paired panel for a Drafter model: this tree's agent vs non-v3 opponents, one JSON line per game.

    python scripts/draft_panel.py --games 300 --seed 57 --out work/panel/new.jsonl
    python scripts/draft_panel.py --games 300 --seed 57 --model champion/agent/pokemon/draft_model.json \
        --out work/panel/old.jsonl                                   # same games, the old model in our seat
    python scripts/draft_panel.py --compare work/panel/new.jsonl work/panel/old.jsonl

Opponents (--opps): v0 / v1 / v2 = the archived versions as they are (card_score drafts, their own pilots); bst =
highest-base-stat drafter + v1 pilot; rand = random drafter + v2 pilot; cs3 = card_score drafter + our pilot;
official_baseline and random (local/pokemon_env.py). Opponent drafts are deterministic here (no eps), unlike
scripts/draft_population.py. `--model` sets ARENA_DRAFT_MODEL for this process: only OUR seat's Drafter reads it
(the listed opponents never use theirs). Game g uses seed * 100000 + g and seat g % 2, like local/benchmark.py,
and the opponent's own random stream (random drafter, bst tie-breaks) restarts from that game seed, so two runs with
the same --seed are paired game by game even after their picks differed in earlier games, and a resumed run replays
the same games. (The drafter-refit track's seed 57 / 58 runs, 09-28, still shared one stream across all games of a
run, so they were not paired against the rand opponent.)
In-sample caveat: v0 / v1 / v2 / bst / rand / cs3 are exactly the opponent families scripts/draft_population.py
generates the refit's training games against, so a model fitted on that data is NOT tested on unfamiliar opponents
here (seeds and pools differ, opponent identity does not). Opponents no fitted model has seen: local.panel's greedy,
protect_spam and the field agents (basic, smoke, llm_fallback, llm_timeout).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from pathlib import Path

os.environ["ARENA_RECORD"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PANEL = ["v0", "v1", "v2", "bst", "rand", "cs3", "official_baseline", "random"]


def make_opponent(name: str, rng):
    import importlib
    from local.pokemon_env import ContestantPlayer, OfficialBaseline, RandomPlayer
    from scripts.draft_population import FAMILIES, PopulationSeat
    if name == "official_baseline":
        return OfficialBaseline()
    if name == "random":
        return RandomPlayer()
    if name in ("v0", "v1", "v2"):
        mod = FAMILIES[name][0]
        return ContestantPlayer(importlib.import_module(mod).create_agent, name)
    mod, drafter, _eps = FAMILIES[name]
    return PopulationSeat(importlib.import_module(mod).create_agent, name, drafter, 0.0, rng)


def opponent_rng(game_seed: int) -> random.Random:
    """The opponent's own random stream for one game (depends on that game's seed only)."""
    return random.Random(game_seed * 7919 + 1)


def run(a) -> None:
    if a.model:
        os.environ["ARENA_DRAFT_MODEL"] = str(Path(a.model).resolve())
    from agent.agent import create_agent
    from local.pokemon_env import ContestantPlayer, play_match
    from local.showdown import Showdown
    rng = random.Random(a.seed * 7919 + 1)          # construction only: every game reseeds it (opponent_rng)
    ours = ContestantPlayer(create_agent, "ours")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    # resume: games already in --out are kept (same seed -> same game), only the missing ones are played
    prev = {}
    if Path(a.out).exists():
        for line in open(a.out):
            try:
                r = json.loads(line)
                prev[(r["opp"], r["seed"])] = r
            except (ValueError, KeyError, TypeError):
                continue
    sd = Showdown()
    try:
        with open(a.out, "a") as fo:
            for name in [o.strip() for o in a.opps.split(",") if o.strip()]:
                opp = make_opponent(name, rng)
                wins, n, dead, t0 = 0, 0, 0, time.time()
                for g in range(a.games):
                    seat = g % 2
                    seed = a.seed * 100000 + g
                    if (name, seed) in prev:
                        wins, n = wins + bool(prev[(name, seed)]["won"]), n + 1
                        dead += any(prev[(name, seed)].get("waived") or [])
                        continue
                    players = [ours, opp] if seat == 0 else [opp, ours]
                    r = None
                    for attempt in range(2):         # the Showdown bridge can die (node killed): restart it once
                        try:
                            if hasattr(opp, "rng"):  # per-game opponent stream: paired across runs and resumes
                                opp.rng = opponent_rng(seed)
                            r = play_match(sd, players[0], players[1], seed=seed)
                            break
                        except (RuntimeError, BrokenPipeError, OSError) as e:
                            print(f"bridge problem at {name} game {g} ({e!r}); restarting it", flush=True)
                            sd.close()
                            sd = Showdown()
                    if r is None:
                        print(f"skipped {name} game {g}", flush=True)
                        continue
                    won = r.winner == seat
                    wins, n = wins + won, n + 1
                    waived = getattr(r, "clause_waived", None) or [0, 0]
                    dead += any(waived)
                    fo.write(json.dumps({"opp": name, "seed": seed, "seat": seat, "won": bool(won),
                                         "tie": r.winner is None, "invalid": [r.invalid[seat], r.invalid[1 - seat]],
                                         "waived": [waived[seat], waived[1 - seat]],
                                         "ours": r.rosters[seat], "theirs": r.rosters[1 - seat]}) + "\n")
                    fo.flush()
                print(f"{name:18s} {wins}/{n} = {100 * wins / max(1, n):.1f}%  ({time.time() - t0:.0f}s)"
                      f"  draft dead ends {dead}", flush=True)
    finally:
        sd.close()


def compare(new_path: str, old_path: str) -> None:
    def load(p):
        out = {}
        for line in open(p):
            r = json.loads(line)
            out[(r["opp"], r["seed"])] = r
        return out
    A, B = load(new_path), load(old_path)
    opps = []
    for k in A:
        if k[0] not in opps:
            opps.append(k[0])
    print(f"{'opponent':18s} {'n':>5s} {'new':>7s} {'old':>7s} {'diff pp':>8s} {'95% CI':>16s}  new-only/old-only  "
          f"McNemar p  invalid(new ours/opp, old ours/opp)")
    tot = [0, 0, 0, 0, 0]
    for o in opps:
        keys = [k for k in A if k[0] == o and k in B]
        n = len(keys)
        if not n:
            continue
        a = sum(A[k]["won"] for k in keys)
        b = sum(B[k]["won"] for k in keys)
        n10 = sum(A[k]["won"] and not B[k]["won"] for k in keys)
        n01 = sum(B[k]["won"] and not A[k]["won"] for k in keys)
        d = (a - b) / n
        var = ((n10 + n01) / n - d * d) / n
        se = math.sqrt(max(var, 0.0))
        p = _mcnemar(n10, n01)
        inv = (sum(A[k]["invalid"][0] for k in keys), sum(A[k]["invalid"][1] for k in keys),
               sum(B[k]["invalid"][0] for k in keys), sum(B[k]["invalid"][1] for k in keys))
        print(f"{o:18s} {n:5d} {100 * a / n:6.1f}% {100 * b / n:6.1f}% {100 * d:+7.2f}  [{100 * (d - 1.96 * se):+6.2f}, "
              f"{100 * (d + 1.96 * se):+6.2f}]  {n10:4d}/{n01:<4d}  {p:8.4f}   {inv}")
        tot = [tot[0] + n, tot[1] + a, tot[2] + b, tot[3] + n10, tot[4] + n01]
    n, a, b, n10, n01 = tot
    if n:
        d = (a - b) / n
        se = math.sqrt(max(((n10 + n01) / n - d * d) / n, 0.0))
        print(f"{'ALL':18s} {n:5d} {100 * a / n:6.1f}% {100 * b / n:6.1f}% {100 * d:+7.2f}  [{100 * (d - 1.96 * se):+6.2f}, "
              f"{100 * (d + 1.96 * se):+6.2f}]  {n10:4d}/{n01:<4d}  {_mcnemar(n10, n01):8.4f}")
        keys = [k for k in A if k in B]
        dead_new = sum(any(A[k].get("waived") or []) for k in keys)
        dead_old = sum(any(B[k].get("waived") or []) for k in keys)
        print(f"draft dead ends (Item Clause waived): new {dead_new}, old {dead_old} of {len(keys)} paired games"
              f" (rows from runs before the count existed have none)")


def _mcnemar(n10: int, n01: int) -> float:
    """Two-sided exact binomial McNemar p-value."""
    m = n10 + n01
    if m == 0:
        return 1.0
    k = min(n10, n01)
    tail = sum(math.comb(m, i) for i in range(k + 1)) / 2 ** m
    return min(1.0, 2 * tail)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=200)
    ap.add_argument("--seed", type=int, default=57)
    ap.add_argument("--opps", default=",".join(PANEL))
    ap.add_argument("--model", default="", help="draft model file for OUR seat (default: agent/pokemon/draft_model.json)")
    ap.add_argument("--out", default="")
    ap.add_argument("--compare", nargs=2, metavar=("NEW", "OLD"))
    a = ap.parse_args()
    if a.compare:
        compare(*a.compare)
        return
    if not a.out:
        sys.exit("--out is required")
    run(a)


if __name__ == "__main__":
    main()
