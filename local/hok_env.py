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
import builtins
import copy
import importlib
import importlib.abc
import importlib.util
import inspect
import json
import math
from pathlib import Path
import random
import subprocess
import sys
import time
import types
from typing import Callable, Protocol

from altruagent import GameState, RESIGN, WAIT
from altruagent.runner import _validate_decision
from local.hok_sim import DiagnosticGame, SimConfig


class Policy(Protocol):
    def decide(self, raw: dict) -> tuple[object, object]: ...
    def on_result(self, result: dict) -> None: ...


Factory = Callable[..., Policy]

_CANDIDATE_SOURCES: dict[str, dict[str, bytes]] = {}
_CANDIDATE_ORIGINS: dict[str, str] = {}


class _CandidateSourceLoader(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Compile isolated candidate-relative imports from immutable source bytes.

    Timestamp pyc caches can silently load the wrong candidate after rapid
    same-sized edits. Never read/write those caches for a candidate package.
    All non-candidate modules retain Python's normal import behavior.
    """
    def find_spec(self, fullname, path=None, target=None):
        namespace, dot, suffix = fullname.partition(".")
        sources = _CANDIDATE_SOURCES.get(namespace)
        if sources is None or not dot:
            return None
        location = suffix.replace(".", "/")
        is_package = location + "/__init__.py" in sources
        name = location + "/__init__.py" if is_package else location + ".py"
        if name not in sources:
            if any(key.startswith(location + "/") for key in sources):
                return importlib.machinery.ModuleSpec(fullname, loader=None, is_package=True)
            return None
        return importlib.util.spec_from_loader(fullname, self, is_package=is_package,
                                               origin=_CANDIDATE_ORIGINS[namespace] + "/" + name)

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        namespace, _, suffix = module.__name__.partition(".")
        name = suffix.replace(".", "/") + ("/__init__.py" if hasattr(module, "__path__") else ".py")
        module.__file__ = module.__spec__.origin
        # Import redirection is local to this frozen module's builtins, NOT a
        # global monkeypatch. Captured roots cannot accidentally import a live
        # agent/local package from another worktree.
        roots = {key.split("/", 1)[0].removesuffix(".py") for key in _CANDIDATE_SOURCES[namespace]}
        def snapshot_import(name, globals=None, locals=None, fromlist=(), level=0):
            if level == 0 and name.split(".", 1)[0] in roots:
                redirected = namespace + "." + name
                result = builtins.__import__(redirected, globals, locals, fromlist, 0)
                return result if fromlist else sys.modules[namespace + "." + name.split(".", 1)[0]]
            return builtins.__import__(name, globals, locals, fromlist, level)
        module.__dict__["__builtins__"] = {**vars(builtins), "__import__": snapshot_import}
        exec(compile(_CANDIDATE_SOURCES[namespace][name], module.__file__, "exec"), module.__dict__)


_SOURCE_LOADER = _CandidateSourceLoader()


def _source_digest(sources: dict[str, bytes]) -> str:
    hasher = hashlib.sha256()
    for name, data in sorted(sources.items()):
        hasher.update(name.encode())
        hasher.update(b"\0" + data)
    return hasher.hexdigest()


def _load_snapshot(sources: dict[str, bytes], origin: str, module="policy", attr="HokPolicy") -> Factory:
    namespace = "_offline_hok_" + hashlib.sha256(origin.encode()).hexdigest()[:12] \
        + "_" + _source_digest(sources)[:16] + "_" + str(len(_CANDIDATE_SOURCES))
    _CANDIDATE_SOURCES[namespace] = dict(sources)
    _CANDIDATE_ORIGINS[namespace] = origin
    if _SOURCE_LOADER not in sys.meta_path:
        sys.meta_path.insert(0, _SOURCE_LOADER)
    package = types.ModuleType(namespace)
    package.__path__ = [origin]
    sys.modules[namespace] = package
    factory = getattr(importlib.import_module(namespace + "." + module), attr)
    if not callable(factory):
        raise ValueError("snapshot factory is not callable")
    return factory


def accepts_clock(factory: Callable) -> bool:
    params = inspect.signature(factory).parameters
    return "clock" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())


def _snapshot_info(factory: Callable, sources: dict[str, bytes], origin: str, kind: str) -> dict:
    params = inspect.signature(factory).parameters
    return {"factory": getattr(factory, "__qualname__", type(factory).__name__), "source": origin + "/policy.py",
            "source_kind": kind, "source_sha256": _source_digest(sources),
            "source_root": origin if kind == "directory_snapshot" else None,
            "file_sha256": {origin + "/" + name: hashlib.sha256(data).hexdigest()
                            for name, data in sorted(sources.items())},
            "hash_scope": "hok package Python files", "clock_parameter": "clock" in params,
            "accepts_extra_kwargs": any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()),
            "loading": "immutable source snapshot; relative imports frozen; package __init__ not executed"}


class Baseline:
    """Clearly named baselines; random samples offered macros + idle, not
    an unresolved raw button. first-legal intentionally idles in this ordering.
    push is a minimal, legal, active reference, not the official LLM example."""

    def __init__(self, name: str, seed: int = 0, clock: Callable | None = None):
        self.name, self.rng = name, random.Random(seed)
        self._clock = clock
        self._last_seat: int | None = None

    def decide(self, raw: dict):
        actions = raw["legal_actions"]["actions"]
        if not actions:
            return None, None
        eligible = sorted({int(a["action_id"].split(":", 1)[0][1:]) for a in actions})
        seat = next((s for s in eligible if self._last_seat is None or s > self._last_seat), eligible[0])
        self._last_seat = seat
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
    if path.name != "policy.py":
        raise ValueError("candidate file must be named policy.py")
    sources = {p.relative_to(path.parent).as_posix(): p.read_bytes() for p in path.parent.rglob("*.py")}
    factory = _load_snapshot(sources, str(path.parent))
    return factory, _snapshot_info(factory, sources, str(path.parent), "directory_snapshot")


def candidate_revision_factory(repository: str | Path, revision: str) -> tuple[Factory, dict]:
    """Read a local Git commit without checkout/cherry-pick, disk extraction,
    network access or changes to agent/. The entire hok Python package is frozen."""
    os.environ["ARENA_RECORD"] = "0"
    repo = str(Path(repository).resolve())
    def git(*args) -> bytes:
        return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True).stdout
    commit = git("rev-parse", "--verify", "--end-of-options", revision + "^{commit}").decode().strip()
    prefix = "agent/hok/"
    names = git("ls-tree", "-r", "--name-only", commit, "--", prefix).decode().splitlines()
    sources = {name[len(prefix):]: git("show", f"{commit}:{name}") for name in names if name.endswith(".py")}
    if "policy.py" not in sources:
        raise ValueError("revision does not contain agent/hok/policy.py")
    origin = f"git:{commit}:agent/hok"
    factory = _load_snapshot(sources, origin)
    return factory, {**_snapshot_info(factory, sources, origin, "git_snapshot"), "git_commit": commit,
                     "requested_revision": revision}


def frozen_import_factory(spec: str, root: str | Path, revision: str | None = None) -> tuple[Factory, dict]:
    """Freeze any source factory package and its relative imports. Capture agent/
    too for research factories importing agent.hok. SDK/third-party modules retain
    their real identities; dependencies outside captured roots are not frozen."""
    os.environ["ARENA_RECORD"] = "0"
    module, sep, attr = spec.partition(":")
    if not sep or not module or not attr or not all(p.isidentifier() for p in module.split(".")):
        raise ValueError("factory must be a dotted source module:callable")
    top = module.split(".", 1)[0]
    if top == "altruagent":
        raise ValueError("the SDK cannot be a candidate source namespace")
    root = Path(root).resolve()
    scopes = list(dict.fromkeys([top, "agent"]))
    commit = None
    if revision is not None:
        def git(*args):
            return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout
        commit = git("rev-parse", "--verify", "--end-of-options", revision + "^{commit}").decode().strip()
        names = git("ls-tree", "-r", "--name-only", commit, "--", *scopes, top + ".py").decode().splitlines()
        sources = {name: git("show", f"{commit}:{name}") for name in names if name.endswith(".py")}
        origin, kind = f"git:{commit}", "git_snapshot"
        live_scopes = []
    else:
        live_scopes = [root / s for s in scopes if (root / s).is_dir()]
        if (root / (top + ".py")).is_file():
            live_scopes.append(root / (top + ".py"))
        files = [file for scope in live_scopes
                 for file in (scope.rglob("*.py") if scope.is_dir() else [scope])]
        sources = {file.relative_to(root).as_posix(): file.read_bytes() for file in files}
        origin, kind = str(root), "directory_snapshot"
    factory = _load_snapshot(sources, origin, module, attr)
    info = _snapshot_info(factory, sources, origin, kind)
    info.update(source=origin + "/" + module.replace(".", "/") + ".py", source_root=None,
                source_scopes=[str(p) for p in live_scopes], entrypoint=spec,
                hash_scope="factory top-level package plus agent/ Python sources",
                loading="immutable bytes; relative and captured-root absolute imports isolated; no global patch")
    if commit:
        info.update(git_commit=commit, requested_revision=revision)
    return factory, info


def configured_factory(factory: Factory, config: dict) -> Factory:
    """Fresh, validated per-instance options; never change policy module globals."""
    if not isinstance(config, dict) or any(k in config for k in ("seed", "clock")):
        raise ValueError("factory config must be an object without seed/clock overrides")
    params = inspect.signature(factory).parameters
    if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        unknown = set(config) - set(params)
        if unknown:
            raise ValueError("factory config contains unsupported arguments")
    frozen = copy.deepcopy(config)
    def make(seed=0, clock=None):
        return _construct(factory, seed=seed, clock=clock, **copy.deepcopy(frozen))
    return make


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
    if info.get("source_kind") == "git_snapshot":
        return True  # Loaded immutable blobs at a resolved commit, never a moving reference.
    manifest = info["file_sha256"]
    root = info.get("source_root")
    try:
        if root and {str(p) for p in Path(root).rglob("*.py")} != set(manifest):
            return False
        scopes = info.get("source_scopes")
        if scopes:
            actual = {str(file) for scope in map(Path, scopes)
                      for file in (scope.rglob("*.py") if scope.is_dir() else [scope])}
            if actual != set(manifest):
                return False
        return all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == sha for path, sha in manifest.items())
    except OSError:
        return False


def opponent_factory(name: str) -> tuple[Factory, dict]:
    os.environ["ARENA_RECORD"] = "0"
    if name in ("random", "first-legal", "idle", "push"):
        def make(seed=0, clock=None):
            return Baseline(name, seed=seed, clock=clock)
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
            "local_validation_errors": 0, "resignations": 0, "callback_exceptions": {},
            "accepted_by_seat": {}, "action_kind_counts": {}, "_latency_ms": [],
            "reserved_decision_slots": 0, "budget_exhaustions": 0, "nonpending_updates": 0,
            "standing_order_changes": 0, "same_macro_reissues": 0, "cancelled_standing_orders": 0}


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
    stats["unused_decision_slots"] = stats["reserved_decision_slots"] - stats["calls"]
    return stats


class GameClock:
    """Only a scalar timestamp, never a closure exposing hidden engine state."""
    __slots__ = ("seconds",)

    def __init__(self):
        self.seconds = 0.0

    def __call__(self) -> float:
        return self.seconds


class DiagnosticTelemetry:
    """Bounded omniscient POST-HOC summaries, never supplied to either policy.
    No observations, raw actions, tags, positions or per-call traces are retained.
    Tick-start order occupancy is in hero-seconds (five heroes for 1s = 5 hero-s).
    It is sampled before respawn/movement, not an exact effect-duration trace.
    """
    def __init__(self, config: SimConfig):
        self.config = config
        self.teams = {c: {"standing_hero_s": {}, "attack_attempts": {}, "damage_dealt": {},
                         "alive_hero_s": 0.0, "dead_hero_s": 0.0, "cooldown_blocked_hero_s": 0.0,
                         "ready_combat_hero_s": 0.0, "idle_alive_hero_s": 0.0,
                         "raw_movement_hero_s": 0.0,
                         "advance_with_attack_offer_hero_s": 0.0, "retreat_at_home_hero_s": 0.0,
                         "retreat_at_full_hp_hero_s": 0.0, "enemy_half_hero_s": 0.0,
                         "first_objective_damage_step": None, "deaths": 0} for c in (1, 2)}
        self.death_events: list[dict] = []
        self.objective_events: list[dict] = []
        self.snapshots: list[dict] = []
        self.interval = max(1, math.ceil(config.max_steps / 12))

    def __call__(self, event: dict):
        team = self.teams[event["camp"]]
        kind = event["kind"]
        if kind == "attack_attempt":
            key = event["macro"] + ":" + event["target"]
            team["attack_attempts"][key] = team["attack_attempts"].get(key, 0) + 1
        elif kind == "damage":
            key = event["source"] + "_to_" + event["target"]
            team["damage_dealt"][key] = team["damage_dealt"].get(key, 0.0) + event["amount"]
            if event["target"] != "hero" and event["amount"] > 0 and team["first_objective_damage_step"] is None:
                team["first_objective_damage_step"] = event["tick_start_step"]
        elif kind == "death":
            team["deaths"] += 1
            if len(self.death_events) < 12:
                self.death_events.append(dict(event))
        elif kind == "objective_destroyed":
            if len(self.objective_events) < 4:  # the frozen model has exactly four objectives
                self.objective_events.append(dict(event))

    def before_tick(self, game: DiagnosticGame):
        dt = self.config.step_s
        for h in game.heroes.values():
            team = self.teams[h.camp]
            if h.hp <= 0:
                team["dead_hero_s"] += dt
                continue
            team["alive_hero_s"] += dt
            macro = game.standing.get(h.seat, "none")
            team["standing_hero_s"][macro] = team["standing_hero_s"].get(macro, 0.0) + dt
            if macro == "none":
                key = "raw_movement_hero_s" if game.fresh.get(h.seat, {}).get("which_button") == 2 \
                    else "idle_alive_hero_s"
                team[key] += dt
            if (h.camp == 1 and h.x > 0) or (h.camp == 2 and h.x < 0):
                team["enemy_half_hero_s"] += dt
            if macro == "macro_retreat":
                base_x = -self.config.half_length if h.camp == 1 else self.config.half_length
                if math.hypot(h.x - base_x, h.z) <= self.config.base_heal_radius:
                    team["retreat_at_home_hero_s"] += dt
                if h.hp >= h.max_hp:
                    team["retreat_at_full_hp_hero_s"] += dt
            if macro == "macro_advance" and game._can_use(h, "macro_attack"):
                team["advance_with_attack_offer_hero_s"] += dt
            if macro in ("macro_attack", "macro_skill1", "macro_skill2", "macro_skill3"):
                if h.ready.get(macro, 0.0) > game.now + 1e-9:
                    team["cooldown_blocked_hero_s"] += dt
                elif game._can_use(h, macro):
                    team["ready_combat_hero_s"] += dt

    def capture(self, game: DiagnosticGame):
        if game.step % self.interval and not game.terminal:
            return
        sample = {"step": game.step, "game_s": game.now, "teams": {}}
        for camp in (1, 2):
            heroes = [h for h in game.heroes.values() if h.camp == camp]
            sample["teams"][str(camp)] = {
                "alive": sum(h.hp > 0 for h in heroes), "hero_hp_total": sum(h.hp for h in heroes),
                "retreating": sum(h.hp > 0 and game.standing.get(h.seat) == "macro_retreat" for h in heroes),
                "objective_damage": game.structure_damage[camp], "kills": game.kills[camp],
                "healing": game.healed[camp],
                "own_structure_hp": {s.type: s.hp for s in game.structures.values() if s.camp == camp}}
        if self.snapshots and self.snapshots[-1]["step"] == game.step:
            return
        if len(self.snapshots) < 13:
            self.snapshots.append(sample)
        elif game.terminal:
            self.snapshots[-1] = sample

    def report(self, game: DiagnosticGame) -> dict:
        return {"scope": "omniscient post-hoc instrumentation; never policy input",
                "units": "pre-respawn/movement tick-start occupancy in hero-seconds; capped damage split "
                         "proportionally by source type; attack attempts are exact model effects, not decisions",
                "teams": {str(c): {**self.teams[c], "healing": game.healed[c], "respawns": game.respawns[c]}
                          for c in (1, 2)},
                "first_deaths": self.death_events, "objective_events": self.objective_events,
                "snapshots": self.snapshots, "max_records": {"first_deaths": 12, "objectives": 4, "snapshots": 13}}


def _policy_call(policy, game: DiagnosticGame, camp: int, stats: dict) -> tuple[bool, dict | None]:
    """One slot. Return whether another slot is useful, and any fatal failure."""
    raw = game.view(camp)
    if not raw["legal_actions"]["actions"]:
        return False, None
    start = time.perf_counter_ns()
    stats["calls"] += 1
    try:
        action, _tag = policy.decide(raw)
    except Exception as exc:
        _count_error(stats, "exceptions", exc)
        return False, {"camp": camp, "kind": "policy_exception"}
    finally:
        stats["_latency_ms"].append((time.perf_counter_ns() - start) / 1e6)
    try:
        normalized = _validate_decision(WAIT if action is None else action,
                                        GameState.from_mcp_state(raw).legal_actions)
        if normalized is not WAIT and normalized is not RESIGN and normalized.action is not None:
            json.dumps(normalized.action, allow_nan=False)
    except Exception:
        stats["invalid"] += 1
        stats["local_validation_errors"] += 1
        return False, {"camp": camp, "kind": "local_validation_error"}
    if normalized is RESIGN:
        stats["resignations"] += 1
        game.resign(camp)
        return False, None
    if normalized is WAIT:
        stats["waits"] += 1
        if game.config.mode == "lockstep":
            stats["invalid"] += 1
            return False, {"camp": camp, "kind": "lockstep_wait"}
        return False, None
    sent = normalized.action if normalized.action is not None else normalized.action_id
    decoded = game._decode(sent)
    seat_hint = decoded.get("seat") if decoded is not None else None
    before = game.standing.get(seat_hint) if type(seat_hint) is int else None
    result = game.submit(camp, sent, raw["state_version"])
    failure = None
    if result.get("error"):
        stats["invalid"] += 1
        if game.config.mode == "lockstep":
            failure = {"camp": camp, "kind": result["error"]}
    else:
        stats["accepted"] += 1
        seat = game._decode(sent)["seat"]
        if seat not in raw["simultaneous"]["your_pending_seats"]:
            stats["nonpending_updates"] += 1
        stats["accepted_by_seat"][str(seat)] = stats["accepted_by_seat"].get(str(seat), 0) + 1
        accepted = game.fresh[seat]
        after = game.standing.get(seat)
        stats["standing_order_changes"] += before != after
        stats["same_macro_reissues"] += accepted.get("macro") is not None and before == after
        stats["cancelled_standing_orders"] += before is not None and after is None
        kind = accepted.get("macro", f"button_{accepted.get('which_button')}")
        stats["action_kind_counts"][kind] = stats["action_kind_counts"].get(kind, 0) + 1
    callback = getattr(policy, "on_result", None)
    if callback:
        try:
            callback(result)
        except Exception as exc:
            _count_error(stats, "callback_exceptions", exc)
            failure = {"camp": camp, "kind": "callback_exception"}
    return failure is None, failure


def play_game(candidate: Factory, opponent: Factory, *, config: SimConfig | None = None,
              seed: int = 0, candidate_camp: int = 1, decision_steps: int = 1,
              calls_per_step: int = 6, diagnostics: bool = False) -> dict:
    """Run one deterministic diagnostic leg. Fresh policy objects per game.

    Server-refused realtime orders get on_result and may retry. The actual SDK
    validator rejects unlisted IDs/types locally (fatal even in realtime).
    Those errors, illegal lockstep orders, exceptions and lockstep WAIT are
    *evaluation protocol forfeits*.
    This does not pretend to reproduce the platform's random takeover after
    a worker dies. Equal slots are allocated to each team, interleaved with
    alternating initiative. WAIT voluntarily gives up the remaining slots.
    A legal policy exhausting its realtime budget NEVER forfeits: time advances.
    """
    os.environ["ARENA_RECORD"] = "0"
    if type(decision_steps) is not int or decision_steps < 1:
        raise ValueError("decision_steps must be a positive integer")
    if type(calls_per_step) is not int or calls_per_step < 5:
        raise ValueError("calls_per_step must be at least five")
    config = config or SimConfig()
    telemetry = DiagnosticTelemetry(config) if diagnostics else None
    game = DiagnosticGame(config, seed, event_sink=telemetry)
    if telemetry:
        telemetry.capture(game)
    game.seats(candidate_camp)
    factories = {candidate_camp: candidate, 3 - candidate_camp: opponent}
    # The role gets the same RNG seed in both legs; only its camp changes.
    clocks = {camp: GameClock() for camp in (1, 2)}
    policies = {camp: _construct(factory, seed=seed if camp == candidate_camp else seed + 1_000_003,
                                 clock=clocks[camp]) for camp, factory in factories.items()}
    clock_injected = {camp: accepts_clock(factory) for camp, factory in factories.items()}
    stats = {camp: _new_stats() for camp in (1, 2)}
    trajectory = hashlib.sha256()
    trajectory.update(game.state_hash().encode())
    protocol_failure = None
    while not game.terminal:
        for clock in clocks.values():
            clock.seconds = game.now
        due = game.config.mode == "lockstep" or game.step % decision_steps == 0
        active = {1, 2} if due else set()
        if due:
            for st in stats.values():
                st["reserved_decision_slots"] += calls_per_step
        for slot in range(calls_per_step if due else 0):
            # Same timestamp/physics for both camps, no wall-clock race advantage.
            order = (1, 2) if (game.step + slot) % 2 == 0 else (2, 1)
            for camp in order:
                if camp not in active:
                    continue
                keep_active, protocol_failure = _policy_call(policies[camp], game, camp, stats[camp])
                if not keep_active:
                    active.discard(camp)
                if protocol_failure:
                    game.finish(3 - camp, "evaluation_protocol_forfeit")
                if game.terminal:
                    break
            if game.terminal or not active:
                break
        if due and not game.terminal and game.config.mode == "realtime":
            for camp in active:
                stats[camp]["budget_exhaustions"] += 1
        if not game.terminal:
            if telemetry:
                telemetry.before_tick(game)
            game.tick()
        if telemetry:
            telemetry.capture(game)
        trajectory.update(game.state_hash().encode())
    return {"seed": seed, "candidate_camp": candidate_camp, "steps": game.step,
            "elapsed_game_s": game.now, "result": game.result(),
            "candidate_score": game.result()["platform_scores"][str(candidate_camp)],
            "protocol_failure": protocol_failure,
            "clock_injected": {"candidate": clock_injected[candidate_camp],
                               "opponent": clock_injected[3 - candidate_camp]},
            "stats": {"candidate": _final_stats(stats[candidate_camp]),
                      "opponent": _final_stats(stats[3 - candidate_camp])},
            "healing": game.healed, "respawns": game.respawns,
            "diagnostics": telemetry.report(game) if telemetry else None,
            "final_state_sha256": game.state_hash(), "trajectory_sha256": trajectory.hexdigest()}
