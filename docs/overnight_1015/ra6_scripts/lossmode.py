"""Replay the lost games of a league JSON (code root = PYTHONPATH / cwd, env as given) and print our bot's key events
(waves, retreats, rush / early-army flags) per game: is the loss our blind attack or their push?
    python lossmode.py league.json [max_games] [ENV=VAL ...]"""
import json
import os
import re
import sys

path = sys.argv[1]
mx = int(sys.argv[2]) if len(sys.argv) > 2 and "=" not in sys.argv[2] else 99
for a in sys.argv[2:]:
    if "=" in a:
        k, v = a.split("=", 1)
        os.environ[k] = v
os.environ["ARENA_RECORD"] = "0"
import local.ra_platform as P  # noqa: E402
import agent.agent as AA  # noqa: E402
import agent.redalert as RA  # noqa: E402

KEEP = re.compile(r"attack wave|retreat|early rush|early enemy|two enemy|all-in|allin|hold|harvester")
rows = [r for r in json.load(open(path))["rows"] if r["result_a"] != "win"][:mx]
cap = []
orig = RA.RedAlertPolicy.log_event


def log_event(self, msg):
    cap.append(msg)
    return orig(self, msg)


RA.RedAlertPolicy.log_event = log_event
for r in rows:
    cap.clear()
    fa, fb = P.make_factory("ours"), P.make_factory(r["b_spec"])
    row = P.play_match(fa(r["seed"] * 2 + 1), fb(r["seed"] * 2 + 2), seed=r["seed"], map_id=r["map"], a_seat=r["a_seat"])
    a = row["a"]
    ev = [m for m in cap if KEEP.search(m)]
    print(f"{r['b_spec']} {r['map']} {r['seed']} s{r['a_seat']}: {row['result_a']} {row['game_s']/60:.1f}m "
          f"K/D {a['kills_cost']}/{a['deaths_cost']} peak {a['peak_army']} vs {row['b']['peak_army']}"
          f"{'' if row['result_a'] == r['result_a'] and row['game_s'] == r['game_s'] else ' (DIFFERS from json)'}")
    for m in ev[:8]:
        print("     ", m[:150])
