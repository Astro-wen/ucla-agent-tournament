"""Measure our agent against red-team (and standard) backgrounds, paired by deal, with a self-play reference.

    python -m local.redteam_measure --bgs talker,talker_quiet,llmq,heuristic --ks 1,3 --seed 2200 --games 1000 --ref

Every background plays the SAME deals: for (seed, game g, k) the roles, our seats and the game rng come from
local.eval_werewolf.deal(seed, g, "llmq", k), whatever the background (eval_werewolf keys its deals by background name,
which would unpair the comparison of two red-team variants). With --ref each game is also played with our seats taken
by the background's own seat (self-play): the paired difference agent - ref says whether our seat does better or
worse than one of the background's seats would in exactly that seat. Reported per background, k and role (a game counts
once per role among our seats): our win rate [Wilson 95% CI], the reference's, the paired difference [95% CI], and
behaviour of our seats (villager-side votes on wolves / abstains, how our seats died). ARENA_WW_TIMING=rt switches the
discussion timing (local/werewolf_env.py). Simulated games never record (ARENA_RECORD=0).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

from . import eval_werewolf as ev
from .werewolf_env import ContestantSeat, Game, deal_roles

ROLES = ("villager", "seer", "wolf")


def play(spec: str, seed: int, g: int, bg: str, k: int, ref: bool = False) -> dict:
    gseed, ours, _ = ev.deal(seed, g, "llmq", k)
    roles = deal_roles(random.Random(gseed))
    kinds = ev.parse_bg(bg)[1]                       # `mixed:A+B`: each seat drawn (same draw for agent and ref)
    draw = random.Random(f"mix:{seed}:{g}:{k}:{bg}")
    kind = [draw.choice(kinds) for _ in range(7)]
    seats = [(ev.make_seat(kind[i]) if ref else ContestantSeat(ev.factory(spec))) if i in ours else ev.make_seat(kind[i])
             for i in range(7)]
    game = Game(seats, random.Random(gseed))
    ret = game.play()
    m: dict = defaultdict(int)
    for i in ours:
        side = "wolf" if roles[i] == "wolf" else "town"
        for v in game.votes:
            if i not in v["votes"]:
                continue
            t = v["votes"][i]
            if side == "town":
                m["town_votes"] += 1
                m["town_abstain"] += t is None
                m["town_vote_wolf"] += t is not None and roles[t] == "wolf"
                m["town_vote_seer"] += t is not None and roles[t] == "seer"
                if v["day"] == 1:
                    m["town_d1_votes"] += 1
                    m["town_d1_wolf"] += t is not None and roles[t] == "wolf"
                    m["town_d1_seer"] += t is not None and roles[t] == "seer"
            else:
                m["wolf_votes"] += 1
                m["wolf_vote_ally"] += t is not None and roles[t] == "wolf"
        died = [d for d in game.dead if d["seat"] == i]
        m[f"{side}_n"] += 1
        m[f"{side}_" + (f"died_{died[0]['cause']}_d{min(died[0]['day'], 3)}" if died else "survived")] += 1
    st = game.stats
    return {"g": g, "roles": [roles[i] for i in ours], "winner": game.winner, "wins": [ret[i] > 0 for i in ours],
            "days": game.day, "m": dict(m),
            "fail": {key: sum(st[key][i] for i in ours) for key in ev.FAIL_KEYS},
            "bg_fail": {key: sum(st[key][i] for i in range(7) if i not in ours) for key in ev.FAIL_KEYS}}


def _chunk(args):
    os.environ.setdefault("ARENA_RECORD", "0")
    spec, seed, bg, k, games, ref = args
    return [(bg, k, ref, play(spec, seed, g, bg, k, ref)) for g in games]


def outcomes(rec: dict) -> dict:
    out = {role: (1.0 if rec["winner"] == ev.SIDE[role] else 0.0) for role in ROLES if role in rec["roles"]}
    out["seat"] = sum(rec["wins"]) / len(rec["wins"])
    return out


def summarize(results: dict) -> dict:
    out = {}
    for (bg, k), by_ref in sorted(results.items()):
        ours, refs = by_ref.get(False, {}), by_ref.get(True, {})
        row = {}
        for key in ROLES + ("seat",):
            a = [outcomes(ours[g])[key] for g in sorted(ours) if key in outcomes(ours[g])]
            r = [outcomes(refs[g])[key] for g in sorted(refs) if key in outcomes(refs[g])]
            d = [outcomes(ours[g])[key] - outcomes(refs[g])[key] for g in sorted(set(ours) & set(refs))
                 if key in outcomes(ours[g])]
            row[key] = {"n": len(a), "rate": sum(a) / len(a) if a else None,
                        "ci": ev.wilson(sum(a), len(a)) if key != "seat" else ev.mean_ci(a)[1:],
                        "ref": sum(r) / len(r) if r else None, "diff": ev.mean_ci(d) if d else None}
        beh: dict = defaultdict(int)
        fails: dict = defaultdict(int)
        for rec in refs.values():
            for key, x in rec["m"].items():
                beh["ref_" + key] += x
        for rec in ours.values():
            for key, x in rec["m"].items():
                beh[key] += x
            for key, x in rec["fail"].items():
                fails[key] += x
            for key, x in rec["bg_fail"].items():
                fails["bg_" + key] += x
        row["behaviour"] = dict(beh)
        row["fails"] = dict(fails)
        out[f"{bg}/k{k}"] = row
    return out


def report(summary: dict) -> None:
    print("win rate of our seats % [Wilson 95% CI] (n) | ref = background seat in our seat (self-play) | "
          "diff = ours - ref, paired, pp [95% CI]")
    for name, row in summary.items():
        print(f"\n{name}")
        for key in ROLES + ("seat",):
            c = row[key]
            if not c["n"]:
                continue
            txt = f"  {key:8s} {100 * c['rate']:5.1f} [{100 * c['ci'][0]:5.1f},{100 * c['ci'][1]:5.1f}] ({c['n']})"
            if c["ref"] is not None:
                d = c["diff"]
                txt += f"   ref {100 * c['ref']:5.1f}   diff {100 * d[0]:+5.1f} [{100 * d[1]:+5.1f},{100 * d[2]:+5.1f}]"
            print(txt)
        b = row["behaviour"]
        for who, p in (("ours", ""), ("ref ", "ref_")):
            tv = b.get(p + "town_votes", 0)
            if tv:
                d1 = max(1, b.get(p + "town_d1_votes", 0))
                print(f"  {who} town votes: on a wolf {100 * b.get(p + 'town_vote_wolf', 0) / tv:.1f}%, on the seer "
                      f"{100 * b.get(p + 'town_vote_seer', 0) / tv:.1f}%, abstain "
                      f"{100 * b.get(p + 'town_abstain', 0) / tv:.1f}% of {tv}; day 1: on a wolf "
                      f"{100 * b.get(p + 'town_d1_wolf', 0) / d1:.1f}%, on the seer "
                      f"{100 * b.get(p + 'town_d1_seer', 0) / d1:.1f}%")
        for who, p in (("ours", ""), ("ref ", "ref_")):
            for side in ("town", "wolf"):
                n = b.get(f"{p}{side}_n", 0)
                if n:
                    pre = p + side + "_"
                    parts = sorted((key[len(pre):], x) for key, x in b.items()
                                   if key.startswith(pre) and ("died" in key or "survived" in key))
                    print(f"  {who} {side} fate (n={n}): " + ", ".join(f"{key} {100 * x / n:.0f}%" for key, x in parts))
        f = {key: x for key, x in row["fails"].items() if x}
        if f:
            print(f"  failures: {f}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bgs", default="talker,llmq,heuristic")
    ap.add_argument("--ks", default="1,3")
    ap.add_argument("--seed", type=int, default=2200)
    ap.add_argument("--games", type=int, default=500)
    ap.add_argument("--spec", default="agent")
    ap.add_argument("--ref", action="store_true", help="also play each game with our seats taken by the background")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--chunk", type=int, default=100)
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    tasks = []
    for bg in a.bgs.split(","):
        ev.parse_bg(bg)
        for k in [int(x) for x in a.ks.split(",")]:
            for ref in ([False, True] if a.ref else [False]):
                for lo in range(0, a.games, a.chunk):
                    tasks.append((a.spec, a.seed, bg, k, list(range(lo, min(a.games, lo + a.chunk))), ref))
    t0 = time.time()
    results: dict = defaultdict(lambda: defaultdict(dict))
    if a.jobs > 1:
        with ProcessPoolExecutor(a.jobs) as ex:
            for out in ex.map(_chunk, tasks):
                for bg, k, ref, rec in out:
                    results[(bg, k)][ref][rec["g"]] = rec
    else:
        for t in tasks:
            for bg, k, ref, rec in _chunk(t):
                results[(bg, k)][ref][rec["g"]] = rec
    summary = summarize(results)
    report(summary)
    print(f"\n{sum(len(v) for r in results.values() for v in r.values())} games in {time.time() - t0:.0f}s "
          f"(seed {a.seed}, timing {os.environ.get('ARENA_WW_TIMING', 'rounds')})")
    if a.out:
        with open(a.out, "w") as f:
            json.dump({"args": vars(a), "summary": summary}, f, indent=1, default=list)


if __name__ == "__main__":
    main()
