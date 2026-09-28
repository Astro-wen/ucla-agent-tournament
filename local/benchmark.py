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


def _which_calc() -> None:
    """Warn when an archived version runs the CANDIDATE's calc.js (same bug as local/ladder.py `_which_calc`): an old
    versions/<name>/agent/pokemon/calc.py falls back to the repo's agent/js/calc.js when its own agent/js has no
    node_modules, so after a calculator change that row silently measures the candidate's calculator."""
    try:
        import agent.pokemon.calc as cand_calc
        d = cand_calc._js_dir()
        cand = (d / "calc.js").resolve() if d else None
        for v in sorted((ROOT / "versions").glob("*/agent")):
            try:
                mod = importlib.import_module(f"versions.{v.parent.name}.agent.pokemon.calc")
                vd = mod._js_dir()
                runs = (vd / "calc.js").resolve() if vd else None
                own = v / "js" / "calc.js"
                if runs and runs == cand and own.exists() and own.read_bytes() != runs.read_bytes():
                    print(f"WARNING: {v.parent.name} runs the candidate's calc.js, not its own {own} "
                          f"(link {v / 'js' / 'node_modules'} to agent/js/node_modules)")
            except Exception as e:                                  # noqa: BLE001 - diagnostics only
                print(f"({v.parent.name}: could not resolve its calc.js: {e!r})")
    except Exception as e:                                          # noqa: BLE001 - diagnostics only
        print(f"(could not resolve calc.js paths: {e!r})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=200)
    ap.add_argument("--seed", type=int, default=31)
    a = ap.parse_args()
    _which_calc()
    from agent.agent import create_agent
    ours = ContestantPlayer(create_agent, "candidate")
    print(f"{'opponent':22s} {'win%':>6s}  95% CI          invalid(ours/opp)")
    total_dead = total_games = 0
    with Showdown() as sd:
        for opp in opponents():
            wins = dead_ends = 0
            inv = [0, 0]
            t0 = time.time()
            for g in range(a.games):
                seat = g % 2
                players = [ours, opp] if seat == 0 else [opp, ours]
                r = play_match(sd, players[0], players[1], seed=a.seed * 100000 + g)
                wins += r.winner == seat
                inv[0] += r.invalid[seat]
                inv[1] += r.invalid[1 - seat]
                dead_ends += any(getattr(r, "clause_waived", None) or [])
            lo, hi = wilson(wins, a.games)
            total_dead, total_games = total_dead + dead_ends, total_games + a.games
            waived = f"   draft dead ends (Item Clause waived) {dead_ends} games" if dead_ends else ""
            print(f"{opp.name:22s} {100 * wins / a.games:5.1f}%  [{100 * lo:4.1f}, {100 * hi:4.1f}]   {inv[0]}/{inv[1]}"
                  f"   ({time.time() - t0:.0f}s){waived}")
    # local/pokemon_env.py Match.draft: a pick where no undrafted card was Item-Clause legal (clause waived for it)
    print(f"draft dead ends (Item Clause waived): {total_dead} of {total_games} games")


if __name__ == "__main__":
    main()
