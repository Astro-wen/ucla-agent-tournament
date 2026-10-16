"""Opponent Protect pattern in given sessions: P(Protect | protected last turn) vs P(Protect | did not), for foes that
have shown Protect, and how many of our attacks went into a Protect."""
import collections
import glob
import json
import re
import sys

LOG = "/home/linmengfdm/ucla-agent-tournament/logs"
PROT = {"Protect", "Detect", "Spiky Shield", "King's Shield", "Silk Trap", "Burning Bulwark", "Baneful Bunker"}
tot = collections.Counter()
per_mon = collections.defaultdict(list)
for sess in sys.argv[1:]:
    recs = [json.loads(l) for l in open(glob.glob(f"{LOG}/{sess}-*.jsonl")[0]) if l.strip()]
    raws = [r.get("raw") or {} for r in recs]
    tp = next((r["observation"] for r in raws if r.get("phase") == "team_preview"), None)
    moving = [r["observation"] for r in raws if r.get("phase") == "moving" and isinstance(r.get("observation"), dict)]
    plog = max((b.get("protocol_log") or [] for b in moving), key=len)
    if isinstance(plog, str):
        plog = plog.splitlines()
    ours = {re.sub(r"[^a-z0-9]", "", p["species"].lower()) for p in tp["your_roster"]}
    side = None
    for l in plog:
        m = re.match(r"\|switch\|(p[12])[ab]: [^|]+\|([^|,]+)", l)
        if m and re.sub(r"[^a-z0-9]", "", m.group(2).lower()) in ours:
            side = m.group(1)
            break
    foe = "p2" if side == "p1" else "p1"
    turn = 0
    acted = {}          # (slot) -> {turn: move}
    onfield_since = {}
    protected_now = set()
    turn_moves = collections.defaultdict(dict)
    hits_into = 0
    for l in plog:
        if l.startswith("|turn|"):
            turn = int(l.split("|")[2])
            protected_now = set()
            continue
        p = l.split("|")
        if len(p) < 3:
            continue
        if p[1] in ("switch", "drag") and p[2].startswith(foe):
            onfield_since[p[2][:3]] = (turn, p[2].split(":", 1)[1].strip())
        if p[1] == "move":
            who = p[2][:3]
            if who.startswith(foe):
                turn_moves[(who, onfield_since.get(who, (0, "?"))[1])][turn] = p[3]
                if p[3] in PROT:
                    protected_now.add(who)
        if p[1] == "-activate" and "move: Protect" in l or (p[1] == "-activate" and any(x in l for x in ("Protect", "Detect"))):
            if p[2].startswith(foe):
                hits_into += 1
    tot["our_moves_blocked"] += hits_into
    for (slot, mon), mv in turn_moves.items():
        ts = sorted(mv)
        if not any(m in PROT for m in mv.values()):
            continue
        seq = "".join("P" if mv[t] in PROT else "a" for t in ts)
        per_mon[mon].append(seq)
        for a, b in zip(ts, ts[1:]):
            if b != a + 1:
                continue
            prev_p, now_p = mv[a] in PROT, mv[b] in PROT
            tot[("after_P" if prev_p else "after_attack", "P" if now_p else "attack")] += 1
print("our attacks blocked by Protect:", tot["our_moves_blocked"])
for k in (("after_attack", "P"), ("after_attack", "attack"), ("after_P", "P"), ("after_P", "attack")):
    print(k, tot[k])
aa = tot[("after_attack", "P")] + tot[("after_attack", "attack")]
pp = tot[("after_P", "P")] + tot[("after_P", "attack")]
print(f"P(Protect | attacked last turn) = {tot[('after_attack','P')]}/{aa}; P(Protect | protected last turn) = {tot[('after_P','P')]}/{pp}")
for mon, seqs in per_mon.items():
    print(f"  {mon:12s} " + " | ".join(seqs))
