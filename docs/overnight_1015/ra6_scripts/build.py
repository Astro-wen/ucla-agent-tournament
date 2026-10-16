"""Build timing in a real RA recording (read only): enemy distinct ids by type (cumulative, lower bound of what they
built) at checkpoints, first-seen time per enemy type and building, and our side: building completion times, unit
counts, cash, harvesters, spending split (towers / infantry / vehicles / buildings) at checkpoints.

    python3 build.py <session> [checkpoints...]
"""
import collections
import glob
import json
import math
import sys

LOG = "/home/linmengfdm/ucla-agent-tournament/logs"
CFG = json.load(open("/home/linmengfdm/wt/ra6/data/red_alert_config.json"))
sess = sys.argv[1]
cps = [float(x) for x in sys.argv[2:]] or [180, 240, 285, 300, 330, 360, 420, 480, 600]
path = glob.glob(f"{LOG}/{sess}*.jsonl")[0]


def cost(t):
    for k in ("units", "buildings"):
        d = (CFG["rules"].get(k) or {}).get(t)
        if isinstance(d, dict) and d.get("cost"):
            return d["cost"]
    return 0


INF = {"e1", "e2", "e3", "e4", "e6", "dog", "spy", "medi", "mech", "shok", "thf", "hijacker", "e7"}
DEF = {"gun", "tsla", "ftur", "pbox", "hbox", "agun", "sam", "gap"}
seen_ids = collections.defaultdict(set)      # enemy type -> ids
first_seen = {}
eb_ids = collections.defaultdict(set)
eb_first = {}
our_ids = collections.defaultdict(set)
our_b_ids = collections.defaultdict(set)
our_b_first = {}
our_u_first = {}
cpi = 0
rows = []
mp = None
for line in open(path):
    if not line.strip():
        continue
    r = json.loads(line)
    raw = r.get("raw") or {}
    ob = raw.get("observation") if isinstance(raw.get("observation"), dict) else None
    if not ob:
        continue
    if mp is None:
        mp = ob.get("map")
        print("map", (mp or {}).get("name") if isinstance(mp, dict) else mp, "we", ob.get("faction"), "they", ob.get("enemy_faction"))
    tm = (raw.get("time") or {}).get("game_seconds") or ob.get("tick", 0) / 25.0
    for e in ob.get("visible_enemies") or []:
        t = e.get("type")
        i = e.get("id")
        seen_ids[t].add(i)
        first_seen.setdefault(t, tm)
    for b in ob.get("visible_enemy_buildings") or []:
        t = b.get("type")
        eb_ids[t].add(b.get("id") or (b.get("x"), b.get("y")))
        eb_first.setdefault(t, tm)
    for u in ob.get("units") or []:
        our_ids[u.get("type")].add(u.get("id"))
        our_u_first.setdefault(u.get("type"), tm)
    for b in ob.get("buildings") or []:
        our_b_ids[b.get("type")].add(b.get("id") or (b.get("x"), b.get("y")))
        our_b_first.setdefault(b.get("type"), tm)
    while cpi < len(cps) and tm >= cps[cpi]:
        eco, mil = ob.get("economy") or {}, ob.get("military") or {}
        en = {t: len(s) for t, s in seen_ids.items() if t not in ("harv",)}
        env = sum(cost(t) * n for t, n in en.items())
        ours = {t: len(s) for t, s in our_ids.items() if t not in ("harv", "mcv")}
        inf = sum(cost(t) * n for t, n in ours.items() if t in INF)
        veh = sum(cost(t) * n for t, n in ours.items() if t not in INF)
        tow = sum(cost(t) * len(s) for t, s in our_b_ids.items() if t in DEF)
        bld = sum(cost(t) * len(s) for t, s in our_b_ids.items() if t not in DEF)
        harv = len(our_ids.get("harv", ()))
        print(f"@{cps[cpi]:.0f} (t={tm:.0f}) cash={eco.get('cash')} K={mil.get('kills_cost')} D={mil.get('deaths_cost')}")
        print(f"   ENEMY built>= {dict(sorted(en.items()))} (={env}) harv_seen={len(seen_ids.get('harv', ()))} "
              f"bld={ {t: len(s) for t, s in eb_ids.items()} }")
        print(f"   OURS  built  {dict(sorted(ours.items()))} inf={inf} veh={veh} towers={tow} bld={bld} harv={harv} "
              f"bldgs={ {t: len(s) for t, s in our_b_ids.items()} }")
        cpi += 1
print("enemy first seen:", {t: round(v) for t, v in sorted(first_seen.items(), key=lambda kv: kv[1])})
print("enemy bld first seen:", {t: round(v) for t, v in sorted(eb_first.items(), key=lambda kv: kv[1])})
print("our bld first:", {t: round(v) for t, v in sorted(our_b_first.items(), key=lambda kv: kv[1])})
print("our unit first:", {t: round(v) for t, v in sorted(our_u_first.items(), key=lambda kv: kv[1])})
