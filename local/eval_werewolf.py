"""Measure our Werewolf agent against reference seats on the local engine.

    python -m local.eval_werewolf --games 2000

Three views, all paired on the same seeds (same roles, same seat):
  A. one seat is ours, the other six are the reference  -> is our seat's score higher than a reference seat there?
  B. every seat is ours vs every seat is the reference  -> villager-side win rate of a full table
  C. both wolves are ours vs both wolves reference (villagers reference) -> wolf-side win rate
"""
from __future__ import annotations

import argparse
import random
from collections import defaultdict

from agent.agent import create_agent

from .werewolf_env import Game, ContestantSeat, OfficialBaseline, RandomSeat

REFS = {"official_baseline": OfficialBaseline, "random": RandomSeat}


def run(seats, seed):
    g = Game(seats, random.Random(seed))
    return g.play(), g.roles, g.winner


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=1000)
    ap.add_argument("--ref", choices=REFS, default="official_baseline")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    Ref = REFS[a.ref]
    ours = lambda: ContestantSeat(create_agent)

    # A: single seat replacement, paired
    by_role = defaultdict(lambda: [0.0, 0.0, 0])
    for g in range(a.games):
        seed = a.seed * 100000 + g
        k = g % 7
        random.seed(seed)
        r_ref, roles, _ = run([Ref() for _ in range(7)], seed)
        random.seed(seed)
        r_our, _, _ = run([ours() if i == k else Ref() for i in range(7)], seed)
        acc = by_role[roles[k]]
        acc[0] += r_our[k]
        acc[1] += r_ref[k]
        acc[2] += 1
    print(f"A. one seat ours among six {a.ref} (avg return of that seat, +1/-1; paired, n per role):")
    for role in ("villager", "seer", "wolf"):
        s_our, s_ref, n = by_role[role]
        print(f"   as {role:8s}: ours {s_our / n:+.3f}  vs  {a.ref} {s_ref / n:+.3f}   (n={n})")

    # B: full table
    wins_our = wins_ref = 0
    for g in range(a.games):
        seed = a.seed * 100000 + g
        random.seed(seed)
        _, _, w1 = run([ours() for _ in range(7)], seed)
        random.seed(seed)
        _, _, w2 = run([Ref() for _ in range(7)], seed)
        wins_our += w1 == "villager"
        wins_ref += w2 == "villager"
    print(f"B. full table villager-side win rate: all ours {100 * wins_our / a.games:.1f}%  vs  all {a.ref} {100 * wins_ref / a.games:.1f}%")

    # C: wolves ours vs wolves ref, villagers ref
    ww_our = ww_ref = 0
    for g in range(a.games):
        seed = a.seed * 100000 + g
        random.seed(seed)
        probe = Game([Ref() for _ in range(7)], random.Random(seed))
        wolves = [i for i in range(7) if probe.roles[i] == "wolf"]
        random.seed(seed)
        _, _, w1 = run([ours() if i in wolves else Ref() for i in range(7)], seed)
        random.seed(seed)
        _, _, w2 = run([Ref() for _ in range(7)], seed)
        ww_our += w1 == "wolf"
        ww_ref += w2 == "wolf"
    print(f"C. wolf-side win rate vs {a.ref} villagers: our wolves {100 * ww_our / a.games:.1f}%  vs  {a.ref} wolves {100 * ww_ref / a.games:.1f}%")


if __name__ == "__main__":
    main()
