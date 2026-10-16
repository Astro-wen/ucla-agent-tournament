"""Real Red Alert recordings (logs/<session>-*.jsonl, read only) fed to v4.7 (every v4.8 switch off) and v4.8
(defaults) of the ra5 worktree: per session the first waves / retreats, the v4.8 events (tank army, harvesters
first), the orders each would send at the recorded frames (anti-tank towers, rifles / rockets after the tank army
was seen, harvesters before 6:30). Approximate: one frame in ~10 s and the real game went on as it did.

    python recall5.py [session ...]          (default: every RA recording)
"""
import collections
import glob
import json
import os
import sys

sys.path.insert(0, "/home/linmengfdm/wt/ra6")
os.environ["ARENA_RECORD"] = "0"
from agent.redalert import RedAlertPolicy  # noqa: E402
from agent.redalert.params import load as load_params  # noqa: E402
from local.ra_rules import Rules  # noqa: E402

LOG = "/home/linmengfdm/ucla-agent-tournament/logs"
CONFIG = Rules().config
V47 = {"ARENA_RA_AT_TOWERS": "0", "ARENA_RA_AT_COMP": "0", "ARENA_RA_HF_KEEP": "0", "ARENA_RA_ANCHOR2": "0",
       "ARENA_RA_SPLIT_FIRE": "0"}
V46 = {**V47, "ARENA_RA_ATTACK_GATE": "0", "ARENA_RA_TRADE_RETREAT": "0", "ARENA_RA_RECOVER": "0"}


def run(recs, env):
    pol = RedAlertPolicy(params=load_params(env))
    waves, evs_out = [], []
    orders = collections.Counter()
    first = {}
    n = 0
    for r in recs:
        raw = r.get("raw") or {}
        ob = raw.get("observation") if isinstance(raw.get("observation"), dict) else None
        if not ob or raw.get("game_type") != "red_alert":
            continue
        try:
            out = pol.decide(raw, CONFIG)
        except Exception as e:  # noqa: BLE001
            waves.append(f"decide failed {e}")
            break
        v = pol.view
        t = v.game_s if v else 0
        evs = pol.events
        if len(evs) < n:
            n = 0
        for e in evs[n:]:
            if e.startswith(("attack wave", "retreat", "all-in")):
                waves.append(e.split(":")[0] if e.startswith("attack wave") else e[:70])
            if "tank army" in e or "harvesters first" in e:
                evs_out.append(e[:90])
        n = len(evs)
        batch = out[0] if isinstance(out, tuple) else out
        for o in batch or []:
            if not isinstance(o, dict):
                continue
            item = o.get("item")
            if o.get("cmd") == "build" and item in ("gun", "tsla", "pbox", "ftur", "proc"):
                first.setdefault(item, round(t))
                orders[f"build {item}"] += 1
            if o.get("cmd") == "attack" and v is not None:
                tt = next((e["type"] for e in v.enemies if e["id"] == o.get("target")), None)
                if tt is not None and tt not in ("e1", "e2", "e3", "e4", "dog", "shok"):
                    orders["attack vehicle"] += len(o.get("units") or [])
                    first.setdefault("split", round(t))
            if o.get("cmd") == "train" and item in ("e1", "e3", "harv"):
                ta = pol.army.tank_army_s
                tag = "after" if ta is not None and t >= ta else "before"
                if item == "harv":
                    tag = "<390" if t < 390 else ">=390"
                orders[f"{item} {tag}"] += int(o.get("count") or 1)
    return waves, evs_out, dict(orders), first, pol.exceptions, pol.army.tank_army_s


sessions = sys.argv[1:]
paths = []
for path in sorted(glob.glob(f"{LOG}/*.jsonl"), key=os.path.getmtime):
    if sessions and not any(os.path.basename(path).startswith(s) for s in sessions):
        continue
    head = open(path).readline()
    if '"red_alert"' not in head:
        continue
    paths.append(path)
for path in paths:
    recs = [json.loads(x) for x in open(path) if x.strip()]
    if not any((r.get("raw") or {}).get("game_type") == "red_alert" for r in recs):
        continue
    sess = os.path.basename(path)[:8]
    res = {k: run(recs, env) for k, env in (("v4.6", V46), ("v4.7", V47), ("v4.8", {}), ("split", {"ARENA_RA_SPLIT_FIRE": "1"}))}
    same = res["v4.7"][0] == res["v4.8"][0] == res["split"][0]
    print(f"{sess} frames={len(recs)} waves v4.7==v4.8: {'SAME' if same else 'DIFF'} exc={[res[k][4] for k in res]}")
    for k in res:
        w, e, o, f, _x, ta = res[k]
        print(f"   {k}: waves {w[:4]}")
        if k != "v4.6":
            print(f"         tank_army_s={ta} events {e[:3]}")
            print(f"         first builds {f} orders {o}")
