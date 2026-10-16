"""Losses of side A in league JSON files: by map, game length, K/D, reason."""
import json
import sys
from collections import Counter

for p in sys.argv[1:]:
    rows = json.load(open(p))["rows"]
    lost = [r for r in rows if r["result_a"] != "win"]
    print(f"== {p}: {len(rows)} games, {len(lost)} not won; by map {dict(Counter(r['map'] for r in lost))}, "
          f"by opp {dict(Counter(r.get('b_spec') for r in lost))}")
    for r in lost[:30]:
        a = r["a"]
        print(f"   seed {r['seed']} {r['map']:15s} seat {r['a_seat']} {r['result_a']:5s} {r['reason']:10s} "
              f"{r['game_s']/60:5.1f}m weap {a['first_weap_s']} K/D {a['kills_cost']}/{a['deaths_cost']} "
              f"army^ {a['peak_army']} vs B army^ {r['b']['peak_army']} opp {r.get('b_spec')}")
