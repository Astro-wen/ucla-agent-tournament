"""For each of our Sucker Punches in real games: the target (from our recorded answer) and what that foe did that turn."""
import ast, collections, glob, json, re
LOG = "/home/linmengfdm/ucla-agent-tournament/logs"
games = json.load(open("/home/linmengfdm/wt/pk1015/_notes/games.json"))
C = collections.Counter()
for g in games:
    if g["opp"] == "SELF":
        continue
    path = glob.glob(f"{LOG}/{g['session']}-*.jsonl")
    recs = [json.loads(l) for l in open(path[0]) if l.strip()]
    plog = []
    answers = {}
    for r in recs:
        raw = r["raw"] if isinstance(r["raw"], dict) else ast.literal_eval(r["raw"])
        obs = raw.get("observation") if isinstance(raw.get("observation"), dict) else {}
        if obs.get("protocol_log") and len(obs["protocol_log"]) > len(plog):
            plog = obs["protocol_log"]
        out = r.get("out")
        if isinstance(out, str):
            try:
                out = ast.literal_eval(out)
            except Exception:
                out = {}
        if raw.get("phase") == "moving" and isinstance(out, dict) and out.get("type") == "doubles_turn":
            answers[obs.get("turn")] = out
    if isinstance(plog, str):
        plog = plog.splitlines()
    tp = next((ast.literal_eval(r["raw"]) if isinstance(r["raw"], str) else r["raw"] for r in recs
               if (r["raw"] if isinstance(r["raw"], dict) else {}).get("phase") == "team_preview"), None)
    ours = {re.sub(r"[^a-z0-9]", "", p["species"].lower()) for p in tp["observation"]["your_roster"]} if tp else set()
    side = None
    for l in plog:
        m = re.match(r"\|switch\|(p[12])[ab]: [^|]+\|([^|,]+)", l)
        if m and re.sub(r"[^a-z0-9]", "", m.group(2).lower()) in ours:
            side = m.group(1); break
    foe = "p2" if side == "p1" else "p1"
    turn = 0
    acts = collections.defaultdict(dict)   # turn -> foe slot -> action
    sp = []
    for l in plog:
        p = l.split("|")
        if l.startswith("|turn|"):
            turn = int(p[2]); continue
        if len(p) < 3:
            continue
        if p[1] == "move" and p[2].startswith(foe):
            acts[turn][p[2][2]] = acts[turn].get(p[2][2]) or p[3]
        elif p[1] in ("switch", "drag") and p[2].startswith(foe):
            acts[turn].setdefault(p[2][2], "SWITCH")
        elif p[1] == "move" and p[2].startswith(side) and p[3] == "Sucker Punch":
            sp.append([turn, p[2][2], "ok"])
        elif p[1] == "-fail" and p[2].startswith(side) and sp and sp[-1][0] == turn and sp[-1][1] == p[2][2]:
            sp[-1][2] = "FAIL"
    for t, slot, res in sp:
        a = answers.get(t) or {}
        tgt = None
        for k in ("slot_0", "slot_1"):
            x = a.get(k) or {}
            if x.get("move_id") == "suckerpunch":
                tgt = x.get("target")
        tslot = {1: "a", 2: "b"}.get(tgt)
        what = acts[t].get(tslot, "?") if tslot else "?"
        cat = ("protect" if what in ("Protect", "Detect", "Spiky Shield", "Silk Trap") else "switch" if what == "SWITCH"
               else "?" if what == "?" else "other")
        C[(res, cat)] += 1
        if res == "FAIL":
            print(g["session"], g["opp"], f"T{t}", "target", tgt, "did", what)
print(sorted(C.items()))
