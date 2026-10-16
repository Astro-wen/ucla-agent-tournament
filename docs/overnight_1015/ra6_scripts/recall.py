"""Every real Red Alert recording in logs/ (read only): feed its sampled frames to v4.6 (v4.7 switches off) and to
v4.7 (defaults) and print the first waves / retreats of each, with the real result from agent-main.out.
Approximate: the recordings keep about one frame in 10 s and the real game went on as it did."""
import glob
import json
import os
import re
import sys

sys.path.insert(0, "/home/linmengfdm/wt/ra6")
os.environ["ARENA_RECORD"] = "0"
from agent.redalert import RedAlertPolicy  # noqa: E402
from agent.redalert.params import load as load_params  # noqa: E402
from local.ra_rules import Rules  # noqa: E402

LOG = "/home/linmengfdm/ucla-agent-tournament/logs"
CONFIG = Rules().config
OFF = {"ARENA_RA_ATTACK_GATE": "0", "ARENA_RA_TRADE_RETREAT": "0", "ARENA_RA_RECOVER": "0"}
results = {}
for f in sorted(glob.glob(f"{LOG}/agent-main.out*")):
    for line in open(f, errors="replace"):
        m = re.search(r"\[match-summary\] session=(\w+) game=red_alert", line)
        if m:
            results.setdefault(m.group(1), []).append(line)
fin = {}
for f in sorted(glob.glob(f"{LOG}/agent-main.out*")):
    pend = []
    for line in open(f, errors="replace"):
        m = re.search(r"\[match red_alert seat=[\w-]+\] finished: your agent \(player \d\) (\w+)", line)
        if m:
            pend.append(m.group(1))
        m = re.search(r"\[match-summary\] session=(\w+) game=red_alert", line)
        if m and pend:
            fin.setdefault(m.group(1), []).append(pend.pop(0))


def run(recs, env):
    pol = RedAlertPolicy(params=load_params(env))
    out = []
    n = 0
    for r in recs:
        raw = r.get("raw") or {}
        ob = raw.get("observation") if isinstance(raw.get("observation"), dict) else None
        if not ob or raw.get("game_type") != "red_alert":
            continue
        try:
            pol.decide(raw, CONFIG)
        except Exception as e:  # noqa: BLE001
            out.append(f"decide failed {e}")
            break
        evs = pol.events
        if len(evs) < n:
            n = 0
        for e in evs[n:]:
            if e.startswith(("attack wave", "retreat", "all-in")):
                out.append(e.split(":")[0] if e.startswith("attack wave") else e)
        n = len(evs)
    return out, pol.exceptions


for path in sorted(glob.glob(f"{LOG}/*.jsonl"), key=os.path.getmtime):
    head = open(path).readline()
    if '"red_alert"' not in head:
        continue
    recs = [json.loads(x) for x in open(path) if x.strip()]
    if not any((r.get("raw") or {}).get("game_type") == "red_alert" for r in recs):
        continue
    sess = os.path.basename(path)[:8]
    a, ea = run(recs, OFF)
    b, eb = run(recs, {})
    tag = "SAME" if a == b else "DIFF"
    print(f"{sess} real={','.join(fin.get(sess, ['?']))} frames={len(recs)} {tag} exc={ea}/{eb}")
    if a != b:
        print(f"    v4.6: {a[:4]}")
        print(f"    v4.7: {b[:4]}")
