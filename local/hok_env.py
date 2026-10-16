"""Trusted-policy offline harness for the explicitly synthetic hok_sim model.

No SDK runner/network is started. ``decide(raw) -> (action, tag)`` and
``on_result(result)`` are the reusable HokPolicy interface. Tags, observations,
actions and exception messages are NEVER recorded. CPU decision latency is
measured, but does not advance simulated time or estimate network performance.
Policies run in-process: this is NOT a sandbox or a hard decision-time limiter.
"""
from __future__ import annotations

import os

os.environ["ARENA_RECORD"] = "0"  # Set before importing any candidate.

import hashlib
import importlib
import importlib.abc
import importlib.util
import inspect
import math
from pathlib import Path
import random
import sys
import time
import types
from typing import Callable, Protocol

from local.hok_sim import DiagnosticGame, SimConfig


class Policy(Protocol):
    def decide(self, raw: dict) -> tuple[object, object]: ...
    def on_result(self, result: dict) -> None: ...


Factory = Callable[..., Policy]

_CANDIDATE_ROOTS: dict[str, Path] = {}


class _CandidateSourceLoader(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Read isolated candidate-relative imports directly from source.

    Timestamp pyc caches can silently load the wrong candidate after rapid
    same-sized edits. Never read/write those caches for a candidate package.
    All non-candidate modules retain Python's normal import behavior.
    """
    def find_spec(self, fullname, path=None, target=None):
        namespace, dot, suffix = fullname.partition(".")
        root = _CANDIDATE_ROOTS.get(namespace)
        if root is None or not dot:
            return None
        location = root.joinpath(*suffix.split("."))
        is_package = location.is_dir()
        source = location / "__init__.py" if is_package else location.with_suffix(".py")
        if not source.is_file():
            return None
        return importlib.util.spec_from_file_location(
            fullname, source, loader=self,
            submodule_search_locations=[str(location)] if is_package else None)

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        path = Path(module.__spec__.origin)
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)


_SOURCE_LOADER = _CandidateSourceLoader()


class Baseline:
    """Clearly named baselines; random samples offered macros + idle, not
    an unresolved raw button. first-legal intentionally idles in this ordering.
    push is a minimal, legal, active reference, not the official LLM example."""

    def __init__(self, name: str, seed: int = 0, **_):
        self.name, self.rng = name, random.Random(seed)

    def decide(self, raw: dict):
        actions = raw["legal_actions"]["actions"]
        if not actions:
            return None, None
        seat = raw["simultaneous"]["your_pending_seats"][0]
        offers = [a for a in actions if a["action_id"].startswith(f"s{seat}:")
                  and (":macro_" in a["action_id"] or a["action_id"].endswith(":button_0"))]
        if self.name == "random":
            return self.rng.choice(offers)["action_id"], None
        if self.name == "first-legal":
            return offers[0]["action_id"], None
        if self.name == "idle":
            return f"s{seat}:button_0", None
        by_macro = {a["action_id"].split(":", 1)[1]: a["action_id"] for a in offers}
        for macro in ("macro_skill3", "macro_skill2", "macro_skill1", "macro_attack", "macro_advance"):
            if macro in by_macro:
                return by_macro[macro], None
        return f"s{seat}:button_0", None

    def on_result(self, result: dict):
        pass


def _construct(factory: Callable, *, seed: int, clock: Callable, **extra):
    """Inject deterministic game time if supported; do not swallow factory errors."""
    params = inspect.signature(factory).parameters
    accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    kwargs = {k: v for k, v in {"seed": seed, "clock": clock, **extra}.items()
              if accepts_kwargs or k in params}
    return factory(**kwargs)


def import_factory(spec: str) -> Factory:
    os.environ["ARENA_RECORD"] = "0"
    module, sep, attr = spec.partition(":")
    if not sep or not module or not attr:
        raise ValueError("factory must be importable.module:callable")
    factory = getattr(importlib.import_module(module), attr)
    if not callable(factory):
        raise ValueError(f"{spec} is not callable")
    return factory


def candidate_factory(directory: str | Path) -> tuple[Factory, dict]:
    """Load only the candidate's policy package, not agent.agent or a live runtime.

    Supports repo/agent/hok/policy.py, agent/hok/policy.py, hok/policy.py or
    policy.py itself. Relative imports inside the hok package are supported.
    Absolute imports must already resolve via the caller's PYTHONPATH.
    """
    os.environ["ARENA_RECORD"] = "0"
    root = Path(directory).resolve()
    paths = [root] if root.is_file() else [root / "agent/hok/policy.py", root / "hok/policy.py",
                                         root / "policy.py"]
    path = next((p for p in paths if p.is_file()), None)
    if path is None:
        raise ValueError(f"no HokPolicy policy.py under {root}")
    package_hash = hashlib.sha256()
    for source in sorted(path.parent.rglob("*.py")):
        package_hash.update(str(source.relative_to(path.parent)).encode())
        package_hash.update(b"\0" + source.read_bytes())
    namespace = "_offline_hok_" + hashlib.sha256(str(path).encode()).hexdigest()[:12] \
        + "_" + package_hash.hexdigest()[:16] + "_" + str(len(_CANDIDATE_ROOTS))
    _CANDIDATE_ROOTS[namespace] = path.parent
    if _SOURCE_LOADER not in sys.meta_path:
        sys.meta_path.insert(0, _SOURCE_LOADER)
    package = types.ModuleType(namespace)
    package.__path__ = [str(path.parent)]
    sys.modules[namespace] = package
    spec = importlib.util.spec_from_file_location(namespace + ".policy", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    # Compile the policy's actual source; the finder does the same for all
    # relative dependencies, without writing into the candidate directory.
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module.HokPolicy, source_info(module.HokPolicy, source_root=path.parent)


def source_info(factory: Callable, source_root: Path | None = None) -> dict:
    path = inspect.getsourcefile(factory)
    files = sorted(source_root.rglob("*.py")) if source_root else ([Path(path)] if path else [])
    hasher = hashlib.sha256()
    for file in files:
        hasher.update(str(file.relative_to(source_root) if source_root else file.name).encode())
        hasher.update(b"\0" + file.read_bytes())
    params = inspect.signature(factory).parameters
    return {"factory": getattr(factory, "__qualname__", type(factory).__name__),
            "source": str(path) if path else None, "source_sha256": hasher.hexdigest() if files else None,
            "hash_scope": "hok package Python files" if source_root else "factory module only",
            "source_root": str(source_root) if source_root else None,
            "file_sha256": {str(file): hashlib.sha256(file.read_bytes()).hexdigest() for file in files},
            "clock_parameter": "clock" in params,
            "accepts_extra_kwargs": any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())}


def source_unchanged(info: dict) -> bool:
    """Detect edited/deleted/added source during an evaluation, without reading
    credentials or including source contents in the report."""
    manifest = info["file_sha256"]
    root = info.get("source_root")
    try:
        if root and {str(p) for p in Path(root).rglob("*.py")} != set(manifest):
            return False
        return all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == sha for path, sha in manifest.items())
    except OSError:
        return False


def opponent_factory(name: str) -> tuple[Factory, dict]:
    os.environ["ARENA_RECORD"] = "0"
    if name in ("random", "first-legal", "idle", "push"):
        def make(seed=0, clock=None):
            return Baseline(name, seed=seed)
        return make, {**source_info(Baseline), "name": name,
                      "description": "first listed action (idle), deliberately inert" if name == "first-legal"
                      else "uniform offered macros + idle per pending seat" if name == "random"
                      else "minimal active attack/advance reference" if name == "push" else "explicit idle control"}
    if ":" in name and not name.startswith("research:"):
        factory = import_factory(name)
        return factory, {**source_info(factory), "name": name}
    # Supplied independently in another owned file; never reimplement those bots here.
    research = importlib.import_module("local.hok_opponents_research")
    bot_name = name.removeprefix("research:")
    def make(seed=0, clock=None):
        return _construct(research.make_bot, seed=seed, clock=clock, name=bot_name)
    return make, {**source_info(research.make_bot), "name": bot_name}


def _new_stats() -> dict:
    return {"calls": 0, "accepted": 0, "invalid": 0, "waits": 0, "exceptions": {},
            "callback_exceptions": {}, "accepted_by_seat": {}, "action_kind_counts": {}, "_latency_ms": []}


def _count_error(stats: dict, key: str, exc: Exception):
    # Exception messages can contain raw decisions. Only keep their types.
    name = type(exc).__name__
    stats[key][name] = stats[key].get(name, 0) + 1


def _final_stats(stats: dict) -> dict:
    samples = sorted(stats.pop("_latency_ms"))
    def quantile(q):
        return samples[min(len(samples) - 1, math.ceil(q * len(samples)) - 1)] if samples else 0.0
    stats["latency_ms"] = {"count": len(samples), "mean": sum(samples) / len(samples) if samples else 0.0,
                           "p50": quantile(0.50), "p95": quantile(0.95), "max": max(samples, default=0.0)}
    stats["legal_fraction"] = (stats["accepted"] / (stats["accepted"] + stats["invalid"])
                               if stats["accepted"] + stats["invalid"] else None)
    return stats


def play_game(candidate: Factory, opponent: Factory, *, config: SimConfig | None = None,
              seed: int = 0, candidate_camp: int = 1, decision_steps: int = 1,
              calls_per_step: int = 6) -> dict:
    """Run one deterministic diagnostic leg. Fresh policy objects per game.

    Illegal realtime orders get on_result and may retry; illegal lockstep
    orders, exceptions and lockstep WAIT are *evaluation protocol forfeits*.
    This does not pretend to reproduce the platform's random takeover after
    a worker dies. Call-budget exhaustion is likewise a harness forfeit in
    lockstep, not evidence of an in-game defeat.
    """
    os.environ["ARENA_RECORD"] = "0"
    if type(decision_steps) is not int or decision_steps < 1:
        raise ValueError("decision_steps must be a positive integer")
    if type(calls_per_step) is not int or calls_per_step < 5:
        raise ValueError("calls_per_step must be at least five")
    game = DiagnosticGame(config, seed)
    game.seats(candidate_camp)
    factories = {candidate_camp: candidate, 3 - candidate_camp: opponent}
    # The role gets the same RNG seed in both legs; only its camp changes.
    policies = {camp: _construct(factory, seed=seed if camp == candidate_camp else seed + 1_000_003,
                                 clock=lambda: game.now) for camp, factory in factories.items()}
    stats = {camp: _new_stats() for camp in (1, 2)}
    trajectory = hashlib.sha256()
    trajectory.update(game.state_hash().encode())
    protocol_failure = None
    while not game.terminal:
        for camp in (1, 2):
            if game.config.mode == "realtime" and game.step % decision_steps:
                continue
            st = stats[camp]
            for _ in range(calls_per_step):
                raw = game.view(camp)
                if not raw["simultaneous"]["your_pending_seats"]:
                    break
                start = time.perf_counter_ns()
                st["calls"] += 1
                try:
                    action, _tag = policies[camp].decide(raw)
                except Exception as exc:
                    _count_error(st, "exceptions", exc)
                    protocol_failure = {"camp": camp, "kind": "policy_exception"}
                    break
                finally:
                    st["_latency_ms"].append((time.perf_counter_ns() - start) / 1e6)
                # SDK WAIT is supported alongside HokPolicy's None.
                from altruagent import WAIT
                if action is None or action is WAIT:
                    st["waits"] += 1
                    if game.config.mode == "lockstep":
                        st["invalid"] += 1
                        protocol_failure = {"camp": camp, "kind": "lockstep_wait"}
                    break
                result = game.submit(camp, action, raw["state_version"])
                if result.get("error"):
                    st["invalid"] += 1
                    if game.config.mode == "lockstep":
                        protocol_failure = {"camp": camp, "kind": result["error"]}
                else:
                    st["accepted"] += 1
                    seat = next(s for s in raw["simultaneous"]["your_pending_seats"] if s not in game.pending)
                    st["accepted_by_seat"][str(seat)] = st["accepted_by_seat"].get(str(seat), 0) + 1
                    accepted = game.fresh[seat]
                    kind = accepted.get("macro", f"button_{accepted.get('which_button')}")
                    st["action_kind_counts"][kind] = st["action_kind_counts"].get(kind, 0) + 1
                callback = getattr(policies[camp], "on_result", None)
                if callback:
                    try:
                        callback(result)
                    except Exception as exc:
                        _count_error(st, "callback_exceptions", exc)
                        protocol_failure = {"camp": camp, "kind": "callback_exception"}
                if protocol_failure:
                    break
            if not protocol_failure and game.config.mode == "lockstep" and set(game.seats(camp)) & game.pending:
                protocol_failure = {"camp": camp, "kind": "call_budget_exhausted"}
            if protocol_failure:
                game.finish(3 - camp, "evaluation_protocol_forfeit")
                break
        if not game.terminal:
            game.tick()
        trajectory.update(game.state_hash().encode())
    return {"seed": seed, "candidate_camp": candidate_camp, "steps": game.step,
            "elapsed_game_s": game.now, "result": game.result(),
            "candidate_score": game.result()["platform_scores"][str(candidate_camp)],
            "protocol_failure": protocol_failure,
            "stats": {"candidate": _final_stats(stats[candidate_camp]),
                      "opponent": _final_stats(stats[3 - candidate_camp])},
            "healing": game.healed, "respawns": game.respawns,
            "final_state_sha256": game.state_hash(), "trajectory_sha256": trajectory.hexdigest()}
