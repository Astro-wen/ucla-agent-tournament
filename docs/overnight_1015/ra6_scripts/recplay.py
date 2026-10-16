"""Feed a real RA recording's sampled frames (logs/<session>-*.jsonl, read only) to the policy with given switches and
print its log events and the attack decision inputs at each frame from t0. Approximate: the recording keeps one frame
in ~10 s, so the bot's memory sees only those frames.

    python recplay.py <session> <t0> [ENV=VAL ...]
"""
import glob
import json
import sys

sys.path.insert(0, "/home/linmengfdm/wt/ra6")
from agent.redalert import RedAlertPolicy  # noqa: E402
from agent.redalert.params import load as load_params  # noqa: E402
from local.ra_rules import Rules  # noqa: E402

LOG = "/home/linmengfdm/ucla-agent-tournament/logs"
sess, t0 = sys.argv[1], float(sys.argv[2])
env = dict(a.split("=", 1) for a in sys.argv[3:])
CONFIG = Rules().config
path = glob.glob(f"{LOG}/{sess}*.jsonl")[0]
recs = [json.loads(x) for x in open(path) if x.strip()]
pol = RedAlertPolicy(params=load_params(env))
seen_ev = 0
for r in recs:
    raw = r.get("raw") or {}
    ob = raw.get("observation") if isinstance(raw.get("observation"), dict) else None
    if not ob:
        continue
    tm = (raw.get("time") or {}).get("game_seconds") or ob.get("tick", 0) / 25.0
    try:
        pol.decide(raw, CONFIG)
    except Exception as e:  # noqa: BLE001
        print("decide failed", e)
        continue
    for e in pol.events[seen_ev:]:
        print(f"   [{tm:5.0f}] EVENT {e}")
    seen_ev = len(pol.events)
    if tm >= t0 and pol.view is not None:
        a, v = pol.army, pol.view
        ids = sorted(i for i in a.main if i in v.unit_by_id)
        try:
            st = a.enemy_strength(v)
        except Exception:  # noqa: BLE001
            st = -1
        print(f"t={tm:5.0f} state={a.state} main={len(ids)} val={a.value(ids, v):.0f} home={len(a.home)} "
              f"strength={st:.0f} prior={a.prior(v):.0f} peak={a.enemy_peak:.0f} blind={a.blind(v)} "
              f"base_seen={a.base_seen} waves={a.waves} fail={a.last_fail_value:.0f}")
