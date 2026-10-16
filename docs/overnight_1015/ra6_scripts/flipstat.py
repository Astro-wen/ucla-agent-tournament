"""For two paired league JSONs: per game low_power_s, peak army, K/D for base and new, flips first; and the mean
low_power_s / peak army over all paired games."""
import json
import statistics
import sys

b = {(r["seed"], r["map"], r["a_seat"], r.get("b_spec")): r for r in json.load(open(sys.argv[1]))["rows"]}
n = {(r["seed"], r["map"], r["a_seat"], r.get("b_spec")): r for r in json.load(open(sys.argv[2]))["rows"]}
ks = sorted(set(b) & set(n), key=str)
sc = {"win": 1, "draw": 0.5}
flips = [k for k in ks if sc.get(b[k]["result_a"], 0) != sc.get(n[k]["result_a"], 0)]
for k in flips:
    print(k, b[k]["result_a"], "->", n[k]["result_a"],
          f"lowpow {b[k]['a']['low_power_s']} -> {n[k]['a']['low_power_s']}  peak {b[k]['a']['peak_army']} -> "
          f"{n[k]['a']['peak_army']}  B peak {b[k]['b']['peak_army']} -> {n[k]['b']['peak_army']}")
for lab, d in (("base", b), ("new", n)):
    print(lab, "mean lowpow", round(statistics.mean(d[k]["a"]["low_power_s"] for k in ks), 1),
          "mean peak", round(statistics.mean(d[k]["a"]["peak_army"] for k in ks)),
          "mean game min", round(statistics.mean(d[k]["game_s"] for k in ks) / 60, 2))
