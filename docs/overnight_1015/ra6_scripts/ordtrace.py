"""Replay one league game and print our bot's order batches between t0 and t1 (game seconds), with unit types.
    python ordtrace.py <opp> <seed> <map> <a_seat> <t0> <t1> [ENV=VAL ...]      (code root: PYTHONPATH / cwd)"""
import collections
import os
import sys

opp, seed, map_id, seat, t0, t1 = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4]), float(sys.argv[5]), \
    float(sys.argv[6])
for a in sys.argv[7:]:
    k, v = a.split("=", 1)
    os.environ[k] = v
os.environ["ARENA_RECORD"] = "0"
import local.ra_platform as P  # noqa: E402
import agent.redalert as RA  # noqa: E402
import agent.agent as AA  # noqa: E402

orig = RA.RedAlertPolicy.decide


def decide(self, raw, config=None):
    out = orig(self, raw, config)
    v = self.view
    if v is not None and t0 <= v.game_s <= t1:
        batch = out[0] if isinstance(out, tuple) else out
        if batch:
            parts = []
            for o in batch:
                if not isinstance(o, dict):
                    continue
                us = o.get("units") or []
                ty = collections.Counter(v.unit_by_id[u]["type"] for u in us if u in v.unit_by_id)
                tgt = o.get("target")
                tt = next((e["type"] for e in v.enemies if e["id"] == tgt), None) if tgt is not None else None
                parts.append(f"{o.get('cmd')}{'->' + str(o.get('to')) if o.get('to') else ''}"
                             f"{'@' + str(tt) if tgt is not None else ''}{dict(ty) if ty else ''}"
                             f"{o.get('item', '')}")
            a = self.army
            print(f"t={v.game_s:6.1f} st={a.state} split={len(a.split)} K/D={v.kills}/{v.deaths} | " + " ; ".join(parts),
                  flush=True)
    return out


RA.RedAlertPolicy.decide = decide
row = P.play_match(AA.create_agent(), P.make_factory(opp)(seed * 2 + 2), seed=seed, map_id=map_id, a_seat=seat)
print("RESULT", row["result_a"], f"{row['game_s']/60:.1f}m", "K/D", row["a"]["kills_cost"], row["a"]["deaths_cost"])
