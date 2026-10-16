"""Timeline of a real RA recording: per sampled frame our army by type, K/D, cash, visible enemies, reasoning."""
import collections
import glob
import json
import sys

LOG = "/home/linmengfdm/ucla-agent-tournament/logs"
sess = sys.argv[1]
t0 = float(sys.argv[2]) if len(sys.argv) > 2 else 0
path = glob.glob(f"{LOG}/{sess}*.jsonl")[0]
recs = [json.loads(x) for x in open(path) if x.strip()]
for r in recs:
    raw = r.get("raw") or {}
    ob = raw.get("observation") if isinstance(raw.get("observation"), dict) else None
    if not ob:
        continue
    tm = (raw.get("time") or {}).get("game_seconds") or ob.get("tick", 0) / 25.0
    if tm < t0:
        continue
    eco, mil = ob.get("economy") or {}, ob.get("military") or {}
    ours = collections.Counter(u.get("type") for u in ob.get("units") or [] if not u.get("wreck"))
    ve = collections.Counter(e.get("type") for e in ob.get("visible_enemies") or [] if not e.get("wreck"))
    vb = collections.Counter(b.get("type") for b in ob.get("visible_enemy_buildings") or [])
    blds = collections.Counter(b.get("type") for b in ob.get("buildings") or [])
    out = r.get("out")
    cmds = collections.Counter()
    if isinstance(out, dict):
        for o in out.get("orders") or out.get("commands") or []:
            cmds[o.get("cmd")] += len(o.get("units") or [1])
    elif isinstance(out, str):
        cmds[out[:20]] += 1
    rs = r.get("reasoning_summary") or ""
    print(f"t={tm:5.0f} cash={eco.get('cash')} harv={eco.get('harvesters')} army={mil.get('army_value')} "
          f"K={mil.get('kills_cost')} D={mil.get('deaths_cost')} ours={dict(ours)} blds={dict(blds)}")
    print(f"        enemy={dict(ve)} ebld={dict(vb)} cmds={dict(cmds)} rs={rs[:300]}")
