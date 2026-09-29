"""How close is the engine search's rebuilt battle to the truth? (local simulator only; ARENA_BATTLE=search agent)

Plays local games (agent/ with ARENA_BATTLE=search vs an opponent package, ARENA_PLOG=1 so the states carry the
protocol log the platform sends) and, at every one of our battle decisions, compares the battle the search rebuilt
from platform-legal information (agent/pokemon/battlestate.py -> agent/js/search.js build) with the simulator's true
battle (local/js/bridge.js `dump`): actives, the foe's brought four, HP, status, boosts, items, volatiles, Fake Out
availability, side / field conditions and their turns left, sleep / toxic counters and the enabled moves. Reports the
mismatch rate of every field and the first examples.

    python scripts/search_fidelity.py --games 20 --seed 91 [--opp champion] [--no-plog] [--same-team]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--games", type=int, default=10)
    ap.add_argument("--seed", type=int, default=91)
    ap.add_argument("--opp", default="champion", help="opponent package holding agent/agent.py (default champion)")
    ap.add_argument("--no-plog", action="store_true", help="without the protocol log (observation only)")
    ap.add_argument("--same-team", action="store_true")
    ap.add_argument("--examples", type=int, default=4)
    a = ap.parse_args()
    os.environ["ARENA_PLOG"] = "0" if a.no_plog else "1"
    os.environ["ARENA_BATTLE"] = "search"
    os.environ["ARENA_RECORD"] = "0"
    import importlib
    from agent.pokemon import battlestate
    from agent.pokemon.engine import SearchEngine
    from local.pokemon_env import ContestantPlayer, play_match
    from local.showdown import Showdown
    import agent.agent as cand

    opp_mod = importlib.import_module(f"{a.opp}.agent.agent")
    last: list = []
    orig = battlestate.build

    def rec(*args, **kw):
        s = orig(*args, **kw)
        last.append(s)
        return s

    battlestate.build = rec
    stats = collections.Counter()
    examples: dict[str, list] = collections.defaultdict(list)

    def bad(key: str, detail) -> None:
        stats[key] += 1
        if len(examples[key]) < a.examples:
            examples[key].append(detail)

    class Wrap(ContestantPlayer):
        def bind(self, match, seat):
            self.match, self.sid = match, ("p1" if seat == 0 else "p2")

        def choose(self, raw):
            if raw.get("phase") != "moving":
                return super().choose(raw)
            truth = self.match.sd.dump()
            last.clear()
            out = super().choose(raw)
            if not last:
                stats["no_state"] += 1
                return out
            eng = SearchEngine.get()
            mine = eng.inspect(state=last[-1])
            real = eng.inspect(battle_json=truth)
            stats["decisions"] += 1
            if mine is None or real is None:
                stats["inspect_failed"] += 1
                return out
            compare(mine, real, self.sid, raw, last[-1])
            return out

    def compare(mine: dict, real: dict, me: str, raw: dict, state: dict) -> None:
        turn = (raw.get("observation") or {}).get("turn")
        for side in ("p1", "p2"):
            who = "me" if side == me else "foe"
            A, B = mine["sides"][side], real["sides"][side]
            if A["active"] != B["active"]:
                bad(f"{who}.active", {"turn": turn, "rebuilt": A["active"], "true": B["active"]})
            ra = {m["species"]: m for m in A["mons"]}
            rb = {m["species"]: m for m in B["mons"]}
            if who == "foe":
                wrong = set(ra) - set(rb)
                stats["foe.slots"] += len(ra)
                stats["foe.filled"] += len(state.get("meta", {}).get("filled") or [])
                if wrong:
                    stats["foe.wrong_mons"] += len(wrong)
                    bad("foe.brought", {"turn": turn, "rebuilt": sorted(ra), "true": sorted(rb)})
            for sp, m in ra.items():
                t = rb.get(sp)
                if t is None:
                    continue
                stats[f"{who}.mons"] += 1
                if m["fainted"] != t["fainted"]:
                    bad(f"{who}.fainted", {"turn": turn, "sp": sp, "rebuilt": m["fainted"], "true": t["fainted"]})
                    continue
                if t["fainted"]:
                    continue
                d = abs(m["hp"] / max(1, m["maxhp"]) - t["hp"] / max(1, t["maxhp"]))
                stats[f"{who}.hp_err_sum_x1000"] += int(1000 * d)
                if (who == "me" and m["hp"] != t["hp"]) or d > 0.011:
                    bad(f"{who}.hp", {"turn": turn, "sp": sp, "rebuilt": m["hp"], "true": t["hp"], "max": t["maxhp"]})
                if m["status"] != t["status"]:
                    bad(f"{who}.status", {"turn": turn, "sp": sp, "rebuilt": m["status"], "true": t["status"]})
                if m["item"] != t["item"]:
                    bad(f"{who}.item", {"turn": turn, "sp": sp, "rebuilt": m["item"], "true": t["item"]})
                if m["ability"] != t["ability"]:
                    bad(f"{who}.ability", {"turn": turn, "sp": sp, "rebuilt": m["ability"], "true": t["ability"]})
                if t["isActive"]:
                    stats[f"{who}.active_mons"] += 1
                    if m["boosts"] != t["boosts"]:
                        bad(f"{who}.boosts", {"turn": turn, "sp": sp, "rebuilt": m["boosts"], "true": t["boosts"]})
                    if (m["activeMoveActions"] > 0) != (t["activeMoveActions"] > 0):
                        bad(f"{who}.fakeout_state", {"turn": turn, "sp": sp, "rebuilt": m["activeMoveActions"],
                                                     "true": t["activeMoveActions"]})
                    va, vb = m["volatiles"], t["volatiles"]
                    for k in set(va) | set(vb):
                        if k not in va or k not in vb:
                            bad(f"{who}.vol.{k}", {"turn": turn, "sp": sp, "rebuilt": va.get(k), "true": vb.get(k)})
                        elif va[k] != vb[k]:
                            bad(f"{who}.volfield.{k}", {"turn": turn, "sp": sp, "rebuilt": va[k], "true": vb[k]})
                    if m.get("slp") != t.get("slp"):
                        bad(f"{who}.slp_time", {"turn": turn, "sp": sp, "rebuilt": m.get("slp"), "true": t.get("slp")})
                    if m.get("tox") != t.get("tox"):
                        bad(f"{who}.tox_stage", {"turn": turn, "sp": sp, "rebuilt": m.get("tox"), "true": t.get("tox")})
                    if m.get("fallen") != t.get("fallen"):
                        bad(f"{who}.fallen", {"turn": turn, "sp": sp, "rebuilt": m.get("fallen"), "true": t.get("fallen")})
            if A["conditions"] != B["conditions"]:
                bad(f"{who}.conditions", {"turn": turn, "rebuilt": A["conditions"], "true": B["conditions"]})
            if who == "me" and A.get("moves") != B.get("moves") and B.get("request") == "move":
                bad("me.enabled_moves", {"turn": turn, "rebuilt": A.get("moves"), "true": B.get("moves")})
            if who == "me" and A.get("request") != B.get("request"):
                bad("me.request", {"turn": turn, "rebuilt": A.get("request"), "true": B.get("request")})
        fa, fb = mine["field"], real["field"]
        for k in ("weather", "terrain"):
            if fa[k] != fb[k] or (fb[k] and fa[f"{k}_d"] != fb[f"{k}_d"]):
                bad(f"field.{k}", {"turn": turn, "rebuilt": [fa[k], fa[f"{k}_d"]], "true": [fb[k], fb[f"{k}_d"]]})
        if fa["pseudo"] != fb["pseudo"]:
            bad("field.pseudo", {"turn": turn, "rebuilt": fa["pseudo"], "true": fb["pseudo"]})

    cp = Wrap(cand.create_agent, "cand")
    op = ContestantPlayer(opp_mod.create_agent, a.opp)
    t0 = time.time()
    wins = 0
    reasons: dict = {}
    with Showdown() as sd:
        for g in range(a.games):
            seat = g % 2
            players = [cp, op] if seat == 0 else [op, cp]
            r = play_match(sd, players[0], players[1], seed=a.seed * 100000 + g, same_team=a.same_team)
            wins += r.winner == seat
            st = cp.agent.pk.search_stats
            stats["searched"] += st["searched"]
            stats["search_fallbacks"] += st["fallback"]
            for k, v in st["reasons"].items():
                reasons[k] = reasons.get(k, 0) + v
    n = max(1, stats["decisions"])
    print(f"{a.games} games, {stats['decisions']} decisions, {time.time() - t0:.0f}s; wins {wins}/{a.games}; "
          f"protocol log {'off' if a.no_plog else 'on'}; search fallbacks {stats['search_fallbacks']} {reasons or ''}")
    print(f"foe four: {stats['foe.filled']} of {stats['foe.slots']} slots filled from the prior, "
          f"{stats['foe.wrong_mons']} wrong ({100 * stats['foe.wrong_mons'] / max(1, stats['foe.slots']):.1f}% of slots)")
    for who in ("me", "foe"):
        m = max(1, stats[f"{who}.mons"])
        print(f"{who}: mean |HP error| {stats[f'{who}.hp_err_sum_x1000'] / m / 10:.2f}% of max HP over {m} alive mons")
    print("mismatches (per decision):")
    for k, v in sorted(stats.items(), key=lambda kv: -kv[1]):
        if k.split(".")[0] in ("me", "foe", "field") and not k.endswith(("mons", "slots", "filled", "x1000")) \
                and k != "foe.wrong_mons":
            print(f"  {k:28s} {v:5d}  ({100 * v / n:.1f}%)")
    print("examples:")
    for k, ex in examples.items():
        for e in ex:
            print(f"  {k}: {json.dumps(e)[:300]}")


if __name__ == "__main__":
    main()
