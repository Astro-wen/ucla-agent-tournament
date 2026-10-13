"""Mixed-table Werewolf evaluation: candidate vs reference agents in the same seats of the same games.

    python -m local.eval_werewolf --games 1000 --seeds 7 --jobs 3            # agent vs champion, all conditions
    python -m local.eval_werewolf --games 200 --bg llm,mixed --ks 1,3,7 --pairs agent:champion

A condition is (background, k): k seats (1..7) are ours, the other 7-k are one background type:
  first      legal_actions[0] (examples/basic_agent.py, smoke_agent.py; the old "official_baseline")
  heuristic  public-record suspicion bot modelled on the platform's demo werewolf_agent.py (local/werewolf_bots.py)
  random     a uniformly random legal action
  llm        a chat-reading, persuadable, noisy stand-in for examples/llm_agent.py (local/werewolf_bots.py)
  llmx       the llm stand-in phrasing freely (casual text, claim-looking table talk) and less persuadable
  llmq       llmx phrased like the real Qwen3 seats: their seer-result wording, plus skeptical table talk that is not
             a verdict ("that doesn't mean they're a wolf", "Seer claim: none yet", "It was the wolves"); it prices
             both reading real LLM chat and misreading it
  rt_*       the red-team challengers (10-08) as permanent opponents: rt_wolfpack (a fake-claiming, counter-claiming
             wolf pair with llmq villagers, local/redteam_wolfpack.py), rt_talker (persuasive LLM-like seats in every
             role, local/redteam_talker.py), rt_detective (a Bayesian village over all 105 deals that also reads our
             fixed phrasing, local/redteam_detective.py), rt_chaos (claim fights, day-1 call tells and noisy wording,
             = chaos4 of local/redteam_chaos.py); each module's variants are registered under their own names too
  mixed      each background seat drawn from first / heuristic / random / llm (llmx / llmq are not drawn, so the deals
             of earlier runs are unchanged); `mixed:K1+K2...` draws each background seat from the listed kinds
  llm_real   the REAL examples/llm_agent.py on Nebius Token Factory (local/llm_seats.py; opt-in, costs API calls, see
             below); deals exactly like `llm`, so a `--bg llm_real` run pairs game by game with a `--bg llm` run
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

Real LLM opponents (NEBIUS_API_KEY from the environment or the checkout's .env; local/llm_client.py caps and ledgers
the spend, all worker processes share one NEBIUS_RUN_ID):

    python -m local.eval_werewolf --llm-real 2 --bg mixed:first+heuristic --ks 1,5 --games 40 --seeds 43

  --llm-real N  in every condition N of the background seats (drawn per game from a stream of their own, identical
                for every agent under test) are the real LLM agent and the rest follow the background; each game is
                also played with those N seats as --llm-control (default `llm`, the deterministic stand-in) on the
                same deal. Condition labels: `<bg>+<N>llm_real` and `<bg>+<N><control>`. The report adds, per k, our
                win rate by role with Wilson CIs against the real seats and against the control, the paired
                real - control difference, the LLM seats' own win rate by role, their rule fallbacks / invalid answers
                / platform failures (the engine's failure model applies to them like to every seat) / chat, and the
                requests, cache hits, timeouts, cost and latency of this run (plus the ledger's spend for the run id).
  With real LLM seats the defaults are 20 games and one agent spec (`agent`, no pairs); a run that would play more
  than --llm-max-games real-LLM games refuses to start; a run a spend cap refuses mid-way stops there and says so
  (the game in progress is dropped, and the controls are cut to the games the real seats finished), so the numbers
  are never a silent mix of model and fallback games. Without a key the LLM seats play the example agent's rule
  fallback (first legal action, no chat) and the run says so before the first game.
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import random
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

from .redteam_chaos import CHAOS_BACKGROUNDS
from .redteam_detective import VARIANTS as DETECTIVE_VARIANTS
from .redteam_talker import SEATS as TALKER_SEATS
from .redteam_wolfpack import KINDS as WOLFPACK_KINDS
from .werewolf_bots import HeuristicSeat, LLMQwenSeat, LLMSeat, LLMVariedSeat
from .werewolf_guard_bots import GUARD_BACKGROUNDS
from .werewolf_env import ContestantSeat, Game, OfficialBaseline, RandomSeat, deal_roles

BACKGROUNDS = {"first": OfficialBaseline, "heuristic": HeuristicSeat, "random": RandomSeat, "llm": LLMSeat,
               "llmx": LLMVariedSeat, "llmq": LLMQwenSeat}
# Red-team backgrounds (10-08, four context-free challengers; see docs/progress.md "狼人杀红队"): every variant under
# its own name, plus the four permanent evaluation opponents rt_* (each challenger's final version; rt_chaos is the
# chaos3 strategy with the chaosx noise on top).
BACKGROUNDS.update(WOLFPACK_KINDS)                      # wolfpack, wolfpack_h (local/redteam_wolfpack.py)
BACKGROUNDS.update(TALKER_SEATS)                        # talker, talker_* (local/redteam_talker.py)
BACKGROUNDS.update(DETECTIVE_VARIANTS)                  # detective, det_v1, det_noskip (local/redteam_detective.py)
BACKGROUNDS.update(CHAOS_BACKGROUNDS)                   # chaos, chaosx, chaos1..chaos5 (local/redteam_chaos.py)
RT_BACKGROUNDS = {"rt_wolfpack": WOLFPACK_KINDS["wolfpack"], "rt_talker": TALKER_SEATS["talker"],
                  "rt_detective": DETECTIVE_VARIANTS["detective"], "rt_chaos": CHAOS_BACKGROUNDS["chaos4"]}
BACKGROUNDS.update(RT_BACKGROUNDS)
BACKGROUNDS.update(GUARD_BACKGROUNDS)                  # cheap legal probes; use <ordinary>+1guard_* for one attacker
MIXED = ("first", "heuristic", "random", "llm")       # what a `mixed` seat is drawn from (fixed: keeps old deals)
LLM_REAL = "llm_real"                                   # the real examples/llm_agent.py (local/llm_seats.py), opt-in
LOW_K = 3                                               # slice "k<=3": few of our seats at a table
AGENTS = {"agent": "agent.agent:create_agent", "champion": "champion.agent.agent:create_agent"}
ROLES = ("villager", "seer", "wolf")
SIDE = {"villager": "villager", "seer": "villager", "wolf": "wolf"}
FAIL_KEYS = ("crash", "illegal", "bad_chat", "timeout", "chat_after_terminate", "window_close")
DEFAULT_PAIRS = "agent:champion,agent/champion:champion,champion/agent:champion"
LLM_SEAT_KEYS = ("decisions", "model_calls", "invalid_answers", "fallbacks", "message_requests", "message_fallbacks")
CLIENT_KEYS = ("calls", "cache_hits", "errors", "timeouts", "refused", "json_errors", "prompt_tokens",
               "completion_tokens", "cost_usd")
_SUB = re.compile(r"(?P<base>.+?)\+(?P<n>[1-6])(?P<kind>[a-z_]+)")
_FACTORIES: dict = {}


def factory(name: str):
    for al in filter(None, os.environ.get("WW_EVAL_ALIASES", "").split(";")):
        alias, _, target = al.partition("=")
        AGENTS.setdefault(alias, target)
    if name not in _FACTORIES:
        mod, _, attr = AGENTS.get(name, name).partition(":")
        _FACTORIES[name] = getattr(importlib.import_module(mod), attr or "create_agent")
    return _FACTORIES[name]


def parse_bg(bg: str) -> tuple[str, list[str], int, str | None]:
    """A background label -> (base, the kinds a base seat is drawn from, n swapped seats, their kind).

    Base: a background name, `mixed` or `mixed:K1+K2...`; an optional suffix `+<n><kind>` swaps n background seats
    per game to <kind> (what --llm-real builds). ValueError for anything else."""
    m = _SUB.fullmatch(bg)
    base, n, kind = (m["base"], int(m["n"]), m["kind"]) if m else (bg, 0, None)
    known = set(BACKGROUNDS) | {LLM_REAL}
    kinds = list(MIXED) if base == "mixed" else base[6:].split("+") if base.startswith("mixed:") else [base]
    if not kinds or any(x not in known for x in kinds) or (kind is not None and kind not in known):
        raise ValueError(f"unknown background {bg!r} (known: {', '.join(sorted(known))}, mixed, mixed:K1+K2, "
                         f"and <bg>+<n><kind>)")
    return base, kinds, n, kind


def uses_llm_real(bg: str) -> bool:
    _, kinds, _, kind = parse_bg(bg)
    return LLM_REAL in kinds or kind == LLM_REAL


def deal(seed: int, g: int, bg: str, k: int):
    """(game rng seed, our seats, background type per seat) -- identical for every agent under test. `llm_real` deals
    like `llm`, and `<bg>+<n><kind>` like <bg> with the same n seats swapped whatever <kind> is: a real-LLM condition
    and its control share every deal."""
    base, kinds, n, kind = parse_bg(bg)
    key = "llm" if base == LLM_REAL else base
    r = random.Random(f"deal:{seed}:{g}:{key}:{k}")
    ours = sorted(r.sample(range(7), k))
    drawn = base == "mixed" or base.startswith("mixed:")
    types = {i: (r.choice(kinds) if drawn else kinds[0]) for i in range(7) if i not in ours}
    gseed = r.getrandbits(62)
    if n:
        if n > len(types):
            raise ValueError(f"{bg}: {n} swapped seats but only {len(types)} background seats at k={k}")
        for i in random.Random(f"swap:{seed}:{g}:{key}:{k}:{n}").sample(sorted(types), n):
            types[i] = kind
    return gseed, ours, types


def make_seat(kind: str):
    if kind == LLM_REAL:
        from .llm_seats import LLMWerewolfSeat                # lazy: runs without real seats never load the client
        return LLMWerewolfSeat()
    return BACKGROUNDS[kind]()


def _client():
    from .llm_client import shared_client
    return shared_client()


def play_one(spec: str, seed: int, g: int, bg: str, k: int) -> dict:
    gseed, ours, types = deal(seed, g, bg, k)
    wolf_spec, vill_spec = spec.split("/", 1) if "/" in spec else (spec, spec)
    roles = deal_roles(random.Random(gseed))
    seats = [ContestantSeat(factory(wolf_spec if roles[i] == "wolf" else vill_spec)) if i in ours
             else make_seat(types[i]) for i in range(7)]
    real = [i for i in sorted(types) if types[i] == LLM_REAL]
    before = None
    if real:
        cs = _client().stats
        before = {key: getattr(cs, key) for key in CLIENT_KEYS} | {"lat": len(cs.latencies)}
    game = Game(seats, random.Random(gseed))
    ret = game.play()
    st = game.stats
    rec = {"g": g, "roles": [game.roles[i] for i in ours], "winner": game.winner,
           "wins": [ret[i] > 0 for i in ours], "days": game.day,
           "fail": {key: sum(st[key][i] for i in ours) for key in FAIL_KEYS},
           "bg_fail": {key: sum(st[key][i] for i in range(7) if i not in ours) for key in FAIL_KEYS},
           "max_ms": max(st["max_ms"][i] for i in ours), "chats": sum(st["chats"][i] for i in ours),
           "dms": sum(st["dms"][i] for i in ours)}
    if real or parse_bg(bg)[2]:
        rec["bg_seats"] = [[types[i], game.roles[i], ret[i] > 0] for i in sorted(types)]   # kind, role, won
    if real:
        cs = _client().stats
        llm = {key: sum(seats[i].stats[key] for i in real) for key in LLM_SEAT_KEYS}
        llm.update({"fail_" + key: sum(st[key][i] for i in real) for key in FAIL_KEYS})
        llm.update({key: sum(st[key][i] for i in real) for key in ("chats", "dms")})
        llm["max_ms"] = max(st["max_ms"][i] for i in real)
        llm.update({key: getattr(cs, key) - before[key] for key in CLIENT_KEYS})
        llm["latencies"] = [round(x, 3) for x in cs.latencies[before["lat"]:]]
        rec["llm"] = llm
    return rec


def _chunk(args):
    spec, seed, bg, k, games = args[:5]
    live = len(args) > 5 and args[5]                     # Nebius was available when the run started
    os.environ.setdefault("ARENA_RECORD", "0")
    real = live and uses_llm_real(bg)
    out = []
    for g in games:
        if real and not _client().available:           # a spend cap (maybe hit by another worker): stop here
            break
        rec = play_one(spec, seed, g, bg, k)
        if real and rec["llm"]["refused"]:
            rec["llm_refused"] = True                  # partly played by the rule fallback: dropped by main()
        out.append((spec, seed, bg, k, rec))
        if real and rec.get("llm_refused"):
            break
        if real:
            print(f"  [llm_real] {spec} seed {seed} {bg} k{k} game {g}: {rec['winner']} wins | ours {rec['roles']} "
                  f"| {rec['llm']['calls']} requests ${rec['llm']['cost_usd']:.4f}", file=sys.stderr, flush=True)
    return out


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
    cw = max([17] + [len(n) for n in summary["conditions"]])
    if show_conditions:
        print(f"{main} per condition: win rate by role"
              + (f", then {wp} paired diff [95% CI] (percentage points)" if wp else ""))
        print(f"{'condition':{cw}s} " + " ".join(f"{k:>24s}" for k in keys))
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
            print(f"{name:{cw}s} " + " ".join(cells))
    print("\npooled: rate [Wilson 95% CI] (n); pairs: paired diff mean [95% CI] (n), percentage points")
    scopes = sorted({k.split("|", 1)[1] for k in summary["pooled"]}, key=lambda s: (s != "all", str(s)))
    sw = max([9] + [len(str(s)) for s in scopes])
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
            print(f"  {str(scope):{sw}s} {who:26s} " + " | ".join(parts))
    print("\nrobustness (summed over all games of each agent spec):")
    for spec, f in summary["fails"].items():
        ours = ", ".join(f"{k} {int(f.get(k, 0))}" for k in FAIL_KEYS)
        bg = sum(int(f.get("bg_" + k, 0)) for k in FAIL_KEYS)
        print(f"  {spec:18s} games {int(f['games'])}: {ours}; slowest decision {f['max_ms']:.1f} ms; "
              f"chats {int(f['chats'])}, DMs {int(f['dms'])}; background seat failures {bg}")
    v = verdict(summary, margin)
    if v and pairs:
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


# ---------------------------------------------------------------- real LLM seats
def _cell(v: list[float], key: str = "role") -> str:
    if not v:
        return "   -"
    r = _rate(v, key)
    return f"{100 * r['rate']:5.1f} [{100 * r['ci'][0]:5.1f},{100 * r['ci'][1]:5.1f}] ({len(v)})"


def _seat_roles(recs, kind: str) -> dict[str, list[float]]:
    """role -> 1/0 per seat-game of the background seats of `kind` (e.g. the real LLM seats, or the control's)."""
    out = defaultdict(list)
    for rec in recs:
        for t, role, won in rec.get("bg_seats", []):
            if t == kind:
                out[role].append(1.0 if won else 0.0)
    out["seat"] = [x for role in ROLES for x in out.get(role, [])]
    return out


def llm_summary(results: dict, specs: list[str], real_labels: dict[str, str | None]) -> dict:
    """Real-LLM conditions vs their controls, pooled over seeds, per (spec, base background, k)."""
    out = {}
    for spec in specs:
        for real_bg, ctrl_bg in real_labels.items():
            ks = sorted({k for (s, _, b, k) in results if s == spec and b == real_bg})
            for k in ks:
                real, ctrl, diffs = [], [], defaultdict(list)
                for (s, seed, b, kk), recs in results.items():
                    if s != spec or b != real_bg or kk != k:
                        continue
                    real += list(recs.values())
                    c = results.get((spec, seed, ctrl_bg, k), {}) if ctrl_bg else {}
                    ctrl += [c[g] for g in sorted(c) if g in recs]
                    for g in sorted(set(recs) & set(c)):
                        oa, ob = role_outcomes(recs[g]), role_outcomes(c[g])
                        for key in oa:
                            diffs[key].append(oa[key] - ob[key])
                ours = {"real": defaultdict(list), "ctrl": defaultdict(list)}
                for which, recs in (("real", real), ("ctrl", ctrl)):
                    for rec in recs:
                        for key, x in role_outcomes(rec).items():
                            ours[which][key].append(x)
                llm = defaultdict(float)
                lat = []
                for rec in real:
                    for key, x in rec.get("llm", {}).items():
                        if key == "latencies":
                            lat += x
                        elif key == "max_ms":
                            llm[key] = max(llm[key], x)
                        else:
                            llm[key] += x
                ctrl_kind = parse_bg(ctrl_bg)[3] if ctrl_bg else None
                out[(spec, real_bg, k)] = {
                    "games": len(real), "ours_real": dict(ours["real"]), "ours_ctrl": dict(ours["ctrl"]),
                    "diff": {key: _diff(v) for key, v in diffs.items()},
                    "llm_seats": dict(_seat_roles(real, LLM_REAL)),
                    "ctrl_seats": dict(_seat_roles(ctrl, ctrl_kind)) if ctrl_kind else {},
                    "ctrl_kind": ctrl_kind, "llm": dict(llm), "latencies": sorted(lat)}
    return out


def llm_report(ls: dict, model: str) -> None:
    keys = ROLES + ("seat",)
    print(f"\nreal-LLM seats: examples/llm_agent.py on {model} (Nebius); control = the same seats played by the "
          f"stand-in, same deals. Win rates % [Wilson 95% CI] (n); seat = per-seat mean [normal CI]")
    tot = defaultdict(float)
    lat_all = []
    for (spec, bg, k), x in ls.items():
        print(f"\n  {spec} in {k} seat(s), background {bg}: {x['games']} games")
        print(f"    {'':24s} " + " ".join(f"{key:>27s}" for key in keys))
        print(f"    {'ours vs real LLM':24s} " + " ".join(f"{_cell(x['ours_real'].get(key, []), key):>27s}" for key in keys))
        if x["ctrl_kind"]:
            print(f"    {'ours vs ' + x['ctrl_kind'] + ' (control)':24s} "
                  + " ".join(f"{_cell(x['ours_ctrl'].get(key, []), key):>27s}" for key in keys))
            print(f"    {'paired real - control':24s} " + " ".join(
                (f"{100 * x['diff'][key]['mean']:+6.1f} [{100 * x['diff'][key]['lo']:+6.1f},"
                 f"{100 * x['diff'][key]['hi']:+6.1f}] ({x['diff'][key]['n']})" if key in x["diff"] else "   -").rjust(27)
                for key in keys))
        print(f"    {'LLM seats themselves':24s} " + " ".join(f"{_cell(x['llm_seats'].get(key, [])):>27s}" for key in keys))
        if x["ctrl_kind"]:
            print(f"    {x['ctrl_kind'] + ' seats (control)':24s} "
                  + " ".join(f"{_cell(x['ctrl_seats'].get(key, [])):>27s}" for key in keys))
        m = x["llm"]
        for key, v in m.items():
            tot[key] = max(tot[key], v) if key == "max_ms" else tot[key] + v
        lat_all += x["latencies"]
        _llm_line(m, x["latencies"], "    ")
    if len(ls) > 1:
        print("\n  all real-LLM games:")
        _llm_line(tot, sorted(lat_all), "    ")


def _llm_line(m: dict, lat: list[float], ind: str) -> None:
    d, calls = m.get("decisions", 0), m.get("model_calls", 0)
    pct = lambda a, b: f"{100 * a / b:.1f}%" if b else "-"                          # noqa: E731
    q = lambda p: f"{lat[min(len(lat) - 1, int(p * len(lat)))]:.1f}" if lat else "-"  # noqa: E731
    fails = ", ".join(f"{key} {int(m.get('fail_' + key, 0))}" for key in FAIL_KEYS)
    print(f"{ind}LLM seats: {int(d)} decisions, rule fallback {int(m.get('fallbacks', 0))} ({pct(m.get('fallbacks', 0), d)}); "
          f"invalid answers {int(m.get('invalid_answers', 0))}/{int(calls)} model calls "
          f"({pct(m.get('invalid_answers', 0), calls)}); chat {int(m.get('chats', 0))} + {int(m.get('dms', 0))} DMs in "
          f"{int(m.get('message_requests', 0))} requests, {int(m.get('message_fallbacks', 0))} chat fallbacks")
    print(f"{ind}platform failure model on the LLM seats: {fails}; slowest decision {m.get('max_ms', 0) / 1000:.1f} s")
    print(f"{ind}client: {int(m.get('calls', 0))} requests, {int(m.get('cache_hits', 0))} cache hits, "
          f"{int(m.get('errors', 0))} failed attempts ({int(m.get('timeouts', 0))} timeouts), {int(m.get('refused', 0))} "
          f"refused, {int(m.get('json_errors', 0))} off-schema; {int(m.get('prompt_tokens', 0))}+"
          f"{int(m.get('completion_tokens', 0))} tokens, ${m.get('cost_usd', 0):.4f}; latency p50 {q(0.5)} s, "
          f"p95 {q(0.95)} s, max {q(1.0)} s")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=None, help="games per condition per seed (default 1000; 20 with "
                                                            "real LLM seats)")
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2])
    ap.add_argument("--bg", default="first,heuristic,random,llm,llmx,mixed,llmq")
    ap.add_argument("--ks", default="1,2,3,4,5,6,7")
    ap.add_argument("--pairs", default=None, help="cand:ref,... ('' for no comparison; default "
                                                  f"{DEFAULT_PAIRS}, or none with real LLM seats)")
    ap.add_argument("--agents", default="", help="extra specs to run without a pair (default `agent` with real LLM "
                                                 "seats and no pairs)")
    ap.add_argument("--alias", action="append", default=[], help="name=module:attr, e.g. old=work.old.agent.agent:create_agent")
    ap.add_argument("--margin", type=float, default=0.01, help="non-inferiority margin (fraction)")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--chunk", type=int, default=100)
    ap.add_argument("--quiet", action="store_true", help="pooled tables only")
    ap.add_argument("--out", default="")
    ap.add_argument("--llm-real", type=int, default=0, choices=range(0, 7), metavar="N",
                    help="N background seats per game are the real examples/llm_agent.py on Nebius (costs API calls)")
    ap.add_argument("--llm-control", default="llm", help="what plays those N seats in the paired control ('' = none)")
    ap.add_argument("--llm-max-games", type=int, default=400, help="refuse to start a run with more real-LLM games")
    a = ap.parse_args(argv)
    for al in a.alias:
        name, _, target = al.partition("=")
        AGENTS[name] = target
        os.environ["WW_EVAL_ALIASES"] = ";".join(a.alias)          # worker processes re-read them
    bgs = [b for b in a.bg.split(",") if b]
    real_labels: dict[str, str | None] = {}
    if a.llm_real:
        if a.llm_control and a.llm_control not in BACKGROUNDS:
            raise SystemExit(f"--llm-control must be one of {', '.join(BACKGROUNDS)} or ''")
        expanded = []
        for b in bgs:
            r = f"{b}+{a.llm_real}{LLM_REAL}"
            c = f"{b}+{a.llm_real}{a.llm_control}" if a.llm_control else None
            real_labels[r] = c
            expanded += [r] + ([c] if c else [])
        bgs = expanded
    for b in bgs:
        try:
            parse_bg(b)
        except ValueError as e:
            raise SystemExit(str(e)) from None
        if b not in real_labels and uses_llm_real(b):
            real_labels[b] = None                     # --bg llm_real: pair it with a --bg llm run by hand
    llm_mode = bool(real_labels)
    if a.games is None:
        a.games = 20 if llm_mode else 1000
    if a.pairs is None:
        a.pairs = "" if llm_mode else DEFAULT_PAIRS
    ks = [int(k) for k in a.ks.split(",") if k]
    pairs = [tuple(p.split(":", 1)) for p in a.pairs.split(",") if p] if a.pairs else []
    specs = []
    for s in [x for p in pairs for x in p] + [x for x in a.agents.split(",") if x]:
        if s not in specs:
            specs.append(s)
    if not specs and llm_mode:
        specs = ["agent"]
    if not specs:
        raise SystemExit("nothing to evaluate: give --pairs or --agents")
    tasks = []
    skipped = set()
    for spec in specs:
        for seed in a.seeds:
            for bg in bgs:
                for k in ks:
                    if k == 7 and bg != bgs[0]:
                        continue                         # all seats ours: the background does not matter
                    if parse_bg(bg)[2] > 7 - k:
                        skipped.add((bg, k))             # fewer background seats than seats to swap
                        continue
                    for lo in range(0, a.games, a.chunk):
                        tasks.append((spec, seed, bg, k, list(range(lo, min(a.games, lo + a.chunk)))))
    for bg, k in sorted(skipped):
        print(f"note: {bg} needs {parse_bg(bg)[2]} background seats, k={k} leaves {7 - k}: skipped", file=sys.stderr)
    live, model = False, None
    if llm_mode:
        planned = sum(len(t[4]) for t in tasks if uses_llm_real(t[2]))
        if planned > a.llm_max_games:
            raise SystemExit(f"{planned} real-LLM games planned (> --llm-max-games {a.llm_max_games}): narrow --bg / "
                             f"--ks / --seeds / --games / the agent specs, or raise --llm-max-games")
        os.environ.setdefault("NEBIUS_RUN_ID", f"ww-eval-{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}")
        from .llm_client import LLMClient
        probe = LLMClient.from_env(cache_path=None)            # not the shared client: worker processes make their own
        why, model = probe.unavailable_reason, probe.model
        probe.close()
        live = why is None
        print(f"real-LLM seats: {planned} games planned, model {model}, run {os.environ['NEBIUS_RUN_ID']}"
              + ("" if live else f"\nWARNING: Nebius unavailable ({why}): the llm_real seats play examples/"
                                 f"llm_agent.py's rule fallback (first legal action, no chat), not a model"),
              flush=True)
        tasks = [t + (live,) for t in tasks]
        order = {b: i for i, b in enumerate(real_labels)}   # real-LLM chunks first: a cap stops them early
        tasks.sort(key=lambda t: (not uses_llm_real(t[2]), t[1], order.get(t[2], 0), t[3], t[4][0]))
    t0 = time.time()
    results: dict = defaultdict(dict)
    if a.jobs > 1:
        with ProcessPoolExecutor(a.jobs) as ex:
            for out in ex.map(_chunk, tasks):
                for spec, seed, bg, k, rec in out:
                    results[(spec, seed, bg, k)][rec["g"]] = rec
    else:
        for t in tasks:
            if live and uses_llm_real(t[2]) and any(r.get("llm_refused") for v in results.values() for r in v.values()):
                continue                                  # a spend cap refused a request: no more real-LLM games
            for spec, seed, bg, k, rec in _chunk(t):
                results[(spec, seed, bg, k)][rec["g"]] = rec
    if llm_mode:
        _cut_to_finished(results, {t[:4] for t in tasks if t[2] in real_labels}, real_labels, a.games, live)
    summary = summarize(results, specs, pairs)
    report(summary, specs, pairs, a.margin, show_conditions=not a.quiet)
    ls = llm_summary(results, specs, real_labels) if llm_mode else {}
    if ls:
        llm_report(ls, model)
        from .llm_client import LLMClient
        c = LLMClient.from_env(cache_path=None, run_id=os.environ["NEBIUS_RUN_ID"])
        sp = c.spent()
        c.close()
        print(f"\nledger, run {os.environ['NEBIUS_RUN_ID']}: {sp['run_calls']} requests, {sp['run_tokens']} tokens, "
              f"${sp['run_usd']:.4f}; ledger total ${sp['total_usd']:.4f} of ${c.budget_usd:.2f}")
    print(f"\n{sum(len(v) for v in results.values())} games in {time.time() - t0:.0f}s", file=sys.stderr)
    if a.out:
        with open(a.out, "w") as f:
            extra = {"llm": {f"{s}|{b}|k{k}": v for (s, b, k), v in ls.items()}} if ls else {}
            json.dump({"args": vars(a), "verdict": verdict(summary, a.margin), **summary, **extra}, f, indent=1,
                      default=list)


def _cut_to_finished(results: dict, expected: set, real_labels: dict, games: int, live: bool) -> None:
    """Drop real-LLM games a spend cap interrupted, and cut every control to the games its real condition finished.
    `expected`: the (spec, seed, real label, k) conditions the run planned."""
    stopped = 0
    for key in sorted(expected, key=str):
        spec, seed, bg, k = key
        recs = results[key]                            # a defaultdict: a condition never started is empty
        for g in [g for g, r in recs.items() if r.get("llm_refused")]:
            del recs[g]
        stopped += games - len(recs)
        ctrl = real_labels[bg]
        if ctrl and (spec, seed, ctrl, k) in results:
            c = results[(spec, seed, ctrl, k)]
            for g in [g for g in c if g not in recs]:
                del c[g]
        if not recs:
            del results[key]
            if ctrl and not results.get((spec, seed, ctrl, k), True):
                del results[(spec, seed, ctrl, k)]
    if live and stopped:
        print(f"WARNING: Nebius refused requests (a spend cap): {stopped} real-LLM games were not played or were "
              f"dropped (partly rule fallback); every table below covers only the games the real seats finished, "
              f"and the controls are cut to the same games", flush=True)


if __name__ == "__main__":
    main()
