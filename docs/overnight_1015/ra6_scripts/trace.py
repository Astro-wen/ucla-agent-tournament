"""Replay one league game (same seeds as local.ra_platform._one) and print our bot's state every N game seconds.
    python trace.py <opp> <seed> <map> <a_seat> [every_s] [ENV=VAL ...]      (code root: PYTHONPATH / cwd)"""
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
import agent.redalert as RA  # noqa: E402
import agent.agent as AA  # noqa: E402

orig = RA.RedAlertPolicy.decide
last = [-1e9]


nev = [0]


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
            h = getattr(self, "harv", None)
            a = self.army
            inf = sum(1 for u in v.units if u["type"] in ("e1", "e2", "e3"))
            veh = sum(1 for u in v.units if u["type"] in ("1tnk", "2tnk", "3tnk", "4tnk", "jeep", "apc", "ftrk"))
            hs = ""
            if h is not None and self.p.harv_first:
                hs = (f"want={h.want(v)} cnt={h.count(v)} ser={h._serial} why={h.threat(v)} ")
            print(f"t={v.game_s:5.0f} cash={v.money:5d} harv={v.unit_count.get('harv', 0)} procs={v.have.get('proc', 0)} "
                  f"weap={v.have.get('weap', 0)} fix={v.have.get('fix', 0)} inf={inf} veh={veh} army={v.army_value} "
                  f"K/D={v.kills}/{v.deaths} {hs}state={a.state} thr={a.threatened}:{a.threat_value:.0f} "
                  f"rush={a.rush} ea={a.early_army} main={len(a.main)}:{a.value(a.main, v):.0f} home={len(a.home)} "
                  f"str={a.enemy_strength(v):.0f} seen={a.base_seen} blind={a.blind(v)} en={len(v.enemies)}",
                  flush=True)
    except Exception as e:  # noqa: BLE001
        print("trace error", e)
    return out


RA.RedAlertPolicy.decide = decide
row = P.play_match(AA.create_agent(), P.make_factory(opp)(seed * 2 + 2), seed=seed, map_id=map_id, a_seat=seat)
a = row["a"]
print("RESULT", row["result_a"], row["reason"], f"{row['game_s']/60:.1f}m", "K/D", a["kills_cost"], a["deaths_cost"],
      "army^", a["peak_army"], "vs", row["b"]["peak_army"])
