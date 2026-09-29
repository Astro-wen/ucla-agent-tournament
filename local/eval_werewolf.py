"""Mixed-table Werewolf evaluation: candidate vs reference agents in the same seats of the same games.

    python -m local.eval_werewolf --games 1000 --seeds 7 --jobs 3            # agent vs champion, all conditions
    python -m local.eval_werewolf --games 200 --bg llm,mixed --ks 1,3,7 --pairs agent:champion

A condition is (background, k): k seats (1..7) are ours, the other 7-k are one background type:
  first      legal_actions[0] (examples/basic_agent.py, smoke_agent.py; the old "official_baseline")
  heuristic  public-record suspicion bot modelled on the platform's demo werewolf_agent.py (local/werewolf_bots.py)
  random     a uniformly random legal action
  llm        a chat-reading, persuadable, noisy stand-in for examples/llm_agent.py (local/werewolf_bots.py)
  llmx       the llm stand-in phrasing freely (casual text, claim-looking table talk) and less persuadable
  mixed      each background seat drawn from first / heuristic / random / llm (llmx is not drawn, so the deals of
             earlier runs are unchanged)
k = 7 does not depend on the background and is played once. For game g of a seed, the roles, our seat set and the
background draw are fixed, so every agent under test plays exactly the same deal (paired). Seeds 1 and 2 were used
while tuning the agent (NOTES.md): confirm on other seeds.

An agent spec is a factory name (`agent`, `champion`, or `module:attr`), or `W/V`: spec W plays our wolf seats and V
our villager and seer seats. With several of our seats at one table the candidate's villagers also play against
the candidate's wolves, so a whole-table comparison cannot say whether one role got worse; the role verdict
therefore uses side-isolated pairs: `agent/champion` vs `champion` (only our wolves changed) for the wolf role and
`champion/agent` vs `champion` (only our villagers and seer changed) for the villager and seer roles. The overall
verdict is the whole-table `agent` vs `champion` per-seat win rate. Pooling over every background and k can hide a
loss in one kind of table, so the verdict also lists slice regressions: per background, over all k and over k <= 3
(the realistic case: few of our seats per table), every role / per-seat paired difference whose 95% CI lies entirely
below 0. SHIP needs none.

Reported per condition and pooled: win rate of our seats by role (a game counts once per role present among our
seats; all our seats of one side share the outcome) with Wilson 95% CIs, the per-seat win rate, and each pair's
paired difference with a 95% CI. Also counted: crashes, illegal actions, rejected chats, chats after terminating,
decisions over the 120 s idle limit, and the slowest decision of our seats.
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import random
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

from .werewolf_bots import HeuristicSeat, LLMSeat, LLMVariedSeat
from .werewolf_env import ContestantSeat, Game, OfficialBaseline, RandomSeat, deal_roles

BACKGROUNDS = {"first": OfficialBaseline, "heuristic": HeuristicSeat, "random": RandomSeat, "llm": LLMSeat,
               "llmx": LLMVariedSeat}
MIXED = ("first", "heuristic", "random", "llm")       # what a `mixed` seat is drawn from (fixed: keeps old deals)
LOW_K = 3                                               # slice "k<=3": few of our seats at a table
AGENTS = {"agent": "agent.agent:create_agent", "champion": "champion.agent.agent:create_agent"}
ROLES = ("villager", "seer", "wolf")
SIDE = {"villager": "villager", "seer": "villager", "wolf": "wolf"}
FAIL_KEYS = ("crash", "illegal", "bad_chat", "timeout", "chat_after_terminate")
DEFAULT_PAIRS = "agent:champion,agent/champion:champion,champion/agent:champion"
_FACTORIES: dict = {}


def factory(name: str):
    for al in filter(None, os.environ.get("WW_EVAL_ALIASES", "").split(";")):
        alias, _, target = al.partition("=")
        AGENTS.setdefault(alias, target)
    if name not in _FACTORIES:
        mod, _, attr = AGENTS.get(name, name).partition(":")
        _FACTORIES[name] = getattr(importlib.import_module(mod), attr or "create_agent")
    return _FACTORIES[name]


def deal(seed: int, g: int, bg: str, k: int):
    """(game rng seed, our seats, background type per seat) -- identical for every agent under test."""
    r = random.Random(f"deal:{seed}:{g}:{bg}:{k}")
    ours = sorted(r.sample(range(7), k))
    kinds = list(MIXED)
    types = {i: (r.choice(kinds) if bg == "mixed" else bg) for i in range(7) if i not in ours}
    return r.getrandbits(62), ours, types


def play_one(spec: str, seed: int, g: int, bg: str, k: int) -> dict:
    gseed, ours, types = deal(seed, g, bg, k)
    wolf_spec, vill_spec = spec.split("/", 1) if "/" in spec else (spec, spec)
    roles = deal_roles(random.Random(gseed))
    seats = [ContestantSeat(factory(wolf_spec if roles[i] == "wolf" else vill_spec)) if i in ours
             else BACKGROUNDS[types[i]]() for i in range(7)]
    game = Game(seats, random.Random(gseed))
    ret = game.play()
    st = game.stats
    return {"g": g, "roles": [game.roles[i] for i in ours], "winner": game.winner,
            "wins": [ret[i] > 0 for i in ours], "days": game.day,
            "fail": {key: sum(st[key][i] for i in ours) for key in FAIL_KEYS},
            "bg_fail": {key: sum(st[key][i] for i in range(7) if i not in ours) for key in FAIL_KEYS},
            "max_ms": max(st["max_ms"][i] for i in ours), "chats": sum(st["chats"][i] for i in ours),
            "dms": sum(st["dms"][i] for i in ours)}


def _chunk(args):
    spec, seed, bg, k, games = args
    os.environ.setdefault("ARENA_RECORD", "0")
    return [(spec, seed, bg, k, play_one(spec, seed, g, bg, k)) for g in games]


# ---------------------------------------------------------------- statistics
def wilson(w: float, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = w / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def mean_ci(xs: list[float], z: float = 1.96) -> tuple[float, float, float]:
    n = len(xs)
    if n == 0:
        return 0.0, 0.0, 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / max(1, n - 1)
    h = z * math.sqrt(var / n)
    return m, m - h, m + h


def role_outcomes(rec: dict) -> dict[str, float]:
    """role -> 1/0 (did our seats of that role win), for the roles present among our seats; 'seat' = per-seat mean."""
    out = {}
    for role in ROLES:
        if role in rec["roles"]:
            out[role] = 1.0 if rec["winner"] == SIDE[role] else 0.0
    out["seat"] = sum(rec["wins"]) / len(rec["wins"])
    return out


def _rate(v: list[float], key: str) -> dict:
    return {"n": len(v), "rate": sum(v) / len(v) if v else None,
            "ci": (wilson(sum(v), len(v)) if key != "seat" else mean_ci(v)[1:]) if v else (None, None)}


def _diff(v: list[float]) -> dict:
    m, lo, hi = mean_ci(v)
    return {"mean": m, "lo": lo, "hi": hi, "n": len(v)}


def _scopes(seed, bg: str, k: int) -> list:
    """Pooled scopes a condition belongs to: everything, its seed, its background, its background at k <= LOW_K."""
    return ["all", seed, bg] + ([f"{bg}/k<={LOW_K}"] if k <= LOW_K else [])


def summarize(results: dict, specs: list[str], pairs: list[tuple[str, str]]) -> dict:
    """results[(spec, seed, bg, k)][g] = record -> per-condition and pooled tables."""
    conds = sorted({(s, b, k) for (_, s, b, k) in results})
    table = {}
    pooled = defaultdict(lambda: defaultdict(list))
    fails = defaultdict(lambda: defaultdict(float))
    for seed, bg, k in conds:
        row = {}
        for spec in specs:
            recs = results.get((spec, seed, bg, k), {})
            agg = defaultdict(list)
            for rec in recs.values():
                for key, x in role_outcomes(rec).items():
                    agg[key].append(x)
                for key in FAIL_KEYS:
                    fails[spec][key] += rec["fail"][key]
                    fails[spec]["bg_" + key] += rec["bg_fail"][key]
                fails[spec]["max_ms"] = max(fails[spec]["max_ms"], rec["max_ms"])
                fails[spec]["games"] += 1
                fails[spec]["chats"] += rec["chats"]
                fails[spec]["dms"] += rec["dms"]
            row[spec] = {key: _rate(v, key) for key, v in agg.items()}
            for key, v in agg.items():
                for scope in _scopes(seed, bg, k):
                    pooled[(spec, scope)][key] += v
        for a_spec, b_spec in pairs:
            a, b = results.get((a_spec, seed, bg, k), {}), results.get((b_spec, seed, bg, k), {})
            diffs = defaultdict(list)
            for g in sorted(set(a) & set(b)):
                oa, ob = role_outcomes(a[g]), role_outcomes(b[g])
                for key in oa:
                    diffs[key].append(oa[key] - ob[key])
            name = f"{a_spec}-{b_spec}"
            row[name] = {key: _diff(v) for key, v in diffs.items()}
            for key, v in diffs.items():
                for scope in _scopes(seed, bg, k):
                    pooled[(name, scope)][key] += v
        table[f"{seed}/{bg}/k{k}"] = row
    pair_names = {f"{a}-{b}" for a, b in pairs}
    pool_out = {f"{who}|{scope}": {key: (_diff(v) if who in pair_names else _rate(v, key)) for key, v in d.items()}
                for (who, scope), d in pooled.items()}
    return {"conditions": table, "pooled": pool_out, "fails": {s: dict(f) for s, f in fails.items()}}


ROLE_PAIRS = (("wolf", "agent/champion-champion"), ("villager", "champion/agent-champion"),
              ("seer", "champion/agent-champion"), ("seat", "agent-champion"))
MIN_SLICE_N = 20


def slice_regressions(summary: dict) -> list[dict]:
    """Per-background slices (all k, and k <= LOW_K) where a role (side-isolated pair) or the whole-table per-seat
    win rate is significantly worse: paired-difference 95% CI entirely below 0."""
    out = []
    for role, pair in ROLE_PAIRS:
        for name, d in sorted(summary["pooled"].items()):
            who, _, scope = name.partition("|")
            if who != pair or str(scope).split("/")[0] not in BACKGROUNDS:
                continue
            x = d.get(role)
            if x and x["n"] >= MIN_SLICE_N and x["hi"] < 0:
                out.append({"scope": scope, "role": role, "pair": pair, **x})
    return out


def verdict(summary: dict, margin: float) -> dict:
    """Role verdicts from the side-isolated pairs, overall from the whole-table pair (pooled over everything), and
    the slice regressions pooling would hide. SHIP: every role non-inferior, overall better, no slice regression."""
    P = summary["pooled"]
    out = {}
    for role, pair in ROLE_PAIRS[:3]:
        d = P.get(f"{pair}|all", {}).get(role)
        if d:
            out[role] = {"pair": pair, **d, "non_inferior": d["lo"] > -margin}
    d = P.get("agent-champion|all", {}).get("seat")
    if d:
        out["overall"] = {"pair": "agent-champion", **d, "better": d["lo"] > 0}
    regs = slice_regressions(summary)
    out["ship"] = bool(out) and all(v.get("non_inferior", True) for k, v in out.items() if k in ROLES) \
        and out.get("overall", {}).get("better", False) and not regs
    out["regressions"] = regs
    return out


def _pct(x):
    return "  -  " if x is None else f"{100 * x:5.1f}"


def report(summary: dict, specs: list[str], pairs: list[tuple[str, str]], margin: float, show_conditions=True) -> None:
    keys = ROLES + ("seat",)
    main = specs[0]
    wp = f"{pairs[0][0]}-{pairs[0][1]}" if pairs else None
    if show_conditions:
        print(f"{main} per condition: win rate by role, then {wp} paired diff [95% CI] (percentage points)")
        print(f"{'condition':17s} " + " ".join(f"{k:>24s}" for k in keys))
        for name, row in summary["conditions"].items():
            cells = []
            for key in keys:
                c = row[main].get(key)
                if c is None or c["rate"] is None:
                    cells.append(f"{'':>24s}")
                    continue
                txt = _pct(c["rate"])
                if wp and key in row.get(wp, {}):
                    d = row[wp][key]
                    txt += f" {100 * d['mean']:+5.1f}[{100 * d['lo']:+5.1f},{100 * d['hi']:+5.1f}]"
                cells.append(f"{txt:>24s}")
            print(f"{name:17s} " + " ".join(cells))
    print("\npooled: rate [Wilson 95% CI] (n); pairs: paired diff mean [95% CI] (n), percentage points")
    scopes = sorted({k.split("|", 1)[1] for k in summary["pooled"]}, key=lambda s: (s != "all", str(s)))
    names = list(specs) + [f"{a}-{b}" for a, b in pairs]
    for scope in scopes:
        for who in names:
            d = summary["pooled"].get(f"{who}|{scope}")
            if not d:
                continue
            parts = []
            for key in keys:
                if key not in d:
                    continue
                x = d[key]
                if "mean" in x:
                    parts.append(f"{key} {100 * x['mean']:+.2f} [{100 * x['lo']:+.2f},{100 * x['hi']:+.2f}] ({x['n']})")
                else:
                    parts.append(f"{key} {_pct(x['rate'])} [{_pct(x['ci'][0])},{_pct(x['ci'][1])}] ({x['n']})")
            print(f"  {str(scope):9s} {who:26s} " + " | ".join(parts))
    print("\nrobustness (summed over all games of each agent spec):")
    for spec, f in summary["fails"].items():
        ours = ", ".join(f"{k} {int(f.get(k, 0))}" for k in FAIL_KEYS)
        bg = sum(int(f.get("bg_" + k, 0)) for k in FAIL_KEYS)
        print(f"  {spec:18s} games {int(f['games'])}: {ours}; slowest decision {f['max_ms']:.1f} ms; "
              f"chats {int(f['chats'])}, DMs {int(f['dms'])}; background seat failures {bg}")
    v = verdict(summary, margin)
    if v:
        print(f"\nverdict (non-inferiority margin {100 * margin:.1f} pp on the lower 95% bound):")
        for k, x in v.items():
            if k in ("ship", "regressions"):
                continue
            flag = ("non-inferior" if x["non_inferior"] else "INFERIOR") if "non_inferior" in x else \
                ("better" if x["better"] else "not better")
            print(f"  {k:8s} {x['pair']:26s} {100 * x['mean']:+.2f} [{100 * x['lo']:+.2f},{100 * x['hi']:+.2f}] "
                  f"n={x['n']}: {flag}")
        print(f"  slice regressions (by background, all k and k<={LOW_K}; 95% CI entirely below 0): "
              f"{len(v['regressions'])}")
        for x in v["regressions"]:
            print(f"    {str(x['scope']):15s} {x['role']:8s} {x['pair']:26s} {100 * x['mean']:+.2f} "
                  f"[{100 * x['lo']:+.2f},{100 * x['hi']:+.2f}] n={x['n']}")
        print(f"  SHIP: {v['ship']}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=1000, help="games per condition per seed")
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2])
    ap.add_argument("--bg", default="first,heuristic,random,llm,llmx,mixed")
    ap.add_argument("--ks", default="1,2,3,4,5,6,7")
    ap.add_argument("--pairs", default=DEFAULT_PAIRS, help="cand:ref,... ('' for no comparison)")
    ap.add_argument("--agents", default="", help="extra specs to run without a pair")
    ap.add_argument("--alias", action="append", default=[], help="name=module:attr, e.g. old=work.old.agent.agent:create_agent")
    ap.add_argument("--margin", type=float, default=0.01, help="non-inferiority margin (fraction)")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--chunk", type=int, default=100)
    ap.add_argument("--quiet", action="store_true", help="pooled tables only")
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    for al in a.alias:
        name, _, target = al.partition("=")
        AGENTS[name] = target
        os.environ["WW_EVAL_ALIASES"] = ";".join(a.alias)          # worker processes re-read them
    bgs = [b for b in a.bg.split(",") if b]
    ks = [int(k) for k in a.ks.split(",") if k]
    pairs = [tuple(p.split(":", 1)) for p in a.pairs.split(",") if p] if a.pairs else []
    specs = []
    for s in [x for p in pairs for x in p] + [x for x in a.agents.split(",") if x]:
        if s not in specs:
            specs.append(s)
    tasks = []
    for spec in specs:
        for seed in a.seeds:
            for bg in bgs:
                for k in ks:
                    if k == 7 and bg != bgs[0]:
                        continue                         # all seats ours: the background does not matter
                    for lo in range(0, a.games, a.chunk):
                        tasks.append((spec, seed, bg, k, list(range(lo, min(a.games, lo + a.chunk)))))
    t0 = time.time()
    results: dict = defaultdict(dict)
    if a.jobs > 1:
        with ProcessPoolExecutor(a.jobs) as ex:
            for out in ex.map(_chunk, tasks):
                for spec, seed, bg, k, rec in out:
                    results[(spec, seed, bg, k)][rec["g"]] = rec
    else:
        for t in tasks:
            for spec, seed, bg, k, rec in _chunk(t):
                results[(spec, seed, bg, k)][rec["g"]] = rec
    summary = summarize(results, specs, pairs)
    report(summary, specs, pairs, a.margin, show_conditions=not a.quiet)
    print(f"\n{sum(len(v) for v in results.values())} games in {time.time() - t0:.0f}s", file=sys.stderr)
    if a.out:
        with open(a.out, "w") as f:
            json.dump({"args": vars(a), "verdict": verdict(summary, a.margin), **summary}, f, indent=1, default=list)


if __name__ == "__main__":
    main()
