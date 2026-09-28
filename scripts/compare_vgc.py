"""VGC doubles must be byte-identical to the champion: replay every real VGC decision through both agents and play
local VGC games with the same seeds, comparing every answer.

    python scripts/compare_vgc.py [--games 30]

Singles states (pokemon_gen9same etc.) are skipped: those are allowed to differ.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

os.environ["ARENA_RECORD"] = "0"

from altruagent import DecisionContext, GameState, LegalAction  # noqa: E402


def norm(out):
    if isinstance(out, LegalAction):
        return {"action_id": out.action_id, "input": out.input}
    return out


def replay(files) -> tuple[int, int]:
    from agent.agent import Contestant as Cand
    from champion.agent.agent import Contestant as Champ
    n = diff = 0
    for f in files:
        a, b = Cand(), Champ()
        for line in open(f):
            rec = json.loads(line)
            raw = rec["raw"]
            if raw.get("game_type") != "pokemon_vgc_doubles_draft":
                break
            ctx = DecisionContext(session_id=raw.get("session_id", "x"), tournament_id=None,
                                  game_type=raw.get("game_type"), agent_id="cmp")
            oa = norm(a.choose_action(GameState.from_mcp_state(raw), ctx))
            ob = norm(b.choose_action(GameState.from_mcp_state(raw), ctx))
            n += 1
            if json.dumps(oa, sort_keys=True, default=str) != json.dumps(ob, sort_keys=True, default=str):
                diff += 1
                print(f"DIFF {f} {raw.get('phase')}: {oa} != {ob}")
    return n, diff


class Recorder:
    def __init__(self, inner):
        self.inner, self.name = inner, inner.name
        self.log = []

    def reset(self):
        self.inner.reset()

    def choose(self, raw):
        out = self.inner.choose(raw)
        self.log.append(json.dumps(norm(out), sort_keys=True, default=str))
        return out


def local_games(games: int) -> tuple[int, int]:
    from local.pokemon_env import ContestantPlayer, OfficialBaseline, play_match
    from local.showdown import Showdown
    from agent.agent import create_agent as cand
    from champion.agent.agent import create_agent as champ
    n = diff = 0
    with Showdown() as sd:
        for g in range(games):
            outs = []
            for fac in (cand, champ):
                me = Recorder(ContestantPlayer(fac, "x"))
                opp = ContestantPlayer(champ, "champ") if g % 3 == 2 else OfficialBaseline()
                players = [me, opp] if g % 2 == 0 else [opp, me]
                r = play_match(sd, players[0], players[1], seed=4242 * 1000 + g)
                outs.append((r.winner, r.turns, r.invalid, me.log))
            n += 1
            if outs[0] != outs[1]:
                diff += 1
                print(f"DIFF local game {g}: winner/turns {outs[0][:3]} vs {outs[1][:3]}, "
                      f"decisions {len(outs[0][3])} vs {len(outs[1][3])}")
    return n, diff


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=30)
    a = ap.parse_args()
    files = sorted(glob.glob("logs/*/*.jsonl"))
    n, d = replay(files)
    print(f"real VGC decisions: {n} compared, {d} differ")
    m, e = local_games(a.games)
    print(f"local VGC games (ours vs baseline / champion, same seeds): {m} compared, {e} differ")
    return 1 if d or e else 0


if __name__ == "__main__":
    sys.exit(main())
