"""Measure our Pokémon agent against reference players on the local Showdown env.

    python -m local.eval_pokemon --games 200 --opponent official_baseline
"""
from __future__ import annotations

import argparse
import math
import time

from agent.agent import create_agent

from .pokemon_env import ContestantPlayer, OfficialBaseline, RandomPlayer, play_match
from .showdown import Showdown

OPPONENTS = {"official_baseline": OfficialBaseline, "random": RandomPlayer}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=100)
    ap.add_argument("--opponent", choices=OPPONENTS, default="official_baseline")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    ours = ContestantPlayer(create_agent)
    opp = OPPONENTS[a.opponent]()
    wins = ties = 0
    inv_ours = inv_opp = 0
    turns = []
    t0 = time.time()
    with Showdown() as sd:
        for g in range(a.games):
            seat = g % 2                       # alternate sides
            players = [ours, opp] if seat == 0 else [opp, ours]
            r = play_match(sd, players[0], players[1], seed=a.seed * 100000 + g)
            if r.winner is None:
                ties += 1
            elif r.winner == seat:
                wins += 1
            inv_ours += r.invalid[seat]
            inv_opp += r.invalid[1 - seat]
            turns.append(r.turns)
            if a.verbose:
                print(f"game {g:3d} seat {seat} -> {'WIN' if r.winner == seat else 'tie' if r.winner is None else 'loss'}"
                      f" in {r.turns} turns | ours {r.rosters[seat]} bring {r.brought[seat]} | inv {r.invalid}")
    n = a.games
    lo, hi = wilson(wins, n)
    print(f"\nours vs {a.opponent}: {wins}/{n} wins ({100 * wins / n:.1f}%), {ties} ties, "
          f"95% CI [{100 * lo:.1f}%, {100 * hi:.1f}%]")
    print(f"invalid/rejected decisions: ours {inv_ours}, {a.opponent} {inv_opp} | avg turns {sum(turns) / n:.1f} | "
          f"{time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
