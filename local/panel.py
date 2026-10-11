"""Promotion panel: any candidate code directory vs a fixed panel of opponents, paired seeds, parallel workers.

    python -m local.panel --candidate-dir ../wt-foo --games 200                   # full panel, 6 niced workers
    python -m local.panel --candidate-dir ../wt-foo --env LOOKAHEAD_ALPHA=0.5 --name foo-a05
    python -m local.panel --candidate-dir versions/v2_joint_lookahead --name v2   # a frozen version as candidate
    python -m local.panel --opps field --games 500 --name v3-field                # only the example-agent field
    python -m local.panel --summary logs/panel/runs/foo-s43.jsonl --compare logs/panel/runs/v3-s43.jsonl

Candidate: the `agent/` package inside --candidate-dir (default: this checkout's working tree). Each worker imports
it as the top-level package `agent`, so it runs on its own data/ and agent/js; the panel stops if its damage
calculator cannot start (--allow-no-calc to run anyway). --env KEY=VALUE applies to the candidate only: the workers
set it around the candidate's imports and decisions and remove it again before an opponent moves. Tuning variables
exported in the shell (LOOKAHEAD_*, ARENA_DRAFT_*, PREVIEW_MATRIX_*) are dropped for everybody, with a warning, because
versions/v2 and v3 read the same names as the candidate.

Panel (--opps, default all 13; groups: all, strength, field; or a comma list):
  strength  official_baseline, random, v0 (versions/v0_heuristic), v1 (versions/v1_exact_damage), v2 (versions/
            v2_joint_lookahead), cautious (local/sparring/v2_cautious: the 09-27 la-fixed v2 with Nash argmax pinned),
            greedy and protect_spam (scripted, local/panel_bots.py), v3 (versions/v3_draft_preview)
  field     the organizers' example agents played through the official runner + the platform's VGC timer
            (local/field_opps.py): basic, smoke, llm_fallback, llm_timeout (the last two are deterministic stand-ins
            for examples/llm_agent.py without a model); `messaging` (= basic in Pokemon) by name or in the group
  llm_live  opt-in only, by name: examples/llm_agent.py with a real model from OPENAI_API_KEY / OPENAI_MODEL /
            OPENAI_BASE_URL (its real latency on the VGC timer; not reproducible, costs API calls)
  llm_real  opt-in only, by name: the same agent on Nebius Token Factory (NEBIUS_API_KEY from the environment or the
            .env; local/llm_client.py caps and ledgers the spend, all workers share one NEBIUS_RUN_ID). Without a key
            it plays the agent's rule fallback (= llm_fallback) and says so before the first game
  simbot    opt-in only, by name: the outside-style engine-search opponent (local/ext_opp.py, local/js/simbot.js):
            each battle turn a joint-action matrix game played out on clones of the real Showdown battle; drafts and
            previews with the champion's code (`simbot_v1`: v1's card_score draft + preview). ~1 s per decision, so
            about 15 s per game; knobs SIMBOT_* from the shell (recorded in the meta file).
  vgcbench_bc / vgcbench_sp   opt-in only, by name, and only where the VGC-Bench install exists (docs/local_sim.md;
            VGCBENCH_HOME, default /mnt/d/altruagent): the open-source learned VGC bots (behaviour cloning on ladder
            logs / + self-play), run in their own venv by local/vgcbench_worker.py (local/vgcbench_opp.py); draft
            with the champion's code, team preview and battle by the policy. ~0.1 s per decision after a ~1 min
            worker start per panel worker; knobs VGCBENCH_* from the shell (recorded in the meta file).
  meta / meta_soft / meta_v3   opt-in only, by name: drafts like the real contestants we met (local/meta_opp.py,
            fitted on our real platform drafts by scripts/fit_meta_drafter.py -> data/meta_drafter.json), previews and
            battles with the champion's code (meta, meta_soft: softmax draws) or versions/v3_draft_preview (meta_v3).
Any versions/<dir> name, or a dotted package that holds agent/agent.py, works as an extra opponent. Every opponent
package must run its own calc.js (versions/*/agent/js/node_modules and local/sparring/*/agent/js/node_modules linked
to agent/js/node_modules, see scripts/setup_machine.sh); the panel checks that before the first game.

Pairing: game g against every opponent uses match seed --seed * 100000 + g and seat g % 2, exactly like
local.benchmark / local.ladder, so two runs with the same --seed see identical pools, drafts and battle RNG. The
default seed 43 is deliberately neither local.benchmark's (--seed 31 replays the benchmark's official_baseline /
random / v0 / v1 / v2 / v3 games, which every candidate is tuned against) nor local.ladder's (7, 11; --opps v3
--seed 7 replays the ladder while agent/ == champion/ == v3). Games do not depend on which worker plays them or in what order:
every game starts with empty damage-calc caches (like a live match in a fresh process), the calculators are started
before a worker's first game (--no-warmup to skip) so start-up never lands inside a timed decision, and
PYTHONHASHSEED is fixed (--hashseed). Re-running a candidate gives the same games; only decisions that hit a
time budget can differ, and those are counted per game: lookahead cap hits and Nash-mix fallbacks (from the
lookahead's STATS), team-preview searches cut at their wall-clock cap (v3's preview `info["wall_hit"]`, still an
answer) and team-preview fallbacks (v3's preview game logging "preview matrix failed", mostly the same cap).
--same-team replaces the snake draft by Match.draft_same: both seats get the same six cards (random from the match's
18-card pool; the draft protocol still shows every set), so only team preview and battle skill differ. Its games are
different games from a normal run with the same --seed: give it its own --name and never --compare the two.
--save-logs writes every game's Showdown protocol log (plus rosters, leads, the candidate's team-preview and battle
answers and simbot's per-decision trace) to <out>.logs/<opp>-g<g>.json for the loss analysis
(scripts/extopp_analyze.py); --only 12,18,30-33 replays just those game indices (same seeds and seats).

Output (default logs/panel/runs/<name>-s<seed>.jsonl, never directly in logs/<dir>/ where scripts/replay_real.py looks
for real recordings): one JSON line per game (cand, opp, seed, g, match_seed, seat, winner, win, tie, turns,
inv_cand, inv_opp, secs, timer_cand / timer_opp = decisions Showdown's timer made, forfeit = "cand" / "opp", pf_* = a
field opponent's platform counters, cap_* / mixfb_* / pvwall_* / pvfb_* = the time-budget events above, warn_* =
WARNING log records per side (with the first one's text; workers count warnings instead of printing them),
tmax_cand / tdraft_cand / bank_cand = the candidate's slowest preview-or-battle and draft decision in real seconds and
its VGC bank left, plus bot counters when available), <out>.meta.json (config, candidate files and code hash, timing)
and <out>.summary.json.
The candidate plays on the platform's VGC timer in real wall time (local.field_opps.WallClockSeat: 90 s team preview,
55 s per request, 420 s bank + 90 s grace): a decision slower than that is played by Showdown's timer instead and an
empty bank forfeits, as on the platform. Opponents play untimed (the frozen versions answer in seconds; the field
opponents have their own simulated clock).
The summary gives each opponent's win rate with a 95% Wilson CI, the unweighted panel mean and the minimum over
opponents. --compare BASE.jsonl pairs games by (opponent, seed, g) and adds per-opponent and panel-mean
differences with 95% CIs, and a verdict:
    PROMOTE  panel-mean difference CI lower bound > 0, no opponent significantly worse, candidate clean
    HOLD     panel-mean difference >= 0, no opponent significantly worse, candidate clean
    REJECT   anything else          (exit code 1; PROMOTE / HOLD exit 0; a run with crashed games exits 2)
where clean = 0 invalid actions, 0 timer-played decisions, 0 forfeits by the candidate, 0 crashed games and every
game's candidate decision times measured (rows from a panel older than the candidate's clock cannot certify it).
Panel-mean CIs are clustered by game: game g is the same seed and seat against every opponent, so its outcomes are
correlated across opponents and only the games are independent (clustered_se; treating the 13 opponents as
independent made the v3 - v2 CI 1.6x too narrow).
The panel mean weighs every opponent equally, so it is a robustness score across styles, not a win-rate estimate
against any real field; read it together with the minimum over opponents. Near-certain opponents (baselines, the
example agents) add almost nothing to the difference or to its standard error, so they barely move the verdict.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import importlib.util
import json
import logging
import math
import multiprocessing as mp
import os
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STRENGTH = ("official_baseline", "random", "v0", "v1", "v2", "cautious", "greedy", "protect_spam", "v3")
FIELD_PANEL = ("basic", "smoke", "llm_fallback", "llm_timeout")
FIELD_ALL = ("basic", "messaging", "smoke", "llm_fallback", "llm_timeout")     # = local.field_opps.FIELD
PANEL = STRENGTH + FIELD_PANEL
GROUPS = {"all": PANEL, "panel": PANEL, "": PANEL, "strength": STRENGTH, "field": FIELD_ALL}
PACKAGES = {"v0": "versions.v0_heuristic", "v1": "versions.v1_exact_damage", "v2": "versions.v2_joint_lookahead",
            "v3": "versions.v3_draft_preview", "cautious": "local.sparring.v2_cautious",
            "champion": "champion"}            # by name only: the frozen champion (= local.ladder's seeds and seats)
BOTS = ("greedy", "protect_spam")
LIVE = ("llm_live", "llm_real")                                                  # = local.field_opps.LIVE
EXT = ("simbot", "simbot_v1")                                                    # = local.ext_opp.NAMES (opt-in)
VGCB = ("vgcbench_bc", "vgcbench_sp")             # = local.vgcbench_opp.NAMES (opt-in, needs the VGC-Bench install)
META = ("meta", "meta_soft", "meta_v3")                                          # = local.meta_opp.NAMES (opt-in)
# per-game counters of the VGC-Bench players (local.vgcbench_opp.EXTRA_KEYS) -> row fields vgb_<key>
VGCB_KEYS = ("preview_fallbacks", "restarts", "vocab_lookups", "vocab_unknown", "unknown_decisions", "retargeted",
             "gimmicks", "header_mismatch", "policy_ms")
BUILTIN = ("official_baseline", "random") + BOTS + FIELD_ALL + LIVE + EXT + VGCB + META
KNOB_PREFIXES = ("LOOKAHEAD_", "ARENA_DRAFT_", "PREVIEW_MATRIX_")
# Time-dependent lookahead counters (agent/pokemon/lookahead.py STATS) -> per-game row fields <name>_cand / <name>_opp:
# a decision that hit the 2.5 s cap, or a Nash mix that fell back (mostly its 0.25 s budget), depends on machine load.
DIAG = {"timeouts": "cap", "mix_fallback": "mixfb"}
# Logged by agent/pokemon/policy.py (v3 and later) when the team-preview game fails or runs past its budget.
PREVIEW_FALLBACK = "preview matrix failed"
# local.field_opps.PlatformSeat.stats keys copied to the rows (pf_<key>) and summed per opponent
PF_KEYS = ("requests", "crashes", "rejected", "timer_defaults", "late", "forfeit", "random_played", "draft_stalls",
           "fallbacks", "model_calls", "invalid_answers")
WARM_CALC = [{"attacker": {"species": "Pikachu", "level": 50}, "defender": {"species": "Pikachu", "level": 50},
              "move": "Thunderbolt"}]
Z = 1.96
# Default panel seed. Deliberately not local.benchmark's 31 (with 31 the official_baseline / random / v0 / v1 / v2 / v3
# rows are exactly the benchmark games every candidate is tuned against and already passes) nor local.ladder's 7 / 11.
DEFAULT_SEED = 43


# ------------------------------------------------------------------------------------------ statistics
def wilson(k: int, n: int, z: float = Z) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def _game(r: dict) -> tuple:
    """The game a row belongs to. Game g is played against EVERY opponent with the same match seed and seat (same
    draft pool, same side), so one g's outcomes are correlated across opponents; different g are independent."""
    return r.get("seed"), r["g"]


def clustered_se(values: dict) -> float:
    """Standard error of the unweighted mean over opponents of their mean outcome, clustered by game.

    values = {opp: {game: x}} (x = a win, or a paired difference). Summing per-opponent standard errors as if the
    opponents were independent understates the error, because game g is the same seed and seat against every
    opponent (review 2: 1.6x on v3 - v2). This is the cluster-robust (sandwich) variance with the G / (G - 1)
    small-sample factor, G = number of games; when every opponent has the same games it equals the plain standard
    error of the per-game average over opponents. Opponents with different game counts (field 500, strength 200)
    are fine: each contributes (x - its mean) / its n to its game's cluster."""
    k = len(values)
    contrib: dict = defaultdict(float)
    for xs in values.values():
        n = len(xs)
        if not n:
            continue
        m = sum(xs.values()) / n
        for game, x in xs.items():
            contrib[game] += (x - m) / (n * k)
    g = len(contrib)
    if g < 2:
        return 0.0
    return math.sqrt(g / (g - 1) * sum(c * c for c in contrib.values()))


def summarize(rows: list[dict], order: list[str] | None = None) -> dict:
    by = defaultdict(list)
    for r in rows:
        by[r["opp"]].append(r)
    order = [o for o in (order or []) if o in by] + sorted(o for o in by if o not in (order or []))
    per = {}
    for opp in order:
        rs = by[opp]
        ok = [r for r in rs if "error" not in r]
        n, w = len(ok), sum(bool(r["win"]) for r in ok)
        lo, hi = wilson(w, n)
        per[opp] = {"n": n, "wins": w, "ties": sum(bool(r.get("tie")) for r in ok), "rate": w / n if n else 0.0,
                    "ci": [lo, hi], "inv_cand": sum(r.get("inv_cand", 0) for r in ok),
                    "inv_opp": sum(r.get("inv_opp", 0) for r in ok), "errors": len(rs) - n,
                    "turns": sum(r.get("turns", 0) for r in ok) / n if n else 0.0,
                    "secs": sum(r.get("secs", 0) for r in ok) / n if n else 0.0,
                    "timer_cand": sum(r.get("timer_cand", 0) for r in ok),
                    "timer_opp": sum(r.get("timer_opp", 0) for r in ok),
                    "forfeit_cand": sum(r.get("forfeit") == "cand" for r in ok),
                    "forfeit_opp": sum(r.get("forfeit") == "opp" for r in ok),
                    # draft dead ends: games where a pick had no Item-Clause-legal card (local/pokemon_env.py Match.draft)
                    "dead_ends": sum(bool(r.get("waive_cand") or r.get("waive_opp")) for r in ok),
                    # the candidate's slowest team-preview / battle decision (real seconds on the VGC timer), and
                    # games without that measurement (rows from before the candidate ran on the timer)
                    "tmax_cand": max((r["tmax_cand"] for r in ok if "tmax_cand" in r), default=0.0),
                    "untimed_cand": sum("tmax_cand" not in r for r in ok)}
        for field in (*DIAG.values(), "pvwall", "pvfb", "warn"):
            for side in ("cand", "opp"):
                per[opp][f"{field}_{side}"] = sum(r.get(f"{field}_{side}", 0) for r in ok)
        for key in ("bot_fallbacks", "bot_protects", "bot_slots", "bot_no_calc"):
            if any(key in r for r in ok):
                per[opp][key] = sum(r.get(key, 0) for r in ok)
        if any("ext_decisions" in r for r in ok):            # simbot (local/ext_opp.py)
            for key in ("ext_decisions", "ext_fallbacks", "ext_errors", "ext_sims", "ext_ms_total"):
                per[opp][key] = sum(r.get(key, 0) for r in ok)
            per[opp]["ext_ms_max"] = max((r.get("ext_ms_max", 0) for r in ok), default=0)
            per[opp]["same_team"] = sum(bool(r.get("same_team")) for r in ok)
        if any("vgb_restarts" in r for r in ok):             # VGC-Bench (local/vgcbench_opp.py)
            for key in VGCB_KEYS:
                per[opp][f"vgb_{key}"] = sum(r.get(f"vgb_{key}", 0) for r in ok)
        if any("pf_requests" in r for r in ok):
            for key in PF_KEYS:
                per[opp][f"pf_{key}"] = sum(r.get(f"pf_{key}", 0) for r in ok)
            per[opp]["pf_crashed_games"] = sum(bool(r.get("pf_crashes")) for r in ok)
    live = [o for o in order if per[o]["n"]]
    out = {"per_opp": per, "opps": live}
    if live:
        rates = [per[o]["rate"] for o in live]
        mean = sum(rates) / len(rates)
        se = clustered_se({o: {_game(r): float(bool(r["win"])) for r in by[o] if "error" not in r} for o in live})
        worst = min(live, key=lambda o: (per[o]["rate"], o))
        out.update(panel_mean=mean, panel_ci=[max(0.0, mean - Z * se), min(1.0, mean + Z * se)], panel_se=se,
                   min_opp=worst, min_rate=per[worst]["rate"],
                   min_ci=per[worst]["ci"], games=sum(per[o]["n"] for o in live),
                   inv_cand=sum(per[o]["inv_cand"] for o in live), inv_opp=sum(per[o]["inv_opp"] for o in live),
                   timer_cand=sum(per[o]["timer_cand"] for o in live),
                   forfeit_cand=sum(per[o]["forfeit_cand"] for o in live),
                   tmax_cand=max(per[o]["tmax_cand"] for o in live),
                   untimed_cand=sum(per[o]["untimed_cand"] for o in live),
                   dead_ends=sum(per[o]["dead_ends"] for o in live),
                   errors=sum(per[o]["errors"] for o in per))
    return out


def compare(rows: list[dict], base: list[dict], order: list[str] | None = None) -> dict:
    """Paired differences (this run - base) on games shared by (opp, seed, g)."""
    key = lambda r: (r["opp"], r["seed"], r["g"])   # noqa: E731
    b = {key(r): r for r in base if "error" not in r}
    by = defaultdict(list)
    diffs: dict = defaultdict(dict)                 # {opp: {game: this - base}} for the clustered panel error
    for r in rows:
        if "error" not in r and key(r) in b:
            x, y = int(bool(r["win"])), int(bool(b[key(r)]["win"]))
            by[r["opp"]].append((x, y))
            diffs[r["opp"]][_game(r)] = x - y
    order = [o for o in (order or []) if o in by] + sorted(o for o in by if o not in (order or []))
    per = {}
    for opp in order:
        d = [x - y for x, y in by[opp]]
        n = len(d)
        mean = sum(d) / n
        var = sum((v - mean) ** 2 for v in d) / (n - 1) if n > 1 else 0.0
        se = math.sqrt(var / n)
        per[opp] = {"n": n, "rate": sum(x for x, _ in by[opp]) / n, "base_rate": sum(y for _, y in by[opp]) / n,
                    "diff": mean, "ci": [mean - Z * se, mean + Z * se], "se": se,
                    "only_cand": sum(1 for x, y in by[opp] if x > y), "only_base": sum(1 for x, y in by[opp] if y > x)}
    out = {"per_opp": per, "opps": order}
    if order:
        k = len(order)
        mean = sum(per[o]["diff"] for o in order) / k
        se = clustered_se({o: diffs[o] for o in order})
        out.update(panel_diff=mean, panel_ci=[mean - Z * se, mean + Z * se], panel_se=se,
                   worse=[o for o in order if per[o]["ci"][1] < 0], better=[o for o in order if per[o]["ci"][0] > 0])
    return out


def unclean(s: dict) -> list[str]:
    """Why the candidate's run cannot be promoted whatever its win rates ([] = clean). The candidate plays on the
    platform's VGC timer in real wall time (local.field_opps.WallClockSeat), so a too-slow candidate shows
    timer-played decisions / forfeits here; rows without that measurement (older panels) cannot certify it."""
    why = []
    for key, text in (("inv_cand", "invalid actions"), ("errors", "crashed games"),
                      ("timer_cand", "decisions played by the VGC timer (too slow)"),
                      ("forfeit_cand", "forfeits on the VGC timer"),
                      ("untimed_cand", "games without the candidate's decision times (rows from an older panel)")):
        if s.get(key, 0):
            why.append(f"{s[key]} {text}")
    return why


def verdict(s: dict, c: dict) -> str:
    if not c.get("opps") or unclean(s) or c["worse"]:
        return "REJECT"
    if c["panel_ci"][0] > 0:
        return "PROMOTE"
    return "HOLD" if c["panel_diff"] >= 0 else "REJECT"


def print_summary(s: dict, title: str = "") -> None:
    if title:
        print(title)
    print(f"{'opponent':18s} {'games':>5s} {'win%':>6s}  95% CI         ties  inv(c/o)  turns  s/game  "
          f"timing-dependent: capHits(c/o) mixFallbacks(c/o) previewWallCuts(c/o) previewFallbacks(c/o)")
    for o in s["opps"]:
        p = s["per_opp"][o]
        extra = (f"  {p['mixfb_cand']}/{p['mixfb_opp']}  {p['pvwall_cand']}/{p['pvwall_opp']}"
                 f"  {p['pvfb_cand']}/{p['pvfb_opp']}")
        if p["warn_cand"] or p["warn_opp"]:
            extra += f"  warnings {p['warn_cand']}/{p['warn_opp']}"
        if "bot_fallbacks" in p:
            extra += (f"  bot: fallbacks {p['bot_fallbacks']}, no-calc turns {p.get('bot_no_calc', 0)}, "
                     f"protects {p.get('bot_protects', 0)}/{p.get('bot_slots', 0)} slot-moves")
        if "vgb_restarts" in p:
            extra += (f"  vgcbench: {p['ext_decisions']} decisions, fallbacks {p['ext_fallbacks']} (team preview "
                      f"{p['vgb_preview_fallbacks']}), worker errors {p['ext_errors']}, restarts {p['vgb_restarts']}, "
                      f"{p['ext_ms_total'] / max(1, p['ext_decisions']):.0f} ms mean / {p['ext_ms_max']:.0f} ms max "
                      f"per decision, unknown-vocab decisions {p['vgb_unknown_decisions']} ({p['vgb_vocab_unknown']}/"
                      f"{p['vgb_vocab_lookups']} lookups), retargeted {p['vgb_retargeted']}, gimmicks stripped "
                      f"{p['vgb_gimmicks']}, header mismatches {p['vgb_header_mismatch']}"
                      + (f", same-team games {p['same_team']}" if p.get("same_team") else ""))
        elif "ext_decisions" in p:
            extra += (f"  simbot: {p['ext_decisions']} decisions, fallbacks {p['ext_fallbacks']}, sim errors "
                      f"{p['ext_errors']}, {p['ext_ms_total'] / max(1, p['ext_decisions']):.0f} ms mean / "
                      f"{p['ext_ms_max']:.0f} ms max per decision"
                      + (f", same-team games {p['same_team']}" if p.get("same_team") else ""))
        if "pf_requests" in p:
            extra += (f"  platform: crashed games {p['pf_crashed_games']}, rejected {p['pf_rejected']}, "
                      f"timer-played {p['timer_opp']} of {p['pf_requests']} requests (draft incl.), forfeits "
                      f"{p['forfeit_opp']}, llm fallbacks {p['pf_fallbacks']} (model calls {p['pf_model_calls']}, "
                      f"invalid answers {p.get('pf_invalid_answers', 0)})")
        if p.get("timer_cand") or p.get("forfeit_cand"):
            extra += f"  CANDIDATE timer-played {p['timer_cand']}, forfeits {p['forfeit_cand']}"
        err = f"  ERRORS {p['errors']}" if p["errors"] else ""
        print(f"{o:18s} {p['n']:5d} {100 * p['rate']:6.1f}  [{100 * p['ci'][0]:4.1f}, {100 * p['ci'][1]:5.1f}]  "
              f"{p['ties']:4d}  {p['inv_cand']:3d}/{p['inv_opp']:<4d} {p['turns']:5.1f}  {p['secs']:6.2f}  "
              f"{p['cap_cand']}/{p['cap_opp']}{extra}{err}")
    if s["opps"]:
        print(f"{'panel mean':18s} {s['games']:5d} {100 * s['panel_mean']:6.1f}  [{100 * s['panel_ci'][0]:4.1f}, "
              f"{100 * s['panel_ci'][1]:5.1f}]  unweighted over {len(s['opps'])} opponents")
        print(f"{'min over opps':18s} {'':5s} {100 * s['min_rate']:6.1f}  [{100 * s['min_ci'][0]:4.1f}, "
              f"{100 * s['min_ci'][1]:5.1f}]  ({s['min_opp']})")
        timed = s["games"] - s.get("untimed_cand", 0)
        print(f"{'candidate clock':18s} slowest preview/battle decision {s.get('tmax_cand', 0.0):.2f} s on the VGC "
              f"timer (limits 90 s / 55 s) in {timed} timed games; timer-played {s['timer_cand']}, forfeits "
              f"{s['forfeit_cand']}" + (f"; {s['untimed_cand']} games UNTIMED" if s.get("untimed_cand") else ""))
        print(f"{'draft dead ends':18s} {s.get('dead_ends', 0)} games with a pick where no card was Item-Clause legal "
              f"(the local env waived the clause for that pick; platform behaviour unknown)")


def print_compare(c: dict, base_name: str) -> None:
    print(f"paired vs {base_name}: difference in win rate (this - base), 95% CI")
    print(f"{'opponent':18s} {'pairs':>5s} {'this':>6s} {'base':>6s} {'diff':>6s}  95% CI            only this/base")
    for o in c["opps"]:
        p = c["per_opp"][o]
        print(f"{o:18s} {p['n']:5d} {100 * p['rate']:6.1f} {100 * p['base_rate']:6.1f} {100 * p['diff']:+6.1f}  "
              f"[{100 * p['ci'][0]:+5.1f}, {100 * p['ci'][1]:+5.1f}]   {p['only_cand']}/{p['only_base']}")
    if c["opps"]:
        print(f"{'panel mean diff':18s} {'':5s} {'':6s} {'':6s} {100 * c['panel_diff']:+6.1f}  "
              f"[{100 * c['panel_ci'][0]:+5.1f}, {100 * c['panel_ci'][1]:+5.1f}]   significantly worse: "
              f"{', '.join(c['worse']) or 'none'}; better: {', '.join(c['better']) or 'none'}")


def load_rows(path: str | Path) -> list[dict]:
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


# ------------------------------------------------------------------------------------------ opponents
def opponent_package(name: str) -> str | None:
    """Dotted package (holding agent/agent.py) for a non-builtin opponent name, or None if unknown."""
    if name in PACKAGES:
        return PACKAGES[name]
    if (ROOT / "versions" / name / "agent" / "agent.py").exists():
        return f"versions.{name}"
    if "." in name and (ROOT.joinpath(*name.split(".")) / "agent" / "agent.py").exists():
        return name
    return None


def resolve_opps(spec: str) -> list[str]:
    spec = spec.strip()
    names = list(GROUPS[spec]) if spec in GROUPS else [x.strip() for x in spec.split(",") if x.strip()]
    bad = [n for n in names if n not in BUILTIN and opponent_package(n) is None]
    if bad:
        raise SystemExit(f"unknown opponent(s) {bad}; groups: {', '.join(k for k in GROUPS if k)}; "
                         f"panel: {', '.join(PANEL)}; also messaging, llm_live, llm_real, {', '.join(EXT + VGCB)}, "
                         f"any versions/<dir> or a dotted package")
    if any(n in VGCB for n in names):
        from .vgcbench_opp import available
        ok, why = available()
        if not ok:
            raise SystemExit(f"{', '.join(n for n in names if n in VGCB)}: {why}")
    if "llm_live" in names and not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("llm_live plays examples/llm_agent.py with a real model: set OPENAI_API_KEY (and optionally "
                         "OPENAI_MODEL / OPENAI_BASE_URL for any OpenAI-compatible endpoint) in the environment")
    if "llm_real" in names:
        from .llm_client import LLMClient
        c = LLMClient.from_env(cache_path=None)
        why = c.unavailable_reason                          # no key, or the budget is already spent
        c.close()
        if why:
            print(f"WARNING: llm_real is unavailable ({why}): it plays examples/llm_agent.py's rule fallback "
                  f"(= llm_fallback), not a model", flush=True)
    return list(dict.fromkeys(names))


# ------------------------------------------------------------------------------------------ candidate-only env
@contextlib.contextmanager
def scoped_env(env: dict):
    """Set `env` in os.environ for the duration of the block, then restore the previous values exactly."""
    if not env:
        yield
        return
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class WarnCounter(logging.Handler):
    """Counts the WARNING+ log records of one game by who was acting: 'cand' inside the candidate's reset / choose,
    'opp' inside the opponent's, 'env' otherwise (the harness). The candidate and a frozen copy of the same code log
    under the same logger names and even the same files (v2 vs v2), so the side comes from the call, not the record.
    `preview` counts PREVIEW_FALLBACK records: v3's 90x90 team-preview game gave up (almost always its 6 s wall-clock
    cap under machine load) and the heuristic preview played instead - timing-dependent, like the lookahead cap hits.
    Installed on the root logger in every worker, so these warnings are counted into the JSONL instead of printed."""

    SIDES = ("cand", "opp", "env")

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.side: str | None = None
        self.reset()

    def reset(self) -> None:
        self.warn = dict.fromkeys(self.SIDES, 0)
        self.preview = dict.fromkeys(self.SIDES, 0)
        self.preview_wall = dict.fromkeys(self.SIDES, 0)
        self.first: dict[str, str] = {}

    def note_preview_wall(self) -> None:
        """A preview game answered but its search stopped at the wall-clock cap (see instrument_preview)."""
        self.preview_wall[self.side or "env"] += 1

    def emit(self, record: logging.LogRecord) -> None:
        side = self.side or "env"
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string is still a warning
            msg = str(record.msg)
        self.warn[side] += 1
        if msg.startswith(PREVIEW_FALLBACK):
            self.preview[side] += 1
        self.first.setdefault(side, f"{record.name}: {msg}"[:160])

    @contextlib.contextmanager
    def acting(self, side: str):
        prev, self.side = self.side, side
        try:
            yield
        finally:
            self.side = prev


WARNS = WarnCounter()


def instrument_preview(pkg_prefix: str) -> bool:
    """Count, per side, v3-style team-preview games whose search stopped at its wall-clock cap but still answered
    (`info["wall_hit"]` of <pkg_prefix>.pokemon.preview.choose; not logged by the agent). The work budget makes the
    preview load-independent; only this cap is not, so such a game may pick another lineup than an unloaded run.
    Wraps `choose` in place (the policy looks it up on the module at call time); answers are unchanged."""
    mod = sys.modules.get(f"{pkg_prefix}.pokemon.preview")
    fn = getattr(mod, "choose", None)
    if fn is None or getattr(fn, "_panel_counted", False):
        return False

    def choose(*a, **k):
        res = fn(*a, **k)
        if isinstance(res, dict) and (res.get("info") or {}).get("wall_hit"):
            WARNS.note_preview_wall()
        return res

    choose._panel_counted = True
    choose.__wrapped__ = fn
    mod.choose = choose
    return True


class Scoped:
    """A Player whose reset / choose run with `env` set (the candidate's --env: nobody else's do) and are attributed
    to `side` in WARNS (the opponent is wrapped too, with no env and side 'opp')."""

    def __init__(self, player, env: dict, side: str = "cand"):
        self.player, self.env, self.side = player, dict(env), side

    @property
    def name(self):
        return self.player.name

    @property
    def agent(self):
        return getattr(self.player, "agent", None)

    def reset(self):
        with scoped_env(self.env), WARNS.acting(self.side):
            self.player.reset()

    def choose(self, raw):
        with scoped_env(self.env), WARNS.acting(self.side):
            return self.player.choose(raw)

    def bind(self, match, seat):
        """Forward Match.bind to players that search the engine themselves (simbot); a no-op for everyone else."""
        bind = getattr(self.player, "bind", None)
        if callable(bind):
            bind(match, seat)


class OracleBind:
    """Local ablation of the engine search (candidate --env ARENA_SEARCH_ORACLE=1): the candidate's search is handed
    the simulator's TRUE battle (local/js/bridge.js `dump`) instead of the battle it rebuilds from platform-legal
    information, everything else unchanged. Measures the information gap; never a candidate for the platform."""

    def __init__(self, player):
        self.player = player

    def __getattr__(self, k):
        return getattr(self.player, k)

    def reset(self):
        self.player.reset()

    def choose(self, raw):
        return self.player.choose(raw)

    def bind(self, match, seat):
        p = self.player
        for _ in range(8):                           # Tap -> Scoped -> WallClockSeat -> ContestantPlayer
            pk = getattr(getattr(p, "agent", None), "pk", None)
            if pk is not None:
                pk.oracle = lambda: match.sd.dump()
                return
            p = getattr(p, "player", None)
            if p is None:
                return


class Tap:
    """Records a player's team-preview and battle answers of one game (--save-logs: `cand_trace` in the game file);
    everything else is passed through unchanged."""

    def __init__(self, player):
        self.player, self.trace = player, []

    @property
    def name(self):
        return self.player.name

    def reset(self):
        self.trace = []
        self.player.reset()

    def bind(self, match, seat):
        bind = getattr(self.player, "bind", None)
        if callable(bind):
            bind(match, seat)

    def choose(self, raw):
        out = self.player.choose(raw)
        if raw.get("phase") in ("team_preview", "moving"):
            shown = out.input.get("action") if hasattr(out, "input") else out
            if not isinstance(shown, (dict, list, str, int, float, type(None))):
                shown = repr(shown)
            self.trace.append({"phase": raw["phase"], "turn": (raw.get("observation") or {}).get("turn"),
                               "out": shown})
        return out


def shell_knobs(environ=None) -> dict:
    """Tuning variables in the (inherited) environment that the frozen versions would read as well."""
    environ = os.environ if environ is None else environ
    return {k: v for k, v in environ.items() if k.startswith(KNOB_PREFIXES)}


# ------------------------------------------------------------------------------------------ worker side
_W: dict = {}


def _load_candidate(cand: Path) -> None:
    """Make `agent` = cand/agent (its relative and absolute `agent.` imports stay inside that directory)."""
    pkg = cand / "agent"
    if "agent" in sys.modules:
        have = Path(sys.modules["agent"].__file__).resolve().parent
        if have != pkg.resolve():
            raise RuntimeError(f"`agent` already imported from {have}, not {pkg}")
        return
    if pkg.resolve() == (ROOT / "agent").resolve():
        importlib.import_module("agent")
        return
    spec = importlib.util.spec_from_file_location("agent", pkg / "__init__.py", submodule_search_locations=[str(pkg)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent"] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop("agent", None)
        raise
    if str(cand) not in sys.path:
        sys.path.append(str(cand))        # the candidate's other top-level modules; never shadows ours


def _warm(pkg_prefix: str) -> dict:
    """Start `<pkg_prefix>.pokemon.calc`'s node calculator now (outside any timed decision)."""
    try:
        calc = importlib.import_module(f"{pkg_prefix}.pokemon.calc")
    except Exception as e:  # noqa: BLE001 - a candidate without this module is legal
        return {"calc": None, "calc_error": f"{type(e).__name__}: {e}"}
    info = {"calc": getattr(calc, "__file__", None)}
    try:
        js = calc._js_dir() if hasattr(calc, "_js_dir") else None
        info["calc_js"] = str(js) if js else None
        dc = calc.DamageCalc.get()
        info["calc_enabled"] = bool(dc.enabled)
        if dc.enabled and _W.get("warmup", True):
            res = dc.batch(WARM_CALC)
            info["calc_ok"] = bool(res and res[0])
    except Exception as e:  # noqa: BLE001
        info["calc_error"] = f"{type(e).__name__}: {e}"
    return info


def calc_origin(pkg_prefix: str, info: dict) -> dict:
    """Does `<pkg_prefix>` run its own calc.js? {'own', 'runs', 'foreign', 'enabled'}: foreign = it runs another
    package's calc.js whose bytes differ from its own (an old calc.py falls back to the repo's agent/js when its own
    js/ has no node_modules link, and the opponent then silently plays with the candidate's calculator)."""
    mod = sys.modules.get(f"{pkg_prefix}.pokemon.calc")
    own = Path(mod.__file__).resolve().parent.parent / "js" / "calc.js" if mod is not None else None
    runs = Path(info["calc_js"]) / "calc.js" if info.get("calc_js") else None
    foreign = bool(own and runs and own.exists() and runs.exists() and runs.resolve() != own.resolve()
                   and runs.read_bytes() != own.read_bytes())
    return {"own": str(own) if own else None, "runs": str(runs) if runs else None, "foreign": foreign,
            "enabled": bool(info.get("calc_enabled"))}


def make_candidate(create_agent, env: dict) -> Scoped:
    """The candidate seat: its agent as the official runner drives it, on the platform's VGC timer in real wall time
    (a decision slower than the timer allows is played by Showdown's timer, an empty bank forfeits, and both count
    against a clean verdict), with --env scoped to its own reset / choose."""
    from .field_opps import WallClockSeat
    from .pokemon_env import ContestantPlayer
    return Scoped(WallClockSeat(ContestantPlayer(create_agent, "candidate")), env)


def _worker_init(cfg: dict) -> None:
    _W.clear()
    _W.update(cfg=cfg, sd=None, opps={}, warmup=cfg["warmup"], info=None, error=None)
    try:
        try:
            os.nice(cfg["nice"])
        except (OSError, AttributeError):
            pass
        if WARNS not in logging.getLogger().handlers:
            logging.getLogger().addHandler(WARNS)
        if cfg["root"] not in sys.path:
            sys.path.insert(0, cfg["root"])
        env = cfg.get("cand_env") or {}
        with scoped_env(env):
            _load_candidate(Path(cfg["candidate_dir"]))
            from . import pokemon_env                           # after the candidate owns the name `agent`
            agent_mod = importlib.import_module("agent.agent")
            info = {"agent_file": agent_mod.__file__, "env_file": pokemon_env.__file__, "pid": os.getpid()}
            pol = sys.modules.get("agent.pokemon.policy")
            if pol is not None:
                info["policy_file"] = pol.__file__
            info.update(_warm("agent"))
            info["preview_counted"] = instrument_preview("agent")
        _W["cand"] = make_candidate(agent_mod.create_agent, env)
        versions = importlib.import_module("versions")
        info["versions_dir"] = list(versions.__path__)[0]
        _W["info"] = info
    except BaseException as e:  # noqa: BLE001 - never let a Pool initializer raise (it would respawn forever)
        import traceback
        _W["error"] = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"


def _candidate_info(opps=()) -> dict:
    """The candidate's load info, plus where every opponent package's calculator comes from."""
    out = {"error": _W.get("error"), **(_W.get("info") or {})}
    if out["error"]:
        return out
    origins = {}
    for name in opps:
        try:
            p = _opponent(name)
            pkg = getattr(p, "pkg", None) or getattr(p, "calc_pkg", None)
            if pkg:
                origins[name] = calc_origin(pkg, getattr(p, "calc_info", {}))
        except Exception as e:  # noqa: BLE001
            origins[name] = {"error": f"{type(e).__name__}: {e}"}
    out["opp_calc"] = origins
    return out


def _opponent(name: str):
    p = _W["opps"].get(name)
    if p is not None:
        return p
    from .pokemon_env import ContestantPlayer, OfficialBaseline, RandomPlayer
    if name == "official_baseline":
        p = OfficialBaseline()
    elif name == "random":
        p = RandomPlayer()
    elif name in FIELD_ALL + LIVE:
        from . import field_opps
        p = field_opps.make(name)
    elif name in BOTS:
        from . import panel_bots
        p = ContestantPlayer(lambda kind=name: panel_bots.create_agent(kind), name)
        p.calc_info = _warm("versions.v1_exact_damage.agent")
        p.calc_pkg = "versions.v1_exact_damage.agent"
    elif name in EXT:
        from . import ext_opp
        p = ext_opp.make(name)                  # p.calc_pkg = the package it drafts / previews with
        p.calc_info = _warm(p.calc_pkg)
        instrument_preview(p.calc_pkg)
    elif name in META:
        from . import meta_opp
        p = meta_opp.make(name)                 # p.calc_pkg = the pilot package (preview + battle)
        p.calc_info = _warm(p.calc_pkg)
        instrument_preview(p.calc_pkg)
    elif name in VGCB:
        from . import vgcbench_opp
        p = vgcbench_opp.make(name)             # p.calc_pkg = the package it drafts with (the champion)
        p.calc_info = _warm(p.calc_pkg)
        instrument_preview(p.calc_pkg)
        try:                                    # worker start-up + checkpoint load now, outside any game
            p.load_info = p.start()
        except Exception as e:  # noqa: BLE001 - the games then count fallbacks / restarts; never kill the worker
            p.load_info = {"error": f"{type(e).__name__}: {e}"}
            logging.getLogger("local.vgcbench").warning("VGC-Bench worker failed to start: %s", e)
    else:
        pkg = opponent_package(name)
        mod = importlib.import_module(f"{pkg}.agent.agent")
        p = ContestantPlayer(mod.create_agent, name)
        p.calc_info = _warm(f"{pkg}.agent")
        p.pkg = f"{pkg}.agent"
        instrument_preview(p.pkg)
    _W["opps"][name] = p
    return p


def _showdown():
    if _W.get("sd") is None:
        from multiprocessing import util
        from .showdown import Showdown
        _W["sd"] = Showdown()
        util.Finalize(None, _close_showdown, exitpriority=10)
    return _W["sd"]


def _close_showdown() -> None:
    sd = _W.get("sd")
    _W["sd"] = None
    if sd is not None:
        sd.close()


def _fresh_calc_cache(pkg_prefix: str) -> None:
    """Empty `<pkg_prefix>.pokemon.calc.DamageCalc`'s result cache (the node process stays up). Every game then
    starts like a live match in a fresh process, and no game depends on what its worker played before: the cache
    is capped at 20000 entries and the batch that crosses the cap loses all its results in old versions (the policy
    falls back to the heuristic for that one decision), so a shared cache made results depend on worker scheduling."""
    mod = sys.modules.get(f"{pkg_prefix}.pokemon.calc")
    inst = getattr(getattr(mod, "DamageCalc", None), "_inst", None)
    cache = getattr(inst, "cache", None)
    if isinstance(cache, dict):
        cache.clear()


def _lookahead_stats(pkg_prefix: str | None) -> dict:
    """Snapshot of the time-dependent counters of `<pkg_prefix>.pokemon.lookahead.STATS` ({} when absent)."""
    mod = sys.modules.get(f"{pkg_prefix}.pokemon.lookahead") if pkg_prefix else None
    st = getattr(mod, "STATS", None)
    return {k: st[k] for k in DIAG if k in st} if isinstance(st, dict) else {}


def _calc_insts() -> dict:
    """Every live DamageCalc singleton in this worker: {module name: (module, instance)}."""
    out = {}
    for name, mod in list(sys.modules.items()):
        if name.endswith("pokemon.calc"):
            inst = getattr(getattr(mod, "DamageCalc", None), "_inst", None)
            if inst is not None:
                out[name] = (mod, inst)
    return out


def _heal_calcs() -> int:
    """Reset + restart every DamageCalc that is disabled or whose node process died, so one bad game cannot turn a
    worker's remaining games into heuristic-only games."""
    import shutil
    revived = 0
    for name, (mod, inst) in _calc_insts().items():
        dead = inst.proc is not None and inst.proc.poll() is not None
        if inst.enabled and not dead:
            continue
        try:
            if inst.proc is not None:
                inst.proc.kill()
        except Exception:  # noqa: BLE001
            pass
        inst.proc = None
        js = mod._js_dir() if hasattr(mod, "_js_dir") else None
        inst.enabled = shutil.which("node") is not None and js is not None
        if hasattr(inst, "ready"):
            inst.ready = False
        try:
            inst.batch(WARM_CALC)
        except Exception:  # noqa: BLE001
            pass
        revived += 1
    return revived


def _calc_pids() -> dict:
    return {name: (inst.proc.pid if inst.proc is not None else None) for name, (_, inst) in _calc_insts().items()}


def _calc_broken(pids: dict) -> list:
    """Calcs that are disabled, dead, or were restarted since `pids` was taken."""
    bad = []
    for name, (_, inst) in _calc_insts().items():
        if not inst.enabled or inst.proc is None or inst.proc.poll() is not None:
            bad.append(name)
        elif pids.get(name) is not None and inst.proc.pid != pids[name]:
            bad.append(name + "(restarted)")
    return bad


def _play(task: tuple[str, int]) -> dict:
    opp, g = task
    cfg = _W["cfg"]
    seed = cfg["seed"] * 100000 + g
    seat = g % 2
    row = {"cand": cfg["name"], "opp": opp, "seed": cfg["seed"], "g": g, "match_seed": seed, "seat": seat}
    if _W.get("error"):
        return {**row, "error": "worker init failed: " + _W["error"].splitlines()[0], "win": False}
    t0 = time.time()
    try:
        from .field_opps import PlatformSeat
        from .pokemon_env import play_match
        o = _opponent(opp)
        sides = {"cand": "agent", "opp": getattr(o, "pkg", None)}
        revived = _heal_calcs()
        for p in {"agent", getattr(o, "pkg", None), getattr(o, "calc_pkg", None)} - {None}:
            _fresh_calc_cache(p)
        pids = _calc_pids()
        before = {s: _lookahead_stats(p) for s, p in sides.items()}
        same, logs_dir = bool(cfg.get("same_team")), cfg.get("logs_dir")
        cand = Tap(_W["cand"]) if logs_dir else _W["cand"]      # --save-logs: keep the candidate's answers too
        if (cfg.get("cand_env") or {}).get("ARENA_SEARCH_ORACLE") == "1":
            cand = OracleBind(cand)                  # local ablation: the engine search reads the true battle
        players = [cand, Scoped(o, {}, "opp")]
        if seat == 1:
            players.reverse()
        WARNS.reset()
        r = play_match(_showdown(), players[0], players[1], seed=seed, same_team=same, keep_log=bool(logs_dir))
        if same:
            row["same_team"] = True
        row.update(winner=r.winner, win=r.winner == seat, tie=r.winner is None, turns=r.turns,
                   inv_cand=r.invalid[seat], inv_opp=r.invalid[1 - seat],
                   timer_cand=r.timer_defaults[seat], timer_opp=r.timer_defaults[1 - seat],
                   waive_cand=r.clause_waived[seat], waive_opp=r.clause_waived[1 - seat],
                   pvfb_cand=WARNS.preview["cand"], pvfb_opp=WARNS.preview["opp"],
                   pvwall_cand=WARNS.preview_wall["cand"], pvwall_opp=WARNS.preview_wall["opp"],
                   warn_cand=WARNS.warn["cand"], warn_opp=WARNS.warn["opp"])
        clock = _W["cand"].player.stats                 # WallClockSeat: the candidate's real decision times
        row.update(tmax_cand=round(clock["max_s"], 3), tdraft_cand=round(clock["draft_max_s"], 3),
                   bank_cand=clock["bank_left"])
        pk = getattr(getattr(_W["cand"], "agent", None), "pk", None)
        if getattr(pk, "battle_mode", None) == "search":     # the engine search's own counters (ARENA_BATTLE=search)
            ss, ms = pk.search_stats, list(pk.search_ms)
            row.update(srch_dec=ss["decisions"], srch_used=ss["used"], srch_same=ss["same_as_classic"],
                       srch_fb=ss["fallback"], srch_skip=ss["skipped_time"],
                       srch_ms_max=max(ms) if ms else 0, srch_ms_sum=round(sum(ms), 1), srch_n=len(ms))
            if ss["reasons"]:
                row["srch_reasons"] = dict(ss["reasons"])
            if getattr(pk, "oracle", None) is not None:
                row["srch_oracle"] = True                 # ARENA_SEARCH_ORACLE=1: searched the true battle
        if WARNS.warn["env"]:
            row["warn_env"] = WARNS.warn["env"]
        for side, text in WARNS.first.items():
            row[f"warn_first_{side}"] = text
        if r.forfeit is not None:
            row["forfeit"] = "cand" if r.forfeit == seat else "opp"
        if revived:
            row["calc_revived"] = revived
        broken = _calc_broken(pids)
        if broken:
            row.update(error="calc died mid-game: " + ",".join(broken), win=False)
        for s, p in sides.items():
            after = _lookahead_stats(p)
            for k, field in DIAG.items():
                if k in after:
                    row[f"{field}_{s}"] = after[k] - before[s].get(k, 0)
        if opp in META:                                  # both drafted sixes (the draft is what differs)
            row.update(six_cand=r.rosters[seat], six_opp=r.rosters[1 - seat])
        if opp in EXT or opp in VGCB:                    # simbot's / VGC-Bench's own counters
            st = o.stats
            row.update(ext_decisions=st["decisions"], ext_fallbacks=st["fallbacks"], ext_errors=st["errors"],
                       ext_sims=st["sims"], ext_ms_total=round(st["ms_total"], 1), ext_ms_max=round(st["ms_max"], 1))
            if opp in VGCB:
                row.update({f"vgb_{k}": round(st.get(k, 0), 1) if k == "policy_ms" else st.get(k, 0)
                            for k in VGCB_KEYS})
        if logs_dir:
            Path(logs_dir).mkdir(parents=True, exist_ok=True)
            game = {k: row[k] for k in ("cand", "opp", "seed", "g", "match_seed", "seat", "winner", "win", "turns")}
            game.update(same_team=same, rosters=r.rosters, brought=r.brought, log=r.log or [],
                        trace=list(getattr(o, "trace", None) or []), cand_trace=cand.trace)
            (Path(logs_dir) / f"{opp}-g{g}.json").write_text(json.dumps(game))
        if isinstance(o, PlatformSeat):
            row.update({f"pf_{k}": o.stats.get(k, 0) for k in PF_KEYS})
            if o.crash_reason:
                row["pf_crash"] = o.crash_reason[:160]
        else:
            st = getattr(getattr(o, "agent", None), "stats", None)
            if isinstance(st, dict) and "fallbacks" in st:
                row.update(bot_fallbacks=st["fallbacks"], bot_protects=st.get("protects", 0),
                           bot_slots=st.get("slots", 0), bot_no_calc=st.get("no_calc", 0))
    except Exception as e:  # noqa: BLE001 - a crashed game is recorded, and the bridge restarted
        import traceback
        row.update(error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()[-2000:], win=False)
        _close_showdown()
    row["secs"] = round(time.time() - t0, 3)
    row["pid"] = os.getpid()
    return row


# ------------------------------------------------------------------------------------------ driver side
def tree_hash(d: Path) -> str:
    """sha256 over the candidate's agent/ sources (.py/.js/.json), to tell runs of different code apart."""
    h = hashlib.sha256()
    base = d / "agent"
    for p in sorted(base.rglob("*")):
        if p.is_file() and p.suffix in (".py", ".js", ".json") and "node_modules" not in p.parts \
                and "__pycache__" not in p.parts:
            h.update(str(p.relative_to(base)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def git_rev(d: Path) -> str | None:
    try:
        rev = subprocess.run(["git", "-C", str(d), "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                             timeout=10)
        if rev.returncode != 0:
            return None
        dirty = subprocess.run(["git", "-C", str(d), "status", "--porcelain", "--", "agent"], capture_output=True,
                               text=True, timeout=10).stdout.strip()
        return rev.stdout.strip() + ("+dirty" if dirty else "")
    except Exception:  # noqa: BLE001
        return None


def null_record_dir(out: Path) -> Path:
    """Versions older than the ARENA_RECORD switch append every decision to $ARENA_RECORD_DIR/<session>.jsonl;
    point that at a directory whose local.jsonl is /dev/null."""
    d = out.parent / ".null_records"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "local.jsonl"
    if not f.is_symlink():
        if f.exists():
            f.unlink()
        try:
            f.symlink_to(os.devnull)
        except OSError:
            pass
    return d


def default_out(name: str, seed: int) -> Path:
    """logs/panel/runs/<name>-s<seed>.jsonl: two levels below logs/ on purpose, because scripts/replay_real.py,
    local.pool --harvest and some tests read every logs/*/*.jsonl as a real platform recording."""
    return ROOT / "logs" / "panel" / "runs" / f"{name}-s{seed}.jsonl"


def parse_only(spec: str) -> list[int]:
    """'12,18,30-33' -> [12, 18, 30, 31, 32, 33]: replay just these game indices (same seeds and seats)."""
    out = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(part))
    return sorted(set(out))


def parse_env(pairs) -> dict:
    env = {}
    for kv in pairs or []:
        if "=" not in kv:
            raise SystemExit(f"--env wants KEY=VALUE, got {kv!r}")
        k, v = kv.split("=", 1)
        env[k] = v
    return env


def run(a) -> int:
    cand = Path(a.candidate_dir).resolve() if a.candidate_dir else ROOT
    if not (cand / "agent" / "__init__.py").exists():
        raise SystemExit(f"{cand} has no agent/ package")
    name = a.name or ("working-tree" if cand == ROOT else cand.name)
    opps = resolve_opps(a.opps)
    out = Path(a.out) if a.out else default_out(name, a.seed)
    out.parent.mkdir(parents=True, exist_ok=True)
    env = parse_env(a.env)
    dropped = shell_knobs()
    for k in dropped:
        os.environ.pop(k)
    if dropped:
        print(f"WARNING: ignoring tuning variables from the shell {sorted(dropped)}: the frozen opponents read the "
              f"same names; pass them with --env to set them for the candidate only", flush=True)
    os.environ["ARENA_RECORD"] = "0"
    hashseed = env.pop("PYTHONHASHSEED", None)   # process-wide by nature: set before the workers start
    if hashseed is not None:
        os.environ["PYTHONHASHSEED"] = hashseed
    elif a.hashseed == "random":                 # set iteration order feeds float sums and tie-breaks in the policies
        os.environ.pop("PYTHONHASHSEED", None)
    else:
        os.environ["PYTHONHASHSEED"] = str(int(a.hashseed))
    os.environ["ARENA_RECORD_DIR"] = str(null_record_dir(out))
    if "llm_real" in opps:                       # one spend-cap run for all workers (local/llm_client.py)
        os.environ.setdefault("NEBIUS_RUN_ID", f"panel-{name}-s{a.seed}-{time.strftime('%Y%m%dT%H%M%S')}")

    done = set()
    if out.exists() and out.stat().st_size:
        if a.resume:
            for r in load_rows(out):
                if r.get("cand") == name and r.get("seed") == a.seed and "error" not in r:
                    done.add((r["opp"], r["g"]))
        elif not a.force:
            raise SystemExit(f"{out} exists: --resume to add the missing games, --force to overwrite")
    games = parse_only(a.only) if a.only else range(a.games)
    tasks = [(o, g) for g in games for o in opps if (o, g) not in done]
    code = tree_hash(cand)
    meta_path = Path(str(out) + ".meta.json")
    if a.resume and meta_path.exists():
        old = json.loads(meta_path.read_text())
        if old.get("code_hash") != code or old.get("env") != env:
            print(f"WARNING: resuming {out} with different code/env (was {old.get('code_hash')} {old.get('env')}, "
                  f"now {code} {env})", flush=True)
        if bool(old.get("same_team")) != bool(a.same_team):
            raise SystemExit(f"{out} was run with same_team={bool(old.get('same_team'))}: resume it the same way")
    logs_dir = str(out) + ".logs" if a.save_logs else None
    cfg = {"root": str(ROOT), "candidate_dir": str(cand), "name": name, "seed": a.seed, "nice": a.nice,
           "warmup": not a.no_warmup, "cand_env": env, "same_team": bool(a.same_team), "logs_dir": logs_dir}
    meta = {"name": name, "candidate_dir": str(cand), "git": git_rev(cand), "code_hash": code, "env": env,
            "shell_knobs_dropped": dropped, "opps": opps, "games": a.games, "seed": a.seed, "jobs": a.jobs,
            "nice": a.nice, "warmup": cfg["warmup"], "pythonhashseed": os.environ.get("PYTHONHASHSEED"),
            "same_team": bool(a.same_team), "logs_dir": logs_dir,
            "simbot_knobs": {k: v for k, v in os.environ.items() if k.startswith("SIMBOT_")},
            **({"vgcbench_knobs": {k: v for k, v in os.environ.items() if k.startswith("VGCBENCH_")}}
               if any(o in VGCB for o in opps) else {}),
            "panel_root": str(ROOT), "python": sys.executable, "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    print(f"panel: candidate {name} = {cand} (code {code}, git {meta['git']}), env {env or '{}'}\n"
          f"       opponents {', '.join(opps)}; {a.games} games each, seed {a.seed}; {len(tasks)} to play "
          f"({len(done)} already in {out}); {a.jobs} workers, nice +{a.nice}", flush=True)

    t0 = time.time()
    n_done = 0
    if tasks:
        ctx = mp.get_context("spawn")
        pool = ctx.Pool(a.jobs, initializer=_worker_init, initargs=(cfg,))
        try:
            info = pool.apply(_candidate_info, (opps,))
            meta["candidate"] = info
            if info.get("error"):
                raise SystemExit("candidate failed to load in a worker:\n" + info["error"])
            print(f"       harness {info.get('env_file')}, opponents {info.get('versions_dir')}\n"
                  f"       agent  {info.get('agent_file')}\n       policy {info.get('policy_file')}\n"
                  f"       calc   {info.get('calc_js')} enabled={info.get('calc_enabled')} ok={info.get('calc_ok')}",
                  flush=True)
            if not Path(info.get("agent_file") or "/").resolve().is_relative_to(cand / "agent"):
                raise SystemExit(f"candidate resolved to {info.get('agent_file')}, outside {cand}")
            for k in ("env_file", "versions_dir"):          # the harness and opponents must be this checkout's
                if not Path(info.get(k) or "/").resolve().is_relative_to(ROOT):
                    raise SystemExit(f"{k} resolved to {info.get(k)}, outside {ROOT}: run from the panel checkout "
                                     f"(cd {ROOT}; PYTHONPATH={ROOT})")
            if not info.get("calc_enabled") and not a.allow_no_calc:
                raise SystemExit("the candidate's damage calculator is unavailable (node / agent/js/node_modules "
                                 "missing?) — it would silently play the heuristic. --allow-no-calc to run anyway.")
            bad = {o: v for o, v in (info.get("opp_calc") or {}).items()
                   if v.get("error") or v.get("foreign") or not v.get("enabled")}
            for o, v in bad.items():
                print(f"       opponent {o}: calc {v}", flush=True)
            if bad and not a.allow_no_calc:
                raise SystemExit(f"opponent(s) {sorted(bad)} would not run their own damage calculator (link "
                                 f"<package>/agent/js/node_modules to agent/js/node_modules, see "
                                 f"scripts/setup_machine.sh). --allow-no-calc to run anyway.")
            meta_path.write_text(json.dumps(meta, indent=1))
            mode = "a" if done else "w"
            last = t0
            with open(out, mode) as fo:
                for row in pool.imap_unordered(_play, tasks, chunksize=1):
                    fo.write(json.dumps(row) + "\n")
                    fo.flush()
                    n_done += 1
                    if "error" in row:
                        print(f"  ERROR {row['opp']} g={row['g']}: {row['error']}", flush=True)
                    now = time.time()
                    if now - last >= a.progress or n_done == len(tasks):
                        last = now
                        rate = n_done / (now - t0)
                        eta = (len(tasks) - n_done) / rate
                        print(f"  {n_done}/{len(tasks)} games, {rate:.2f} games/s, eta {eta:.0f}s", flush=True)
            pool.close()
            pool.join()
        finally:
            pool.terminate()
    elapsed = time.time() - t0
    rows = latest([r for r in load_rows(out) if r.get("cand") == name and r.get("seed") == a.seed])
    s = summarize(rows, opps)
    meta.update(elapsed_s=round(elapsed, 1), games_played=n_done,
                games_per_s=round(n_done / elapsed, 3) if n_done and elapsed else None,
                finished=time.strftime("%Y-%m-%d %H:%M:%S"))
    meta_path.write_text(json.dumps(meta, indent=1))
    print(f"\n{name}: seed {a.seed}, {n_done} games in {elapsed:.0f}s"
          + (f" = {n_done / elapsed:.2f} games/s with {a.jobs} workers" if n_done and elapsed else ""))
    print_summary(s)
    if "llm_real" in opps:
        from .llm_client import LLMClient
        run_id = os.environ["NEBIUS_RUN_ID"]
        c = LLMClient.from_env(cache_path=None, run_id=run_id)
        spent = c.spent()
        c.close()
        print(f"llm_real spend (ledger, run {run_id}): {spent['run_calls']} requests, {spent['run_tokens']} tokens, "
              f"${spent['run_usd']:.4f}; ledger total ${spent['total_usd']:.4f} of ${c.budget_usd:.2f}")
    return finish(s, rows, a, out)


def latest(rows: list[dict]) -> list[dict]:
    """One row per (cand, opp, seed, g): a finished game beats a crashed attempt (--resume replays crashes)."""
    keep: dict = {}
    for r in rows:
        k = (r.get("cand"), r["opp"], r["seed"], r["g"])
        if k not in keep or "error" in keep[k]:
            keep[k] = r
    return list(keep.values())


def finish(s: dict, rows: list[dict], a, out: Path | None) -> int:
    """Comparison + verdict when asked; exit code 1 = REJECT, 2 = crashed games (no verdict asked), else 0."""
    res = {"summary": s}
    rc = 0
    if s.get("errors"):
        print(f"WARNING: {s['errors']} game(s) crashed; see the 'error'/'trace' fields in the JSONL")
        rc = 2
    if a.compare:
        base = load_rows(a.compare)
        c = compare(rows, base, s["opps"])
        print()
        print_compare(c, a.compare)
        v = verdict(s, c)
        why = unclean(s)
        if not c["opps"]:
            print(f"no games shared with {a.compare}: was it run with the same --seed and opponents?")
        print("verdict:", v + (f"  (candidate not clean: {'; '.join(why)})" if why else ""))
        res.update(compare=c, verdict=v, unclean=why, base=str(a.compare))
        rc = 1 if v == "REJECT" else rc
    if out is not None:
        Path(str(out) + ".summary.json").write_text(json.dumps(res, indent=1))
    if a.json:
        print(json.dumps(res))
    return rc


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidate-dir", help="directory holding the candidate's agent/ package (default: this checkout)")
    ap.add_argument("--name", help="label for this candidate (default: the directory name)")
    ap.add_argument("--env", action="append", metavar="KEY=VALUE", help="environment for the candidate only (repeatable)")
    ap.add_argument("--opps", default="all",
                    help=f"group (all, strength, field) or comma list (default all: {','.join(PANEL)})")
    ap.add_argument("--games", type=int, default=200, help="games per opponent (default 200)")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED,
                    help=f"match seed = seed * 100000 + g (default {DEFAULT_SEED}: not local.benchmark's 31 nor "
                         f"local.ladder's 7 / 11, so a promotion run plays games nobody tuned on)")
    ap.add_argument("--jobs", type=int, default=6, help="parallel worker processes (default 6)")
    ap.add_argument("--nice", type=int, default=10, help="niceness increment of the workers (default 10)")
    ap.add_argument("--out", help="per-game JSONL (default logs/panel/runs/<name>-s<seed>.jsonl)")
    ap.add_argument("--resume", action="store_true", help="keep --out and play only its missing games")
    ap.add_argument("--force", action="store_true", help="overwrite --out")
    ap.add_argument("--compare", metavar="BASE.jsonl", help="paired comparison + verdict against another run")
    ap.add_argument("--summary", nargs="+", metavar="RUN.jsonl", help="only summarise existing runs (no games)")
    ap.add_argument("--allow-no-calc", action="store_true",
                    help="run even if the candidate's or an opponent's own damage calculator is unavailable")
    ap.add_argument("--no-warmup", action="store_true", help="do not start damage calculators before the first game")
    ap.add_argument("--hashseed", default="0", help="PYTHONHASHSEED of the workers (default 0; 'random' = unset)")
    ap.add_argument("--progress", type=float, default=30.0, help="seconds between progress lines")
    ap.add_argument("--json", action="store_true", help="print the summary (and comparison) as one JSON line")
    ap.add_argument("--same-team", action="store_true",
                    help="both seats battle with the same six cards (random from the pool) instead of the snake draft")
    ap.add_argument("--save-logs", action="store_true",
                    help="write each game's Showdown protocol log (+ both sides' answers) to <out>.logs/<opp>-g<g>.json")
    ap.add_argument("--only", metavar="G,G-G",
                    help="play only these game indices (e.g. 12,18,30-33: replay example games with the same seeds)")
    a = ap.parse_args(argv)
    if a.summary:
        rc = 0
        for path in a.summary:
            rows = latest(load_rows(path))
            s = summarize(rows, list(PANEL) + [o for o in FIELD_ALL + EXT + VGCB if o not in PANEL])
            names = sorted({(r.get("cand"), r.get("seed")) for r in rows}, key=str)
            print_summary(s, f"== {path}  ({', '.join(f'{n} seed {sd}' for n, sd in names)})")
            rc = max(rc, finish(s, rows, a, None))
            print()
        sys.exit(rc)
    sys.exit(run(a))


if __name__ == "__main__":
    main()
