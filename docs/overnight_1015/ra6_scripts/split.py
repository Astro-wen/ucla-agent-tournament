"""Win rate by opponent x map of one or more league JSONs; plus the losses (seed, map, K/D, game min, waves...)."""
import json
import sys
from collections import defaultdict

for path in sys.argv[1:]:
    rows = json.load(open(path))["rows"]
    g = defaultdict(lambda: [0, 0])
    for r in rows:
        k = (r.get("b_spec"), r["map"])
        g[k][1] += 1
        g[k][0] += {"win": 1.0, "draw": 0.5}.get(r["result_a"], 0.0)
    print(path)
    for k, (w, n) in sorted(g.items(), key=str):
        print(f"   {k[0]:9s} {k[1]:15s} {w:5.1f}/{n:3d} = {100*w/n:5.1f}%")
    if "-l" in sys.argv[0:1] or True:
        for r in rows:
            if r["result_a"] != "win":
                a = r["a"]
                arm = a.get("army") or {}
                print(f"     L {r['seed']} {r['map']:14s} {r.get('b_spec'):9s} {r['result_a']:5s} {r['game_s']/60:5.1f}m "
                      f"K/D {a.get('kills_cost')}/{a.get('deaths_cost')} waves {arm.get('waves')} "
                      f"tr {arm.get('trade_retreats')} kept {arm.get('comp_kept')} seen {arm.get('base_seen')}")
