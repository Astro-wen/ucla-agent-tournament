"""Write _notes/runs/sucker_counts-<seed>.json: our Sucker Punch use / fail counts per arm per opponent, from the saved .logs.
usage: sucker_counts.py SEED"""
import json, os, sys, importlib.util
here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("paired", os.path.join(here, "paired.py"))
src = open(os.path.join(here, "paired.py")).read().split("\nargs = sys.argv")[0]   # functions only
ns = {}
exec(compile(src, "paired.py", "exec"), ns)
seed = sys.argv[1]
out = {}
for arm in ("base", "suck1"):
    for opp in ("meta", "champion"):
        p = os.path.join(here, "runs", f"{arm}-{opp}-{seed}.jsonl")
        if not os.path.exists(p):
            continue
        u, f, g, gu = ns["sucker"](p)
        out.setdefault(arm, {})[opp] = {"use": u, "fail": f, "games": g, "games_with_use": gu,
                                        "fail_rate": round(f / u, 3) if u else None}
dst = os.path.join(here, "runs", f"sucker_counts-{seed}.json")
json.dump(out, open(dst, "w"), indent=1)
print(json.dumps(out))
