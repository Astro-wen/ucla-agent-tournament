"""Auditable, paired Werewolf benchmark; see docs/werewolf_benchmark.md.

This is local evidence, not permission to deploy. Each record is one independent
synthetic table; multiple seats on a table never inflate the statistical sample.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import inspect
import json
import logging
import math
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import time

# Set before loading either the policy or the simulator, including spawned workers.
os.environ["ARENA_RECORD"] = "0"
os.environ["ARENA_STATE"] = "0"
os.environ["ARENA_SUMMARY"] = "0"

from . import eval_werewolf as ev
from .werewolf_env import ContestantSeat, Game, deal_roles

DEVELOPMENT_SEEDS = tuple(range(151, 160))
HELDOUT_SEEDS = (28101, 28102)
SEED_REGISTRIES = {
    "evidence": (DEVELOPMENT_SEEDS, HELDOUT_SEEDS),
    # Registered before the guarding-strategy search; original confirmation seeds are already consumed.
    "guards": (tuple(range(161, 170)), (29301, 29302)),
}
DEFAULT_BACKGROUNDS = ("first", "random", "heuristic", "llmq", "rt_wolfpack", "rt_talker", "rt_detective", "rt_chaos")
VARIANTS = ("reference", "candidate", "wolf_only", "village_only")
LATENCY_BOUNDS_MS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 50, 100, 250, 500, 1000, 2000, 5000, 10000, 120000)
ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = ("agent", "champion", "local", "versions", "altruagent", "examples")


@dataclass(frozen=True)
class Scenario:
    seed: int
    game: int
    background: str
    k: int

    @property
    def key(self):
        return f"{self.seed}/{self.background}/k{self.k}/{self.game}"


@contextmanager
def environment(values):
    """Per-seat settings: candidate flags cannot contaminate frozen reference seats."""
    before = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class _Errors(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.count = 0

    def emit(self, record):
        self.count += 1


class MeasuredSeat(ContestantSeat):
    def __init__(self, factory, settings):
        with environment(settings):
            super().__init__(factory)
        self.settings = settings
        self.latency = Counter()
        self.paths = Counter()
        self.logged_errors = 0
        self.trace = []
        self.trace_enabled = False
        self.policy_fallbacks = 0
        self.policy_errors = 0
        self._instrumented_policy = None

    def reset(self):
        with environment(self.settings):
            super().reset()
        self._instrument_policy()
        fresh = getattr(self.agent, "_fresh", None)
        if callable(fresh):
            def audited_fresh(*args, **kwargs):
                result = fresh(*args, **kwargs)
                self._instrument_policy()
                return result
            self.agent._fresh = audited_fresh

    def _instrument_policy(self):
        """Count silent policy-level fallback/helper failures without changing their answers.

        act/chat intentionally catch exceptions, so a successful outer decision does
        not prove the strategy ran. Reattach if the Contestant refreshes its policy.
        Several helper wrappers may see one exception: these are diagnostic counters,
        not a count of distinct incidents. This cannot detect an arbitrary exception
        swallowed wholly inside a new helper that never signals fallback.
        """
        policy = getattr(self.agent, "ww", None)
        if policy is None or policy is self._instrumented_policy:
            return
        self._instrumented_policy = policy

        def audited(name, method):
            def call(*args, **kwargs):
                if name == "fallback":
                    self.policy_fallbacks += 1
                try:
                    result = method(*args, **kwargs)
                except Exception:
                    self.policy_errors += 1
                    raise
                if inspect.isgenerator(result):
                    def iterate():
                        try:
                            yield from result
                        except Exception:
                            self.policy_errors += 1
                            raise
                    return iterate()
                return result
            return call

        for name in dir(policy):
            if name.startswith("__") or name in ("act", "chat"):
                continue
            method = getattr(policy, name)
            if callable(method):
                setattr(policy, name, audited(name, method))

    def _call(self, raw, kind, fn):
        self._instrument_policy()
        errors = _Errors()
        logger = logging.getLogger()
        logger.addHandler(errors)
        started = time.perf_counter()
        try:
            with environment(self.settings):
                out = fn(raw)
            if kind == "action":
                self.paths[(getattr(self.agent, "last_decision", None) or {}).get("path", "unreported")] += 1
            if self.trace_enabled and len(self.trace) < 80:
                # Synthetic decisions only; intentionally omit observations, memory and reasoning.
                self.trace.append({"day": raw["game_state"]["day"], "phase": raw["game_state"]["phase"],
                                   "kind": kind, "choice": out if isinstance(out, (str, dict, type(None))) else "hold"})
            return out
        finally:
            ms = 1000 * (time.perf_counter() - started)
            bucket = next((str(b) for b in LATENCY_BOUNDS_MS if ms <= b), "over_120000")
            self.latency[bucket] += 1
            self.logged_errors += errors.count
            logger.removeHandler(errors)

    def choose(self, raw):
        return self._call(raw, "action", super().choose)

    def message(self, raw):
        return self._call(raw, "message", super().message)


def _uses_candidate(variant, role):
    return variant == "candidate" or (variant == "wolf_only" and role == "wolf") or (variant == "village_only" and role != "wolf")


def play_variant(scenario, variant, candidate, reference, candidate_env, trace=False):
    gseed, ours, types = ev.deal(scenario.seed, scenario.game, scenario.background, scenario.k)
    roles = deal_roles(random.Random(gseed))
    seats = []
    for i in range(7):
        if i not in ours:
            seats.append(ev.make_seat(types[i]))
            continue
        use = _uses_candidate(variant, roles[i])
        settings = candidate_env if use else {}
        with environment(settings):
            seat = MeasuredSeat(ev.factory(candidate if use else reference), settings)
        seat.trace_enabled = trace
        seats.append(seat)
    game = Game(seats, random.Random(gseed))
    returns = game.play()
    controlled = [seats[i] for i in ours]
    latency, paths = Counter(), Counter()
    for seat in controlled:
        latency.update(seat.latency)
        paths.update(seat.paths)
    failures = {key: sum(game.stats[key][i] for i in ours) for key in ev.FAIL_KEYS}
    failures.update({"fallbacks": sum(getattr(s.agent, "fallbacks", 0) for s in controlled),
                     "overruns": sum(getattr(s.agent, "overruns", 0) for s in controlled),
                     "policy_fallbacks": sum(s.policy_fallbacks for s in controlled),
                     "policy_errors": sum(s.policy_errors for s in controlled),
                     "listen_errors": sum(getattr(s.agent, "listen_stats", {}).get("errors", 0) for s in controlled),
                     "logged_errors": sum(s.logged_errors for s in controlled),
                     "fallback_paths": sum(n for p, n in paths.items() if p.split("+")[0] in ("fallback", "crash", "busy", "overrun"))})
    rec = {"scenario": asdict(scenario), "id": scenario.key, "variant": variant,
           "deal": {"seed": gseed, "seats": ours, "roles": [roles[i] for i in ours], "background": types},
           "winner": game.winner, "wins": [returns[i] > 0 for i in ours], "days": game.day,
           "failures": failures,
           "background_failures": {key: sum(game.stats[key][i] for i in types) for key in ev.FAIL_KEYS},
           "max_ms": max(game.stats["max_ms"][i] for i in ours), "latency_histogram": dict(latency),
           "decision_paths": dict(paths), "chats": sum(game.stats["chats"][i] for i in ours),
           "dms": sum(game.stats["dms"][i] for i in ours)}
    if trace:
        rec["synthetic_trace"] = {"full_roles": roles, "deaths": game.dead, "votes": game.votes,
                                  "messages": game.messages[:100],
                                  "decisions": {str(i): seats[i].trace for i in ours}}
    return rec


def play_pair(task):
    scenario, candidate, reference, candidate_env, settings, trace = task
    # k=1 role-isolated games equal the full candidate when our role matches.
    variants = VARIANTS[:2] if scenario.k == 1 else VARIANTS
    out = []
    with environment(settings | {"ARENA_RECORD": "0", "ARENA_STATE": "0"}):
        for variant in variants:
            try:
                out.append(play_variant(scenario, variant, candidate, reference, candidate_env, trace))
            except Exception as exc:
                out.append({"id": scenario.key, "scenario": asdict(scenario), "variant": variant,
                            "harness_error": f"{type(exc).__name__}: {str(exc)[:500]}"})
    return out


def exact_mcnemar(improved, worsened):
    """Two-sided exact binomial discordance test, stable even for thousands of pairs."""
    n = improved + worsened
    if not n:
        return 1.0
    k = min(improved, worsened)
    logs = [math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) - n * math.log(2)
            for i in range(k + 1)]
    peak = max(logs)
    return min(1.0, 2 * math.exp(peak) * sum(math.exp(x - peak) for x in logs))


def paired_stats(pairs):
    """Paired table-level estimates; binary roles get a conservative second interval.

    `ci95` is the usual paired normal interval. `conservative_ci95` subtracts
    Bonferroni 97.5% Wilson intervals for improved and worsened probabilities;
    unlike a zero-variance normal interval it does not assert perfect certainty
    from an unchanged small sample. Wilson coverage is approximate, not exact.
    """
    if not pairs:
        return {"n": 0, "candidate_rate": None, "reference_rate": None, "delta": None,
                "ci95": [None, None], "conservative_ci95": [None, None], "mcnemar_p": None}
    ds = [a - b for a, b in pairs]
    n = len(ds)
    mean = sum(ds) / n
    variance = sum((d - mean) ** 2 for d in ds) / max(1, n - 1)
    half = 1.96 * math.sqrt(variance / n)
    binary = all(a in (0, 1) and b in (0, 1) for a, b in pairs)
    improved, worsened = sum(d > 0 for d in ds), sum(d < 0 for d in ds)
    conservative = [None, None]
    if binary:
        a = ev.wilson(improved, n, z=2.2414027276)
        b = ev.wilson(worsened, n, z=2.2414027276)
        conservative = [a[0] - b[1], a[1] - b[0]]
    return {"n": n, "candidate_rate": sum(a for a, _ in pairs) / n,
            "reference_rate": sum(b for _, b in pairs) / n, "delta": mean,
            "ci95": [max(-1.0, mean - half), min(1.0, mean + half)],
            "conservative_ci95": conservative, "improved": improved, "worsened": worsened,
            "unchanged": n - improved - worsened,
            "mcnemar_p": exact_mcnemar(improved, worsened) if binary else None}


def _pair_outcomes(a, b, role):
    if role == "seat":
        return sum(a["wins"]) / len(a["wins"]), sum(b["wins"]) / len(b["wins"])
    indices = [i for i, r in enumerate(a["deal"]["roles"]) if r == role]
    if not indices:
        return None
    i = indices[0]  # same-side seats share an outcome; count the table once.
    return float(a["wins"][i]), float(b["wins"][i])


def summarize(records, expected, min_gain=0.02, margin=0.03, min_n=200):
    rows = defaultdict(dict)
    integrity = []
    for rec in records:
        key, variant = rec["id"], rec["variant"]
        if variant in rows[key]:
            integrity.append(f"duplicate {key}/{variant}")
        rows[key][variant] = rec
    expected_keys = {s.key for s in expected}
    if set(rows) != expected_keys:
        integrity.append("scenario set differs from the planned manifest")
    samples = defaultdict(list)
    failures = defaultdict(Counter)
    background_failures = defaultdict(Counter)
    latency = defaultdict(Counter)
    maxima = defaultdict(float)
    for scenario in expected:
        variants = VARIANTS[:2] if scenario.k == 1 else VARIANTS
        row = rows.get(scenario.key, {})
        if set(row) != set(variants):
            integrity.append(f"incomplete pair {scenario.key}")
            continue
        if any("harness_error" in r for r in row.values()):
            integrity.append(f"harness error {scenario.key}")
            continue
        ref = row["reference"]
        if any(r["deal"] != ref["deal"] for r in row.values()):
            integrity.append(f"mismatched roles/seats/background {scenario.key}")
            continue
        for variant, rec in row.items():
            failures[variant].update(rec["failures"])
            background_failures[variant].update(rec["background_failures"])
            latency[variant].update(rec["latency_histogram"])
            maxima[variant] = max(maxima[variant], rec["max_ms"])
        scopes = ("all", f"seed:{scenario.seed}", f"opponent:{scenario.background}", f"k:{scenario.k}",
                  f"opponent:{scenario.background}/k:{scenario.k}", f"seed:{scenario.seed}/k:{scenario.k}")
        for role in ("seat", *ev.ROLES):
            variant = "candidate" if scenario.k == 1 or role == "seat" else "wolf_only" if role == "wolf" else "village_only"
            pair = _pair_outcomes(row[variant], ref, role)
            if pair is None:
                continue
            for scope in scopes:
                samples[(scope, role)].append(pair)
    metrics = defaultdict(dict)
    for (scope, role), pairs in sorted(samples.items()):
        metrics[scope][role] = paired_stats(pairs)
    primary = metrics.get("k:1", {}).get("seat", paired_stats([]))
    gates = {"complete_pairs": not integrity,
             "primary_sample": primary["n"] >= min_n,
             "significant_primary_gain": primary["conservative_ci95"][0] is not None and primary["conservative_ci95"][0] > 0
                 and primary["mcnemar_p"] < 0.05,
             "substantial_primary_gain": primary["delta"] is not None and primary["delta"] >= min_gain,
             "zero_protocol_and_strategy_failures": all(not any(value for key, value in c.items() if key != "window_close")
                                                      for c in failures.values()),
             "zero_background_protocol_failures": all(not any(value for key, value in c.items() if key != "window_close")
                                                     for c in background_failures.values()),
             "max_decision_under_10s": bool(maxima) and all(ms <= 10000 for ms in maxima.values()),
             "p99_decision_under_5s": bool(latency) and all((histogram_percentile(h, 0.99) or float("inf")) <= 5000 for h in latency.values())}
    role_checks = {}
    for scope in ("all", "k:1"):
        for role in ev.ROLES:
            m = metrics.get(scope, {}).get(role, paired_stats([]))
            role_checks[f"{scope}/{role}"] = m["n"] >= min_n and m["conservative_ci95"][0] is not None and m["conservative_ci95"][0] > -margin
    gates["roles_noninferior"] = all(role_checks.values())
    regressions = []
    for scope, roles in metrics.items():
        if not scope.startswith("opponent:"):
            continue
        for role, result in roles.items():
            p = result["mcnemar_p"]
            if result["n"] >= 20 and result["delta"] < 0 and ((p is not None and p < 0.05) or result["ci95"][1] < 0):
                regressions.append({"scope": scope, "role": role, **result})
    gates["no_significant_opponent_regression"] = not regressions
    return {"metrics": dict(metrics), "primary": primary, "gates": gates, "role_checks": role_checks,
            "local_evidence_pass": all(gates.values()), "integrity_errors": integrity,
            "regressions": regressions, "failures": {k: dict(v) for k, v in failures.items()},
            "background_failures": {k: dict(v) for k, v in background_failures.items()},
            "discussion_window_closures": {variant: {"controlled": counts.get("window_close", 0),
                                                       "background": background_failures[variant].get("window_close", 0)}
                                           for variant, counts in failures.items()},
            "latency": {k: {"max_ms": maxima[k], "histogram": dict(v), "p99_upper_ms": histogram_percentile(v, 0.99)}
                        for k, v in latency.items()}}


def histogram_percentile(histogram, q):
    if not histogram or sum(histogram.values()) <= 0:
        return None
    target = math.ceil(sum(histogram.values()) * q)
    total = 0
    for bound in LATENCY_BOUNDS_MS:
        total += histogram.get(str(bound), 0)
        if total >= target:
            return bound
    return None


def source_manifest():
    files = set()
    for directory in SOURCE_ROOTS:
        files.update(ROOT.glob(f"{directory}/**/*.py"))
    for pattern in ("pyproject.toml", "requirements*.txt", "requirements*.lock", "uv.lock", "poetry.lock", "champion/VERSION"):
        files.update(ROOT.glob(pattern))
    files = sorted(files)
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    try:
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        revision = None
    return {"git_revision": revision, "digest": digest, "files": hashes}


def parse_candidate_env(items):
    out = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not re.fullmatch(r"ARENA_WW_[A-Z0-9_]+", key) or any(x in key for x in ("SECRET", "TOKEN", "KEY", "PASSWORD")):
            raise ValueError("--candidate-env requires a nonsecret ARENA_WW_* setting in NAME=value form")
        out[key] = value
    return out


def build_scenarios(seeds, backgrounds, ks, games):
    if games < 1 or not seeds or not backgrounds or not ks or any(k < 1 or k > 7 for k in ks):
        raise ValueError("positive games, seeds, backgrounds and k in 1..7 are required")
    if len(set(seeds)) != len(seeds) or len(set(backgrounds)) != len(backgrounds) or len(set(ks)) != len(ks):
        raise ValueError("duplicate seeds/backgrounds/k would duplicate evidence")
    for bg in backgrounds:
        ev.parse_bg(bg)
        if ev.uses_llm_real(bg):
            raise ValueError("real paid LLMs are intentionally excluded from this deterministic benchmark")
    return [Scenario(seed, g, bg, k) for seed in seeds for bg in backgrounds for k in ks
            if k != 7 or bg == backgrounds[0] for g in range(games)]


def print_report(summary):
    print("Paired LOCAL simulation; primary metric is one controlled seat (k=1).")
    for scope in ("k:1", "all"):
        for role, m in summary["metrics"].get(scope, {}).items():
            lo, hi = m["conservative_ci95"] if m["conservative_ci95"][0] is not None else m["ci95"]
            p = "n/a" if m["mcnemar_p"] is None else f"{m['mcnemar_p']:.5g}"
            print(f"{scope:6} {role:8} {m['candidate_rate']:.1%} vs {m['reference_rate']:.1%}; "
                  f"delta {100*m['delta']:+.2f} pp [{100*lo:+.2f}, {100*hi:+.2f}]; n={m['n']}; exact p={p}")
    print("Automatic discussion-window closures (reported separately from protocol/strategy errors): "
          + json.dumps(summary["discussion_window_closures"], sort_keys=True))
    print("Checks: " + ", ".join(f"{key}={'PASS' if value else 'FAIL'}" for key, value in summary["gates"].items()))
    print(f"Local evidence pass: {summary['local_evidence_pass']}; regressions: {len(summary['regressions'])}. "
          "Real-platform validation remains required.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", default="agent")
    parser.add_argument("--reference", default="champion")
    parser.add_argument("--candidate-env", action="append", default=[])
    parser.add_argument("--split", choices=("development", "heldout"), default="development")
    parser.add_argument("--seed-registry", choices=tuple(SEED_REGISTRIES), default="evidence")
    parser.add_argument("--seeds", nargs="+", type=int)
    parser.add_argument("--games", type=int, default=200, help="paired tables per seed/background/k")
    parser.add_argument("--backgrounds", default=",".join(DEFAULT_BACKGROUNDS))
    parser.add_argument("--ks", default="1,3")
    parser.add_argument("--timing", choices=("rounds", "rt"), default="rt")
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--trace-games", type=int, default=0, help="bounded synthetic trace for first N pairs total (max 10)")
    parser.add_argument("--min-gain", type=float, default=0.02)
    parser.add_argument("--margin", type=float, default=0.03)
    parser.add_argument("--min-n", type=int, default=200)
    parser.add_argument("--out", type=Path, required=True, help="new directory; refuses overwrite")
    args = parser.parse_args(argv)
    development_seeds, heldout_seeds = SEED_REGISTRIES[args.seed_registry]
    seeds = args.seeds or ([development_seeds[0]] if args.split == "development" else list(heldout_seeds))
    if args.split == "development" and any(s not in development_seeds for s in seeds):
        parser.error(f"development seeds are registered as {development_seeds}")
    if args.split == "heldout" and tuple(seeds) != heldout_seeds:
        parser.error(f"heldout must run the complete preregistered seeds {heldout_seeds}")
    if args.jobs < 1 or not 0 <= args.trace_games <= 10 or args.min_n < 1 or not 0 <= args.min_gain <= 1 or not 0 <= args.margin <= 1:
        parser.error("invalid jobs, trace count, minimum sample size or fraction")
    try:
        candidate_env = parse_candidate_env(args.candidate_env)
        scenarios = build_scenarios(seeds, args.backgrounds.split(","), [int(k) for k in args.ks.split(",")], args.games)
    except ValueError as exc:
        parser.error(str(exc))
    for spec in (args.candidate, args.reference):
        module = ev.AGENTS.get(spec, spec).split(":", 1)[0]
        if module.split(".")[0] not in SOURCE_ROOTS:
            parser.error(f"factory {spec!r} is outside source-manifest roots {SOURCE_ROOTS}")
    if args.out.resolve().parent == (ROOT / "logs").resolve():
        parser.error("use logs/werewolf_benchmarks/<run>, not logs/<run>: the real-recording replay scans logs/*/*.jsonl")
    if args.out.exists():
        parser.error("output directory already exists; use a fresh name to preserve prior evidence")
    args.out.mkdir(parents=True)
    settings = {"ARENA_WW_TIMING": args.timing, "ARENA_RECORD": "0", "ARENA_STATE": "0", "ARENA_SUMMARY": "0"}
    inherited = {key: value for key, value in os.environ.items()
                 if re.fullmatch(r"ARENA_WW_[A-Z0-9_]+", key) and not any(x in key for x in ("SECRET", "TOKEN", "KEY", "PASSWORD"))}
    inherited.update({key: os.environ[key] for key in ("ARENA_TC_WATCHDOG", "ARENA_REASONING") if key in os.environ})
    source = source_manifest()
    manifest = {"schema_version": 1, "config": {**vars(args), "out": str(args.out), "seeds": seeds},
                "candidate_env": candidate_env, "simulator_env": settings, "inherited_strategy_env": inherited, "source": source,
                "python": sys.version, "tables": len(scenarios),
                "planned_games": sum(2 if s.k == 1 else 4 for s in scenarios),
                "scope": "local simulator only; no real opponent win-rate claim", "started_unix": time.time()}
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    tasks = [(s, args.candidate, args.reference, candidate_env, settings, i < args.trace_games) for i, s in enumerate(scenarios)]
    records = []
    started = time.perf_counter()
    executor = ProcessPoolExecutor(args.jobs) if args.jobs > 1 else None
    try:
        runs = executor.map(play_pair, tasks, chunksize=10) if executor else map(play_pair, tasks)
        with (args.out / "games.jsonl").open("w") as log:
            for index, recs in enumerate(runs, 1):
                for rec in recs:
                    log.write(json.dumps(rec, sort_keys=True) + "\n")
                    records.append(rec)
                if index % 100 == 0:
                    log.flush()
                    print(f"{index}/{len(scenarios)} paired tables complete", file=sys.stderr, flush=True)
    finally:
        if executor:
            executor.shutdown()
    summary = summarize(records, scenarios, args.min_gain, args.margin, args.min_n)
    final_source = source_manifest()
    summary["gates"]["source_unchanged_during_run"] = source["digest"] == final_source["digest"]
    summary["local_evidence_pass"] = all(summary["gates"].values())
    summary["elapsed_s"] = time.perf_counter() - started
    summary["final_source"] = final_source
    summary["split"] = args.split
    summary["heldout_warning"] = "Once examined, these seeds are no longer fresh for a revised candidate. Register new seeds before further confirmation."
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print_report(summary)
    return summary


if __name__ == "__main__":
    main()
