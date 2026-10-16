"""Select deals (game indices) whose screened candidate six has a Sucker Punch user. usage: sel.py opp seed [max]"""
import json, sys
o, s = sys.argv[1], sys.argv[2]
mx = int(sys.argv[3]) if len(sys.argv) > 3 else 10 ** 9
try:                                   # override: _notes/max_<opp>_<seed> holds a smaller deal count
    mx = min(mx, int(open(f"/home/user/ucla-agent-tournament/_notes/max_{o}_{s}").read().strip()))
except (OSError, ValueError):
    pass
SU = {"vgc-kingambit", "vgc-chien-pao"}
gs = []
n = 0
for l in open(f"/home/user/ucla-agent-tournament/_notes/screen/{o}-{s}.jsonl"):
    r = json.loads(l)
    n += 1
    if SU & set(r["off"]["ours"]):
        gs.append(r["g"])
gs = gs[:mx]
print(",".join(map(str, gs)))
print(f"{len(gs)} of {n}", file=sys.stderr)
