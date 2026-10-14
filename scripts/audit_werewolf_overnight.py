"""Read-only independent audit of the frozen 33101/33102 Werewolf confirmation.

No simulator or policy imports; no simulations, network requests, or secret reads.
Only --out is written (exclusive creation). Missing/invalid evidence never passes.
The statistics, deterministic deal reconstruction, and 2x2 contrasts below are
implemented here rather than calling benchmark/research summary functions.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime
from fractions import Fraction
from functools import lru_cache
import gzip
import hashlib
import json
import math
from pathlib import Path
import random
import signal
import sys
import time
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "overnight-confirm-33101-33102-"
BACKGROUNDS = tuple((
    "first,random,heuristic,llm,llmx,llmq,mixed,rt_wolfpack,rt_talker,"
    "rt_detective,rt_chaos,guard_silent,guard_abstain,guard_fixed,guard_echo,"
    "guard_fake,guard_contradict,first_high_night,first_random_night,first_switch,"
    "pop_coordinated,pop_prompt_seer,pop_late_claim,pop_pressure_bus,pop_distance,"
    "pop_observed_switch,pop_survivor_kill,pop_anti_majority"
).split(","))
SEEDS = (33101, 33102)
ROLES = ("seat", "villager", "seer", "wolf")
PROFILES = ("reference", "night", "listen", "combined")
FACTORIES = ("agent", "local.werewolf_factorial_research:f16",
             "local.werewolf_factorial_research:f32", "local.werewolf_factorial_research:f48")
VARIANTS = ("reference", "candidate", "wolf_only", "village_only")
COMPARISONS = (("night", "reference"), ("listen", "reference"), ("combined", "reference"),
               ("combined", "night"), ("combined", "listen"))
PANELS = {"simple7": ("first", "random", "heuristic", "guard_silent", "guard_abstain", "guard_fixed", "guard_echo"),
          "new_population8": tuple(b for b in BACKGROUNDS if b.startswith("pop_")), "all28": BACKGROUNDS}
RUNS = {"rt-strict": ("rt", "strict", 500, 1), "rounds-strict": ("rounds", "strict", 500, 1),
        "rt-legacy": ("rt", "legacy", 200, 1), "k3-legacy": ("rt", "legacy", 250, 3)}
SOURCE_ROOTS = ("agent", "champion", "local", "versions", "altruagent", "examples")
SOURCE_PATTERNS = ("pyproject.toml", "requirements*.txt", "requirements*.lock", "uv.lock", "poetry.lock",
                   "champion/VERSION", "data/werewolf_learned_likelihoods.json")
FAIL_KEYS = {"crash", "illegal", "bad_chat", "timeout", "chat_after_terminate", "window_close"}
INTERNAL_KEYS = {"fallbacks", "overruns", "policy_fallbacks", "policy_errors", "listen_errors", "logged_errors", "fallback_paths"}
FAMILY_SIZE = 2 * 3 * 28 * 4
DECLARED_AT = "2026-10-13T06:14:00-07:00"


def now():
    return datetime.now().astimezone().isoformat()


@contextmanager
def bounded_io(seconds=180):
    """Interrupt a stuck public-file read; retain error and never change OS settings."""
    def expire(signum, frame):
        raise TimeoutError(f"public-file operation exceeded {seconds}s")
    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class PublicIO:
    def __init__(self, deadline, attempts=2):
        self.deadline = deadline
        self.attempts = attempts
        self.errors = []

    def run(self, label, function, seconds=180):
        for attempt in range(1, self.attempts + 1):
            try:
                remaining = self.deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError("audit deadline reached")
                with bounded_io(min(seconds, max(1, remaining))):
                    return function()
            except (OSError, EOFError, UnicodeError, json.JSONDecodeError, zlib.error) as exc:
                self.errors.append({"operation": label, "attempt": attempt, "time": now(),
                                    "error": f"{type(exc).__name__}: {exc}"})
                if attempt == self.attempts or time.time() + 30 >= self.deadline:
                    raise
                print(f"I/O retry in 30s: {label}: {exc}", flush=True)
                time.sleep(30)

    def json(self, path):
        return self.run(str(path), lambda: json.loads(path.read_text()))

    def sha(self, path):
        return self.run(f"SHA256 {path}", lambda: sha256(path))


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_digest(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def wilson(successes, n, z=1.96):
    if not n:
        return [None, None]
    # Count-scale formula, deliberately independent of ev.wilson's implementation.
    divisor = n + z * z
    center = (successes + z * z / 2) / divisor
    radius = z * math.sqrt(successes * (n - successes) / n + z * z / 4) / divisor
    return [center - radius, center + radius]


@lru_cache(maxsize=None)
def exact_discordance(improved, worsened):
    """Exact integer binomial tail, rounded to float only for JSON presentation."""
    if min(improved, worsened) < 0 or any(type(x) is not int for x in (improved, worsened)):
        raise ValueError("nonnegative integer discordances required")
    n, k = improved + worsened, min(improved, worsened)
    coefficient = tail = 1
    for j in range(1, k + 1):
        coefficient = coefficient * (n - j + 1) // j
        tail += coefficient
    return float(min(Fraction(1), Fraction(2 * tail, 1 << n)))


class Moments:
    def __init__(self, duplicated=frozenset()):
        self.n = 0
        self.total = self.square = 0.0
        self.duplicated = duplicated
        self.repeat = {}

    def add(self, value, cluster):
        self.n += 1
        self.total += value
        self.square += value * value
        if cluster in self.duplicated:
            row = self.repeat.setdefault(cluster, [0, 0.0, 0.0])
            row[0] += 1
            row[1] += value
            row[2] += value * value

    def report(self):
        n = self.n
        if not n:
            return {"n": 0, "clusters": 0, "mean": None, "se": None, "ci95": [None, None]}
        mean = self.total / n
        residual = max(0.0, self.square - self.total * self.total / n)
        clusters = n
        for count, total, square in self.repeat.values():
            clusters -= count - 1
            residual += (total - count * mean) ** 2 - (square - 2 * mean * total + count * mean * mean)
        se = math.sqrt(max(0.0, residual) * clusters / (clusters - 1)) / n if clusters > 1 else None
        return {"n": n, "clusters": clusters, "mean": mean, "se": se,
                "ci95": [mean - 1.96 * se, mean + 1.96 * se] if se is not None else [None, None],
                "cluster_unit": "deal_seed", "duplicate_observations": n - clusters}


class Pair:
    def __init__(self, duplicated=frozenset()):
        self.moments = Moments(duplicated)
        self.candidate = self.reference = 0.0
        self.improved = self.worsened = 0
        self.binary = True

    def add(self, candidate, reference, cluster):
        self.candidate += candidate
        self.reference += reference
        self.improved += candidate > reference
        self.worsened += candidate < reference
        self.binary &= candidate in (0, 1) and reference in (0, 1)
        self.moments.add(candidate - reference, cluster)

    def report(self):
        m, n = self.moments, self.moments.n
        if not n:
            return {"n": 0}
        delta = m.total / n
        variance = max(0.0, m.square - n * delta * delta) / max(1, n - 1)
        half = 1.96 * math.sqrt(variance / n)
        a, b = wilson(self.improved, n, 2.2414027276), wilson(self.worsened, n, 2.2414027276)
        report = {"n": n, "candidate_rate": self.candidate / n, "reference_rate": self.reference / n,
                  "delta": delta, "ci95": [max(-1.0, delta - half), min(1.0, delta + half)],
                  "conservative_ci95": [a[0] - b[1], a[1] - b[0]] if self.binary else [None, None],
                  "improved": self.improved, "worsened": self.worsened,
                  "unchanged": n - self.improved - self.worsened,
                  "mcnemar_p": exact_discordance(self.improved, self.worsened) if self.binary else None,
                  "binary": self.binary, "clustered": m.report()}
        if self.binary:
            report.update(candidate_wins=int(self.candidate), reference_wins=int(self.reference),
                          candidate_wilson_ci95=wilson(self.candidate, n), reference_wilson_ci95=wilson(self.reference, n))
        return report


def contrasts(reference, night, listen, combined):
    return {"night_main": (night - reference + combined - listen) / 2,
            "listen_main": (listen - reference + combined - night) / 2,
            "interaction_difference_in_differences": combined - night - listen + reference}


def independent_deal(seed, game, background, k):
    if background not in BACKGROUNDS:
        raise ValueError("this audit only supports the predeclared direct backgrounds")
    rng = random.Random(f"deal:{seed}:{game}:{background}:{k}")
    ours = sorted(rng.sample(range(7), k))
    types = {str(i): rng.choice(("first", "heuristic", "random", "llm")) if background == "mixed" else background
             for i in range(7) if i not in ours}
    deal_seed = rng.getrandbits(62)
    roles = ["wolf", "wolf", "seer", "villager", "villager", "villager", "villager"]
    random.Random(deal_seed).shuffle(roles)
    return {"seed": deal_seed, "seats": ours, "roles": [roles[i] for i in ours], "background": types}


class Checks:
    def __init__(self):
        self.count = 0
        self.failures = []
        self.numeric_fields = 0
        self.metric_slices = 0
        self.max_abs_error = 0.0
        self.max_relative_error = 0.0

    def require(self, condition, label, details=None):
        self.count += 1
        if not condition:
            self.failures.append({"check": label, "details": details})

    def compare(self, actual, reported, path):
        self.count += 1
        if isinstance(reported, dict):
            if not isinstance(actual, dict):
                self.failures.append({"check": path, "actual_type": str(type(actual)), "reported_type": "dict"})
                return
            for key, value in reported.items():
                if key not in actual:
                    self.failures.append({"check": f"{path}/{key}", "error": "reported field not recomputed"})
                else:
                    self.compare(actual[key], value, f"{path}/{key}")
        elif isinstance(reported, list):
            if not isinstance(actual, (list, tuple)) or len(actual) != len(reported):
                self.failures.append({"check": path, "actual": actual, "reported": reported})
            else:
                for i, (a, b) in enumerate(zip(actual, reported)):
                    self.compare(a, b, f"{path}/{i}")
        elif type(reported) in (int, float) and type(actual) in (int, float):
            self.numeric_fields += 1
            error = abs(actual - reported)
            relative = error / max(abs(actual), abs(reported)) if actual or reported else 0
            self.max_abs_error = max(self.max_abs_error, error)
            self.max_relative_error = max(self.max_relative_error, relative)
            # Integer fields must be exact; float tolerances permit lgamma-vs-integer p rounding only.
            matches = actual == reported if type(reported) is int else math.isclose(actual, reported, rel_tol=2e-9, abs_tol=2e-12)
            if not matches:
                self.failures.append({"check": path, "actual": actual, "reported": reported})
        elif actual != reported:
            self.failures.append({"check": path, "actual": actual, "reported": reported})

    def report(self):
        return {"passed": not self.failures, "assertions": self.count, "numeric_fields_compared": self.numeric_fields,
                "reported_metric_slices_compared": self.metric_slices, "max_absolute_numeric_difference": self.max_abs_error,
                "max_relative_numeric_difference": self.max_relative_error, "failures": self.failures,
                "float_tolerance": {"relative": 2e-9, "absolute": 2e-12}, "integer_counts_require_exact_equality": True}


def scopes_for(scenario, seats, k):
    bg, seed = scenario["background"], scenario["seed"]
    scopes = ["all", f"opponent:{bg}", f"seed:{seed}"]
    if k == 1:
        scopes.append(f"seat:{seats[0]}")
    else:
        scopes.extend((f"k:{k}", f"opponent:{bg}/k:{k}", f"seed:{seed}/k:{k}"))
    scopes.extend(f"panel:{name}" for name, members in PANELS.items() if bg in members)
    return scopes


def histogram_percentile(histogram, q=0.99):
    target = math.ceil(sum(histogram.values()) * q)
    total = 0
    for bound, value in sorted(histogram.items(), key=lambda item: float(item[0])):
        total += value
        if total >= target and target > 0:
            return float(bound)
    return None


def expected_plan(games, k):
    return {f"{seed}/{bg}/k{k}/{game}": {"seed": seed, "background": bg, "k": k, "game": game}
            for seed in SEEDS for bg in BACKGROUNDS for game in range(games)}


def command_for(name, directory, python):
    timing, visibility, games, k = RUNS[name]
    if k == 1:
        cmd = [python, "-m", "local.werewolf_research", "--split", "heldout"]
        for label, factory in zip(PROFILES, FACTORIES):
            cmd.extend(("--profile", f"{label}={factory}"))
        cmd += ["--games", str(games), "--backgrounds", ",".join(BACKGROUNDS), "--timing", timing,
                "--visibility", visibility, "--jobs", "3", "--trace-games", "1", "--out", str(directory)]
    else:
        cmd = [python, "-m", "local.werewolf_benchmark", "--seed-registry", "overnight", "--split", "heldout",
               "--games", "250", "--ks", "3", "--jobs", "3", "--candidate", FACTORIES[-1], "--reference", "agent",
               "--backgrounds", ",".join(BACKGROUNDS), "--timing", "rt", "--out", str(directory)]
    return cmd


def audit_run(root, name, manifest, summary):
    """Stream raw records; detailed observations/traces are never copied into the report."""
    timing, visibility, games, k = RUNS[name]
    directory = root / "logs/werewolf_benchmarks" / (PREFIX + name)
    path = directory / ("games.jsonl.gz" if k == 1 else "games.jsonl")
    checks = Checks()
    config = manifest["config"]
    expected_config = {"seeds": list(SEEDS), "seed_registry": "overnight", "split": "heldout", "games": games,
                       "backgrounds": ",".join(BACKGROUNDS), "timing": timing, "jobs": 3, "out": str(directory)}
    if k == 1:
        expected_config.update(visibility=visibility, profile=[f"{a}={b}" for a, b in zip(PROFILES, FACTORIES)],
                               profile_env=[], trace_games=1, stop_at=None)
        checks.compare(manifest["profiles"], [[a, b, {}] for a, b in zip(PROFILES, FACTORIES)], "profiles")
        checks.compare(manifest["settings"], {"ARENA_WW_TIMING": timing, "ARENA_WW_TACTICS": "off", "ARENA_WW_DM": "off"}, "settings")
    else:
        expected_config.update(ks="3", candidate=FACTORIES[-1], reference="agent", candidate_env=[], trace_games=0,
                               min_gain=0.02, margin=0.03, min_n=200)
        checks.compare(manifest["candidate_env"], {}, "candidate_env")
        checks.require(not manifest["candidate_env"], "no candidate env overrides")
        checks.compare(manifest["simulator_env"], {"ARENA_WW_TIMING": "rt", "ARENA_RECORD": "0", "ARENA_STATE": "0", "ARENA_SUMMARY": "0"}, "simulator_env")
        inherited = manifest.get("inherited_strategy_env", {})
        for key in ("ARENA_WW_TACTICS", "ARENA_WW_DM"):
            checks.require(inherited.get(key, "off") == "off", f"k3 frozen default {key}", inherited.get(key))
    checks.compare(config, expected_config, "config")
    expected = expected_plan(games, k)
    deals = {key: independent_deal(s["seed"], s["game"], s["background"], k) for key, s in expected.items()}
    frequencies = Counter(d["seed"] for d in deals.values())
    duplicated = {seed for seed, count in frequencies.items() if count > 1}
    checks.compare(manifest["planned_games"], len(expected) * 4, "planned_games")
    checks.compare(manifest["planned_scenarios"] if k == 1 else manifest["tables"], len(expected), "planned_scenarios")
    labels = PROFILES if k == 1 else VARIANTS
    label_key = "profile" if k == 1 else "variant"
    pair_counts = defaultdict(lambda: Pair(duplicated))
    factorial = defaultdict(lambda: Moments(duplicated))
    absolute = defaultdict(lambda: [0, 0])
    failures, background, latency = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    maxima = defaultdict(float)
    diagnostics = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    raw_counts, role_counts, seat_counts, seed_counts, bg_counts = Counter(), Counter(), Counter(), Counter(), Counter()
    seen, completed = set(), set()
    raw_lines = harness_errors = 0
    batch = []

    def consume(rows):
        nonlocal harness_errors
        key = rows[0].get("id")
        checks.require(len(rows) == 4 and [r.get(label_key) for r in rows] == list(labels), "four ordered cells", key)
        checks.require(key not in completed, "scenario not repeated", key)
        checks.require(key in expected, "scenario in frozen plan", key)
        if key in completed or key not in expected or len(rows) != 4 or [r.get(label_key) for r in rows] != list(labels):
            return
        completed.add(key)
        scenario, deal = expected[key], deals[key]
        valid = True
        for row in rows:
            label = row.get(label_key)
            checks.compare(row.get("scenario"), scenario, f"{key}/{label}/scenario")
            checks.require(row.get("id") == key, "profile id match", [key, label])
            if "harness_error" in row:
                harness_errors += 1
                checks.require(False, "harness_error", {"id": key, "label": label, "error": row["harness_error"]})
                valid = False
                continue
            if k == 1:
                checks.compare({"seed": row.get("deal_seed"), "seats": [row.get("seat")], "roles": [row.get("role")]},
                               {a: deal[a] for a in ("seed", "seats", "roles")}, f"{key}/{label}/deal")
                wins = [row.get("win")]
            else:
                checks.compare(row.get("deal"), deal, f"{key}/{label}/deal")
                wins = row.get("wins", [])
            checks.require(len(wins) == k and all(type(w) is bool for w in wins), "boolean wins for each seat", [key, label])
            checks.require(row.get("winner") in ("wolf", "villager"), "valid terminal winner", [key, label])
            expected_wins = [("wolf" if role == "wolf" else "villager") == row.get("winner") for role in deal["roles"]]
            checks.compare(wins, expected_wins, f"{key}/{label}/wins_vs_winner")
            checks.require(type(row.get("days")) is int and row["days"] > 0, "positive terminal day", [key, label])
            for field, required in (("failures", FAIL_KEYS | INTERNAL_KEYS), ("background_failures", FAIL_KEYS)):
                counts = row.get(field, {})
                checks.require(required <= set(counts), "complete failure instrumentation", [key, label, field, sorted(required - set(counts))])
                checks.require(all(type(v) is int and v >= 0 for v in counts.values()), "nonnegative failure counts", [key, label, field])
            failures[label].update(row["failures"])
            background[label].update(row["background_failures"])
            hist = row["latency_histogram"]
            checks.require(bool(hist) and all(type(v) is int and v >= 0 for v in hist.values()), "latency counts", [key, label])
            checks.require(math.isfinite(row["max_ms"]) and row["max_ms"] >= 0, "finite nonnegative max latency", [key, label])
            latency[label].update(hist)
            maxima[label] = max(maxima[label], row["max_ms"])
            if k == 1:
                for role in ("all", row["role"]):
                    group = f"{label}/{scenario['background']}/{role}"
                    for metric, (numerator, denominator) in row.get("diagnostics", {}).items():
                        diagnostics[group][metric][0] += numerator
                        diagnostics[group][metric][1] += denominator
        if not valid:
            return
        role_counts.update(deal["roles"])
        seat_counts.update(str(seat) for seat in deal["seats"])
        seed_counts[str(scenario["seed"])] += 1
        bg_counts[scenario["background"]] += 1
        scopes = scopes_for(scenario, deal["seats"], k)
        by_label = dict(zip(labels, rows))
        if k == 1:
            role = deal["roles"][0]
            for scope in scopes:
                for r in ("seat", role):
                    for label, row in by_label.items():
                        absolute[(label, scope, r)][0] += row["win"]
                        absolute[(label, scope, r)][1] += 1
                    for candidate, reference in COMPARISONS:
                        pair_counts[(f"{candidate}-{reference}", scope, r)].add(int(by_label[candidate]["win"]), int(by_label[reference]["win"]), deal["seed"])
                    values = [int(by_label[label]["win"]) for label in PROFILES]
                    for effect, value in contrasts(*values).items():
                        factorial[(scope, r, effect)].add(value, deal["seed"])
        else:
            ref = by_label["reference"]
            for r in ROLES:
                indices = list(range(k)) if r == "seat" else [i for i, role in enumerate(deal["roles"]) if role == r][:1]
                if not indices:
                    continue
                variant = "candidate" if r == "seat" else "wolf_only" if r == "wolf" else "village_only"
                for scope in scopes:
                    candidate = sum(by_label[variant]["wins"][i] for i in indices) / len(indices)
                    reference = sum(ref["wins"][i] for i in indices) / len(indices)
                    pair_counts[("role_isolated", scope, r)].add(candidate, reference, deal["seed"])
                    # Explicitly retain all side variants as diagnostics, never substitute them for isolated metrics.
                    for label in VARIANTS[1:]:
                        value = sum(by_label[label]["wins"][i] for i in indices) / len(indices)
                        pair_counts[(f"{label}-reference", scope, r)].add(value, reference, deal["seed"])
                    for label, row in by_label.items():
                        absolute[(label, scope, r)][0] += sum(row["wins"][i] for i in indices) / len(indices)
                        absolute[(label, scope, r)][1] += 1
            # Each controlled seat gets a separate descriptive slice, but never three independent tables.
            for i, seat in enumerate(deal["seats"]):
                role = deal["roles"][i]
                variant = "wolf_only" if role == "wolf" else "village_only"
                pair_counts[("role_isolated", f"seat:{seat}", role)].add(int(by_label[variant]["wins"][i]), int(ref["wins"][i]), deal["seed"])

    opener = gzip.open if k == 1 else open
    with opener(path, "rt") as stream:
        for line in stream:
            raw_lines += 1
            row = json.loads(line)
            identity = (row.get("id"), row.get(label_key))
            checks.require(identity not in seen, "raw cell not duplicated", identity)
            seen.add(identity)
            raw_counts[str(row.get(label_key))] += 1
            batch.append(row)
            if len(batch) == 4:
                consume(batch)
                batch = []
        if batch:
            consume(batch)
    checks.require(completed == set(expected), "all planned scenarios exactly once", {"missing": sorted(set(expected) - completed), "extra": sorted(completed - set(expected))})
    checks.compare(raw_lines, len(expected) * 4, "raw record count")
    checks.compare(dict(raw_counts), {label: len(expected) for label in labels}, "per-profile raw count")
    metrics = defaultdict(lambda: defaultdict(dict))
    for (comparison, scope, role), pair in sorted(pair_counts.items()):
        value = pair.report()
        if k == 1 and visibility == "strict" and comparison in ("night-reference", "listen-reference", "combined-reference") and scope.startswith("opponent:"):
            value["bonferroni_family_size"] = FAMILY_SIZE
            value["bonferroni_p"] = min(1.0, value["mcnemar_p"] * FAMILY_SIZE)
            value["family_reject_005"] = value["bonferroni_p"] < 0.05
        metrics[comparison][scope][role] = value
    if k == 1:
        recomputed_reported = {label: metrics[f"{label}-reference"] for label in PROFILES[1:]}
    else:
        recomputed_reported = metrics["role_isolated"]
    reported_metrics = summary["metrics"]
    checks.compare(recomputed_reported, reported_metrics, "summary/metrics")
    # Require the complete reported schema as well as comparing every reported value.
    required_scopes = {"all", *(f"opponent:{bg}" for bg in BACKGROUNDS), *(f"seed:{s}" for s in SEEDS)}
    if k == 1:
        required_scopes.update(f"seat:{s}" for s in range(7))
        checks.require(set(reported_metrics) == set(PROFILES[1:]), "all reported candidate profiles present")
    else:
        required_scopes.update({"k:3", *(f"opponent:{bg}/k:3" for bg in BACKGROUNDS), *(f"seed:{s}/k:3" for s in SEEDS)})
    required_fields = {"n", "candidate_rate", "reference_rate", "delta", "ci95", "conservative_ci95", "improved", "worsened", "mcnemar_p"}
    if k == 1:
        required_fields.update(("candidate_wins", "reference_wins", "candidate_wilson_ci95", "reference_wilson_ci95"))
    else:
        required_fields.add("unchanged")
    for comparisons in ([reported_metrics[label] for label in PROFILES[1:]] if k == 1 else [reported_metrics]):
        checks.require(set(comparisons) == required_scopes, "complete reported scope grid", sorted(set(comparisons) ^ required_scopes))
        for scope, roles in comparisons.items():
            checks.require(set(roles) == set(ROLES), "complete reported role grid", scope)
            for role, value in roles.items():
                checks.require(set(value) == required_fields, "complete reported statistic fields", [scope, role, sorted(set(value) ^ required_fields)])
        checks.metric_slices += sum(len(roles) for roles in comparisons.values())
    for field in ("failures", "background_failures", "latency"):
        checks.require(set(summary[field]) == set(labels), "all reported instrumented profiles present", field)
    for label in labels:
        checks.require(set(summary["failures"][label]) == set(failures[label]), "all reported failure counters present", label)
        checks.require(set(summary["background_failures"][label]) == set(background[label]), "all reported background counters present", label)
    checks.compare(dict(failures), summary["failures"], "summary/failures")
    checks.compare(dict(background), summary["background_failures"], "summary/background_failures")
    latency_report = {label: {"max_ms": maxima[label], "histogram": dict(latency[label]), "p99_upper_ms": histogram_percentile(latency[label])} for label in labels}
    checks.compare(latency_report, summary["latency"], "summary/latency")
    end_digest = summary["source_digest_end"] if k == 1 else summary["final_source"]["digest"]
    checks.compare(manifest["source"]["digest"], manifest_digest(manifest["source"]["files"]), "source begin manifest digest")
    checks.require(manifest["source"]["digest"] == end_digest, "reported source begin/end equality")
    if k == 1:
        checks.compare(dict(diagnostics), summary["diagnostic_totals"], "summary/diagnostic_totals")
        checks.require(set(diagnostics) == set(summary["diagnostic_totals"]), "all reported diagnostic groups present")
        for group, values in diagnostics.items():
            checks.require(set(values) == set(summary["diagnostic_totals"].get(group, {})), "all reported diagnostic metrics present", group)
        checks.compare(len(completed), summary["completed_scenarios"], "summary/completed_scenarios")
        checks.compare(raw_lines, summary["games"], "summary/games")
        checks.compare(harness_errors, summary["harness_errors"], "summary/harness_errors")
    else:
        checks.compare(manifest["source"]["files"], summary["final_source"]["files"], "source complete begin/end file manifests")
        checks.require(not summary["integrity_errors"], "benchmark integrity errors absent", summary["integrity_errors"])
    zero_failures = all(not any(v for key, v in c.items() if key != "window_close") for c in failures.values())
    zero_background = all(not any(v for key, v in c.items() if key != "window_close") for c in background.values())
    checks.require(zero_failures, "zero controlled protocol and internal failures")
    checks.require(zero_background, "zero background protocol failures")
    closures = {label: {"controlled": failures[label]["window_close"], "background": background[label]["window_close"]} for label in labels}
    if k == 1:
        recomputed_gates = {"complete": completed == set(expected) and harness_errors == 0,
                            "source_unchanged": manifest["source"]["digest"] == end_digest,
                            "no_protocol_or_policy_failures": zero_failures, "no_background_failures": zero_background}
        checks.compare(recomputed_gates, summary["gates"], "summary/gates")
    else:
        checks.compare(closures, summary["discussion_window_closures"], "summary/discussion_window_closures")
        # The k3-only benchmark has no k1 primary; preserve that deliberate failure rather than inventing one.
        empty_primary = {"n": 0, "candidate_rate": None, "reference_rate": None, "delta": None,
                         "ci95": [None, None], "conservative_ci95": [None, None], "mcnemar_p": None}
        checks.compare(empty_primary, summary["primary"], "summary/primary")
        role_checks = {f"{scope}/{role}": scope == "all" and metrics["role_isolated"][scope][role]["n"] >= 200
                       and metrics["role_isolated"][scope][role]["conservative_ci95"][0] > -0.03
                       for scope in ("all", "k:1") for role in ("villager", "seer", "wolf")}
        checks.compare(role_checks, summary["role_checks"], "summary/role_checks")
        regressions = []
        for scope, roles in sorted(metrics["role_isolated"].items()):
            if not scope.startswith("opponent:"):
                continue
            for role, value in sorted(roles.items()):
                if value["n"] >= 20 and value["delta"] < 0 and ((value["mcnemar_p"] is not None and value["mcnemar_p"] < 0.05) or value["ci95"][1] < 0):
                    regressions.append({"scope": scope, "role": role, **value})
        checks.compare(regressions, summary["regressions"], "summary/regressions")
        recomputed_gates = {"complete_pairs": not checks.failures, "primary_sample": False,
                            "significant_primary_gain": False, "substantial_primary_gain": False,
                            "zero_protocol_and_strategy_failures": zero_failures,
                            "zero_background_protocol_failures": zero_background,
                            "max_decision_under_10s": all(v["max_ms"] <= 10000 for v in latency_report.values()),
                            "p99_decision_under_5s": all(v["p99_upper_ms"] is not None and v["p99_upper_ms"] <= 5000 for v in latency_report.values()),
                            "roles_noninferior": all(role_checks.values()),
                            "no_significant_opponent_regression": not regressions,
                            "source_unchanged_during_run": manifest["source"]["digest"] == end_digest}
        checks.compare(recomputed_gates, summary["gates"], "summary/gates")
        checks.compare(all(recomputed_gates.values()), summary["local_evidence_pass"], "summary/local_evidence_pass")
    absolute_rates = defaultdict(lambda: defaultdict(dict))
    for (label, scope, role), (wins, n) in sorted(absolute.items()):
        binary = k == 1 or role != "seat"
        absolute_rates[label][scope][role] = {"n": n, "wins": int(wins) if binary else wins, "rate": wins / n,
                                             "wilson_ci95": wilson(wins, n) if binary else None,
                                             "unit": "one table" if role != "seat" or k == 1 else "one table's controlled-seat mean"}
    effects = defaultdict(lambda: defaultdict(dict))
    for (scope, role, effect), moment in sorted(factorial.items()):
        effects[scope][role][effect] = moment.report()
    negatives = []
    for comparison, scopes in metrics.items():
        for scope, roles in scopes.items():
            for role, value in roles.items():
                if value["delta"] < 0 or value["worsened"]:
                    negatives.append({"comparison": comparison, "scope": scope, "role": role, "delta": value["delta"],
                                      "improved": value["improved"], "worsened": value["worsened"],
                                      "negative_net": value["delta"] < 0, "metric_pointer": f"metrics/{comparison}/{scope}/{role}"})
    run = {"status": "pass" if not checks.failures else "fail", "checks": checks.report(), "raw_records": raw_lines,
           "scenario_count": len(completed), "planned_simulations": len(expected) * 4,
           "counts": {"profile_or_variant": dict(raw_counts), "role_seat_occurrences": dict(role_counts), "controlled_seat_occurrences": dict(seat_counts),
                      "seed_tables": dict(seed_counts), "background_tables": dict(bg_counts)},
           "sampling": {"unique_deal_seeds": len(frequencies), "duplicated_deal_seeds": len(duplicated),
                        "max_scenarios_per_deal_seed": max(frequencies.values()), "independent_unit": "deal_seed; each k3 table once per role",
                        "raw_simulations_are_not_independent_sample_size": True},
           "metrics": metrics, "absolute_win_rates": absolute_rates, "factorial_2x2": effects,
           "all_negative_or_loss_slices": negatives, "failures": failures, "background_failures": background,
           "discussion_window_closures": closures, "latency": latency_report,
           "reported_gates": summary["gates"], "source_digest_begin": manifest["source"]["digest"], "source_digest_end": end_digest,
           "source_end_full_manifest_available": k == 3,
           "timing": timing, "visibility": visibility, "k": k, "elapsed_s_reported": summary["elapsed_s"],
           "started_reported": manifest.get("started", manifest.get("started_unix")), "ended_reported": summary.get("ended"),
           "diagnostic_totals_reaggregated_not_rederived_from_full_traces": k == 1,
           "diagnostic_totals_sha256": hashlib.sha256(json.dumps(diagnostics, sort_keys=True).encode()).hexdigest() if k == 1 else None}
    return run, set(frequencies)


def promotion_checks(run, comparison="combined-reference"):
    metrics = run["metrics"][comparison]
    primary = metrics["all"]["seat"]
    role_checks = {role: metrics["all"][role]["n"] >= 200 and metrics["all"][role]["conservative_ci95"][0] > -0.03 for role in ROLES[1:]}
    regressions, material_point_losses = [], []
    for scope, roles in metrics.items():
        if not scope.startswith("opponent:"):
            continue
        for role, value in roles.items():
            if value["delta"] < -0.03:
                material_point_losses.append({"scope": scope, "role": role, "delta": value["delta"]})
            if value["n"] >= 20 and value["delta"] < 0 and ((value["mcnemar_p"] is not None and value["mcnemar_p"] < 0.05) or value["ci95"][1] < 0):
                regressions.append({"scope": scope, "role": role, "delta": value["delta"], "p": value["mcnemar_p"], "ci95": value["ci95"]})
    gates = {"audited_integrity": run["status"] == "pass", "primary_n_at_least_200": primary["n"] >= 200,
             "primary_gain_at_least_2pp": primary["delta"] >= 0.02,
             "primary_significant_conservative": primary["conservative_ci95"][0] > 0 and primary["mcnemar_p"] < 0.05,
             "role_noninferiority_minus3pp": all(role_checks.values()),
             "no_significant_opponent_regression": not regressions,
             "max_decision_under_10s": all(v["max_ms"] <= 10000 for v in run["latency"].values()),
             "p99_decision_under_5s": all(v["p99_upper_ms"] is not None and v["p99_upper_ms"] <= 5000 for v in run["latency"].values())}
    return {"gates": gates, "local_evidence_pass": all(gates.values()), "role_checks": role_checks,
            "primary": primary, "significant_opponent_regressions_unadjusted_guard": regressions,
            "opponent_point_losses_below_minus3pp_descriptive": material_point_losses,
            "no_regression_detected_is_not_proof_of_noninferiority_in_every_opponent_cell": True}


def current_sources(root, io):
    files = set()
    for directory in SOURCE_ROOTS:
        files.update(root.glob(f"{directory}/**/*.py"))
    for pattern in SOURCE_PATTERNS:
        files.update(root.glob(pattern))
    hashes, errors = {}, []
    for path in sorted(files):
        name = str(path.relative_to(root))
        try:
            hashes[name] = io.sha(path)
        except (OSError, EOFError, UnicodeError) as exc:
            errors.append({"path": name, "error": f"{type(exc).__name__}: {exc}"})
    return {"digest": manifest_digest(hashes) if not errors else None, "files": hashes, "errors": errors,
            "read_completed": not errors, "created": now()}


def wait_for_summaries(root, deadline, spacing, io):
    if spacing < 30:
        raise ValueError("poll spacing must be at least 30s")
    attempts = []
    while True:
        ready, errors = {}, []
        for name in RUNS:
            path = root / "logs/werewolf_benchmarks" / (PREFIX + name) / "summary.json"
            try:
                with bounded_io(10):
                    ready[name] = path.is_file() and path.stat().st_size > 0
            except OSError as exc:
                ready[name] = False
                errors.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
        attempts.append({"time": now(), "ready": ready, "errors": errors})
        if all(ready.values()) or time.time() + spacing > deadline:
            return ready, attempts
        print(f"Waiting {spacing}s for completed summary files: {[k for k, v in ready.items() if not v]}", flush=True)
        time.sleep(spacing)


def self_tests():
    class AuditTests(unittest.TestCase):
        def test_exact_integer_binomial(self):
            cases = [(0, 0, 1), (1, 0, 1), (2, 0, Fraction(1, 2)), (3, 0, Fraction(1, 4)),
                     (5, 0, Fraction(1, 16)), (3, 1, Fraction(5, 8)), (8, 2, Fraction(7, 64)),
                     (10, 0, Fraction(1, 512)), (1000, 0, Fraction(1, 1 << 999))]
            for a, b, expected in cases:
                self.assertEqual(exact_discordance(a, b), float(expected))
                self.assertEqual(exact_discordance(b, a), float(expected))
            for n in range(1, 31):
                for successes in range(n + 1):
                    k = min(successes, n - successes)
                    independent = min(Fraction(1), Fraction(2 * sum(math.comb(n, j) for j in range(k + 1)), 2 ** n))
                    self.assertEqual(exact_discordance(successes, n - successes), float(independent))

        def test_wilson_and_paired(self):
            self.assertAlmostEqual(wilson(0, 10)[0], 0)
            self.assertAlmostEqual(wilson(10, 10)[1], 1)
            self.assertAlmostEqual(wilson(0, 10)[1], 1.96 ** 2 / (10 + 1.96 ** 2))
            for n in (1, 10, 100):
                pair = Pair()
                for i in range(n):
                    pair.add(1, 1, i)
                report = pair.report()
                self.assertEqual(report["delta"], 0)
                self.assertEqual(report["mcnemar_p"], 1)
                self.assertEqual(report["ci95"], [0, 0])
                self.assertLess(report["conservative_ci95"][0], 0)
                self.assertGreater(report["conservative_ci95"][1], 0)
            pair = Pair()
            for i, (a, b) in enumerate(((1, 0), (1, 0), (0, 1), (0, 0))):
                pair.add(a, b, i)
            report = pair.report()
            self.assertEqual((report["n"], report["improved"], report["worsened"]), (4, 2, 1))
            self.assertEqual(report["delta"], 0.25)
            self.assertAlmostEqual(report["clustered"]["se"], math.sqrt(11 / 48))
            pair.add(1 / 3, 2 / 3, 4)
            self.assertIsNone(pair.report()["mcnemar_p"])
            self.assertEqual(pair.report()["conservative_ci95"], [None, None])

        def test_interaction_formula(self):
            self.assertEqual(contrasts(0, 1, 1, 0)["interaction_difference_in_differences"], -2)
            self.assertEqual(contrasts(1, 0, 0, 1)["interaction_difference_in_differences"], 2)
            self.assertEqual(contrasts(0, 0, 0, 1), {"night_main": 0.5, "listen_main": 0.5, "interaction_difference_in_differences": 1})
            self.assertAlmostEqual(contrasts(0.1, 0.3, 0.4, 0.6)["interaction_difference_in_differences"], 0)

        def test_cluster_correction(self):
            moment = Moments({"a", "b"})
            for value, cluster in ((1, "a"), (1, "a"), (-1, "b"), (-1, "b")):
                moment.add(value, cluster)
            report = moment.report()
            self.assertEqual(report["clusters"], 2)
            self.assertEqual(report["mean"], 0)
            self.assertEqual(report["se"], 1)
            self.assertEqual(report["duplicate_observations"], 2)
            singleton = Moments({"a"})
            singleton.add(1, "a")
            self.assertIsNone(singleton.report()["se"])

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(AuditTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return {"passed": result.wasSuccessful(), "tests_run": result.testsRun,
            "failures": [text for _, text in result.failures], "errors": [text for _, text in result.errors],
            "checks": ["known exact binomial cases and all n<=30 cases", "zero-discordance uncertainty", "normal paired variance",
                       "fractional k3 is not McNemar", "2x2 difference-in-differences", "duplicate cluster sandwich variance"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--out", type=Path, default=ROOT / "docs/werewolf_overnight_confirmation.json")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--synthetic-integrity-report", type=Path, help="optional local synthetic mutation-test results, code hash must match")
    parser.add_argument("--wait-until", default="2026-10-13T07:35:00-07:00")
    parser.add_argument("--finish-by", default="2026-10-13T07:43:00-07:00")
    parser.add_argument("--poll-seconds", type=int, default=60)
    args = parser.parse_args(argv)
    tests = self_tests()
    if args.self_test:
        return 0 if tests["passed"] else 1
    if not tests["passed"]:
        return 1
    if args.out.exists():
        parser.error("refuse to overwrite existing evidence; choose a new authorized --out")
    if not args.out.parent.is_dir():
        parser.error("output parent must already exist")
    wait_deadline = datetime.fromisoformat(args.wait_until).timestamp()
    finish_deadline = datetime.fromisoformat(args.finish_by).timestamp()
    if args.poll_seconds < 30 or wait_deadline > finish_deadline:
        parser.error("poll spacing >=30s and waiting must end before audit deadline")
    io = PublicIO(finish_deadline)
    report = {"schema_version": 1, "created": now(), "audit_scope": "independent recomputation; local scripted simulations only; no live-platform strength claim",
              "audit_script": str(Path(__file__)), "audit_code_sha256": io.sha(Path(__file__)),
              "audit_command": [sys.executable, *sys.argv], "self_tests": tests,
              "analysis_declaration": {"declared_at": DECLARED_AT, "seeds": SEEDS, "backgrounds": BACKGROUNDS, "panels": PANELS,
                                       "planned_simulations": 324800, "confirmatory_family_size": FAMILY_SIZE,
                                       "family": "2 strict timings x 3 candidate-reference comparisons x 28 backgrounds x 4 overlapping seat/role slices",
                                       "family_alpha": 0.05, "bonferroni_threshold": 0.05 / FAMILY_SIZE,
                                       "overlapping_slices": "seat overlaps role slices; comparisons share references; Bonferroni does not require independence",
                                       "additional_descriptive_only": ["legacy", "k3", "combined-night", "combined-listen", "2x2 interactions", "aggregated panels"],
                                       "selection": "f48 was fixed from development before heldout; no candidate changes or confirmation reruns",
                                       "promotion": {"k1_gain": 0.02, "role_noninferiority_margin": -0.03, "min_n": 200,
                                                     "zero_errors": True, "unadjusted_opponent_regression_guard_unchanged": True,
                                                     "live_validation_separately_required": True}},
              "statistical_methods": {"exact_p": "min(1, 2*sum_{j=0}^{min(improved,worsened)} choose(d,j)/2^d); exact Python integers/Fraction before float",
                                      "conservative_ci95": "97.5% Wilson intervals (z=2.2414027276) for improved and worsened probabilities, then [L_i-U_w,U_i-L_w]; approximate coverage, not exact",
                                      "paired_normal_ci95": "mean difference +/-1.96*sample_sd/sqrt(n); paired not unpaired",
                                      "clustered_ci95": "deal_seed sandwich SE sqrt(G/(G-1)*sum_c(sum_i_in_c(d_i-mean))^2)/n; timing kept separate",
                                      "interaction": "per game Y_NL-Y_N-Y_L+Y_0, range [-2,2]; not a binary McNemar test",
                                      "main_effects": "N=(Y_N-Y_0+Y_NL-Y_L)/2; L=(Y_L-Y_0+Y_NL-Y_N)/2",
                                      "absolute_intervals": "95% Wilson per binary role/table outcome; no binomial CI on fractional mixed-side means"},
              "limitations": ["Synthetic background distribution is equally weighted and does not estimate real tournament population.",
                              "File equality at begin/end cannot rule out an unobserved change and reversal between snapshots.",
                              "Research runner retains only end source digest, not an end per-file manifest; k3 retains both.",
                              "Internal failure counters cannot detect every conceivable swallowed exception.",
                              "Diagnostics are independently reaggregated from stored numerator/denominator values, not regenerated from omitted full observations.",
                              "Research manifests do not snapshot arbitrary inherited strategy environment; explicit settings and profile environments are audited.",
                              "Seed 33101/33102 is consumed after inspection and cannot validate a revised candidate as fresh holdout."],
              "runs": {}, "errors": [], "source_manifests": {}}
    if args.synthetic_integrity_report:
        synthetic = io.json(args.synthetic_integrity_report)
        report["synthetic_integrity_tests"] = synthetic
        report["synthetic_integrity_tests_sha256"] = io.sha(args.synthetic_integrity_report)
        if synthetic.get("audit_code_sha256") != report["audit_code_sha256"] or synthetic.get("passed") is not True:
            parser.error("synthetic integrity results must pass and refer to this exact audit code")
    queue_path = Path("/tmp/ww_overnight_confirmation_1013.py")
    try:
        report["queue_driver"] = {"path": str(queue_path), "sha256": io.sha(queue_path),
                                  "source": io.run(str(queue_path), queue_path.read_text)}
    except (OSError, UnicodeError) as exc:
        report["queue_driver"] = {"path": str(queue_path), "error": str(exc)}
    begin_sources = current_sources(args.root, io)
    report["source_audit_begin"] = begin_sources
    ready, waits = wait_for_summaries(args.root, wait_deadline, args.poll_seconds, io)
    report["completion_wait"] = {"deadline": args.wait_until, "minimum_spacing_seconds": args.poll_seconds, "observations": waits}
    cluster_sets = {}
    for name in RUNS:
        directory = args.root / "logs/werewolf_benchmarks" / (PREFIX + name)
        if not ready[name]:
            report["runs"][name] = {"status": "incomplete", "reason": "summary not complete before bounded waiting deadline"}
            continue
        try:
            print(f"Auditing {name} raw evidence", flush=True)
            manifest = io.json(directory / "manifest.json")
            summary = io.json(directory / "summary.json")
            raw_name = "games.jsonl.gz" if RUNS[name][3] == 1 else "games.jsonl"
            raw_path = directory / raw_name
            before = raw_path.stat()
            run, clusters = io.run(f"stream/CRC/audit {raw_path}", lambda: audit_run(args.root, name, manifest, summary), seconds=240)
            hashes = {filename: io.sha(directory / filename) for filename in ("manifest.json", "summary.json", raw_name)}
            after = raw_path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                run["status"] = "fail"
                run["checks"]["passed"] = False
                run["checks"]["failures"].append({"check": "raw file changed during audit"})
            source = manifest.pop("source")
            report["source_manifests"][source["digest"]] = source
            run["manifest_without_source"] = manifest
            run["source_manifest_ref"] = source["digest"]
            run["file_sha256"] = hashes
            run["raw_bytes"] = after.st_size
            run["raw_path"] = str(raw_path)
            run["command_reconstructed_from_queue_and_manifest"] = command_for(name, directory, str(args.root / ".venv/bin/python"))
            run["source_matches_independent_audit_begin"] = source["files"] == begin_sources["files"] and begin_sources["read_completed"]
            if not run["source_matches_independent_audit_begin"]:
                run["status"] = "fail"
                run["checks"]["passed"] = False
                run["checks"]["failures"].append({"check": "source differs from independently hashed audit begin"})
            if name == "k3-legacy":
                run["k3_interpretation"] = "Seat is controlled-seat mean with both sides changed. A stronger controlled wolf can reduce this mean by defeating more controlled good seats. Only village_only versus reference isolates changes to our village strategy; wolf_only isolates wolves. Role metrics count each table once, not each same-role seat. Legacy visibility only."
                report["source_manifests"].setdefault(summary["final_source"]["digest"], summary["final_source"])
                run["reported_regressions"] = summary["regressions"]
                run["reported_local_evidence_pass"] = summary["local_evidence_pass"]
            report["runs"][name] = run
            cluster_sets[name] = clusters
            print(f"{name}: {run['status']}, {run['raw_records']} simulations, {run['checks']['reported_metric_slices_compared']} compared slices", flush=True)
        except (OSError, EOFError, UnicodeError, ValueError, KeyError, TypeError, zlib.error) as exc:
            error = {"run": name, "time": now(), "error": f"{type(exc).__name__}: {exc}"}
            report["errors"].append(error)
            report["runs"][name] = {"status": "incomplete", **error}
            print(error, flush=True)
    end_sources = current_sources(args.root, io)
    report["source_audit_end"] = end_sources
    report["source_audit_begin_end_equal"] = begin_sources["read_completed"] and end_sources["read_completed"] and begin_sources["files"] == end_sources["files"]
    report["source_all_runs_same_digest"] = len(report["source_manifests"]) == 1
    try:
        report["audit_code_sha256_end"] = io.sha(Path(__file__))
    except OSError as exc:
        report["audit_code_sha256_end"] = None
        report["errors"].append({"operation": "audit code end hash", "error": str(exc)})
    report["audit_code_unchanged"] = report["audit_code_sha256"] == report["audit_code_sha256_end"]
    report["source_snapshot_difference"] = {
        "added": sorted(set(end_sources["files"]) - set(begin_sources["files"])),
        "removed": sorted(set(begin_sources["files"]) - set(end_sources["files"])),
        "changed": sorted(name for name in set(begin_sources["files"]) & set(end_sources["files"])
                          if begin_sources["files"][name] != end_sources["files"][name])}
    report["cross_run_deal_overlap"] = {f"{a}|{b}": len(cluster_sets[a] & cluster_sets[b])
                                       for i, a in enumerate(cluster_sets) for b in list(cluster_sets)[i + 1:]}
    report["unique_deal_seeds_all_runs"] = len(set().union(*cluster_sets.values())) if cluster_sets else 0
    report["simulations_recomputed"] = sum(r.get("raw_records", 0) for r in report["runs"].values())
    report["multiplicity"] = {"family_size": FAMILY_SIZE, "tested": 0, "adjusted_positive": [], "adjusted_negative": [], "unadjusted_negative": []}
    for name in ("rt-strict", "rounds-strict"):
        run = report["runs"][name]
        if "metrics" not in run:
            continue
        for comparison in ("night-reference", "listen-reference", "combined-reference"):
            for bg in BACKGROUNDS:
                for role, value in run["metrics"][comparison][f"opponent:{bg}"].items():
                    report["multiplicity"]["tested"] += 1
                    item = {"run": name, "comparison": comparison, "background": bg, "role": role, "delta": value["delta"],
                            "n": value["n"], "p": value["mcnemar_p"], "bonferroni_p": value["bonferroni_p"]}
                    if value["family_reject_005"]:
                        report["multiplicity"]["adjusted_positive" if value["delta"] > 0 else "adjusted_negative"].append(item)
                    if value["delta"] < 0 and value["mcnemar_p"] < 0.05:
                        report["multiplicity"]["unadjusted_negative"].append(item)
    report["promotion_assessment"] = {name: promotion_checks(run) for name, run in report["runs"].items() if RUNS[name][3] == 1 and "metrics" in run}
    report["audit_complete_and_passed"] = (report["simulations_recomputed"] == 324800 and all(r["status"] == "pass" for r in report["runs"].values())
                                           and report["source_audit_begin_end_equal"] and report["source_all_runs_same_digest"]
                                           and report["audit_code_unchanged"] and report["multiplicity"]["tested"] == FAMILY_SIZE)
    strict_pass = all(report["promotion_assessment"].get(name, {}).get("local_evidence_pass", False) for name in ("rt-strict", "rounds-strict"))
    report["verdict"] = {"promote": False, "retain_current_default": True,
                         "strict_local_gates_pass": strict_pass,
                         "reason": "incomplete or failed independent audit" if not report["audit_complete_and_passed"] else
                                   "local gates do not all pass; default remains unchanged" if not strict_pass else
                                   "local gates alone cannot authorize promotion; live validation remains outstanding",
                         "no_live_platform_claim": True, "no_policy_edits_or_simulation_reruns_by_auditor": True}
    report["io_errors_and_retries"] = io.errors
    report["completed"] = now()
    # Compact JSON keeps the full audit under the 4 MiB tracked-report limit without dropping loss slices.
    text = json.dumps(report, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
    if len(text.encode()) > 4 * 1024 * 1024:
        # Full detail remains in a new deep ignored evidence file; the tracked report points to its hash.
        extra = args.root / "logs/werewolf_benchmarks" / ("overnight-audit-extra-" + datetime.now().strftime("%Y%m%dT%H%M%S") + ".json.gz")
        with gzip.open(extra, "xt", compresslevel=6) as stream:
            stream.write(text)
        for run in report["runs"].values():
            if "metrics" in run:
                run["metrics"] = {c: {s: v for s, v in scopes.items() if s == "all" or s.startswith("panel:")} for c, scopes in run["metrics"].items()}
                run["absolute_win_rates"] = {c: {s: v for s, v in scopes.items() if s == "all" or s.startswith(("panel:", "opponent:"))} for c, scopes in run["absolute_win_rates"].items()}
                run["factorial_2x2"] = {s: v for s, v in run["factorial_2x2"].items() if s == "all" or s.startswith("panel:")}
        report["full_extra"] = {"path": str(extra), "sha256": sha256(extra), "contains": "complete report including every slice"}
        text = json.dumps(report, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
    with args.out.open("x") as stream:
        stream.write(text)
    print(json.dumps({"out": str(args.out), "bytes": len(text.encode()), "audit_pass": report["audit_complete_and_passed"],
                      "simulations": report["simulations_recomputed"], "verdict": report["verdict"]}, indent=2), flush=True)
    return 0 if report["audit_complete_and_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
