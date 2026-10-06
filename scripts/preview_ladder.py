"""local.ladder with one JSON line per game, so preview variants can be compared game by game on the same seeds.

    PREVIEW_MATRIX_KNOBS='{"mode": "argmax"}' python scripts/preview_ladder.py --games 300 --seed 11 --out runs/x.jsonl

Everything except the candidate's team preview is deterministic given the seed (same draft, same battle RNG), so
two variants run on the same seed differ only through the lineups they pick: compare them with --compare.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # this tree's agent/ and champion/, not main's

from local.eval_pokemon import wilson  # noqa: E402
from local.pokemon_env import ContestantPlayer, play_match  # noqa: E402
from local.showdown import Showdown  # noqa: E402


def run(a) -> None:
    from agent.agent import create_agent as cand_factory
    from champion.agent.agent import create_agent as champ_factory
    cand, champ = ContestantPlayer(cand_factory, "candidate"), ContestantPlayer(champ_factory, "champion")
    wins = 0
    t0 = time.time()
    with Showdown() as sd, open(a.out, "w") as out:
        for g in range(a.start, a.start + a.games):
            seat = g % 2
            players = [cand, champ] if seat == 0 else [champ, cand]
            r = play_match(sd, players[0], players[1], seed=a.seed * 100000 + g)
            won = r.winner == seat
            wins += won
            out.write(json.dumps({"g": g, "seat": seat, "won": won, "tie": r.winner is None, "turns": r.turns,
                                  "brought": r.brought, "rosters": r.rosters, "invalid": r.invalid}) + "\n")
            out.flush()
    lo, hi = wilson(wins, a.games)
    print(f"candidate vs champion: {wins}/{a.games} = {100 * wins / a.games:.1f}%, 95% CI [{100 * lo:.1f}%, "
          f"{100 * hi:.1f}%] | {time.time() - t0:.0f}s")


def compare(files: list[str]) -> None:
    rows = [{json.loads(l)["g"]: json.loads(l) for l in open(f)} for f in files]
    base = rows[0]
    for f, r in zip(files, rows):
        common = sorted(set(base) & set(r))
        w = sum(r[g]["won"] for g in common)
        diff_pick = [g for g in common if r[g]["brought"] != base[g]["brought"]]
        gain = sum(r[g]["won"] - base[g]["won"] for g in diff_pick)
        flips_up = sum(r[g]["won"] and not base[g]["won"] for g in common)
        flips_dn = sum(base[g]["won"] and not r[g]["won"] for g in common)
        print(f"{f:40s} n={len(common)} win={100 * w / max(1, len(common)):.1f}% | vs {files[0]}: "
              f"+{flips_up} -{flips_dn} (net {flips_up - flips_dn:+d}), games with a different lineup: "
              f"{len(diff_pick)} (net {gain:+d})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=300)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--out", default="runs/ladder.jsonl")
    ap.add_argument("--compare", nargs="*")
    a = ap.parse_args()
    if a.compare:
        compare(a.compare)
    else:
        run(a)
