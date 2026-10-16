"""One sim game: our groups / the enemy in sight, every N s between t0 and t1 (positions, values, state, K/D).
    python fight.py <opp> <seed> <map> <seat> <t0> <t1> [every] [ENV=VAL ...]"""
import collections
import math
import os
import sys

opp, seed, map_id, seat, t0, t1 = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4]), float(sys.argv[5]), float(sys.argv[6])
every = float(sys.argv[7]) if len(sys.argv) > 7 and "=" not in sys.argv[7] else 3.0
for a in sys.argv[7:]:
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


def cen(us):
    if not us:
        return None
    return (round(sum(u["x"] for u in us) / len(us)), round(sum(u["y"] for u in us) / len(us)))


def decide(self, raw, config=None):
    out = orig(self, raw, config)
    v = self.view
    if v is None:
        return out
    evs = self.events
    if len(evs) < nev[0]:
        nev[0] = 0
    for e in evs[nev[0]:]:
        if t0 - 30 <= v.game_s <= t1:
            print(f"   [{v.game_s:5.0f}] {e[:140]}")
    nev[0] = len(evs)
    if t0 <= v.game_s <= t1 and v.game_s - last[0] >= every:
        last[0] = v.game_s
        a = self.army
        rules = v.st.rules

        def grp(ids):
            us = [v.unit_by_id[i] for i in ids if i in v.unit_by_id]
            c = collections.Counter(u["type"] for u in us)
            return f"{len(us)}@{cen(us)} {dict(c)}"
        ens = [e for e in v.enemies if e["type"] not in ("harv",) and not rules.is_building(e["type"])]
        ec = collections.Counter(e["type"] for e in ens)
        base = v.base
        dmin = min((math.hypot(e["x"] - base[0], e["y"] - base[1]) for e in ens), default=None) if base else None
        print(f"t={v.game_s:5.0f} st={a.state} K/D={v.kills}/{v.deaths} base={base} rally={v.st.rally} "
              f"main {grp(a.main)} | home {grp(a.home)} | reinf {grp(a.reinf)} || EN {len(ens)}@{cen(ens)} "
              f"d={dmin and round(dmin)} {dict(ec)}", flush=True)
    return out


RA.RedAlertPolicy.decide = decide
fa, fb = P.make_factory("ours"), P.make_factory(opp)
row = P.play_match(fa(seed * 2 + 1), fb(seed * 2 + 2), seed=seed, map_id=map_id, a_seat=seat)
a = row["a"]
print("RESULT", row["result_a"], row["reason"], f"{row['game_s']/60:.1f}m", "K/D", a["kills_cost"], a["deaths_cost"])
