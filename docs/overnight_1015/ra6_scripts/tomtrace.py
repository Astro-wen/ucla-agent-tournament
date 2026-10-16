"""Replay one league game (same seeds as local.ra_platform) and print BOTH sides every N game seconds: our bot's
state (as trace.py) and the scripted opponent's economy / army.
    python tomtrace.py <opp> <seed> <map> <a_seat> [every_s] [ENV=VAL ...]      (code root: PYTHONPATH / cwd)"""
import collections
import os
import sys

opp, seed, map_id, seat = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4])
every = float(sys.argv[5]) if len(sys.argv) > 5 and "=" not in sys.argv[5] else 30.0
for a in sys.argv[5:]:
    if "=" in a:
        k, v = a.split("=", 1)
        os.environ[k] = v
os.environ["ARENA_RECORD"] = "0"
import local.ra_platform as P  # noqa: E402
import local.ra_bots_strong as S  # noqa: E402
import agent.redalert as RA  # noqa: E402
import agent.agent as AA  # noqa: E402

orig = RA.RedAlertPolicy.decide
last = [-1e9]
lastb = [-1e9]
nev = [0]
QUIET = os.environ.get("TT_QUIET")


def decide(self, raw, config=None):
    out = orig(self, raw, config)
    try:
        v = self.view
        evs = self.events
        if len(evs) < nev[0]:
            nev[0] = 0
        for e in evs[nev[0]:]:
            print(f"   [{v.game_s:5.0f}] {e}", flush=True)
        nev[0] = len(evs)
        if v is not None and v.game_s - last[0] >= every:
            last[0] = v.game_s
            a = self.army
            ty = collections.Counter(u["type"] for u in v.units if u["type"] not in ("harv", "mcv"))
            tw = collections.Counter(b["type"] for b in v.buildings if b["type"] in ("gun", "tsla", "ftur", "pbox", "hbox"))
            print(f"A t={v.game_s:5.0f} cash={v.money:5d} harv={v.unit_count.get('harv', 0)} procs={v.have.get('proc', 0)} "
                  f"weap={v.have.get('weap', 0)} army={v.army_value} K/D={v.kills}/{v.deaths} {dict(ty)} tw={dict(tw)} "
                  f"state={a.state} thr={a.threatened}:{a.threat_value:.0f} ea={a.early_army} "
                  f"main={len(a.main)}:{a.value(a.main, v):.0f} home={len(a.home)} str={a.enemy_strength(v):.0f}",
                  flush=True)
    except Exception as e:  # noqa: BLE001
        print("trace error", e)
    return out


RA.RedAlertPolicy.decide = decide
bot = P.make_factory(opp)(seed * 2 + 2)
origb = bot.decide


def bdecide(raw, obs, la):
    out = origb(raw, obs, la)
    gs = (raw.get("time") or {}).get("game_seconds") or obs["tick"] / 25
    if gs - lastb[0] >= every:
        lastb[0] = gs
        e = obs.get("economy") or {}
        m = obs.get("military") or {}
        ty = collections.Counter(u["type"] for u in obs.get("units") or [] if not u.get("wreck") and u["type"] not in ("harv",))
        bl = collections.Counter(b["type"] for b in obs.get("buildings") or [])
        print(f"B t={gs:5.0f} cash={e.get('cash')} ore={e.get('ore')} harv={e.get('harvesters')} army={m.get('army_value')} "
              f"{dict(ty)} blds={dict(bl)} att={len(getattr(bot, 'att', ()))} pushed={getattr(bot, 'pushed', None)}",
              flush=True)
    return out


bot.decide = bdecide
row = P.play_match(AA.create_agent(), lambda *_a, **_k: bot, seed=seed, map_id=map_id, a_seat=seat) \
    if False else P.play_match(AA.create_agent(), bot, seed=seed, map_id=map_id, a_seat=seat)
a = row["a"]
print("RESULT", row["result_a"], row["reason"], f"{row['game_s']/60:.1f}m", "K/D", a["kills_cost"], a["deaths_cost"],
      "army^", a["peak_army"], "vs", row["b"]["peak_army"])
