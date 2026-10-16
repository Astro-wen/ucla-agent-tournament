"""Cumulative income of both sides (wealth = cash + ore + own units / buildings at cost + paid production + deaths,
minus the 5000 start and the refineries' free harvesters) at given seconds, per map, in sim games.
    python inc.py <opp> <t1,t2,..> <map:seat:seed> ..."""
import json
import os
import sys

os.environ["ARENA_RECORD"] = "0"
import local.ra_platform as P  # noqa: E402
import agent.redalert as RA  # noqa: E402

CFG = json.load(open(os.path.join(os.environ.get("PYTHONPATH", "."), "data/red_alert_config.json")))


def cost(t):
    for k in ("units", "buildings"):
        d = (CFG["rules"].get(k) or {}).get(t)
        if isinstance(d, dict) and d.get("cost"):
            return d["cost"]
    return 2000 if t in ("fact", "mcv") else 0


def wealth(obs):
    e = obs.get("economy") or {}
    m = obs.get("military") or {}
    w = int(e.get("cash") or 0) + int(e.get("ore") or 0)
    blds = obs.get("buildings") or []
    w += sum(cost(b["type"]) for b in blds)
    w += sum(cost(u["type"]) for u in obs.get("units") or [] if not u.get("wreck"))
    w += sum(int(cost(p.get("item")) * float(p.get("progress") or 0)) for p in obs.get("production") or [])
    w += int(m.get("deaths_cost") or 0)
    procs = sum(1 for b in blds if b["type"] == "proc")
    return w - 5000 - 2000 - 1100 * procs


opp = sys.argv[1]
marks = [float(x) for x in sys.argv[2].split(",")]
for spec in sys.argv[3:]:
    map_id, seat, seed = spec.split(":")
    seat, seed = int(seat), int(seed)
    got = {"A": {}, "B": {}}
    orig = RA.RedAlertPolicy.decide

    def dec(self, raw, config=None):
        out = orig(self, raw, config)
        ob = raw.get("observation") if isinstance(raw.get("observation"), dict) else None
        gs = (raw.get("time") or {}).get("game_seconds") or 0
        for m in marks:
            if ob and gs >= m and m not in got["A"]:
                got["A"][m] = wealth(ob)
        return out
    RA.RedAlertPolicy.decide = dec
    fb = P.make_factory(opp)(seed * 2 + 2)
    bo = fb.decide

    def bdec(raw, obs, la):
        gs = (raw.get("time") or {}).get("game_seconds") or obs["tick"] / 25
        for m in marks:
            if gs >= m and m not in got["B"]:
                got["B"][m] = wealth(obs)
        return bo(raw, obs, la)
    fb.decide = bdec
    row = P.play_match(P.make_factory("ours")(seed * 2 + 1), fb, seed=seed, map_id=map_id, a_seat=seat)
    RA.RedAlertPolicy.decide = orig
    print(f"{map_id:14s} s{seat} {seed} {row['result_a']:4s} " + " ".join(
        f"{int(m)}s A {got['A'].get(m, 0):5d} B {got['B'].get(m, 0):5d} |" for m in marks))
