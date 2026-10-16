"""Positions in a real RA recording (read only): our combat units by type with (x,y,hp), enemies, base center,
defenses, between t0 and t1. Also the events of each frame (unit_lost / under_attack kinds counted)."""
import collections
import glob
import json
import sys

LOG = "/home/linmengfdm/ucla-agent-tournament/logs"
sess, t0, t1 = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
path = glob.glob(f"{LOG}/{sess}*.jsonl")[0]
for line in open(path):
    if not line.strip():
        continue
    r = json.loads(line)
    raw = r.get("raw") or {}
    ob = raw.get("observation") if isinstance(raw.get("observation"), dict) else None
    if not ob:
        continue
    tm = (raw.get("time") or {}).get("game_seconds") or ob.get("tick", 0) / 25.0
    if tm < t0 or tm > t1:
        continue
    bc = ob.get("base_center")
    print(f"t={tm:.0f} base={bc}")
    g = collections.defaultdict(list)
    for u in ob.get("units") or []:
        if u.get("type") in ("harv", "mcv"):
            g["harv"].append((u["x"], u["y"], u.get("hp")))
        else:
            g[u["type"]].append((u["x"], u["y"], u.get("hp")))
    for k, xs in sorted(g.items()):
        print(f"   our {k:5s} {xs}")
    d = [(b["type"], b["x"], b["y"], b.get("hp")) for b in ob.get("buildings") or [] if b["type"] in ("gun", "tsla", "ftur", "pbox", "hbox", "weap", "fact", "proc")]
    print(f"   blds {d}")
    ge = collections.defaultdict(list)
    for e in ob.get("visible_enemies") or []:
        ge[e["type"]].append((e["x"], e["y"], e.get("hp")))
    for k, xs in sorted(ge.items()):
        print(f"   EN  {k:5s} {xs}")
    ev = collections.Counter(e.get("kind") for e in ob.get("events") or [])
    print(f"   events {dict(ev)}")
