"""Ladder: the working-tree agent (candidate) vs the frozen champion (champion/agent), same seeds, sides alternated.

    python -m local.ladder --games 400            # prints win rate + 95% CI; exit 0 if candidate >= champion
    python -m local.ladder --games 400 --json     # machine-readable line at the end

Promotion rule used by the improvement loop: candidate's 95% CI lower bound > 50% (clearly better),
or >= 50% win rate with zero invalid actions when the change is a pure robustness fix.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from .eval_pokemon import wilson
from .pokemon_env import ContestantPlayer, play_match
from .showdown import Showdown


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=400)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    from agent.agent import create_agent as cand_factory
    try:
        from champion.agent.agent import create_agent as champ_factory
    except ImportError:
        sys.exit("no champion/ snapshot — run scripts/freeze_champion.sh first")
    cand, champ = ContestantPlayer(cand_factory, "candidate"), ContestantPlayer(champ_factory, "champion")
    wins = ties = 0
    inv = [0, 0]
    turns = []
    t0 = time.time()
    with Showdown() as sd:
        for g in range(a.games):
            seat = g % 2
            players = [cand, champ] if seat == 0 else [champ, cand]
            r = play_match(sd, players[0], players[1], seed=a.seed * 100000 + g)
            if r.winner is None:
                ties += 1
            elif r.winner == seat:
                wins += 1
            inv[0] += r.invalid[seat]
            inv[1] += r.invalid[1 - seat]
            turns.append(r.turns)
    n = a.games
    lo, hi = wilson(wins, n)
    print(f"candidate vs champion: {wins}/{n} = {100 * wins / n:.1f}% (ties {ties}), 95% CI [{100 * lo:.1f}%, {100 * hi:.1f}%]"
          f" | invalid: candidate {inv[0]}, champion {inv[1]} | avg turns {sum(turns) / n:.1f} | {time.time() - t0:.0f}s")
    verdict = "PROMOTE" if lo > 0.5 else ("HOLD" if wins / n >= 0.5 else "REJECT")
    print("verdict:", verdict)
    if a.json:
        print(json.dumps({"wins": wins, "games": n, "ties": ties, "win_rate": wins / n, "ci": [lo, hi],
                          "invalid_candidate": inv[0], "invalid_champion": inv[1], "verdict": verdict}))
    sys.exit(0 if verdict != "REJECT" else 1)


if __name__ == "__main__":
    main()
