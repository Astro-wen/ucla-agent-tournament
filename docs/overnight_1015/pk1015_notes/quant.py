"""Quantify patterns across real Pokemon games vs a given opponent (default LeCharmander).
Per game: result, our first pick + whether brought, opp Tailwind / Trick Room, Dragonite, Scale Shot; our Sucker Punch
uses / fails (and why), our Protect uses / fails, our attacks into opp Protect, Fake Out; first faint side."""
import collections, glob, json, re, sys, ast

LOG = "/home/linmengfdm/ucla-agent-tournament/logs"
PROT = {"Protect", "Detect", "Spiky Shield", "King's Shield", "Silk Trap", "Burning Bulwark", "Baneful Bunker"}
games = json.load(open("/home/linmengfdm/wt/pk1015/_notes/games.json"))
want = sys.argv[1] if len(sys.argv) > 1 else "LeCharmander"
sp = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())


def load(sess):
    path = glob.glob(f"{LOG}/{sess}-*.jsonl")
    recs = [json.loads(l) for l in open(path[0]) if l.strip()]
    out = []
    for r in recs:
        raw = r.get("raw")
        if isinstance(raw, str):
            try:
                raw = ast.literal_eval(raw)
            except Exception:
                raw = {}
        out.append((r, raw or {}))
    return out


T = collections.Counter()
rows = []
for g in games:
    if want.lower() not in g["opp"].lower():
        continue
    recs = load(g["session"])
    raws = [x[1] for x in recs]
    obs = lambda r: r.get("observation") if isinstance(r.get("observation"), dict) else None
    tp = next((obs(r) for r in raws if r.get("phase") == "team_preview" or (obs(r) or {}).get("phase") == "team_preview"), None)
    moving = [obs(r) for r in raws if obs(r) and (obs(r).get("phase") == "moving" or r.get("phase") == "moving")]
    picks = []
    for r, raw in recs:
        o = str(r.get("out"))
        if "draft_pick:" in o:
            m = re.search(r"draft_pick:vgc-([a-z0-9-]+)", o)
            if m:
                picks.append(sp(m.group(1)))
    if not moving or not tp:
        rows.append((g, "no battle")); continue
    plog = max((b.get("protocol_log") or [] for b in moving), key=len)
    if isinstance(plog, str):
        plog = plog.splitlines()
    ours = {sp(p["species"]) for p in tp["your_roster"]}
    theirs = [sp(p["species"]) for p in tp["opponent_roster"]]
    side = None
    for l in plog:
        m = re.match(r"\|switch\|(p[12])[ab]: [^|]+\|([^|,]+)", l)
        if m and sp(m.group(2)) in ours:
            side = m.group(1); break
    foe = "p2" if side == "p1" else "p1"
    brought = {"us": set(), "opp": set()}
    st = collections.Counter()
    turn = 0
    last_prot = {}
    prev_move = {}
    first_faint = None
    pend_sp = None
    turn_moves = collections.defaultdict(dict)   # turn -> slot -> move
    for l in plog:
        p = l.split("|")
        if l.startswith("|turn|"):
            turn = int(p[2]); continue
        if len(p) < 3:
            continue
        tag = p[1]
        if tag in ("switch", "drag"):
            who = "us" if p[2].startswith(side) else "opp"
            brought[who].add(sp(p[3].split(",")[0]))
        elif tag == "move":
            us = p[2].startswith(side)
            mon = sp(p[2].split(":", 1)[1])
            mv = p[3]
            turn_moves[turn][p[2][:3]] = (mv, p[4][:3] if len(p) > 4 and p[4] else "")
            if us:
                st["our_moves"] += 1
                if mv in PROT:
                    st["our_protect"] += 1
                    if last_prot.get(p[2][:3]) == turn - 1:
                        st["our_consec_protect"] += 1
                    last_prot[p[2][:3]] = turn
                if mv == "Sucker Punch":
                    st["our_sucker"] += 1
                    pend_sp = (turn, p[2][:3], l)
                if mv == "Fake Out":
                    st["our_fakeout"] += 1
            else:
                st["opp_moves"] += 1
                if mv in PROT:
                    st["opp_protect"] += 1
                if mv == "Tailwind":
                    st["opp_tailwind"] += 1
                    st.setdefault("opp_tw_turn", turn)
                if mv == "Trick Room":
                    st["opp_tr"] += 1
                if mv == "Scale Shot":
                    st["opp_scaleshot"] += 1
                if mv == "Fake Out":
                    st["opp_fakeout"] += 1
                if mv in ("Spore", "Rage Powder", "Follow Me", "Sleep Powder"):
                    st["opp_redirect_sleep"] += 1
        elif tag == "-fail":
            us = p[2].startswith(side)
            if us and pend_sp and pend_sp[1] == p[2][:3] and pend_sp[0] == turn:
                st["our_sucker_fail"] += 1
                pend_sp = None
            elif us and last_prot.get(p[2][:3]) == turn:
                st["our_protect_fail"] += 1
        elif tag == "-activate" and "move: Protect" in l or (tag == "-activate" and ("Detect" in l or "Shield" in l or "Silk Trap" in l)):
            if p[2].startswith(foe):
                st["ours_into_opp_protect"] += 1
            else:
                st["opp_into_our_protect"] += 1
        elif tag == "faint":
            if first_faint is None:
                first_faint = "us" if p[2].startswith(side) else "opp"
            if p[2].startswith(side):
                st["our_faints"] += 1
            else:
                st["opp_faints"] += 1
        elif tag == "-boost" and p[2].startswith(foe) and len(p) > 3 and p[3] == "spe":
            st["opp_spe_boosts"] += int(p[4]) if len(p) > 4 and p[4].isdigit() else 1
    st["turns"] = turn
    first = picks[0] if picks else "?"
    rows.append((g, dict(st=st, first=first, first_brought=first in brought["us"], ours=sorted(brought["us"]),
                         opp=sorted(brought["opp"]), theirs=theirs, ff=first_faint)))
    for k, v in st.items():
        T[(g["result"], k)] += v
    T[(g["result"], "n")] += 1
    T[(g["result"], "first_benched")] += first not in brought["us"]
    T[(g["result"], "first_faint_us")] += first_faint == "us"
    T[(g["result"], "opp_dragonite_brought")] += "dragonite" in brought["opp"]
    T[(g["result"], "opp_tw_games")] += st["opp_tailwind"] > 0
    T[(g["result"], "our_sucker_games")] += st["our_sucker"] > 0

for g, d in rows:
    if isinstance(d, str):
        print(g["when"], g["session"], g["result"], d); continue
    s = d["st"]
    print(f"{g['when'][:16]} {g['session']} {g['result'][:1].upper()} T{s['turns']:2d} first={d['first']}{'' if d['first_brought'] else '(BENCH)'} "
          f"ff={d['ff']} us={','.join(d['ours'])} opp={','.join(d['opp'])} | prot {s['our_protect']}/{s['our_consec_protect']}c/{s['our_protect_fail']}f "
          f"sucker {s['our_sucker']}/{s['our_sucker_fail']}f intoP {s['ours_into_opp_protect']} oppP {s['opp_protect']} oppTW {s['opp_tailwind']} "
          f"oppTR {s['opp_tr']} scale {s['opp_scaleshot']} oppFO {s['opp_fakeout']}")
print()
keys = sorted({k for _, k in T})
for res in ("won", "lost"):
    n = T[(res, "n")]
    print(res, n, " ".join(f"{k}={T[(res, k)]}" for k in keys if k != "n"))
