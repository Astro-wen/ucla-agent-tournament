"""List real pokemon games: session, date, opponent, result, from logs/agent-main.out*."""
import glob, re, sys, json, os
LOG = "/home/linmengfdm/ucla-agent-tournament/logs"
files = sorted(glob.glob(f"{LOG}/agent-main.out*"))
pending = []          # (game, opponents, selfplay, count)
seat_info = {}
games = []
last_finished = None
for f in files:
    for line in open(f, errors="replace"):
        line = line.rstrip("\n")
        m = re.search(r"Match assigned: (\S+) \((\w+)\)(, self-play: your agent plays all (\d+))?", line)
        if m:
            n = int(m.group(4)) if m.group(4) else 1
            pending.append([m.group(1), None, m.group(2), n, line[:19]])
            continue
        m = re.search(r"^\S+ \S+\s+Opponents: (.*)$", line)
        if m and pending:
            # attach to most recent assignment without opponents
            for p in reversed(pending):
                if p[1] is None:
                    p[1] = m.group(1).strip()
                    break
            continue
        m = re.search(r"\[match (\S+) seat=(\S+)\] starting", line)
        if m:
            for i, p in enumerate(pending):
                if p[0] == m.group(1):
                    seat_info[m.group(2)] = (p[1] or "SELF", p[2], p[4])
                    p[3] -= 1
                    if p[3] <= 0:
                        pending.pop(i)
                    break
            continue
        m = re.search(r"\[match (\S+) seat=(\S+)\] finished: your agent \(player (\d)\) (\w+)", line)
        if m:
            last_finished = (m.group(1), m.group(2), m.group(3), m.group(4))
            continue
        m = re.search(r"\[match-summary\] session=(\w+) game=(\S+)", line)
        if m and last_finished:
            g, seat, pl, res = last_finished
            opp, kind, when = seat_info.get(seat, ("?", "?", "?"))
            games.append(dict(session=m.group(1), game=g, opp=opp, kind=kind, when=when, player=pl, result=res, seat=seat))
            last_finished = None
want = sys.argv[1] if len(sys.argv) > 1 else None
out = [g for g in games if g["game"].startswith("pokemon") and (want is None or want.lower() in g["opp"].lower())]
for g in out:
    rec = glob.glob(f"{LOG}/{g['session']}-*.jsonl")
    print(g["when"], g["session"], g["opp"], g["kind"], "p" + g["player"], g["result"], "rec" if rec else "NOREC")
import collections
c = collections.Counter((g["opp"], g["result"]) for g in out)
print(c)
json.dump(out, open("/home/linmengfdm/wt/pk1015/_notes/games.json", "w"), indent=1)
