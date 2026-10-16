"""Discordant games of a pair of runs: did either arm use Sucker Punch, did the brought four / leads differ?"""
import json, sys
C, B = sys.argv[1], sys.argv[2]
def rows(p):
    return {(json.loads(l)["opp"], json.loads(l)["g"]): json.loads(l) for l in open(p)}
c, b = rows(C), rows(B)
n = same_bring = 0
for k in sorted(set(c) & set(b)):
    lc = json.load(open(f"{C}.logs/{k[0]}-g{k[1]}.json")); lb = json.load(open(f"{B}.logs/{k[0]}-g{k[1]}.json"))
    s = c[k]["seat"]
    sc = sum("|Sucker Punch|" in l and l.split("|")[2].startswith("p%d" % (s + 1)) for l in lc["log"])
    sb = sum("|Sucker Punch|" in l and l.split("|")[2].startswith("p%d" % (s + 1)) for l in lb["log"])
    same = lc["brought"] == lb["brought"] and lc["rosters"] == lb["rosters"]
    n += 1; same_bring += same
    if c[k]["win"] != b[k]["win"]:
        print(k, "cand win" if c[k]["win"] else "base win", "sucker c/b", sc, sb, "same rosters+brought" if same else "DIFFERENT draft/preview")
print(f"{same_bring}/{n} games with identical rosters and brought teams")
