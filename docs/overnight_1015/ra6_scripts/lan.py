"""Which 7000-credit composition does the sim's damage model (counters.ratio, the same versus tables as the simulator)
favour against Tom's 9.5-minute army (15 e1, 7 2tnk, 3 e3)?"""
import json
import sys

sys.path.insert(0, "/home/linmengfdm/wt/ra6")
from agent.redalert import counters  # noqa: E402
from agent.redalert.world import Rules  # noqa: E402

rules = Rules(json.load(open("/home/linmengfdm/wt/ra6/data/red_alert_config.json")))
tom = [("e1", 15, 1.0), ("2tnk", 7, 1.0), ("e3", 3, 1.0)]
tom_t = [("e1", 6, 1.0), ("3tnk", 5, 1.0), ("e3", 2, 1.0)]
for name, f in [
    ("70 e1", [("e1", 70, 1)]),
    ("23 e3", [("e3", 23, 1)]),
    ("8 2tnk", [("2tnk", 8, 1)]),
    ("20e1 3e3 5.5tnk(real edf9)", [("e1", 20, 1), ("e3", 3, 1), ("2tnk", 5.5, 1)]),
    ("10e1 10e3 3.5tnk", [("e1", 10, 1), ("e3", 10, 1), ("2tnk", 3.5, 1)]),
    ("12e1 6e3 5tnk", [("e1", 12, 1), ("e3", 6, 1), ("2tnk", 5.2, 1)]),
    ("6e1 10e3 4.1tnk", [("e1", 6, 1), ("e3", 10, 1), ("2tnk", 4.1, 1)]),
    ("4 gun+ 4e1 5e3 3tnk", [("gun", 4, 1), ("e1", 4, 1), ("e3", 5, 1), ("2tnk", 2.6, 1)]),
]:
    cost = sum(rules.cost(t) * n for t, n, _ in f)
    print(f"{name:28s} cost {cost:6.0f} vs tom(8350) {counters.ratio(f, tom, rules):.2f}  vs 3tnk-mix(7150) "
          f"{counters.ratio(f, tom_t, rules):.2f}")
