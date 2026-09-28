"""Singles formats panel: an agent vs reference opponents on the local singles envs (local/singles_env.py).

    python -m local.singles_eval --format same --games 200                  # candidate vs every opponent
    python -m local.singles_eval --format all --games 100 --opponents official_baseline,random,champion
    python -m local.singles_eval --format random --games 200 --agent champion --opponents v1_exact_damage

Agents / opponents: candidate (agent/), champion (champion/agent), versions/<name>, official_baseline, random.
Seats alternate; game g uses seed (seed * 100000 + g) for every row, so rows are paired across agents.
Prints one line per (format, opponent): wins, ties, 95% Wilson CI of wins / games, invalid answers (ours / theirs),
average turns, our average and max decision time.
"""
from __future__ import annotations

import argparse
import importlib
import json
import time
from pathlib import Path

from .eval_pokemon import wilson
from .showdown import Showdown
from .singles_env import FORMATS, ContestantPlayer, OfficialBaseline, RandomPlayer, play_singles

ROOT = Path(__file__).resolve().parents[1]


def make_player(name: str):
    if name == "official_baseline":
        return OfficialBaseline()
    if name == "random":
        return RandomPlayer()
    if name == "candidate":
        mod = importlib.import_module("agent.agent")
    elif name == "champion":
        mod = importlib.import_module("champion.agent.agent")
    else:
        mod = importlib.import_module(f"versions.{name}.agent.agent")
    return ContestantPlayer(mod.create_agent, name)


class Timed:
    """Wraps a player to time its decisions."""

    def __init__(self, p):
        self.p, self.name = p, p.name
        self.times: list[float] = []

    def reset(self):
        self.p.reset()

    def choose(self, raw):
        t = time.perf_counter()
        try:
            return self.p.choose(raw)
        finally:
            self.times.append(time.perf_counter() - t)


def run(fmt: str, agent: str, opp_name: str, games: int, seed: int, opp_team: str = "platform") -> dict:
    ours = Timed(make_player(agent))
    opp = make_player(opp_name)
    wins = ties = 0
    inv = [0, 0]
    turns = []
    errors: list = []
    t0 = time.time()
    with Showdown() as sd:
        for g in range(games):
            seat = g % 2
            players = [ours, opp] if seat == 0 else [opp, ours]
            r = play_singles(sd, fmt, players[0], players[1], seed=seed * 100000 + g, opp_team=opp_team)
            if r.winner is None:
                ties += 1
            elif r.winner == seat:
                wins += 1
            inv[0] += r.invalid[seat]
            inv[1] += r.invalid[1 - seat]
            turns.append(r.turns)
            for who, choice, detail in r.errors:
                errors.append(("ours" if who == seat else "opp", choice, str(detail)[:120]))
    lo, hi = wilson(wins, games)
    ts = sorted(ours.times) or [0.0]
    return {"errors": errors[:10],
            "format": fmt, "agent": agent, "opponent": opp_name, "wins": wins, "ties": ties, "games": games,
            "win_rate": wins / games, "ci": [lo, hi], "invalid_ours": inv[0], "invalid_opp": inv[1],
            "avg_turns": sum(turns) / max(1, len(turns)), "avg_ms": 1000 * sum(ts) / len(ts),
            "p99_ms": 1000 * ts[max(0, int(len(ts) * 0.99) - 1)], "max_ms": 1000 * ts[-1], "secs": time.time() - t0}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--format", default="all", help=f"one of {sorted(FORMATS)} or all")
    ap.add_argument("--games", type=int, default=100)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--agent", default="candidate")
    ap.add_argument("--opponents", default="official_baseline,random,v0_heuristic,v1_exact_damage,champion")
    ap.add_argument("--opp-team", default="platform", choices=["platform", "factory", "randbats"],
                    help="teambuild: what a team-less seat gets (the platform's fallback, a random factory OU team -- "
                         "the agent's own source of foe sets, flattering --, or a random-battle team at lv 100)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-calc", action="store_true",
                    help="the candidate plays without the damage calculator (the fallback path; opponents keep theirs)")
    a = ap.parse_args()
    if a.no_calc:
        from agent.pokemon.calc import DamageCalc
        DamageCalc.get().enabled = False
    fmts = sorted(FORMATS) if a.format == "all" else a.format.split(",")
    for fmt in fmts:
        for opp in a.opponents.split(","):
            r = run(fmt, a.agent, opp, a.games, a.seed, a.opp_team)
            print(f"{fmt:9s} {a.agent} vs {opp:18s} {r['wins']:4d}/{r['games']} = {100 * r['win_rate']:5.1f}% "
                  f"(ties {r['ties']}) CI [{100 * r['ci'][0]:4.1f}, {100 * r['ci'][1]:4.1f}] | invalid "
                  f"{r['invalid_ours']}/{r['invalid_opp']} | turns {r['avg_turns']:.1f} | ms avg {r['avg_ms']:.0f} "
                  f"p99 {r['p99_ms']:.0f} max {r['max_ms']:.0f} | {r['secs']:.0f}s", flush=True)
            if a.json:
                print(json.dumps(r), flush=True)


if __name__ == "__main__":
    main()
