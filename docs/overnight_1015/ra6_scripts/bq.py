"""Debug: the scripted bot's production queue and orders over time in one sim game.
    python bq.py <opp> <seed> <map> <seat> <t0> <t1>"""
import os
import sys

os.environ["ARENA_RECORD"] = "0"
import local.ra_platform as P  # noqa: E402
import agent.agent as AA  # noqa: E402

opp, seed, map_id, seat, t0, t1 = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4]), float(sys.argv[5]), float(sys.argv[6])
bot = P.make_factory(opp)(seed * 2 + 2)
orig = bot.decide
last = [-1]


def dec(raw, obs, la):
    out = orig(raw, obs, la)
    gs = (raw.get("time") or {}).get("game_seconds") or obs["tick"] / 25
    if t0 <= gs <= t1 and int(gs) // 5 != last[0]:
        last[0] = int(gs) // 5
        e = obs.get("economy") or {}
        print(f"{gs:5.0f} cash={e.get('cash')} ore={e.get('ore')} pow={e.get('power_provided')}/{e.get('power_drained')} "
              f"prod={[(p.get('queue'), p.get('item'), round(p.get('progress', 0), 2)) for p in obs.get('production') or []]} "
              f"avail={[a for a in obs.get('available_production') or [] if a in ('1tnk','2tnk','3tnk','jeep','harv','fix')]} out={[o for o in out if o.get('cmd') in ('train','build')]}")
    return out


bot.decide = dec
row = P.play_match(AA.create_agent(), bot, seed=seed, map_id=map_id, a_seat=seat)
print("RESULT", row["result_a"], row["reason"], f"{row['game_s']/60:.1f}m")
