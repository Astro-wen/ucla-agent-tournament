"""Both sides' army / economy at given game seconds in sim games (league seeding).
    python snapat.py <opp> <map> <seat> <t1,t2,..> <seed> [seed ...]"""
import collections
import os
import sys

os.environ["ARENA_RECORD"] = "0"
import local.ra_platform as P  # noqa: E402
import agent.redalert as RA  # noqa: E402

opp, map_id, seat = sys.argv[1], sys.argv[2], int(sys.argv[3])
marks = [float(x) for x in sys.argv[4].split(",")]
COST = {"e1": 100, "e3": 300, "e2": 160, "1tnk": 700, "2tnk": 850, "3tnk": 1150, "jeep": 500, "ftrk": 600, "apc": 850,
        "arty": 850, "v2rl": 900}
for seed in [int(s) for s in sys.argv[5:]]:
    seen = {"A": set(), "B": set()}
    lines = []

    def fmt(side, gs, obs):
        u = collections.Counter(x["type"] for x in obs.get("units") or [] if not x.get("wreck"))
        val = sum(COST.get(t, 0) * n for t, n in u.items())
        b = collections.Counter(x["type"] for x in obs.get("buildings") or [])
        tw = {t: n for t, n in b.items() if t in ("pbox", "ftur", "gun", "tsla", "hbox")}
        e = obs.get("economy") or {}
        return (f"   {side} t={gs:4.0f} army {val:5d} {dict((t, n) for t, n in u.items() if t in COST)} harv {u.get('harv', 0)} "
                f"cash {int(e.get('cash') or 0) + int(e.get('ore') or 0)} weap {b.get('weap', 0)} tw {tw}")

    orig = RA.RedAlertPolicy.decide

    def dec(self, raw, config=None):
        out = orig(self, raw, config)
        st = raw
        ob = (st.get("observation") if isinstance(st.get("observation"), dict) else None)
        gs = (st.get("time") or {}).get("game_seconds") or 0
        for m in marks:
            if gs >= m and m not in seen["A"] and ob:
                seen["A"].add(m)
                lines.append((m, 0, fmt("A", gs, ob)))
        return out
    RA.RedAlertPolicy.decide = dec
    fb = P.make_factory(opp)(seed * 2 + 2)
    ob_orig = fb.decide

    def bdec(raw, obs, la):
        gs = (raw.get("time") or {}).get("game_seconds") or obs["tick"] / 25
        for m in marks:
            if gs >= m and m not in seen["B"]:
                seen["B"].add(m)
                lines.append((m, 1, fmt("B", gs, obs)))
        return ob_orig(raw, obs, la)
    fb.decide = bdec
    row = P.play_match(P.make_factory("ours")(seed * 2 + 1), fb, seed=seed, map_id=map_id, a_seat=seat)
    RA.RedAlertPolicy.decide = orig
    print(f"{opp} {map_id} {seed} s{seat}: {row['result_a']} {row['game_s']/60:.1f}m K/D {row['a']['kills_cost']}/{row['a']['deaths_cost']}")
    for _m, _s, ln in sorted(lines):
        print(ln)
