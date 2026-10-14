from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import hashlib
import json
import random

from agent.matchstate import encode
from agent.werewolf.policy import WerewolfPolicy
from . import eval_werewolf as ev
from . import werewolf_benchmark as wb
from .werewolf_env import ContestantSeat
from .werewolf_research import ResearchGame, PairCounts


SETTINGS = {"ARENA_RECORD": "0", "ARENA_STATE": "0", "ARENA_SUMMARY": "0", "ARENA_TC_WATCHDOG": "0",
            "ARENA_WW_TIMING": "rt", "ARENA_WW_TACTICS": "off", "ARENA_WW_DM": "off",
            "ARENA_WW_MESSAGING": "on", "ARENA_WW_WORDING": "new", "ARENA_WW_SAME_NAMES": "off", "ARENA_WW_LISTEN": "on"}
PROFILES = ("b0", "first", "random")


def canonical(value):
    return json.dumps(encode(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


class SimpleControlledSeat(ContestantSeat):
    def __init__(self, mode):
        if mode not in ("first", "random"):
            raise ValueError("unknown calibration profile")
        super().__init__(lambda: None, name=mode)
        self.mode = mode

    def choose(self, raw):
        actions = raw["legal_actions"]["actions"]
        return (actions[0] if self.mode == "first" else self.rng.choice(actions))["action_id"]

    def message(self, raw):
        return None


@dataclass(frozen=True)
class Intervention:
    index: int
    action: int
    expected_prefix: str
    expected_input: str
    expected_proposal: int


@dataclass
class ReplayGame(ResearchGame):
    controlled: int = 0
    collect_points: bool = False
    intervention: Intervention | None = None
    points: list[dict] = field(default_factory=list)
    own_decisions: int = 0
    events: int = 0
    prefix: str = field(default_factory=lambda: digest("ww-causal-events-v1"))
    applied: bool = False

    def event(self, value):
        if self.events >= 5000:
            raise ValueError("unexpected callback count")
        self.prefix = hashlib.sha256(bytes.fromhex(self.prefix) + canonical(value)).hexdigest()
        self.events += 1

    def _choose(self, seat, sub, legal):
        raw = self.state(seat, "moving", sub, legal)
        raw_hash, before = digest(raw), self.prefix
        ours = seat == self.controlled
        index = self.own_decisions if ours else None
        if ours:
            self.own_decisions += 1
            if self.own_decisions > 30:
                raise ValueError("unexpected controlled decision count")
        force = self.intervention if ours and self.intervention and index == self.intervention.index else None
        if force:
            if (self.applied or type(force.action) is not int or force.action not in legal
                    or force.expected_prefix != before or force.expected_input != raw_hash):
                raise ValueError("illegal intervention or mismatched replay prefix/input")
        proposed = super()._choose(seat, sub, legal)
        policy = getattr(getattr(self.seats[seat], "agent", None), "ww", None)
        memory = deepcopy(policy.export_memory()) if ours and self.collect_points and policy else None
        if force and force.expected_proposal != proposed:
            raise ValueError("baseline proposal differs at intervention")
        actual = force.action if force else proposed
        if force:
            self.applied = True
            self.action_trace[-1].update(action=actual, choice=str(actual))
        if ours and self.collect_points:
            self.points.append({"index": index, "day": self.day, "phase": sub, "legal": list(legal),
                                "proposed": proposed, "action": actual, "evaluator_prefix": before,
                                "input_hash": raw_hash, "raw": deepcopy(raw), "memory": memory})
        self.event({"kind": "action", "seat": seat, "input": raw_hash, "action": actual})
        return actual

    def _ask_message(self, seat, sent):
        raw_hash = digest(self.state(seat, "messaging", "day_vote"))
        start = len(self.messages)
        result = super()._ask_message(seat, sent)
        self.event({"kind": "message", "seat": seat, "input": raw_hash, "result": result,
                    "messages": self.messages[start:], "terminated": list(self.terminated), "sent": dict(sent)})
        return result


def summarize_game(game, scenario, label, me, gseed, returns):
    controlled = game.seats[me]
    errors = {name: sum(counts) for name, counts in game.stats.items() if name in ev.FAIL_KEYS and name != "window_close"}
    if isinstance(controlled, wb.MeasuredSeat):
        errors.update(policy_errors=controlled.policy_errors, policy_fallbacks=controlled.policy_fallbacks,
                      logged_errors=controlled.logged_errors, fallbacks=controlled.agent.fallbacks,
                      overruns=controlled.agent.overruns, listen_errors=controlled.agent.listen_stats.get("errors", 0),
                      fallback_paths=sum(n for p, n in controlled.paths.items() if p.split("+")[0] in ("fallback", "crash", "busy", "overrun")))
    trace = {"roles": game.roles, "dead": game.dead, "votes": game.votes, "messages": game.messages, "actions": game.action_trace}
    row = {"id": scenario.key, "scenario": asdict(scenario), "profile": label, "seat": me,
           "deal_seed": gseed, "role": game.roles[me], "win": returns[me] > 0, "winner": game.winner,
           "days": game.day, "trace_hash": digest(trace), "event_hash": game.prefix, "events": game.events,
           "errors": errors, "window_closures": sum(game.stats["window_close"]), "intervention_applied": game.applied,
           "decisions": game.own_decisions}
    if any(errors.values()):
        raise ValueError(f"invalid rollout: {errors}")
    if game.intervention and not game.applied:
        raise ValueError("intervention point was not reached")
    return row


def play(scenario, profile="b0", intervention=None, collect=False, timing="rt"):
    if profile not in PROFILES or scenario.k != 1 or ev.uses_llm_real(scenario.background):
        raise ValueError("only fixed local single-seat profiles allowed")
    if intervention is not None and profile != "b0":
        raise ValueError("only B0 continuation interventions are defined")
    gseed, ours, types = ev.deal(scenario.seed, scenario.game, scenario.background, 1)
    me = ours[0]
    with wb.environment(SETTINGS | {"ARENA_WW_TIMING": timing}):
        controlled = wb.MeasuredSeat(ev.factory("agent"), {}) if profile == "b0" else SimpleControlledSeat(profile)
        seats = [controlled if s == me else ev.make_seat(types[s]) for s in range(7)]
        game = ReplayGame(seats, random.Random(gseed), strict_messaging=True, controlled=me,
                          collect_points=collect, intervention=intervention)
        returns = game.play()
        row = summarize_game(game, scenario, profile, me, gseed, returns)
    return row, game.points


def select_losses(records, backgrounds, per_role=100):
    selected, availability = [], {}
    for role in ("villager", "seer"):
        buckets = {}
        for background in backgrounds:
            rows = [r for r in records if r["profile"] == "b0" and r["role"] == role and not r["win"]
                    and r["scenario"]["background"] == background]
            buckets[background] = deque(sorted(rows, key=lambda r: hashlib.sha256(("ww-causal-loss-v1:" + r["id"]).encode()).hexdigest()))
        availability[role] = {b: len(rows) for b, rows in buckets.items()}
        count = 0
        while count < per_role:
            progress = False
            for background in backgrounds:
                if buckets[background] and count < per_role:
                    selected.append(buckets[background].popleft())
                    count += 1
                    progress = True
            if not progress:
                raise ValueError(f"insufficient fixed-pool {role} losses: {count}/{per_role}")
    if len({r["id"] for r in selected}) != len(selected):
        raise ValueError("duplicate diagnostic selection")
    return selected, availability


def describe(point):
    shadow = WerewolfPolicy()
    if point["memory"] is not None:
        shadow.import_memory(deepcopy(point["memory"]))
    raw = deepcopy(point["raw"])
    with wb.environment(SETTINGS):
        decision = shadow.act(raw)
        v = shadow._view(raw)
        c = shadow.claims(v, private=True)
        private_wolves, clears = shadow.seer_knowledge(v) if v.role == "seer" else ([], [])
    if int(decision["action_id"]) != point["proposed"]:
        raise ValueError("seat-visible reconstruction did not reproduce B0")
    reason = decision["reasoning_summary"]
    category = ("investigation" if point["phase"] == "night_seer" else "private_wolf" if private_wolves
                else "authenticated_seer_report" if c.trusted is not None and v.dead.get(c.trusted) == "seer"
                else "live_claim" if c.trusted_wolves(v.alive) else "claim_conflict" if c.liars or len(c.live(v)) >= 2
                else "soft")
    return {"category": category, "reason": reason, "private_wolves": private_wolves, "private_clears": clears,
            "checked": sorted(v.investigations), "known_clear_proposal": point["proposed"] in clears,
            "visible_hash": digest({"raw": point["raw"], "memory": point["memory"]})}


def branches(record, before_branch=None):
    if record["profile"] != "b0" or record["role"] not in ("villager", "seer") or record["win"] is not False:
        raise ValueError("diagnosis requires a selected B0 good-role loss")
    scenario = wb.Scenario(**record["scenario"])
    replay, points = play(scenario, collect=True)
    for key in ("trace_hash", "event_hash", "role", "seat", "deal_seed", "win"):
        if replay[key] != record[key]:
            raise ValueError(f"baseline replay mismatch: {key}")
    for point in points:
        if point["phase"] not in ("day_vote", "night_seer"):
            raise ValueError("unexpected good-role action phase")
        context = describe(point)
        for action in point["legal"]:
            if before_branch:
                before_branch()
            override = Intervention(point["index"], action, point["evaluator_prefix"], point["input_hash"], point["proposed"])
            result, _ = play(scenario, intervention=override)
            noop = action == point["proposed"]
            if noop and any(result[k] != replay[k] for k in ("trace_hash", "event_hash", "win", "winner", "days")):
                raise ValueError("no-op intervention changed full continuation")
            guarded = ((action in context["private_wolves"] if context["private_wolves"] else action not in context["private_clears"])
                       if point["phase"] == "day_vote" else
                       (action not in context["checked"] or all(a in context["checked"] for a in point["legal"])))
            yield {"case": record["id"], "role": record["role"], "background": scenario.background,
                   "point": point["index"], "day": point["day"], "phase": point["phase"],
                   "baseline_action": point["proposed"], "action": action, "noop": noop,
                   "retains_private_fact_and_unread_guard": guarded, "outcome": result, "context": context,
                   "point_observation": point if noop else None}


def calibration_metrics(records):
    grouped = defaultdict(dict)
    for row in records:
        group = grouped[row["id"]]
        if row["profile"] in group:
            raise ValueError("duplicate calibration profile")
        group[row["profile"]] = row
    pairs = defaultdict(PairCounts)
    for group in grouped.values():
        if set(group) != set(PROFILES):
            raise ValueError("incomplete calibration group")
        base = group["b0"]
        for name in ("first", "random"):
            other = group[name]
            if any(base[k] != other[k] for k in ("id", "scenario", "seat", "deal_seed", "role")):
                raise ValueError("calibration pair identity mismatch")
            for scope in ("all", "background:" + base["scenario"]["background"], "seed:" + str(base["scenario"]["seed"])):
                for role in ("seat", base["role"]):
                    pairs[(name, scope, role)].add(base["win"], other["win"])
    out = {}
    for (name, scope, role), pair in pairs.items():
        out.setdefault(name, {}).setdefault(scope, {})[role] = pair.report()
    return out
