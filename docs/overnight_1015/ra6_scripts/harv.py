"""Harvesters and economy fields over time in a real RA recording."""
import glob
import json
import sys

LOG = "/home/linmengfdm/ucla-agent-tournament/logs"
sess = sys.argv[1]
t0, t1 = float(sys.argv[2]), float(sys.argv[3])
path = glob.glob(f"{LOG}/{sess}*.jsonl")[0]
recs = [json.loads(x) for x in open(path) if x.strip()]
for r in recs:
    raw = r.get("raw") or {}
    ob = raw.get("observation") if isinstance(raw.get("observation"), dict) else None
    if not ob:
        continue
    tm = (raw.get("time") or {}).get("game_seconds") or ob.get("tick", 0) / 25.0
    if tm < t0 or tm > t1:
        continue
    eco = ob.get("economy") or {}
    hs = [(u["x"], u["y"], int(u.get("idle", 0)), u.get("hp")) for u in ob.get("units") or [] if u.get("type") == "harv"]
    procs = [(b["x"], b["y"]) for b in ob.get("buildings") or [] if b.get("type") == "proc"]
    prod = [(p.get("queue"), p.get("item"), round(p.get("progress", 0), 2)) for p in ob.get("production") or []]
    print(f"t={tm:5.0f} eco={eco} bc={ob.get('base_center')} procs={procs}")
    print(f"      harv={hs}")
    print(f"      prod={prod}")
