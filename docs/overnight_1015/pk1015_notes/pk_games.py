"""Turn-by-turn digest of selected real Pokemon games: draft picks, rosters, leads, every move / faint."""
import glob
import json
import re
import sys

LOG = "/home/linmengfdm/ucla-agent-tournament/logs"
RES = dict(a.split("=") for a in sys.argv[1:])     # session=W/L


def sp(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


for sess, res in RES.items():
    path = glob.glob(f"{LOG}/{sess}-*.jsonl")
    recs = [json.loads(l) for l in open(path[0]) if l.strip()]
    raws = [r.get("raw") or {} for r in recs]
    draft = [r.get("observation") for r in raws if r.get("phase") == "draft" and isinstance(r.get("observation"), dict)]
    tp = next((r["observation"] for r in raws if r.get("phase") == "team_preview"), None)
    moving = [r["observation"] for r in raws if r.get("phase") == "moving" and isinstance(r.get("observation"), dict)]
    print(f"\n######## {sess} {res}  ({len(moving)} battle decisions)")
    if draft:
        last = draft[-1]
        picks = last.get("picks") or last.get("draft_picks") or []
        if picks:
            print("  picks:", " ".join(f"{p.get('player', p.get('by', '?'))}:{sp(str(p.get('species') or p.get('card_id') or p.get('card', '')))}{'(auto)' if p.get('auto') else ''}" for p in picks)[:600])
    if tp:
        print("  our 6  :", [sp(p["species"]) for p in tp["your_roster"]])
        print("  their 6:", [sp(p["species"]) for p in tp["opponent_roster"]])
    if not moving:
        continue
    plog = max((b.get("protocol_log") or [] for b in moving), key=len)
    if isinstance(plog, str):
        plog = plog.splitlines()
    ours = {sp(p["species"]) for p in tp["your_roster"]} if tp else set()
    side = None
    for l in plog:
        m = re.match(r"\|switch\|(p[12])[ab]: [^|]+\|([^|,]+)", l)
        if m and sp(m.group(2)) in ours:
            side = m.group(1)
            break
    turn, line = 0, []
    for l in plog:
        if l.startswith("|turn|"):
            if line:
                print(f"  T{turn}: " + " ; ".join(line))
            turn, line = int(l.split("|")[2]), []
            continue
        p = l.split("|")
        if len(p) < 3:
            continue
        tag = p[1]
        who = lambda x: ("US " if x.startswith(side) else "OPP ") + sp(x.split(":", 1)[1]) if ":" in x else x
        if tag == "move":
            tgt = who(p[4]) if len(p) > 4 and p[4] else ""
            extra = " [miss]" if "[miss]" in l else (" [still]" if "[still]" in l else "")
            line.append(f"{who(p[2])}>{p[3]}>{tgt}{extra}")
        elif tag == "switch" or tag == "drag":
            line.append(f"{who(p[2])} in")
        elif tag == "faint":
            line.append(f"**{who(p[2])} FAINT**")
        elif tag in ("-crit", "-miss"):
            line.append(f"({tag[1:]} {who(p[2])})")
        elif tag == "win":
            line.append(f"WIN {p[2]}")
    if line:
        print(f"  T{turn}: " + " ; ".join(line))
