"""Share of games where our roster slot 1 (first draft pick) was not brought, + win rate split. usage: kf_bench.py RUN.jsonl..."""
import json, glob, sys
for run in sys.argv[1:]:
    n = b = bw = kw = 0
    for f in glob.glob(run + ".logs/*.json"):
        g = json.load(open(f))
        team = g["brought"][g.get("seat", 0)].split()[-1]
        n += 1
        if "1" not in team:
            b += 1; bw += bool(g["win"])
        else:
            kw += bool(g["win"])
    print(f"{run}: {n} games, first pick benched {b} ({100*b/max(1,n):.0f}%), win benched {bw}/{b}, brought {kw}/{n-b}")
