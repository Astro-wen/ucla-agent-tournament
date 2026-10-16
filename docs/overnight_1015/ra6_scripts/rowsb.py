import json
import sys

d = json.load(open(sys.argv[1]))
for r in d["rows"][: int(sys.argv[2]) if len(sys.argv) > 2 else 4]:
    a, b = r["a"], r["b"]
    print(r["seed"], r["map"], r["result_a"], round(r["game_s"]))
    for side, s in (("A", a), ("B", b)):
        print(f"  {side} {s['agent']} {s['country']} weap {s['first_weap_s']} proc {s['first_proc_s']} peak {s['peak_army']} "
              f"K {s['kills_cost']} D {s['deaths_cost']} orders {s['orders']} batches {s['batches']} unplaced {s['unplaced']} "
              f"lowpow {s['low_power_s']} problems {str(s['problems'])[:300]}")
        if s["invalid_details"]:
            print("     invalid:", str(s["invalid_details"])[:600])
