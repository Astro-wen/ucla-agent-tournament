"""Enemy build evidence in a real RA recording (read only): for each frame with enemies in sight, the number of
DISTINCT enemy unit ids of each type seen so far (a lower bound of what they built), the visible army then, and
the enemy buildings seen with their first time."""
import collections
import glob
import json
import sys

LOG = "/home/linmengfdm/ucla-agent-tournament/logs"
CFG = json.load(open("/home/linmengfdm/wt/ra6/data/red_alert_config.json"))
sess = sys.argv[1]
t1 = float(sys.argv[2]) if len(sys.argv) > 2 else 1e9
path = glob.glob(f"{LOG}/{sess}*.jsonl")[0]
recs = [json.loads(x) for x in open(path) if x.strip()]


def cost(t):
    for k in ("units", "buildings"):
        d = (CFG["rules"].get(k) or {}).get(t)
        if isinstance(d, dict) and d.get("cost"):
            return d["cost"]
    return 0


ids = {}
blds = {}
first = True
for r in recs:
    raw = r.get("raw") or {}
    ob = raw.get("observation") if isinstance(raw.get("observation"), dict) else None
    if not ob:
        continue
    if first:
        print(sess, "we", ob.get("faction"), "they", ob.get("enemy_faction"), "map", (ob.get("map") or {}).get("name"))
        first = False
    tm = (raw.get("time") or {}).get("game_seconds") or ob.get("tick", 0) / 25.0
    if tm > t1:
        break
    ens = [e for e in ob.get("visible_enemies") or [] if not e.get("wreck")]
    new = False
    for e in ens:
        if e.get("id") not in ids:
            ids[e.get("id")] = (e.get("type"), tm)
            new = True
    for b in ob.get("visible_enemy_buildings") or []:
        if b.get("id") not in blds:
            blds[b.get("id")] = (b.get("type"), tm)
    if ens and new:
        c = collections.Counter(t for t, _ in ids.values())
        vis = collections.Counter(e.get("type") for e in ens)
        val = sum(cost(t) * n for t, n in vis.items() if t != "harv")
        print(f"t={tm:4.0f} distinct-so-far {dict(c)} | visible({val}) {dict(vis)}")
bc = collections.Counter(t for t, _ in blds.values())
print("buildings seen:", dict(bc))
print("  first:", sorted(((t, round(s)) for t, s in blds.values()), key=lambda x: x[1])[:30])
