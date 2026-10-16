"""low_power_s / first_weap_s / peak army by result and map from league JSONs."""
import collections
import json
import statistics
import sys

for p in sys.argv[1:]:
    rows = json.load(open(p))["rows"]
    g = collections.defaultdict(list)
    for r in rows:
        g[(r["map"], r["result_a"])].append(r)
        g[("ALL", r["result_a"])].append(r)
    print(p)
    for k in sorted(g):
        rs = g[k]
        lp = [r["a"]["low_power_s"] for r in rs]
        print(f"  {k[0]:15s} {k[1]:5s} n={len(rs):3d} low_power mean {statistics.mean(lp):5.1f} med {statistics.median(lp):5.1f} "
              f">20s {sum(1 for x in lp if x > 20):3d}  weap {statistics.median([r['a']['first_weap_s'] or 0 for r in rs]):.0f} "
              f"peak {statistics.median([r['a']['peak_army'] for r in rs]):.0f}")
