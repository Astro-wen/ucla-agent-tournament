"""Measure our Pokémon agent against reference players on the local Showdown env.

    python -m local.eval_pokemon --games 200 --opponent official_baseline
    python -m local.eval_pokemon --games 200 --opponent smoke          # an organizers' example agent (local/field_opps.py)
    OPENAI_API_KEY=... python -m local.eval_pokemon --games 20 --opponent llm_live   # examples/llm_agent.py, real model
    python -m local.eval_pokemon --games 10 --opponent llm_real   # the same agent on Nebius (NEBIUS_API_KEY; capped spend)
"""
from __future__ import annotations

import argparse
import math
import time

from agent.agent import create_agent

from . import field_opps
from .pokemon_env import ContestantPlayer, OfficialBaseline, RandomPlayer, play_match
from .showdown import Showdown

OPPONENTS = {"official_baseline": OfficialBaseline, "random": RandomPlayer,
             **{name: (lambda name=name: field_opps.make(name)) for name in (*field_opps.FIELD, *field_opps.LIVE)}}


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
    timer = [0, 0]
    forfeits = [0, 0]
    pf = {}
    turns = []
    llm = live = None
    if a.opponent == "llm_real":                 # a spend cap reached mid-run would leave the rest to the rule fallback
        from .llm_client import shared_client
        llm = shared_client()
        live, refused0 = llm.available, llm.stats.refused
        if not live:
            print(f"WARNING: Nebius unavailable ({llm.unavailable_reason}): llm_real plays examples/llm_agent.py's "
                  f"rule fallback (= llm_fallback), not a model", flush=True)
    t0 = time.time()
    with Showdown() as sd:
        for g in range(a.games):
            if live and (llm.stats.refused > refused0 or not llm.available):
                print(f"WARNING: Nebius refused requests after {g} games ({llm.unavailable_reason or 'a spend cap'})"
                      f": stopping here; the rest would be rule fallback (the last game may be partly)", flush=True)
                break
            seat = g % 2                       # alternate sides
            players = [ours, opp] if seat == 0 else [opp, ours]
            r = play_match(sd, players[0], players[1], seed=a.seed * 100000 + g)
            if r.winner is None:
                ties += 1
            elif r.winner == seat:
                wins += 1
            inv_ours += r.invalid[seat]
            inv_opp += r.invalid[1 - seat]
            timer[0] += r.timer_defaults[seat]
            timer[1] += r.timer_defaults[1 - seat]
            if r.forfeit is not None:
                forfeits[r.forfeit != seat] += 1
            for k, v in getattr(opp, "stats", {}).items():
                pf[k] = pf.get(k, 0) + v
            turns.append(r.turns)
            if a.verbose:
                print(f"game {g:3d} seat {seat} -> {'WIN' if r.winner == seat else 'tie' if r.winner is None else 'loss'}"
                      f" in {r.turns} turns | ours {r.rosters[seat]} bring {r.brought[seat]} | inv {r.invalid}"
                      + (f" | forfeit seat {r.forfeit}" if r.forfeit is not None else ""))
    n = len(turns)
    if not n:
        print("no games played")
        return
    lo, hi = wilson(wins, n)
    print(f"\nours vs {a.opponent}: {wins}/{n} wins ({100 * wins / n:.1f}%), {ties} ties, "
          f"95% CI [{100 * lo:.1f}%, {100 * hi:.1f}%]")
    print(f"invalid/rejected decisions: ours {inv_ours}, {a.opponent} {inv_opp} | avg turns {sum(turns) / n:.1f} | "
          f"{time.time() - t0:.0f}s")
    if pf or any(timer) or any(forfeits):
        print(f"platform model: timer-played decisions ours {timer[0]}, {a.opponent} {timer[1]} | forfeits ours "
              f"{forfeits[0]}, {a.opponent} {forfeits[1]} | {a.opponent} counters {pf}")
    if llm is not None:
        from .llm_seats import invalid_rate
        c = llm
        r = invalid_rate(pf)
        print(f"llm_real ({c.model}): rule fallback on {pf.get('fallbacks', 0)}/{pf.get('requests', 0)} decisions "
              f"({100 * r['fallback_rate']:.1f}%), invalid answers {pf.get('invalid_answers', 0)}/"
              f"{pf.get('model_calls', 0)} model calls ({100 * r['invalid_answer_rate']:.1f}%) | client {c.stats.as_dict()}"
              + ("" if c.available else f" | UNAVAILABLE: {c.unavailable_reason}"))


if __name__ == "__main__":
    main()
