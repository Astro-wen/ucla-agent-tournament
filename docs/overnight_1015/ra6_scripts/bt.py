"""Build timeline of OUR side in a real RA recording (read only): when the n-th building of each type first appears,
first tank / n-th tank times, harvester counts, cumulative spending by category up to given times."""
import collections
import glob
import json
import sys

LOG = "/home/linmengfdm/ucla-agent-tournament/logs"
CFG = json.load(open("/home/linmengfdm/wt/ra6/data/red_alert_config.json"))
sess = sys.argv[1]
path = glob.glob(f"{LOG}/{sess}*.jsonl")[0]
recs = [json.loads(x) for x in open(path) if x.strip()]


def cost(t):
    for k in ("units", "buildings"):
        d = (CFG["rules"].get(k) or {}).get(t)
        if isinstance(d, dict) and d.get("cost"):
            return d["cost"]
    return 0


firstn = {}
seen_ids = {}
TANKS = ("1tnk", "2tnk", "3tnk", "4tnk", "ttnk", "v2rl", "arty", "jeep", "ftrk", "apc")
DEF = ("gun", "tsla", "ftur", "pbox", "hbox", "agun", "sam")
spent = collections.Counter()   # category -> cost of newly seen ids
marks = [120, 180, 240, 300, 360]
out_marks = {}
for r in recs:
    raw = r.get("raw") or {}
    ob = raw.get("observation") if isinstance(raw.get("observation"), dict) else None
    if not ob:
        continue
    tm = (raw.get("time") or {}).get("game_seconds") or ob.get("tick", 0) / 25.0
    bc = collections.Counter(b.get("type") for b in ob.get("buildings") or [])
    for t, n in bc.items():
        for i in range(1, n + 1):
            firstn.setdefault((t, i), tm)
    for b in ob.get("buildings") or []:
        if b.get("id") not in seen_ids:
            seen_ids[b.get("id")] = tm
            t = b.get("type")
            cat = "def" if t in DEF else "eco" if t in ("proc", "powr", "apwr") else "prod" if t in ("weap", "barr", "tent", "fact") else "other:" + t
            spent[cat] += cost(t)
    for u in ob.get("units") or []:
        if u.get("id") not in seen_ids and not u.get("wreck"):
            seen_ids[u.get("id")] = tm
            t = u.get("type")
            cat = "harv" if t == "harv" else "veh" if t in TANKS else "inf" if t in ("e1", "e2", "e3", "e4", "e6", "dog", "spy") else "other:" + t
            spent[cat] += cost(t)
    uc = collections.Counter(u.get("type") for u in ob.get("units") or [] if not u.get("wreck"))
    nt = sum(uc[t] for t in TANKS)
    for i in range(1, nt + 1):
        firstn.setdefault(("veh", i), tm)
    for m in marks:
        if tm >= m and m not in out_marks:
            out_marks[m] = (round(tm), dict(spent), uc.get("harv", 0), (ob.get("economy") or {}).get("cash"))
print(sess, "faction", (recs[0].get("raw") or {}).get("observation", {}).get("faction") if isinstance((recs[0].get("raw") or {}).get("observation"), dict) else "")
order = sorted(firstn.items(), key=lambda kv: kv[1])
print("  ", " ".join(f"{t}{'' if i == 1 else i}@{v:.0f}" for (t, i), v in order if t != "veh" or i in (1, 2, 3, 4, 6, 8)))
for m, (tm, sp, hv, cash) in sorted(out_marks.items()):
    print(f"  by {m}s (frame {tm}): harv={hv} cash={cash} spent(first-seen, excl. initial proc harv)={sp}")
