"""Benchmark panel: the working-tree agent vs every reference opponent and every archived version.

    python -m local.benchmark --games 200

Opponents: official_baseline, random, and versions/<name>/agent (frozen past champions, oldest first).
The ladder (local/ladder.py) only compares against the current champion; this panel catches
non-transitive regressions (beating the last version while getting worse against older styles).
"""
from __future__ import annotations

import argparse
import importlib
import time
from pathlib import Path

from .eval_pokemon import wilson
from .pokemon_env import ContestantPlayer, OfficialBaseline, RandomPlayer, play_match
from .showdown import Showdown

ROOT = Path(__file__).resolve().parents[1]


def opponents() -> list:
    out = [OfficialBaseline(), RandomPlayer()]
    for d in sorted((ROOT / "versions").glob("*/agent")):
        mod = importlib.import_module(f"versions.{d.parent.name}.agent.agent")
        out.append(ContestantPlayer(mod.create_agent, d.parent.name))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=200)
    ap.add_argument("--seed", type=int, default=31)
    a = ap.parse_args()
    from agent.agent import create_agent
    ours = ContestantPlayer(create_agent, "candidate")
    print(f"{'opponent':22s} {'win%':>6s}  95% CI          invalid(ours/opp)")
    with Showdown() as sd:
        for opp in opponents():
            wins = 0
            inv = [0, 0]
            t0 = time.time()
            for g in range(a.games):
                seat = g % 2
                players = [ours, opp] if seat == 0 else [opp, ours]
                r = play_match(sd, players[0], players[1], seed=a.seed * 100000 + g)
                wins += r.winner == seat
                inv[0] += r.invalid[seat]
                inv[1] += r.invalid[1 - seat]
            lo, hi = wilson(wins, a.games)
            print(f"{opp.name:22s} {100 * wins / a.games:5.1f}%  [{100 * lo:4.1f}, {100 * hi:4.1f}]   {inv[0]}/{inv[1]}"
                  f"   ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
