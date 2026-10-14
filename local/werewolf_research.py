from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
import gzip
from itertools import islice
import json
import math
import os
from pathlib import Path
import random
import sys
import time

os.environ["ARENA_RECORD"] = "0"
os.environ["ARENA_STATE"] = "0"
os.environ["ARENA_SUMMARY"] = "0"

from . import eval_werewolf as ev
from . import werewolf_benchmark as wb
from .werewolf_env import Game
from .werewolf_research_metrics import compact_decision_trace, game_metrics


@dataclass
class ResearchGame(Game):
    strict_messaging: bool = True
    action_trace: list[dict] = field(default_factory=list)

    def state(self, seat, phase, sub, actions=None):
        raw = super().state(seat, phase, sub, actions)
        if self.strict_messaging and phase != "messaging":
            raw["new_messages"] = []
        return raw

    def _choose(self, seat, sub, legal):
        record = {"day": self.day, "seat": seat, "phase": sub, "legal": list(legal),
                  "alive": self.living(), "investigations_before": dict(self.invest) if self.roles[seat] == "seer" else {}}
        raw = self.state(seat, "moving", sub, legal)
        action = super()._choose(seat, sub, legal)
        record.update(compact_decision_trace(raw, str(action)))
        record["action"] = action
        if len(self.action_trace) >= 100:
            raise RuntimeError("unexpectedly long synthetic game")
        self.action_trace.append(record)
        return action


@dataclass
class PairCounts:
    n: int = 0
    candidate_wins: int = 0
    reference_wins: int = 0
    improved: int = 0
    worsened: int = 0

    def add(self, candidate, reference):
        if candidate not in (0, 1) or reference not in (0, 1):
            raise ValueError("single-seat binary outcomes required")
        self.n += 1
        self.candidate_wins += int(candidate)
        self.reference_wins += int(reference)
        self.improved += int(candidate > reference)
        self.worsened += int(candidate < reference)

    def report(self):
        if not self.n:
            return {"n": 0}
        n = self.n
        delta = (self.improved - self.worsened) / n
        variance = (self.improved + self.worsened - n * delta * delta) / max(1, n - 1)
        half = 1.96 * math.sqrt(max(0, variance) / n)
        a = ev.wilson(self.improved, n, 2.2414027276)
        b = ev.wilson(self.worsened, n, 2.2414027276)
        return {**asdict(self), "candidate_rate": self.candidate_wins / n, "reference_rate": self.reference_wins / n,
                "delta": delta, "ci95": [max(-1, delta - half), min(1, delta + half)],
                "conservative_ci95": [a[0] - b[1], a[1] - b[0]],
                "candidate_wilson_ci95": ev.wilson(self.candidate_wins, n),
                "reference_wilson_ci95": ev.wilson(self.reference_wins, n),
                "mcnemar_p": wb.exact_mcnemar(self.improved, self.worsened)}


def metric_ratios(value, prefix=""):
    if isinstance(value, dict) and "numerator" in value and "denominator" in value:
        return {prefix: [value["numerator"], value["denominator"]]}
    result = {}
    if isinstance(value, dict):
        for name, child in value.items():
            if name != "outcome":
                result.update(metric_ratios(child, f"{prefix}.{name}" if prefix else name))
    return result


def play_profile(scenario, label, factory, settings, strict, trace=False):
    gseed, ours, types = ev.deal(scenario.seed, scenario.game, scenario.background, 1)
    me = ours[0]
    controlled = wb.MeasuredSeat(ev.factory(factory), settings)
    seats = [controlled if i == me else ev.make_seat(types[i]) for i in range(7)]
    game = ResearchGame(seats, random.Random(gseed), strict_messaging=strict)
    returns = game.play()
    fail = {key: game.stats[key][me] for key in ev.FAIL_KEYS}
    fail.update(fallbacks=getattr(controlled.agent, "fallbacks", 0), overruns=getattr(controlled.agent, "overruns", 0),
                policy_fallbacks=controlled.policy_fallbacks, policy_errors=controlled.policy_errors,
                logged_errors=controlled.logged_errors,
                listen_errors=getattr(controlled.agent, "listen_stats", {}).get("errors", 0),
                fallback_paths=sum(v for k, v in controlled.paths.items() if k.split("+")[0] in ("fallback", "crash", "busy", "overrun")))
    result = {"id": scenario.key, "scenario": asdict(scenario), "profile": label, "seat": me, "role": game.roles[me],
              "deal_seed": gseed, "win": returns[me] > 0, "winner": game.winner, "days": game.day,
              "failures": fail, "background_failures": {k: sum(game.stats[k][s] for s in types) for k in ev.FAIL_KEYS},
              "max_ms": game.stats["max_ms"][me], "latency_histogram": dict(controlled.latency),
              "chats": game.stats["chats"][me], "dms": game.stats["dms"][me]}
    decision_traces = defaultdict(list)
    for row in game.action_trace:
        decision_traces[row["seat"]].append(row)
    result["diagnostics"] = metric_ratios(game_metrics(game, ours, decision_traces))
    if trace:
        result["trace"] = {"roles": game.roles, "votes": game.votes, "dead": game.dead,
                           "messages": game.messages[:100], "actions": game.action_trace}
    return result


def run_task(task):
    scenario, profiles, settings, strict, trace = task
    records = []
    with wb.environment(settings | {"ARENA_RECORD": "0", "ARENA_STATE": "0", "ARENA_SUMMARY": "0"}):
        for label, factory, profile_env in profiles:
            try:
                records.append(play_profile(scenario, label, factory, profile_env, strict, trace))
            except Exception as exc:
                records.append({"id": scenario.key, "scenario": asdict(scenario), "profile": label,
                                "harness_error": f"{type(exc).__name__}: {str(exc)[:300]}"})
    return records


def run_chunk(tasks):
    return [run_task(task) for task in tasks]


def bounded_outputs(pool, tasks, slots):
    remaining = iter(tasks)
    pending = deque()

    def submit():
        chunk = tuple(islice(remaining, 10))
        if chunk:
            pending.append(pool.submit(run_chunk, chunk))

    for _ in range(slots):
        submit()
    try:
        while pending:
            yield from pending.popleft().result()
            submit()
    finally:
        for future in pending:
            future.cancel()


def parse_profiles(items, environments=()):
    profiles = []
    for item in items:
        label, sep, factory = item.partition("=")
        if not sep or not label or not factory or label in {p[0] for p in profiles}:
            raise ValueError("profiles require distinct NAME=FACTORY labels")
        module = ev.AGENTS.get(factory, factory).split(":", 1)[0]
        if module.split(".")[0] not in wb.SOURCE_ROOTS:
            raise ValueError("profile factory outside archived source roots")
        profiles.append((label, factory, {}))
    if len(profiles) < 2 or profiles[0][0] != "reference":
        raise ValueError("first profile must be reference=FACTORY, followed by at least one candidate")
    by_name = {label: env for label, _, env in profiles}
    for item in environments:
        label, separator, setting = item.partition(":")
        if not separator or label not in by_name:
            raise ValueError("profile environment must reference a declared profile")
        by_name[label].update(wb.parse_candidate_env([setting]))
    return profiles


def summarize_batch(records, profile_names, counts, failures, background, latency, maxima):
    if [r["profile"] for r in records] != profile_names:
        raise ValueError("missing or reordered profile")
    if any("harness_error" in r for r in records):
        return False
    reference = records[0]
    signature = tuple(reference[k] for k in ("id", "seat", "role", "deal_seed"))
    for r in records:
        if tuple(r[k] for k in ("id", "seat", "role", "deal_seed")) != signature:
            raise ValueError("unpaired profile outcome")
        name = r["profile"]
        failures[name].update(r["failures"])
        background[name].update(r["background_failures"])
        latency[name].update(r["latency_histogram"])
        maxima[name] = max(maxima[name], r["max_ms"])
        if name == "reference":
            continue
        scopes = ("all", f"opponent:{r['scenario']['background']}", f"seed:{r['scenario']['seed']}", f"seat:{r['seat']}")
        for scope in scopes:
            for role in ("seat", r["role"]):
                counts[(name, scope, role)].add(r["win"], reference["win"])
    return True


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", action="append", required=True)
    parser.add_argument("--profile-env", action="append", default=[])
    parser.add_argument("--seed-registry", choices=tuple(wb.SEED_REGISTRIES), default="overnight")
    parser.add_argument("--split", choices=("development", "heldout"), default="development")
    parser.add_argument("--seeds", nargs="+", type=int)
    parser.add_argument("--games", type=int, default=100)
    parser.add_argument("--backgrounds", default=",".join(wb.DEFAULT_BACKGROUNDS))
    parser.add_argument("--timing", choices=("rt", "rounds"), default="rt")
    parser.add_argument("--visibility", choices=("strict", "legacy"), default="strict")
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--trace-games", type=int, default=0)
    parser.add_argument("--stop-at")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    development, heldout = wb.SEED_REGISTRIES[args.seed_registry]
    seeds = args.seeds or (list(heldout) if args.split == "heldout" else [development[0]])
    if args.split == "heldout" and tuple(seeds) != heldout or args.split == "development" and any(s not in development for s in seeds):
        parser.error("seeds must belong to the registered split; confirmation uses both reserved seeds")
    try:
        profiles = parse_profiles(args.profile, args.profile_env)
        scenarios = wb.build_scenarios(seeds, args.backgrounds.split(","), [1], args.games)
        stop_at = datetime.fromisoformat(args.stop_at).timestamp() if args.stop_at else None
    except ValueError as exc:
        parser.error(str(exc))
    if args.jobs < 1 or not 0 <= args.trace_games <= 10:
        parser.error("invalid worker or bounded trace count")
    if args.out.exists() or args.out.resolve().parent == (wb.ROOT / "logs").resolve():
        parser.error("use a NEW logs/werewolf_benchmarks/<run> directory; never overwrite evidence")
    args.out.mkdir(parents=True)
    source = wb.source_manifest()
    settings = {"ARENA_WW_TIMING": args.timing, "ARENA_WW_TACTICS": "off", "ARENA_WW_DM": "off"}
    names = [p[0] for p in profiles]
    manifest = {"config": vars(args) | {"out": str(args.out), "seeds": seeds}, "profiles": profiles,
                "source": source, "python": sys.version, "planned_scenarios": len(scenarios),
                "planned_games": len(scenarios) * len(profiles), "started": datetime.now().astimezone().isoformat(),
                "settings": settings, "scope": "local scripted single-seat exploration; not live strength"}
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    counts = defaultdict(PairCounts)
    diagnostics = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    failures, background, latency = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    maxima = defaultdict(float)
    tasks = [(s, profiles, settings, args.visibility == "strict", i < args.trace_games) for i, s in enumerate(scenarios)]
    completed = errors = 0
    started = time.monotonic()
    pool = ProcessPoolExecutor(args.jobs) if args.jobs > 1 else None
    outputs = bounded_outputs(pool, tasks, args.jobs * 2) if pool else map(run_task, tasks)
    try:
        with gzip.open(args.out / "games.jsonl.gz", "wt", compresslevel=3) as stream:
            for records in outputs:
                for record in records:
                    stream.write(json.dumps(record, separators=(",", ":")) + "\n")
                    if "harness_error" not in record:
                        for role in ("all", record["role"]):
                            group = f"{record['profile']}/{record['scenario']['background']}/{role}"
                            for metric, (numerator, denominator) in record.get("diagnostics", {}).items():
                                diagnostics[group][metric][0] += numerator
                                diagnostics[group][metric][1] += denominator
                if not summarize_batch(records, names, counts, failures, background, latency, maxima):
                    errors += 1
                completed += 1
                if completed % 100 == 0:
                    stream.flush()
                    print(f"{completed}/{len(scenarios)} scenarios; {datetime.now().astimezone().isoformat()}", file=sys.stderr, flush=True)
                if stop_at and time.time() >= stop_at:
                    break
    finally:
        if hasattr(outputs, "close"):
            outputs.close()
        if pool:
            pool.shutdown(wait=True, cancel_futures=True)
    metrics = defaultdict(lambda: defaultdict(dict))
    for (profile, scope, role), counter in counts.items():
        metrics[profile][scope][role] = counter.report()
    end_source = wb.source_manifest()
    gates = {"complete": completed == len(scenarios) and errors == 0,
             "source_unchanged": source["digest"] == end_source["digest"],
             "no_protocol_or_policy_failures": all(not any(v for k, v in c.items() if k != "window_close") for c in failures.values()),
             "no_background_failures": all(not any(v for k, v in c.items() if k != "window_close") for c in background.values())}
    summary = {"metrics": metrics, "diagnostic_totals": diagnostics, "gates": gates, "completed_scenarios": completed, "games": completed * len(profiles),
               "harness_errors": errors, "failures": failures, "background_failures": background,
               "latency": {p: {"max_ms": maxima[p], "p99_upper_ms": wb.histogram_percentile(h, .99)} for p, h in latency.items()},
               "elapsed_s": time.monotonic() - started, "ended": datetime.now().astimezone().isoformat(),
               "source_digest_end": end_source["digest"], "promotion": "not assessed; unchanged benchmark gates and live validation still required"}
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    for profile, results in metrics.items():
        print(profile, json.dumps(results["all"]["seat"], sort_keys=True), flush=True)
    print("integrity", gates, flush=True)
    return summary


if __name__ == "__main__":
    main()
