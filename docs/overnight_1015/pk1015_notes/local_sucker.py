"""Sucker Punch outcome in local saved panel logs: uses / fails, by side (cand seat vs opp), and win rate of games with
cand Sucker Punch use."""
import collections, glob, json, sys

C = collections.Counter()
for d in sys.argv[1:]:
    for f in glob.glob(d + "/*.json"):
        try:
            g = json.load(open(f))
        except Exception:
            continue
        log = g.get("log") or []
        seat = g.get("seat", 0)
        cand = "p1" if seat == 0 else "p2"
        pend = None
        turn = 0
        used = {"cand": 0, "opp": 0}
        for l in log:
            p = l.split("|")
            if l.startswith("|turn|"):
                turn = int(p[2]); pend = None; continue
            if len(p) < 3:
                continue
            if p[1] == "move":
                side = "cand" if p[2].startswith(cand) else "opp"
                if p[3] == "Sucker Punch":
                    C[(side, "use")] += 1
                    used[side] += 1
                    pend = (p[2][:3], side)
                else:
                    pend = None if pend and pend[0] == p[2][:3] else pend
            elif p[1] == "-fail" and pend and p[2][:3] == pend[0]:
                C[(pend[1], "fail")] += 1
                pend = None
            elif p[1] in ("-damage", "-activate", "-immune", "-miss") and pend:
                if p[1] == "-activate" and "Protect" in l:
                    C[(pend[1], "into_protect")] += 1
                pend = None if p[1] != "-damage" else pend
        C["games"] += 1
        if used["cand"]:
            C["cand_sp_games"] += 1
            C["cand_sp_wins"] += bool(g.get("win"))
        C["wins"] += bool(g.get("win"))
for k, v in sorted(C.items(), key=str):
    print(k, v)
