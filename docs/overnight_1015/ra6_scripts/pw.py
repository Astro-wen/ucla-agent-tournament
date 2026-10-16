"""Power over time in a real recording (read only): frames where drained > provided, with buildings / production."""
import collections
import glob
import json
import sys

LOG = "/home/linmengfdm/ucla-agent-tournament/logs"
for sess in sys.argv[1:]:
    path = glob.glob(f"{LOG}/{sess}*.jsonl")[0]
    print(sess)
    for x in open(path):
        if not x.strip():
            continue
        r = json.loads(x)
        raw = r.get("raw") or {}
        ob = raw.get("observation") if isinstance(raw.get("observation"), dict) else None
        if not ob:
            continue
        tm = (raw.get("time") or {}).get("game_seconds") or ob.get("tick", 0) / 25.0
        e = ob.get("economy") or {}
        prov, dr = e.get("power_provided"), e.get("power_drained")
        if prov is None or tm > 480:
            continue
        bc = collections.Counter(b.get("type") for b in ob.get("buildings") or [])
        prod = [p.get("item") for p in ob.get("production") or []]
        flag = "LOW" if dr > prov else "   "
        print(f"  {flag} t={tm:4.0f} power {prov}/{dr} blds {dict(bc)} prod {prod}")
