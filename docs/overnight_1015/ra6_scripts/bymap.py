"""Per opponent x map W/L/D of league JSONs, plus losses' reason / minute / K-D / peaks.
    python bymap.py a.json [b.json ...] [-v]"""
import collections
import json
import sys

verbose = "-v" in sys.argv
for p in [a for a in sys.argv[1:] if a != "-v"]:
    rows = json.load(open(p))["rows"]
    agg = collections.defaultdict(lambda: [0, 0, 0])
    for r in rows:
        k = (r.get("b_spec"), r["map"])
        i = {"win": 0, "loss": 1}.get(r["result_a"], 2)
        agg[k][i] += 1
        agg[(r.get("b_spec"), "ALL")][i] += 1
    print(p)
    for k in sorted(agg, key=str):
        w, l, dd = agg[k]
        print(f"  {k[0]:10s} {k[1]:15s} {w:3d}W {l:3d}L {dd:2d}D  {100*(w+0.5*dd)/max(1,w+l+dd):5.1f}%")
    if verbose:
        for r in rows:
            if r["result_a"] != "win":
                a, b = r["a"], r["b"]
                print(f"    {r['result_a']:5s} {r['map']:14s} seed {r['seed']} seat {r['a_seat']} {r['reason']} "
                      f"{r['game_s']/60:4.1f}m K/D {a['kills_cost']}/{a['deaths_cost']} peak {a['peak_army']} vs {b['peak_army']} "
                      f"weap {a.get('first_weap_s')}")
