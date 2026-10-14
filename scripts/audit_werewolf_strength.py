"""Independent, standard-library-only audit of the frozen 37301/37302 experiment.

No simulator/policy imports, games, models, network, credentials or source edits.
Reads only named experiment artifacts and the public files in their source inventory.
Writes only data/werewolf_strength_confirmation_audit.json; self-tests use /tmp.
The two timings reuse deals and MUST NOT be pooled as independent observations.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime
from functools import lru_cache
import gzip
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import random
import signal
import sys
import tempfile
import time
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "strength-confirm-37301-37302-"
PLAN = "data/werewolf_strength_confirmation_plan.json"
BASELINE = "data/werewolf_current_baseline.json"
OUTPUT = "data/werewolf_strength_confirmation_audit.json"
CANDIDATE = "local.werewolf_night_priority_research:tie_high"
CANDIDATE_FILE = "local/werewolf_night_priority_research.py"
PIN = "24f5e7723302a09de95e0ceb034291100ac612642c31c2109066c964f16764db"
SEEDS = (37301, 37302)
BACKGROUNDS = tuple((
    "first,random,heuristic,llm,llmx,llmq,mixed,rt_wolfpack,rt_talker,"
    "rt_detective,rt_chaos,guard_silent,guard_abstain,guard_fixed,guard_echo,"
    "guard_fake,guard_contradict,first_high_night,first_random_night,first_switch,"
    "pop_coordinated,pop_prompt_seer,pop_late_claim,pop_pressure_bus,pop_distance,"
    "pop_observed_switch,pop_survivor_kill,pop_anti_majority").split(","))
ROLES = ("seat", "villager", "seer", "wolf")
PROFILES = ("reference", "candidate")
# Fixed before reading confirmation outcomes. These subsets overlap, not new data.
SIMPLE7 = ("first", "random", "heuristic", "guard_silent", "guard_abstain", "guard_fixed", "guard_echo")
PANELS = {
    "simple7": SIMPLE7,
    "other21": tuple(b for b in BACKGROUNDS if b not in SIMPLE7),
    "strong_scripted_proxy8": ("llm", "llmx", "llmq", "mixed", "rt_wolfpack", "rt_talker", "rt_detective", "rt_chaos"),
    "population8": tuple(b for b in BACKGROUNDS if b.startswith("pop_")),
}
FAIL_KEYS = {"crash", "illegal", "bad_chat", "timeout", "chat_after_terminate", "window_close"}
INTERNAL_KEYS = {"fallbacks", "overruns", "policy_fallbacks", "policy_errors", "logged_errors", "listen_errors", "fallback_paths"}
SOURCE_ROOTS = {"agent", "champion", "local", "versions", "altruagent", "examples"}
BOUNDS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 50, 100, 250, 500, 1000, 2000, 5000, 10000, 120000)
FAMILY = 2 * 28 * 4
Z_PAIRED = 2.2414027276


def now():
    return datetime.now().astimezone().isoformat()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def inventory_digest(files):
    return digest_bytes(json.dumps(files, sort_keys=True).encode())


@contextmanager
def bounded_io(seconds):
    def expired(signum, frame):
        raise TimeoutError(f"public experiment read exceeded {seconds:.1f}s")
    old = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, max(0.01, seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


class PublicIO:
    def __init__(self, deadline):
        self.deadline = deadline
        self.errors = []

    def run(self, label, operation, seconds=120):
        for attempt in (1, 2):
            try:
                remaining = self.deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError("audit deadline reached")
                with bounded_io(min(seconds, remaining)):
                    return operation()
            except (OSError, EOFError, UnicodeError, json.JSONDecodeError, zlib.error) as exc:
                self.errors.append({"operation": label, "attempt": attempt, "at": now(),
                                    "error": f"{type(exc).__name__}: {exc}"})
                # A permission denial is a blocker, never a reason to bypass controls.
                if isinstance(exc, PermissionError) or attempt == 2 or time.time() + 5 >= self.deadline:
                    raise
                print(f"READ RETRY {attempt}/2 {label}: {exc}", flush=True)
                time.sleep(2)

    def bytes(self, path):
        return self.run(str(path), path.read_bytes)

    def json(self, path):
        raw = self.bytes(path)
        return json.loads(raw), {"path": str(path.relative_to(ROOT)), "bytes": len(raw), "sha256": digest_bytes(raw)}


class Checks:
    def __init__(self):
        self.total = 0
        self.errors = []

    def require(self, condition, message):
        self.total += 1
        if not condition:
            self.errors.append(message)

    def same(self, actual, expected, label):
        if isinstance(expected, dict):
            self.require(isinstance(actual, dict), label + ": expected mapping")
            if not isinstance(actual, dict):
                return
            self.require(set(actual) == set(expected), label + ": keys differ")
            for key in expected.keys() & actual.keys():
                self.same(actual[key], expected[key], f"{label}/{key}")
        elif isinstance(expected, (list, tuple)):
            self.require(isinstance(actual, (list, tuple)) and len(actual) == len(expected), label + ": sequence shape")
            if isinstance(actual, (list, tuple)) and len(actual) == len(expected):
                for index, (a, e) in enumerate(zip(actual, expected)):
                    self.same(a, e, f"{label}/{index}")
        elif isinstance(expected, float):
            self.require(isinstance(actual, (float, int)) and math.isfinite(actual)
                         and math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-12), label + ": numeric mismatch")
        else:
            self.require(actual == expected, label + ": mismatch")

    def report(self):
        return {"passed": not self.errors, "comparisons": self.total, "errors": self.errors}


def wilson(wins, n, z=1.96):
    require(type(n) is int and type(wins) is int and 0 <= wins <= n and n > 0, "invalid Wilson counts")
    p = wins / n
    z2 = z * z
    center = (p + z2 / (2 * n)) / (1 + z2 / n)
    radius = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return [max(0.0, center - radius), min(1.0, center + radius)]


@lru_cache(maxsize=4096)
def exact_mcnemar(improved, worsened):
    """Two-sided Binomial(d, 1/2), summing integer coefficients, not normal tails."""
    require(type(improved) is int and type(worsened) is int and min(improved, worsened) >= 0, "invalid discordance")
    d = improved + worsened
    if d == 0:
        return 1.0
    term = total = 1
    for k in range(1, min(improved, worsened) + 1):
        term = term * (d - k + 1) // k
        total += term
    return min(1.0, (2 * total) / (1 << d))


@dataclass
class Pair:
    n: int = 0
    candidate_wins: int = 0
    reference_wins: int = 0
    improved: int = 0
    worsened: int = 0

    def add(self, candidate, reference):
        require(type(candidate) is bool and type(reference) is bool, "nonboolean win")
        self.n += 1
        self.candidate_wins += candidate
        self.reference_wins += reference
        self.improved += candidate and not reference
        self.worsened += reference and not candidate

    def report(self):
        if not self.n:
            return {"n": 0}
        require(self.candidate_wins - self.reference_wins == self.improved - self.worsened, "inconsistent pair counts")
        n = self.n
        delta = (self.improved - self.worsened) / n
        variance = max(0.0, (self.improved + self.worsened - n * delta * delta) / max(1, n - 1))
        radius = 1.96 * math.sqrt(variance / n)
        a, b = wilson(self.improved, n, Z_PAIRED), wilson(self.worsened, n, Z_PAIRED)
        return {**asdict(self), "candidate_rate": self.candidate_wins / n, "reference_rate": self.reference_wins / n,
                "delta": delta, "ci95": [max(-1, delta - radius), min(1, delta + radius)],
                "conservative_ci95": [a[0] - b[1], a[1] - b[0]],
                "candidate_wilson_ci95": wilson(self.candidate_wins, n),
                "reference_wilson_ci95": wilson(self.reference_wins, n), "mcnemar_p": exact_mcnemar(self.improved, self.worsened)}


def expected_deal(seed, game, background):
    # Independent reproduction of the documented deterministic sampling recipe.
    rng = random.Random(f"deal:{seed}:{game}:{background}:1")
    seat = rng.sample(range(7), 1)[0]
    if background == "mixed":
        for _ in range(6):
            rng.choice(("first", "heuristic", "random", "llm"))
    deal_seed = rng.getrandbits(62)
    roles = ["wolf", "wolf", "seer", "villager", "villager", "villager", "villager"]
    random.Random(deal_seed).shuffle(roles)
    return deal_seed, seat, roles[seat], roles


def scenario_key(seed, background, game):
    return f"{seed}/{background}/k1/{game}"


def histogram_percentile(histogram):
    target = math.ceil(sum(histogram.values()) * .99)
    if target == 0:
        return None
    count = 0
    for bound in BOUNDS:
        count += histogram.get(str(bound), 0)
        if count >= target:
            return bound
    raise ValueError("latency histogram outside known bins")


class StreamAudit:
    def __init__(self, seeds=SEEDS, backgrounds=BACKGROUNDS, games=500):
        self.seeds, self.backgrounds, self.games = tuple(seeds), tuple(backgrounds), games
        self.expected = {scenario_key(s, b, g) for s in seeds for b in backgrounds for g in range(games)}
        self.seen = set()
        self.pending = {}
        self.signatures = {}
        self.counts = defaultdict(Pair)
        self.failures = {p: Counter({k: 0 for k in FAIL_KEYS | INTERNAL_KEYS}) for p in PROFILES}
        self.background_failures = {p: Counter({k: 0 for k in FAIL_KEYS}) for p in PROFILES}
        self.latency = {p: Counter() for p in PROFILES}
        self.maxima = {p: 0.0 for p in PROFILES}
        self.diagnostics = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
        self.nonwolf_outcome_changes = Counter({r: 0 for r in ("villager", "seer")})
        self.nonwolf_behavior_changes = Counter({r: 0 for r in ("villager", "seer")})
        self.nonwolf_behavior_examples = []
        self.trace_records = 0
        self.raw_lines = 0

    def feed(self, r):
        require(isinstance(r, dict) and "harness_error" not in r, "harness error or malformed record")
        s = r["scenario"]
        require(set(s) == {"seed", "game", "background", "k"}, "scenario fields mismatch")
        require(all(type(s[k]) is int for k in ("seed", "game", "k")), "noninteger scenario")
        key = scenario_key(s["seed"], s["background"], s["game"])
        require(s["k"] == 1 and r["id"] == key and key in self.expected, "unexpected scenario key")
        profile = r["profile"]
        require(profile in PROFILES, "unexpected profile")
        identity = (key, profile)
        require(identity not in self.seen, "duplicate scenario/profile")
        self.seen.add(identity)
        self.raw_lines += 1
        ds, seat, role, roles = expected_deal(s["seed"], s["game"], s["background"])
        require(type(r["seat"]) is int and type(r["deal_seed"]) is int, "invalid deal/seat types")
        require((r["deal_seed"], r["seat"], r["role"]) == (ds, seat, role), "wrong deterministic deal/seat/role")
        require(type(r["win"]) is bool and r["winner"] in ("wolf", "villager"), "invalid result")
        require(r["win"] == (r["winner"] == ("wolf" if role == "wolf" else "villager")), "win/winner disagree")
        require(type(r["days"]) is int and r["days"] > 0, "invalid game duration")
        for label, expected_keys, total in (("failures", FAIL_KEYS | INTERNAL_KEYS, self.failures),
                                            ("background_failures", FAIL_KEYS, self.background_failures)):
            require(set(r[label]) == expected_keys, f"incomplete {label} evidence")
            require(all(type(v) is int and v >= 0 for v in r[label].values()), f"invalid {label} counters")
            total[profile].update(r[label])
        require(all(k in {str(b) for b in BOUNDS} and type(v) is int and v >= 0
                    for k, v in r["latency_histogram"].items()), "invalid latency bins")
        require(isinstance(r["max_ms"], (int, float)) and math.isfinite(r["max_ms"]) and r["max_ms"] >= 0, "invalid latency")
        self.latency[profile].update(r["latency_histogram"])
        self.maxima[profile] = max(self.maxima[profile], r["max_ms"])
        for metric, value in r["diagnostics"].items():
            require(isinstance(value, list) and len(value) == 2 and all(isinstance(v, (int, float)) and math.isfinite(v) for v in value), "invalid diagnostic")
            for group in ("all", role):
                total = self.diagnostics[f"{profile}/{s['background']}/{group}"][metric]
                total[0] += value[0]
                total[1] += value[1]
        if "trace" in r:
            self.trace_records += 1
            require(r["trace"]["roles"] == roles, "trace roles disagree with deterministic deal")
        pair = self.pending.setdefault(key, {})
        pair[profile] = r
        if len(pair) == 2:
            reference, candidate = pair["reference"], pair["candidate"]
            signature = (s["seed"], s["background"], s["game"], ds, seat, role)
            require(all(reference[k] == candidate[k] for k in ("id", "scenario", "deal_seed", "seat", "role")), "unmatched pair")
            self.signatures[key] = signature
            scopes = ["all", f"opponent:{s['background']}", f"seed:{s['seed']}", f"seat:{seat}"]
            scopes += [f"panel:{name}" for name, members in PANELS.items() if s["background"] in members]
            for scope in scopes:
                for group in ("seat", role):
                    self.counts[(scope, group)].add(candidate["win"], reference["win"])
            if role != "wolf":
                self.nonwolf_outcome_changes[role] += candidate["win"] != reference["win"]
                fields = ("win", "winner", "days", "chats", "dms", "diagnostics", "trace")
                different = [f for f in fields if candidate.get(f) != reference.get(f)]
                self.nonwolf_behavior_changes[role] += bool(different)
                if different and len(self.nonwolf_behavior_examples) < 10:
                    self.nonwolf_behavior_examples.append({"id": key, "fields": different})
            del self.pending[key]

    def finish(self):
        require(not self.pending, "missing paired profile record")
        require(set(self.signatures) == self.expected, "missing scenarios")
        require(len(self.seen) == 2 * len(self.expected), "wrong raw record count")
        metrics = defaultdict(dict)
        for (scope, role), count in sorted(self.counts.items()):
            metrics[scope][role] = count.report()
        return dict(metrics)


def scan_raw(path, seeds=SEEDS, backgrounds=BACKGROUNDS, games=500):
    audit = StreamAudit(seeds, backgrounds, games)
    raw_digest = hashlib.sha256()
    raw_bytes = 0
    with gzip.open(path, "rb") as stream:
        for line_number, line in enumerate(stream, 1):
            raw_digest.update(line)
            raw_bytes += len(line)
            try:
                audit.feed(json.loads(line))
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"raw line {line_number}: {exc}") from exc
            if line_number % 10000 == 0:
                print(f"RAW {path.parent.name}: {line_number} records", flush=True)
    metrics = audit.finish()
    compressed_digest = hashlib.sha256()
    compressed_bytes = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            compressed_digest.update(chunk)
            compressed_bytes += len(chunk)
    return audit, metrics, {"path": str(path), "records": audit.raw_lines,
                            "compressed_bytes": compressed_bytes, "compressed_sha256": compressed_digest.hexdigest(),
                            "uncompressed_bytes": raw_bytes, "uncompressed_sha256": raw_digest.hexdigest()}


def assessment(metrics):
    all_seat = metrics["all"]["seat"]
    roles = {r: {"n": metrics["all"][r]["n"], "conservative_ci95": metrics["all"][r]["conservative_ci95"],
                 "enough_samples": metrics["all"][r]["n"] >= 200,
                 "noninferiority_pass": metrics["all"][r]["n"] >= 200 and metrics["all"][r]["conservative_ci95"][0] > -.03}
             for r in ROLES[1:]}
    negatives, unacceptable, undersampled = [], [], []
    for bg in BACKGROUNDS:
        for role, m in metrics.get("opponent:" + bg, {}).items():
            entry = {"background": bg, "role": role, **m, "family_size": FAMILY,
                     "bonferroni_adjusted_p": min(1.0, FAMILY * m["mcnemar_p"])}
            entry["prespecified_regression_flag"] = m["n"] >= 200 and m["delta"] < -.03 and m["ci95"][1] < 0
            entry["below_200_not_evidence_of_safety"] = m["n"] < 200
            if m["delta"] < 0:
                negatives.append(entry)
            if entry["prespecified_regression_flag"]:
                unacceptable.append(entry)
            if m["n"] < 200:
                undersampled.append({"background": bg, "role": role, "n": m["n"]})
    negatives.sort(key=lambda x: (x["delta"], x["background"], x["role"]))
    gates = {"average_benefit_conservative_lower_gt_zero": all_seat["conservative_ci95"][0] > 0,
             "substantial_point_gain_ge_2pp": all_seat["delta"] >= .02,
             "all_role_noninferiority_n_ge_200_lower_gt_minus_3pp": all(v["noninferiority_pass"] for v in roles.values()),
             "no_prespecified_unacceptable_background_role_regression": not unacceptable}
    return {"gates": gates, "average_statistical_success": gates["average_benefit_conservative_lower_gt_zero"],
            "local_strength_gates_pass": all(gates.values()), "dominance": all_seat["worsened"] == 0 and all_seat["improved"] > 0,
            "role_checks": roles, "all_negative_background_role_slices": negatives,
            "unacceptable_background_role_slices": unacceptable, "undersampled_background_role_slices": undersampled,
            "first_switch": metrics.get("opponent:first_switch", {}),
            "first_switch_negative_development_risk_persists": {r: m["delta"] < 0 for r, m in metrics.get("opponent:first_switch", {}).items()},
            "all_negative_other_scopes": [{"scope": scope, "role": role, **m} for scope, values in metrics.items()
                                           if not scope.startswith("opponent:") for role, m in values.items() if m["delta"] < 0],
            "slice_caution": "A CI crossing zero, especially n<200, is not proof of safety. Bonferroni is descriptive; it does not erase the prespecified regression gate."}


def safe_source_name(name):
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        return False
    if p.parts[0] in SOURCE_ROOTS and p.suffix == ".py":
        return True
    if name in ("champion/VERSION", "data/werewolf_learned_likelihoods.json", "pyproject.toml", "uv.lock", "poetry.lock"):
        return True
    return len(p.parts) == 1 and name.startswith("requirements") and p.suffix in (".txt", ".lock")


def snapshot_sources(root, listed, io, label):
    result = {"started": now(), "files": {}, "unreadable_files": [], "label": label}
    for index, name in enumerate(sorted(listed), 1):
        require(safe_source_name(name), f"unapproved source path: {name}")
        path = root / name
        require(not path.is_symlink(), f"source symlink not followed: {name}")
        try:
            result["files"][name] = digest_bytes(io.bytes(path))
        except (OSError, UnicodeError) as exc:
            result["unreadable_files"].append({"path": name, "error": f"{type(exc).__name__}: {exc}"})
        if index % 25 == 0 or index == len(listed):
            print(f"SOURCE {label}: {index}/{len(listed)} files; failures={len(result['unreadable_files'])}", flush=True)
    result.update(ended=now(), read_completed=not result["unreadable_files"] and len(result["files"]) == len(listed),
                  expected_file_count=len(listed), digest=inventory_digest(result["files"]))
    result["matches_frozen_inventory"] = result["read_completed"] and result["files"] == listed
    return result


def validate_metadata(manifest, summary, timing, plan, baseline, checks):
    config = manifest["config"]
    for key, expected in {"seed_registry": "strength", "split": "heldout", "seeds": list(SEEDS), "games": 500,
                          "backgrounds": ",".join(BACKGROUNDS), "timing": timing, "visibility": "strict", "profile_env": []}.items():
        checks.same(config.get(key), expected, "manifest config " + key)
    checks.same(manifest["profiles"], [["reference", "agent", {}], ["candidate", CANDIDATE, {}]], "factories and per-profile settings")
    checks.same(manifest["settings"], {"ARENA_WW_TIMING": timing, "ARENA_WW_TACTICS": "off", "ARENA_WW_DM": "off"}, "explicit strategy environment")
    checks.same(manifest["planned_scenarios"], 28000, "planned scenarios")
    checks.same(manifest["planned_games"], 56000, "planned games")
    checks.same(summary["completed_scenarios"], 28000, "completed scenarios")
    checks.same(summary["games"], 56000, "reported game records")
    checks.same(summary["harness_errors"], 0, "reported harness errors")
    checks.require(datetime.fromisoformat(plan["declared_before_confirmation"]) <= datetime.fromisoformat(manifest["started"]), "candidate frozen before run began")
    source = manifest["source"]
    checks.same(source["digest"], inventory_digest(source["files"]), "begin source full inventory digest")
    checks.same(summary["source_digest_end"], source["digest"], "runner begin/end source digest")
    checks.same(source["files"].get(CANDIDATE_FILE), PIN, "manifest candidate pin")
    selected = baseline["checked_werewolf_and_shared_runtime_files"]
    checks.same(len(selected), 10, "B0 selected public agent files")
    for name, digest in selected.items():
        checks.same(source["files"].get(name), digest, "B0 unchanged source " + name)


def audit_run(root, timing, manifest, summary, plan, baseline, io):
    checks = Checks()
    validate_metadata(manifest, summary, timing, plan, baseline, checks)
    path = root / "logs/werewolf_benchmarks" / (PREFIX + timing) / "games.jsonl.gz"
    raw, metrics, provenance = io.run(str(path), lambda: scan_raw(path), seconds=420)
    ordinary = {scope: values for scope, values in metrics.items() if not scope.startswith("panel:")}
    checks.same(summary["metrics"], {"candidate": ordinary}, "all independently derived reported statistics")
    checks.same(summary["failures"], raw.failures, "controlled failure counters")
    checks.same(summary["background_failures"], raw.background_failures, "background failure counters")
    checks.same(summary["diagnostic_totals"], dict(raw.diagnostics), "all raw diagnostic totals")
    latency = {p: {"max_ms": raw.maxima[p], "p99_upper_ms": histogram_percentile(raw.latency[p])} for p in PROFILES}
    checks.same(summary["latency"], latency, "latency totals")
    zero_controlled = all(v == 0 for p in PROFILES for k, v in raw.failures[p].items() if k != "window_close")
    zero_background = all(v == 0 for p in PROFILES for k, v in raw.background_failures[p].items() if k != "window_close")
    gates = {"complete": len(raw.signatures) == 28000 and raw.raw_lines == 56000,
             "source_unchanged": manifest["source"]["digest"] == summary["source_digest_end"],
             "no_protocol_or_policy_failures": zero_controlled, "no_background_failures": zero_background}
    checks.same(summary["gates"], gates, "recomputed runner gates")
    checks.require(all(gates.values()), "all independent runner integrity gates")
    checks.same(raw.trace_records, 4, "two bounded traced scenarios with two profiles each")
    checks.require(all(v["max_ms"] <= 10000 for v in latency.values()), "max decision latency <=10s")
    checks.require(all(v["p99_upper_ms"] is not None and v["p99_upper_ms"] <= 5000 for v in latency.values()), "p99 decision latency <=5s")
    checks.require(not any(raw.nonwolf_outcome_changes.values()), "villager/seer pairwise outcomes unchanged")
    checks.require(not any(raw.nonwolf_behavior_changes.values()), "villager/seer available behavioral evidence unchanged")
    result = {"status": "complete", "timing": timing, "visibility": "strict", "controlled_seats": 1,
              "started": manifest["started"], "ended": summary["ended"], "raw_provenance": provenance,
              "unique_scenarios": len(raw.signatures), "unique_deal_seeds": len({s[3] for s in raw.signatures.values()}),
              "integrity": checks.report(), "recomputed_runner_gates": gates, "all_metrics": metrics,
              "assessment": assessment(metrics), "controlled_failures": raw.failures,
              "background_failures": raw.background_failures, "latency": latency,
              "discussion_window_closures": {p: {"controlled": raw.failures[p]["window_close"],
                                                  "background": raw.background_failures[p]["window_close"]} for p in PROFILES},
              "nonwolf_pairwise_outcome_changes": raw.nonwolf_outcome_changes,
              "nonwolf_available_behavior_changes": raw.nonwolf_behavior_changes,
              "nonwolf_behavior_difference_examples": raw.nonwolf_behavior_examples,
              "bounded_trace_records": raw.trace_records,
              "all_metrics_sha256": digest_bytes(json.dumps(metrics, sort_keys=True, separators=(",", ":")).encode()),
              "source_manifest_begin": manifest["source"], "source_digest_end_reported": summary["source_digest_end"],
              "runner_end_full_file_inventory_available": False,
              "manifest_without_source": {k: v for k, v in manifest.items() if k != "source"}}
    return result, raw.signatures


def policy_review(source):
    tree = ast.parse(source)
    imports = sorted(n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom))
    attributes = sorted({n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "v"})
    return {"candidate_source": source, "source_sha256": digest_bytes(source.encode()),
            "imports": imports, "view_attributes": attributes,
            "expected_imports_and_view_attributes": imports == ["agent.agent", "agent.werewolf.policy"] and attributes == ["allies", "others_alive", "role"],
            "manual_review": "Frozen tie_high factory uses NightPriorityPolicy. _kill_plan retains original on non-wolf and claimed-seer paths, keeps suspicion priority, changes minimum-score seat tie-break from low to high. Only role/allies/others_alive from the seat view plus inherited public-evidence suspicion are read; no simulator, complete-role-table, external file or network access is present in this candidate source. This narrow static review is not a formal whole-program information-flow proof.",
            "locations": ["local/werewolf_night_priority_research.py:15-29", "local/werewolf_night_priority_research.py:54-55", "agent/werewolf/policy.py:457-470"]}


def wait_artifact(path, io, wait_until):
    last_notice = 0
    while time.time() < wait_until:
        if io.run(str(path) + " existence", path.exists, seconds=15):
            try:
                return io.json(path)
            except json.JSONDecodeError:
                # A just-created summary may still be in its one-shot write.
                pass
        if time.time() - last_notice >= 60:
            print(f"WAIT {path.parent.name}/{path.name} until {datetime.fromtimestamp(wait_until).astimezone().isoformat()}", flush=True)
            last_notice = time.time()
        time.sleep(min(15, max(0, wait_until - time.time())))
    raise TimeoutError(f"not ready before bounded wait deadline: {path}")


def write_report(report, root):
    report["updated_at"] = now()
    content = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    require(len(content.encode()) <= 4 * 1024 * 1024, "report exceeds 4 MiB limit")
    (root / OUTPUT).write_text(content)
    print(f"REPORT {root / OUTPUT} ({len(content.encode())} bytes)", flush=True)


def self_tests():
    class AuditTests(unittest.TestCase):
        def fixture(self, games=1):
            result = []
            for game in range(games):
                ds, seat, role, _ = expected_deal(37301, game, "first")
                for profile in PROFILES:
                    result.append({"id": scenario_key(37301, "first", game),
                                   "scenario": {"seed": 37301, "game": game, "background": "first", "k": 1},
                                   "profile": profile, "deal_seed": ds, "seat": seat, "role": role, "win": True,
                                   "winner": "wolf" if role == "wolf" else "villager", "days": 1,
                                   "failures": dict.fromkeys(FAIL_KEYS | INTERNAL_KEYS, 0),
                                   "background_failures": dict.fromkeys(FAIL_KEYS, 0), "latency_histogram": {"1": 1},
                                   "max_ms": 0.3, "chats": 0, "dms": 0, "diagnostics": {"test": [1, 1]}})
            return result

        def scan(self, records, games=1):
            with tempfile.TemporaryDirectory(prefix="ww-strength-audit-", dir="/tmp") as temporary:
                path = Path(temporary) / "games.jsonl.gz"
                with gzip.open(path, "wt") as output:
                    for r in records:
                        output.write(json.dumps(r) + "\n")
                return scan_raw(path, (37301,), ("first",), games)

        def test_zero_discordance_has_uncertainty(self):
            m = Pair(1000, 500, 500, 0, 0).report()
            self.assertLess(m["conservative_ci95"][0], 0)
            self.assertGreater(m["conservative_ci95"][1], 0)
            self.assertEqual(m["mcnemar_p"], 1)

        def test_integer_exact_known_cases(self):
            for a, b, p in ((0, 0, 1), (1, 0, 1), (2, 0, .5), (3, 0, .25), (5, 1, .21875), (10, 0, 1 / 512), (10, 10, 1)):
                self.assertEqual(exact_mcnemar(a, b), p)
                self.assertEqual(exact_mcnemar(b, a), p)
            self.assertEqual(exact_mcnemar(1000, 1000), 1)

        def test_wilson_extremes(self):
            self.assertAlmostEqual(wilson(0, 100)[0], 0)
            self.assertAlmostEqual(wilson(100, 100)[1], 1)
            self.assertAlmostEqual(wilson(50, 100)[0], .40382982859014716)

        def test_valid_compressed_records(self):
            raw, metrics, provenance = self.scan(self.fixture(3), 3)
            self.assertEqual(metrics["all"]["seat"]["n"], 3)
            self.assertEqual(raw.raw_lines, 6)
            self.assertEqual(len(provenance["compressed_sha256"]), 64)

        def test_duplicate_rejected(self):
            records = self.fixture()
            with self.assertRaisesRegex(ValueError, "duplicate"):
                self.scan(records + records[:1])

        def test_wrong_role_rejected(self):
            records = self.fixture()
            records[1]["role"] = "wolf" if records[1]["role"] != "wolf" else "villager"
            with self.assertRaisesRegex(ValueError, "deal/seat/role"):
                self.scan(records)

        def test_wrong_seat_and_deal_rejected(self):
            for field in ("seat", "deal_seed"):
                records = self.fixture()
                records[1][field] += 1
                with self.assertRaisesRegex(ValueError, "deal/seat/role"):
                    self.scan(records)

        def test_missing_record_rejected(self):
            with self.assertRaisesRegex(ValueError, "missing paired"):
                self.scan(self.fixture()[:1])
            with self.assertRaisesRegex(ValueError, "missing scenarios"):
                self.scan(self.fixture(), 2)

        def test_incomplete_failure_evidence_rejected(self):
            records = self.fixture()
            del records[0]["failures"]["fallbacks"]
            with self.assertRaisesRegex(ValueError, "incomplete failures"):
                self.scan(records)

        def test_bad_reported_gate_and_stat_detected(self):
            c = Checks()
            c.same({"complete": True}, {"complete": False}, "gate corruption")
            c.same({"n": 3, "delta": .02}, {"n": 3, "delta": .01}, "stat corruption")
            self.assertEqual(len(c.errors), 2)

        def test_bad_strength_gate_cannot_be_hidden(self):
            m = Pair(1000, 505, 500, 5, 0).report()
            role = Pair(300, 150, 150, 0, 0).report()
            regression = Pair(300, 100, 140, 0, 40).report()
            metrics = {"all": {"seat": m, **{r: role for r in ROLES[1:]}}, "opponent:first_switch": {"wolf": regression}}
            result = assessment(metrics)
            self.assertFalse(result["gates"]["substantial_point_gain_ge_2pp"])
            self.assertFalse(result["gates"]["no_prespecified_unacceptable_background_role_regression"])
            self.assertFalse(result["local_strength_gates_pass"])
            self.assertEqual(len(result["all_negative_background_role_slices"]), 1)

        def test_source_inventory_and_path_checks(self):
            self.assertEqual(inventory_digest({"b": "2", "a": "1"}), inventory_digest({"a": "1", "b": "2"}))
            self.assertTrue(safe_source_name(CANDIDATE_FILE))
            for name in (".env", "agent/../../.env", "/tmp/x.py", "scripts/x.py", "data/other.json"):
                self.assertFalse(safe_source_name(name))

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(AuditTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return {"passed": result.wasSuccessful(), "tests_run": result.testsRun,
            "failures": len(result.failures), "errors": len(result.errors), "at": now(),
            "fixtures": "synthetic compressed JSONL only, temporary directories under /tmp", "simulations": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--wait-until", default="2026-10-13T23:13:00-07:00")
    parser.add_argument("--deadline", default="2026-10-13T23:19:00-07:00")
    args = parser.parse_args(argv)
    tests = self_tests()
    if args.self_test:
        return 0 if tests["passed"] else 1
    require(tests["passed"], "audit self-tests failed")
    deadline = datetime.fromisoformat(args.deadline).timestamp()
    wait_until = datetime.fromisoformat(args.wait_until).timestamp()
    require(time.time() < wait_until < deadline, "invalid bounded audit deadlines")
    io = PublicIO(deadline)
    report = {"audit": "WW-strength-confirmation-37301-37302", "started_at": now(), "status": "in_progress",
              "independent_standard_library_only": True, "new_simulations_by_audit": 0, "self_tests": tests,
              "prespecified_panels": PANELS, "panel_definition_fixed_before_confirmation_outcome_read": True,
              "thresholds": {"average_benefit_lower_gt": 0, "substantial_point_gain_ge": .02,
                             "role_min_n": 200, "role_conservative_lower_gt": -.03,
                             "background_role_regression": "n>=200 AND delta<-0.03 AND ordinary95 upper<0",
                             "background_role_multiplicity": FAMILY},
              "statistical_methods": {"absolute_ci": "Wilson z=1.96", "paired_ci": "Wilson on improved/n and worsened/n at z=2.2414027276, [a.low-b.high,a.high-b.low]",
                                      "ordinary_ci": "paired difference sample-variance normal CI z=1.96, descriptive and prespecified regression flag only",
                                      "mcnemar": "two-sided exact integer binomial coefficients, final probability converted to float",
                                      "multiplicity": "Bonferroni min(1,224*p), two timings x28 backgrounds x4 outcomes; panels/seeds/seats descriptive"},
              "runs": {}, "errors": [], "limitations": [
                  "Fixed local scripted opponents, not real model calls, tournament agents or live validation.",
                  "28,000 scenario/deal keys reused by reference/candidate and both timings; never 112,000 independent deals.",
                  "k3 was not run in this confirmation round: multi-controlled-seat robustness is unvalidated.",
                  "Runner retains full begin source file hashes but only an end aggregate digest; audit separately hashes every listed public file before/after, not an invented runner end inventory.",
                  "Full per-file SHA256 inventories are preserved, not a copied source tree. Candidate source text alone is included for bounded static review.",
                  "Source snapshots cannot exclude an unobserved modification and reversal between snapshots, and source inventory is limited to listed files.",
                  "Runner records explicit settings and profile settings, but not all inherited strategy environment values.",
                  "Development seeds selected this candidate; their positive results are not pooled into held-out evidence.",
                  "Nominal nonsignificance is not proof of no harm; all negative slices and underpowered slices are retained."]}
    signatures = {}
    first_source = None
    try:
        plan, plan_prov = io.json(ROOT / PLAN)
        baseline, baseline_prov = io.json(ROOT / BASELINE)
        require(plan["candidate"] == CANDIDATE and plan["candidate_sha256"] == PIN and plan["seeds"] == list(SEEDS), "unexpected frozen plan")
        require(plan["min_gain"] == .02 and plan["role_margin"] == .03, "prespecified thresholds changed")
        require(baseline["protocol"]["backgrounds"] == list(BACKGROUNDS), "B0 backgrounds changed")
        report.update(plan=plan, plan_provenance=plan_prov, baseline_provenance=baseline_prov,
                      baseline_id=baseline["id"], baseline_reference_files=baseline["checked_werewolf_and_shared_runtime_files"],
                      audit_program_provenance={"path": "scripts/audit_werewolf_strength.py", "sha256": digest_bytes(io.bytes(Path(__file__))),
                                                "numeric_comparison_relative_tolerance": 1e-9, "numeric_comparison_absolute_tolerance": 1e-12})
        candidate_bytes = io.bytes(ROOT / CANDIDATE_FILE)
        require(digest_bytes(candidate_bytes) == PIN, "current candidate differs from preregistered pin")
        report["candidate_source_review"] = policy_review(candidate_bytes.decode())
        report["development_context_not_confirmatory"] = {"231": .00464, "232": .00661, "235": .00357, "236": .00839, "237": .00179, "238": .00661,
                                                          "source": "parent handoff; not independently re-audited here", "known_risk": "first_switch negative development slice"}
        for timing in ("rt", "rounds"):
            directory = ROOT / "logs/werewolf_benchmarks" / (PREFIX + timing)
            manifest, manifest_prov = wait_artifact(directory / "manifest.json", io, wait_until)
            if first_source is None:
                first_source = manifest["source"]["files"]
                report["source_audit_begin"] = snapshot_sources(ROOT, first_source, io, "audit_begin")
                write_report(report, ROOT)
            summary, summary_prov = wait_artifact(directory / "summary.json", io, wait_until)
            # Never scan an actively written raw gzip; summary is published after it closes.
            run, signatures[timing] = audit_run(ROOT, timing, manifest, summary, plan, baseline, io)
            run["manifest_provenance"] = manifest_prov
            run["summary_provenance"] = summary_prov
            report["runs"][timing] = run
            write_report(report, ROOT)
            m = run["all_metrics"]["all"]["seat"]
            print(f"RESULT {timing}: {m['candidate_wins']}/{m['n']} vs {m['reference_wins']}/{m['n']}; delta={m['delta']:+.8f}; conservative95={m['conservative_ci95']}; gates={run['assessment']['gates']}", flush=True)
    except (OSError, ValueError, KeyError, TypeError, EOFError, zlib.error) as exc:
        report["errors"].append({"at": now(), "error": f"{type(exc).__name__}: {exc}"})
        print(f"AUDIT BLOCKER {type(exc).__name__}: {exc}", flush=True)
    if first_source is not None:
        try:
            report["source_audit_end"] = snapshot_sources(ROOT, first_source, io, "audit_end")
        except (OSError, ValueError) as exc:
            report["errors"].append({"at": now(), "error": f"end source snapshot: {exc}"})
    report["io_read_attempt_errors"] = io.errors
    completed = len(report["runs"]) == 2
    same_deals = completed and signatures["rt"] == signatures["rounds"]
    report["sample_provenance"] = {"completed_timings": list(report["runs"]),
                                   "raw_game_records_audited": sum(r["raw_provenance"]["records"] for r in report["runs"].values()),
                                   "scenario_timing_pairs": sum(r["unique_scenarios"] for r in report["runs"].values()),
                                   "unique_scenario_deals_across_timings": len(set().union(*(set(s) for s in signatures.values()))) if signatures else 0,
                                   "same_deal_seat_role_across_timings": same_deals,
                                   "unique_deal_rng_seeds_across_timings": len({v[3] for s in signatures.values() for v in s.values()}),
                                   "independent_timing_pooled_test_performed": False}
    source_ok = (report.get("source_audit_begin", {}).get("matches_frozen_inventory", False)
                 and report.get("source_audit_end", {}).get("matches_frozen_inventory", False)
                 and all(r["source_manifest_begin"]["files"] == first_source for r in report["runs"].values()))
    integrity = completed and same_deals and source_ok and not report["errors"] and all(r["integrity"]["passed"] for r in report["runs"].values())
    report["final_gates"] = {"complete_two_timings": completed, "paired_sample_provenance": same_deals,
                             "all_source_inventories_match": source_ok, "independent_integrity_pass": integrity,
                             "average_statistical_success_each_timing": completed and all(r["assessment"]["average_statistical_success"] for r in report["runs"].values()),
                             "unchanged_local_strength_gates_each_timing": completed and all(r["assessment"]["local_strength_gates_pass"] for r in report["runs"].values()),
                             "k3_validated_this_round": False, "live_validated_this_round": False, "adoptable": False}
    report["status"] = "complete" if completed and not report["errors"] else "partial_or_blocked"
    report["outcome"] = "Not adoptable: distinguish local average statistical benefit from +2pp substantial-gain, slice safety, k3 robustness and live/deployment gates. No policy or deployment was changed."
    report["ended_at"] = now()
    write_report(report, ROOT)
    return 0 if integrity else 2


if __name__ == "__main__":
    sys.exit(main())
