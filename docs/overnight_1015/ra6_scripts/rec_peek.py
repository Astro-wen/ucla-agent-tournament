"""Peek at one RA recording (read only): row keys, observation keys, one unit / building / production entry."""
import glob
import json
import sys

sess = sys.argv[1]
path = glob.glob(f"/home/linmengfdm/ucla-agent-tournament/logs/{sess}-*.jsonl")[0]
rows = [json.loads(l) for l in open(path) if l.strip()]
print(len(rows), sorted(rows[0].keys()))
for r in rows[:3] + rows[10:11]:
    st = r.get("raw") or {}
    print("raw keys", sorted(st.keys()))
    ob = st.get("observation") if isinstance(st.get("observation"), dict) else {}
    print("obs keys", sorted(ob.keys()))
    print("time", st.get("time"), "tick", ob.get("tick"))
    print("economy", ob.get("economy"))
    print("production", json.dumps(ob.get("production"))[:800])
    us = ob.get("units") or []
    print("unit0", json.dumps(us[:2])[:600])
    bs = ob.get("buildings") or []
    print("bld0", json.dumps(bs[:2])[:600])
    print("map", ob.get("map"), ob.get("map_name"), st.get("map"))
    for k in ("action", "decision", "orders", "tag"):
        if k in r:
            print(k, json.dumps(r[k])[:300])
ticks = []
for r in rows:
    st = r.get("raw") or {}
    ob = st.get("observation") if isinstance(st.get("observation"), dict) else {}
    if ob:
        ticks.append(ob.get("tick"))
print("ticks", ticks)
