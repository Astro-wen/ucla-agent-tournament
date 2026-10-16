"""Compact timeline of a real RA recording (read only): t, cash, harvesters, our combat units by type, army value,
K/D, our defenses, visible enemy units by type (+ their cost) and enemy buildings, enemy distance to our base."""
import collections
import glob
import json
import math
import sys

LOG = "/home/linmengfdm/ucla-agent-tournament/logs"
CFG = json.load(open("/home/linmengfdm/wt/ra6/data/red_alert_config.json"))
sess = sys.argv[1]
t0 = float(sys.argv[2]) if len(sys.argv) > 2 else 0
t1 = float(sys.argv[3]) if len(sys.argv) > 3 else 1e9
path = glob.glob(f"{LOG}/{sess}*.jsonl")[0]
recs = [json.loads(x) for x in open(path) if x.strip()]


def cost(t):
    for k in ("units", "buildings"):
        d = (CFG["rules"].get(k) or {}).get(t)
        if isinstance(d, dict) and d.get("cost"):
            return d["cost"]
    return 0


first = True
for r in recs:
    raw = r.get("raw") or {}
    ob = raw.get("observation") if isinstance(raw.get("observation"), dict) else None
    if not ob:
        continue
    if first:
        print("map", (raw.get("match_rules") or ob.get("match_rules") or {}), "faction", ob.get("faction"),
              "keys", sorted(ob.keys())[:40])
        first = False
    tm = (raw.get("time") or {}).get("game_seconds") or ob.get("tick", 0) / 25.0
    if tm < t0 or tm > t1:
        continue
    eco, mil = ob.get("economy") or {}, ob.get("military") or {}
    bc = ob.get("base_center") or {}
    bx, by = (bc.get("x", 0), bc.get("y", 0)) if isinstance(bc, dict) else (bc[0], bc[1]) if bc else (0, 0)
    ours = collections.Counter(u.get("type") for u in ob.get("units") or [] if not u.get("wreck") and u.get("type") not in ("harv", "mcv"))
    harv = sum(1 for u in ob.get("units") or [] if u.get("type") == "harv")
    oval = sum(cost(t) * n for t, n in ours.items())
    defs = collections.Counter(b.get("type") for b in ob.get("buildings") or [] if b.get("type") in ("gun", "tsla", "ftur", "pbox", "hbox", "agun", "sam"))
    ens = [e for e in ob.get("visible_enemies") or [] if not e.get("wreck")]
    ve = collections.Counter(e.get("type") for e in ens)
    evl = sum(cost(t) * n for t, n in ve.items() if t != "harv")
    dmin = min((math.hypot(e.get("x", 0) - bx, e.get("y", 0) - by) for e in ens if e.get("type") != "harv"), default=None)
    vb = collections.Counter(b.get("type") for b in ob.get("visible_enemy_buildings") or [])
    prod = [(p.get("item")) for p in ob.get("production") or []]
    rs = (r.get("reasoning_summary") or "")[:110]
    print(f"t={tm:4.0f} $={eco.get('cash')} hv={harv} army={mil.get('army_value')} K={mil.get('kills_cost')} "
          f"D={mil.get('deaths_cost')} our({oval})={dict(ours)} def={dict(defs)}")
    print(f"       EN({evl}) d={dmin and round(dmin)} {dict(ve)} eb={dict(vb)} prod={prod} | {rs}")
