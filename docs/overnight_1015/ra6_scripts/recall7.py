"""Real Red Alert recordings (logs/<session>-*.jsonl, read only) fed to the ra6 worktree's bot under several env
configs: per session the waves / retreats, first build / train orders of interest, exceptions. Approximate: one frame
in ~10 s and the real game went on as it did.

    python recall7.py NAME=ENV=VAL,ENV=VAL ... [-- session ...]     (NAME=  alone = defaults)
"""
import collections
import glob
import json
import os
import sys

sys.path.insert(0, os.environ.get("RA_ROOT", "/home/linmengfdm/wt/ra6"))
os.environ["ARENA_RECORD"] = "0"
from agent.redalert import RedAlertPolicy  # noqa: E402
from agent.redalert.params import load as load_params  # noqa: E402
from local.ra_rules import Rules  # noqa: E402

LOG = "/home/linmengfdm/ucla-agent-tournament/logs"
CONFIG = Rules().config
args = sys.argv[1:]
sessions = []
if "--" in args:
    i = args.index("--")
    args, sessions = args[:i], args[i + 1:]
cfgs = []
for a in args:
    name, _, envs = a.partition("=")
    env = {}
    for kv in filter(None, envs.split(",")):
        k, _, val = kv.partition("=")
        env[k] = val
    cfgs.append((name, env))


def run(recs, env):
    pol = RedAlertPolicy(params=load_params(env))
    waves = []
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
                waves.append(e.split(":")[0] if e.startswith("attack wave") else e[:60])
        n = len(evs)
        batch = out[0] if isinstance(out, tuple) else out
        for o in batch or []:
            if isinstance(o, dict) and o.get("cmd") in ("build", "train"):
                it = o.get("item")
                if it in ("gun", "tsla", "pbox", "ftur", "hbox", "weap", "fix", "proc", "2tnk", "3tnk", "1tnk"):
                    k = it
                    c = 2
                    while k in first and it in ("weap", "proc"):
                        k = f"{it}{c}"
                        c += 1
                    if it in ("weap", "proc") and any(first.get(x) == round(t) for x in first if x.startswith(it)):
                        continue
                    first.setdefault(k, round(t))
    return waves, first, pol.exceptions


paths = []
for path in sorted(glob.glob(f"{LOG}/*.jsonl"), key=os.path.getmtime):
    if sessions and not any(os.path.basename(path).startswith(s) for s in sessions):
        continue
    if '"red_alert"' not in open(path).readline():
        continue
    paths.append(path)
tot = collections.Counter()
for path in paths:
    recs = [json.loads(x) for x in open(path) if x.strip()]
    if not any((r.get("raw") or {}).get("game_type") == "red_alert" for r in recs):
        continue
    sess = os.path.basename(path)[:8]
    res = {name: run(recs, env) for name, env in cfgs}
    base = res[cfgs[0][0]][0]
    same = all(res[nm][0] == base for nm, _e in cfgs)
    tot["sessions"] += 1
    tot["same_waves"] += same
    for nm in res:
        tot[f"exc_{nm}"] += res[nm][2]
    print(f"{sess} frames={len(recs)} waves {'SAME' if same else 'DIFF'} exc={[res[k][2] for k in res]}")
    for nm in res:
        w, f, _x = res[nm]
        fb = " ".join(f"{k}@{v}" for k, v in sorted(f.items(), key=lambda kv: kv[1]))
        print(f"   {nm:6s} waves {w[:4]}")
        print(f"          first {fb}")
print(dict(tot))
