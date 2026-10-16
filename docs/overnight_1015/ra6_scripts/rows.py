"""Print rows of a local.ra_platform JSON: seed, map, seat, opponent, result, minutes, K/D, max army, first weap, waves."""
import json
import sys

for p in sys.argv[1:]:
    d = json.load(open(p))
    rows = d["rows"]
    if rows:
        print("keys", sorted(rows[0].keys()))
        print("a keys", sorted(rows[0]["a"].keys()))
        print("b keys", sorted((rows[0].get("b") or {}).keys()))
    for r in rows:
        a = r["a"]
        b = r.get("b") or {}
        print(f"{r['seed']} {r['map'][:6]} seat{r['a_seat']} {r.get('b_spec')} {r['result_a']:5s} {r['game_s']/60:5.1f}m "
              f"K/D {a.get('kills_cost')}/{a.get('deaths_cost')} maxarmy {a.get('max_army')} weap {a.get('first_weap_s')} "
              f"waves {a.get('waves')} inv {a.get('invalid_actions')} | B inv {b.get('invalid_actions')} rej {b.get('rejected')} {b.get('refusals', '')}")
