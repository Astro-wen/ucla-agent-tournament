"""Head-to-head of two Drafter settings: both seats run this agent (same model, same battle code), only the
Drafter(...) overrides differ; same seeds, sides alternated.

    python scripts/draft_h2h.py --games 400 --seed 5 --a '{"max_depth": 4}' --b '{"max_depth": 1}'
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

os.environ["ARENA_RECORD"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.agent import create_agent                       # noqa: E402
from local.eval_pokemon import wilson                      # noqa: E402
from local.pokemon_env import ContestantPlayer, play_match  # noqa: E402
from local.showdown import Showdown                        # noqa: E402


def factory(kw: dict):
    def make():
        c = create_agent()
        c.pk.drafter_kwargs = dict(kw)
        return c
    return make


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=200)
    ap.add_argument("--seed", type=int, default=5)
    ap.add_argument("--a", default="{}")
    ap.add_argument("--b", default="{}")
    a = ap.parse_args()
    ka, kb = json.loads(a.a), json.loads(a.b)
    A, B = ContestantPlayer(factory(ka), "A"), ContestantPlayer(factory(kb), "B")
    wins = 0
    t0 = time.time()
    with Showdown() as sd:
        for g in range(a.games):
            seat = g % 2
            players = [A, B] if seat == 0 else [B, A]
            r = play_match(sd, players[0], players[1], seed=a.seed * 100000 + g)
            wins += r.winner == seat
    lo, hi = wilson(wins, a.games)
    print(f"A {ka} vs B {kb}: {wins}/{a.games} = {100 * wins / a.games:.1f}%, 95% CI [{100 * lo:.1f}, {100 * hi:.1f}]"
          f" | {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
