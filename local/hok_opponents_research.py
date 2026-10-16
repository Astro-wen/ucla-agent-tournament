"""Independent, observation-only HoK opponents for LOCAL research, not deployment.

Interface: ``make_bot(name, seed=0, clock=None)`` returns a fresh match-scoped bot with
``decide(raw) -> (action, tag)`` and ``on_result(result)``. Actions are current
offered IDs, or a raw-button payload filled from its current legal values.
``(None, None)`` means no eligible offer (the real-time caller may WAIT).

All bots use the SAME round-robin scheduler: one currently legal owned seat per
call. In REALTIME, pending only tracks heroes without a fresh action this step,
so already-ordered heroes remain eligible for reevaluation. Only LOCKSTEP
restricts the scheduler to ``simultaneous.your_pending_seats`` when supplied.
There is no wall clock, standing-order suppression, batching, or hidden access.
Repeated orders deliberately receive the same scheduling opportunity as changes.
The evaluator must provide the same number of decision opportunities to both
teams; this module does not itself advance a simulator.
In particular, random can keep choosing forever: cap equal runtime calls per
team and advance game time, rather than waiting for quiescence or declaring a
forfeit. One call issues at most one order; five eligible calls cover five seats.
The optional injected game clock is retained for factory compatibility. These
opponents have no time-based rules, so they do not read it or a wall clock:
pulse cadence remains explicitly measured in each hero's decision opportunities.

Baselines:
* idle: explicitly send the offered no-op, cancelling an old standing order.
* first_legal: first usable offered action for the scheduled seat, in input order.
* uniform_random: uniformly sample distinct current macro IDs plus ONE offered
  idle action, not a fixed macro menu and not the large raw-button space.
* objective_rusher: hit a nearby labelled enemy structure, otherwise advance.
* opportunistic_combat: prioritize nearby labelled heroes, then other targets.
* cautious_defender: heal, avoid visible numerical disadvantage, and guard
  visibly threatened allied structures; otherwise engage/advance.

Stress cases (functional coverage, NOT evidence of competitive strength):
* pulse_pressure: seeded, repeatable four-turn engagement / two-turn retreat
  waves, measured in each hero's own decision opportunities.
* split_pressure: seeded heterogeneous roles (two rushers, two fighters, one
  defender) across the five seats, exercising simultaneous different orders.

If an idle is absent, idle's safety fallback is the first usable current offer;
random samples only macros when idle is absent. If neither exists, every bot
falls back to a usable raw button. No fabricated button_0 is ever submitted.

Seeded bots use a private random.Random, never process-global randomness. The
five non-random baselines intentionally ignore the seed for action selection.
All thresholds below are unvalidated research heuristics, NOT engine mechanics.
Nothing here imports the candidate policy, SDK, evaluator, or simulator.

PHASE 2: BOT_NAMES remains the frozen eight-opponent development suite.
HELDOUT_NAMES is separate; do not silently pool it into development results.
Definitions and parameters below were fixed before any held-out scoring:
* relay_pressure: at most ONE routine recovery permit, assigned to the lowest
  observed own-team HP share (seeded seat priority breaks ties). Emergency
  retreat <=20% bypasses the permit; request below 60%, release at 85%.
  After 12 owner opportunities without a net one-percentage-point HP gain,
  release the permit and defer that hero's routine requests for 10 of its own
  opportunities. Other heroes use offered attack, otherwise advance.
* counterpunch_interceptor: conserve skills until a currently attackable,
  explicitly visible enemy is <=45% HP (only with observed max_hp), lost >=20%
  of its previous visible HP, or moved >=10% closer to our PREVIOUS position.
  Burst only with offered skills whose labels name that same nearest hero;
  skill preference is seeded. Hold for an approaching enemy at most three
  opportunities per continuously visible target, then attack/advance. Retreat
  at own HP <=25%. Fog immediately discards that target's history and wait count.
Neither claims arbitrary focus fire, selectable routes, guaranteed regeneration,
known minion timings, or a 5v5 raw-button mapping. Counts are decision
opportunities, not inferred engine seconds; optional clocks do not affect them.
Seeds 8101/8102 are reserved for a parent-frozen candidate, not smoke tests.
"""

from __future__ import annotations

import copy
import math
import random
import re
from dataclasses import dataclass
from typing import Any, Callable

BASELINE_NAMES = (
    "idle",
    "first_legal",
    "uniform_random",
    "objective_rusher",
    "opportunistic_combat",
    "cautious_defender",
)
STRESS_NAMES = ("pulse_pressure", "split_pressure")
BOT_NAMES = BASELINE_NAMES + STRESS_NAMES
HELDOUT_NAMES = ("relay_pressure", "counterpunch_interceptor")
ALL_BOT_NAMES = BOT_NAMES + HELDOUT_NAMES
WEAK_CONTROL_SCOPE = {
    "idle": "Explicit offered no-op control, not a model of a novice human.",
    "first_legal": "Input-order control; its behavior depends on menu order, not strategic ability.",
    "uniform_random": "Uniform current macro IDs plus one idle; not uniform raw inputs or human behavior.",
}
HELDOUT_PARAMETERS = {
    "relay_pressure": {
        "emergency_hp": 0.20, "request_hp": 0.60, "release_hp": 0.85,
        "minimum_net_hp_gain": 0.01, "stagnant_owner_opportunities": 12,
        "routine_cooldown_opportunities": 10, "routine_permits": 1,
    },
    "counterpunch_interceptor": {
        "emergency_hp": 0.25, "vulnerable_hp": 0.45, "observed_hp_loss": 0.20,
        "approach_fraction": 0.10, "hold_opportunities_per_visible_target": 3,
    },
}
_ALIASES = {
    "random": "uniform_random",
    "rusher": "objective_rusher",
    "combat": "opportunistic_combat",
    "defender": "cautious_defender",
}

_SEAT_ACTION = re.compile(r"^s([0-9]{1,9}):(.+)$")
_DISTANCE = re.compile(r"(?<![\w.+-])(\d+(?:\.\d+)?)\s+away\b", re.IGNORECASE)
_TARGET = re.compile(r"nearest:\s*([^,()]*)", re.IGNORECASE)
_CONTACT = 8000.0
_NEIGHBOURHOOD = 12000.0
_GUARD_RADIUS = 16000.0
_SKILLS_AND_ATTACK = ("macro_skill3", "macro_skill2", "macro_skill1", "macro_attack")
_STRUCTURE_ACTIONS = ("macro_attack", "macro_skill1", "macro_skill2", "macro_skill3")


@dataclass(frozen=True)
class _Choice:
    action_id: str
    base: str
    label: str
    action: str | dict[str, Any]
    idle: bool = False


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _integer_id(value: Any, minimum: int, maximum: int) -> int | None:
    """JSON integer or bounded decimal string, never bool/float/truncation."""
    if isinstance(value, str):
        value = value.strip()
        if not re.fullmatch(r"[0-9]{1,9}", value):
            return None
        value = int(value)
    return value if type(value) is int and minimum <= value <= maximum else None


def _seat(value: Any) -> int | None:
    return _integer_id(value, 0, 9)


def _camp(value: Any) -> int | None:
    return _integer_id(value, 1, 2)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _position(entity: dict) -> tuple[float, float] | None:
    x, z = _number(entity.get("x")), _number(entity.get("z"))
    return (x, z) if x is not None and z is not None else None


def _near(a: tuple[float, float], b: tuple[float, float], radius: float) -> bool:
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= radius


def _summary(raw: dict, seat: int) -> dict:
    seats = _mapping(_mapping(raw.get("observation")).get("seats"))
    entry = _mapping(seats.get(str(seat), seats.get(seat)))
    if not entry:
        entry = next((_mapping(value) for key, value in seats.items() if _seat(key) == seat), {})
    return _mapping(entry.get("summary"))


def _offers(raw: dict) -> dict[int, list[_Choice]]:
    """Materialize only enumerated actions; never invent a missing idle/button."""
    actions = _mapping(raw.get("legal_actions")).get("actions")
    if not isinstance(actions, list):
        return {}
    by_seat: dict[int, list[_Choice]] = {}
    seen: set[str] = set()
    for item in actions:
        if not isinstance(item, dict):
            continue
        action_id = item.get("action_id")
        if not isinstance(action_id, str) or action_id in seen:
            continue
        match = _SEAT_ACTION.fullmatch(action_id)
        if not match:
            continue
        seat, base = _seat(match[1]), match[2]
        if seat is None:
            continue
        template = _mapping(_mapping(item.get("input")).get("action"))
        payload: str | dict[str, Any] = action_id
        fields = [key for key in template if isinstance(key, str) and key.startswith("legal_")]
        if fields:
            if any(not isinstance(template[key], list) or not template[key] for key in fields):
                continue  # No advertised legal completion exists.
            payload = {key: copy.deepcopy(value) for key, value in template.items()
                       if key not in fields and key != "seat"}
            for key in fields:
                payload[key[6:]] = copy.deepcopy(template[key][0])
            payload["seat"] = seat
        label = item.get("label")
        idle = base == "button_0" or (
            base.startswith("button_") and type(template.get("which_button")) is int
            and template["which_button"] == 0
        )
        choice = _Choice(action_id, base, label if isinstance(label, str) else "", payload, idle)
        by_seat.setdefault(seat, []).append(choice)
        seen.add(action_id)
    return by_seat


def _eligible(raw: dict, offers: dict[int, list[_Choice]]) -> list[int]:
    # An explicit roster is authoritative, including an empty one. When absent,
    # use the public camp/seat mapping, or the legal list if both are omitted.
    if "seats" in raw:
        roster = raw["seats"] if isinstance(raw["seats"], list) else []
        owned = {seat for value in roster if (seat := _seat(value)) is not None}
    elif (camp := _camp(raw.get("camp"))) is not None:
        owned = set(range(0, 5) if camp == 1 else range(5, 10))
    else:
        owned = set(offers)
    eligible = set(offers) & owned
    # Public HoK guide §§2–4: pending is freshness, NOT permission, in realtime.
    if _mapping(raw.get("pacing")).get("mode") == "realtime":
        return sorted(eligible)
    simultaneous = _mapping(raw.get("simultaneous"))
    pending = simultaneous.get("your_pending_seats")
    if isinstance(pending, list):
        pending = {seat for value in pending if (seat := _seat(value)) is not None}
        eligible &= pending
    return sorted(eligible)


def _idle(choices: list[_Choice]) -> _Choice | None:
    return next((choice for choice in choices if choice.idle), None)


def _prefer(choices: list[_Choice], *bases: str) -> _Choice:
    for base in bases:
        for choice in choices:
            if choice.base == base:
                return choice
    # With no desired macro, choose a real offered idle, then first legal action.
    return _idle(choices) or choices[0]


def _target(choice: _Choice) -> tuple[str, float]:
    target, distance = _TARGET.search(choice.label), _DISTANCE.search(choice.label)
    return (target[1].strip().lower() if target else "",
            float(distance[1]) if distance else math.inf)


def _combat(choices: list[_Choice], *, structures_only: bool = False) -> _Choice | None:
    candidates: list[tuple[int, int, float, int, _Choice]] = []
    # For objectives prefer the ordinary attack: ability damage to structures is
    # not documented, so do not assume an ultimate is a better siege action.
    ordering = _STRUCTURE_ACTIONS if structures_only else _SKILLS_AND_ATTACK
    for index, choice in enumerate(choices):
        if choice.base not in _SKILLS_AND_ATTACK:
            continue
        target, distance = _target(choice)
        if any(word in target for word in ("ally", "allied", "friendly")):
            continue
        # Public guide §3 explicitly says attack is absent when nothing is in
        # range. Its current offer is stronger evidence than an optional label
        # or a guessed universal attack radius. Skill reach is still heuristic.
        if choice.base != "macro_attack" and (not target or distance > _CONTACT):
            continue
        structure = any(word in target for word in ("tower", "crystal", "structure"))
        if structures_only and not structure:
            continue
        # Labels, not access to enemy objects or simulator targeting internals.
        priority = 0 if "hero" in target else 1
        candidates.append((priority, ordering.index(choice.base), distance, index, choice))
    return min(candidates, key=lambda item: item[:4])[-1] if candidates else None


class ResearchBot:
    """One team's match-scoped opponent. Create a NEW instance for every match."""

    NAMES = BOT_NAMES

    def __init__(self, name: str, seed: int | str = 0,
                 clock: Callable[[], float] | None = None) -> None:
        if name not in self.NAMES:
            raise ValueError(f"Unknown HoK opponent {name!r}; choose from {', '.join(self.NAMES)}")
        self.name = name
        self.seed = seed
        self._clock = clock  # No timed rules yet; never substitute wall-clock time.
        self._rng = random.Random(seed)
        self._last_seat: int | None = None
        self._recovering: set[int] = set()
        self._turns: dict[int, int] = {}
        self._pulse_offset = self._rng.randrange(6) if name == "pulse_pressure" else 0
        self._roles = ["objective_rusher", "objective_rusher", "opportunistic_combat",
                       "opportunistic_combat", "cautious_defender"]
        if name == "split_pressure":
            self._rng.shuffle(self._roles)
        self.views = 0
        self.orders = 0
        self.idles = 0
        self.refused = 0
        self.by_macro: dict[str, int] = {}

    def decide(self, raw: dict) -> tuple[str | dict[str, Any] | None, str | None]:
        self.views += 1
        raw = _mapping(raw)
        offers = _offers(raw)
        eligible = _eligible(raw, offers)
        if not eligible:
            return None, None
        # Pending order and legal-action order cannot starve another eligible hero.
        seat = next((s for s in eligible if self._last_seat is None or s > self._last_seat), eligible[0])
        choices = offers[seat]
        role = self._roles[seat % 5] if self.name == "split_pressure" else self.name
        choice = self._choose(role, raw, seat, choices)
        self._last_seat = seat
        self._turns[seat] = self._turns.get(seat, 0) + 1
        self.orders += 1
        self.idles += int(choice.idle)
        if choice.base.startswith("macro_"):
            self.by_macro[choice.base] = self.by_macro.get(choice.base, 0) + 1
        # Do not share mutable action payloads with a caller.
        return copy.deepcopy(choice.action), f"{self.name}: seat {seat} {choice.base}"

    def _choose(self, role: str, raw: dict, seat: int, choices: list[_Choice]) -> _Choice:
        if role == "idle":
            return _idle(choices) or choices[0]
        if role == "first_legal":
            return choices[0]
        if role == "uniform_random":
            macros = [choice for choice in choices if choice.base.startswith("macro_")]
            idle = _idle(choices)
            return self._rng.choice(macros + ([idle] if idle else [])) if macros or idle else choices[0]
        if role == "objective_rusher":
            return _combat(choices, structures_only=True) or _prefer(
                choices, "macro_advance", "macro_attack", "macro_skill1", "macro_skill2", "macro_skill3")
        if role == "cautious_defender":
            return self._defend(raw, seat, choices)
        if role == "pulse_pressure":
            phase = (self._turns.get(seat, 0) + self._pulse_offset) % 6
            if phase >= 4 and any(choice.base == "macro_retreat" for choice in choices):
                return _prefer(choices, "macro_retreat")
        # Opportunistic combat and the aggressive portion of pulse_pressure.
        return _combat(choices) or _prefer(
            choices, "macro_advance", "macro_attack", "macro_skill1", "macro_skill2", "macro_skill3")

    def _defend(self, raw: dict, seat: int, choices: list[_Choice]) -> _Choice:
        summary = _summary(raw, seat)
        me = _mapping(summary.get("my_hero"))
        hp, maximum = _number(me.get("hp")), _number(me.get("max_hp"))
        share = hp / maximum if hp is not None and maximum is not None and maximum > 0 else None
        if hp is not None and hp <= 0:
            self._recovering.discard(seat)
            return _idle(choices) or choices[0]
        if share is not None and share < 0.55:
            self._recovering.add(seat)
        elif share is not None and share >= 0.90:
            self._recovering.discard(seat)
        if seat in self._recovering and any(choice.base == "macro_retreat" for choice in choices):
            return _prefer(choices, "macro_retreat")

        position = _position(me)
        camp = _camp(raw.get("camp")) or _camp(me.get("camp")) or (1 if seat < 5 else 2)
        enemies: dict[Any, tuple[float, float]] = {}
        allies: dict[Any, tuple[float, float]] = {}
        heroes = summary.get("other_heroes")
        for index, hero in enumerate(heroes if isinstance(heroes, list) else []):
            if not isinstance(hero, dict) or hero.get("visible") is not True:
                continue
            other_hp = _number(hero.get("hp"))
            location = _position(hero)
            other_camp = _camp(hero.get("camp"))
            if location is None or other_camp is None or (other_hp is not None and other_hp <= 0):
                continue
            identity = hero.get("runtime_id")
            key = ("id", identity) if isinstance(identity, (str, int)) else ("entry", index)
            (allies if other_camp == camp else enemies)[key] = location
        if position is not None:
            nearby_enemies = sum(_near(position, other, _NEIGHBOURHOOD) for other in enemies.values())
            nearby_allies = 1 + sum(_near(position, other, _NEIGHBOURHOOD) for other in allies.values())
            if nearby_enemies > nearby_allies and any(choice.base == "macro_retreat" for choice in choices):
                return _prefer(choices, "macro_retreat")
        engagement = _combat(choices)
        if engagement is not None:
            return engagement
        structures = summary.get("structures")
        for structure in structures if isinstance(structures, list) else []:
            if not isinstance(structure, dict) or _camp(structure.get("camp")) != camp:
                continue
            health, anchor = _number(structure.get("hp")), _position(structure)
            if health is None or health <= 0 or anchor is None or position is None:
                continue
            kind = str(structure.get("type") or "").upper()
            if not any(word in kind for word in ("TOWER", "CRYSTAL")):
                continue
            threatened = any(_near(anchor, enemy, _NEIGHBOURHOOD) for enemy in enemies.values())
            if threatened and _near(position, anchor, _GUARD_RADIUS) and _idle(choices) is not None:
                return _idle(choices)  # Hold this guard position, not an unsupported targeted move.
        return _prefer(choices, "macro_advance", "macro_attack", "macro_skill1", "macro_skill2", "macro_skill3")

    def on_result(self, result: Any) -> None:
        """Observe refusal counts only; no retry loops, extra turns, or hidden data."""
        if isinstance(result, dict) and result.get("error"):
            self.refused += 1

    def summary_text(self) -> str:
        return (f"hok opponent={self.name} views={self.views} orders={self.orders} "
                f"idles={self.idles} refused={self.refused}")


def _health_share(entity: dict) -> float | None:
    hp, maximum = _number(entity.get("hp")), _number(entity.get("max_hp"))
    if hp is None or maximum is None or maximum <= 0:
        return None
    return max(0.0, min(1.0, hp / maximum))


def _own_team(raw: dict, seat: int) -> list[int]:
    """Read only granted own-team summaries, never the other camp's summaries."""
    team = set(range(0, 5) if seat < 5 else range(5, 10))
    if "seats" in raw:
        roster = raw["seats"] if isinstance(raw["seats"], list) else []
        team &= {number for value in roster if (number := _seat(value)) is not None}
    return sorted(team)


def _hero_id(value: Any) -> str | None:
    if type(value) is int and 0 <= value < 10**20:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value.strip()):
        return str(int(value.strip()))
    return None


def _label_hero(choice: _Choice | None) -> str | None:
    if choice is None:
        return None
    target, _ = _target(choice)
    if any(word in target for word in ("ally", "allied", "friendly")):
        return None
    match = re.search(r"\bhero\s+([0-9]{1,20})\b", target)
    return _hero_id(match[1]) if match else None


@dataclass(frozen=True)
class _SeenHero:
    position: tuple[float, float] | None
    hp: float | None
    share: float | None


def _visible_foes(summary: dict, camp: int) -> dict[str, _SeenHero]:
    visible: dict[str, _SeenHero] = {}
    heroes = summary.get("other_heroes")
    for hero in heroes if isinstance(heroes, list) else []:
        if not isinstance(hero, dict) or hero.get("visible") is not True:
            continue
        other_camp = _camp(hero.get("camp"))
        identity = _hero_id(hero.get("runtime_id"))
        hp = _number(hero.get("hp"))
        if other_camp is None or other_camp == camp or identity is None or (hp is not None and hp <= 0):
            continue
        visible[identity] = _SeenHero(_position(hero), hp, _health_share(hero))
    return visible


class HeldoutBot(ResearchBot):
    """Separate phase-2 policies; the original eight dispatch paths are unchanged."""

    NAMES = HELDOUT_NAMES

    def __init__(self, name: str, seed: int | str = 0,
                 clock: Callable[[], float] | None = None) -> None:
        super().__init__(name, seed=seed, clock=clock)
        self._priority = list(range(5))
        self._skill_preference = ["macro_skill1", "macro_skill2", "macro_skill3"]
        if name == "relay_pressure":
            self._rng.shuffle(self._priority)
        else:
            self._rng.shuffle(self._skill_preference)
        self._relay_owner: int | None = None
        self._relay_best = 0.0
        self._relay_stagnant = 0
        self._relay_cooldown: dict[int, int] = {}
        self._sight: dict[int, tuple[tuple[float, float] | None, dict[str, _SeenHero]]] = {}
        self._waits: dict[int, dict[str, int]] = {}

    def decide(self, raw: dict) -> tuple[str | dict[str, Any] | None, str | None]:
        if self.name == "counterpunch_interceptor":
            # A fog gap can arrive while another hero gets this call's order.
            # Prune it now, without granting that hero another decision turn.
            public = _mapping(raw)
            for seat, (position, previous) in list(self._sight.items()):
                camp = _camp(public.get("camp")) or (1 if seat < 5 else 2)
                visible = _visible_foes(_summary(public, seat), camp) if seat in _own_team(public, seat) else {}
                self._sight[seat] = (position, {key: hero for key, hero in previous.items() if key in visible})
                self._waits[seat] = {key: count for key, count in self._waits.get(seat, {}).items() if key in visible}
        return super().decide(raw)

    def _choose(self, role: str, raw: dict, seat: int, choices: list[_Choice]) -> _Choice:
        if role == "relay_pressure":
            return self._relay(raw, seat, choices)
        return self._counterpunch(raw, seat, choices)

    def _release_permit(self, *, cooldown: bool = False) -> None:
        if cooldown and self._relay_owner is not None:
            owner = self._relay_owner
            self._relay_cooldown[owner] = (
                self._turns.get(owner, 0) + 1
                + HELDOUT_PARAMETERS["relay_pressure"]["routine_cooldown_opportunities"])
        self._relay_owner = None
        self._relay_stagnant = 0

    def _relay(self, raw: dict, seat: int, choices: list[_Choice]) -> _Choice:
        params = HELDOUT_PARAMETERS["relay_pressure"]
        shares = {member: _health_share(_mapping(_summary(raw, member).get("my_hero")))
                  for member in _own_team(raw, seat)}
        if self._relay_owner is not None:
            current = shares.get(self._relay_owner)
            if current is None or current <= params["emergency_hp"] or current >= params["release_hp"]:
                self._release_permit()
        if self._relay_owner is None:
            requesting = [member for member, hp in shares.items()
                          if hp is not None and params["emergency_hp"] < hp < params["request_hp"]
                          and self._turns.get(member, 0) >= self._relay_cooldown.get(member, 0)]
            if requesting:
                self._relay_owner = min(requesting, key=lambda member: (
                    shares[member], self._priority.index(member % 5)))
                self._relay_best = shares[self._relay_owner]
        share = shares.get(seat)
        if share is not None and share <= 0:
            return _idle(choices) or choices[0]
        can_retreat = any(choice.base == "macro_retreat" for choice in choices)
        if share is not None and share <= params["emergency_hp"] and can_retreat:
            return _prefer(choices, "macro_retreat")
        if seat == self._relay_owner:
            if share is not None and share >= self._relay_best + params["minimum_net_hp_gain"]:
                self._relay_best, self._relay_stagnant = share, 0
            else:
                self._relay_stagnant += 1
            if can_retreat and self._relay_stagnant < params["stagnant_owner_opportunities"]:
                return _prefer(choices, "macro_retreat")
            self._release_permit(cooldown=True)
        return _prefer(choices, "macro_attack", "macro_advance", "macro_skill1", "macro_skill2", "macro_skill3")

    def _counterpunch(self, raw: dict, seat: int, choices: list[_Choice]) -> _Choice:
        params = HELDOUT_PARAMETERS["counterpunch_interceptor"]
        summary = _summary(raw, seat)
        me = _mapping(summary.get("my_hero"))
        position = _position(me)
        camp = _camp(raw.get("camp")) or (1 if seat < 5 else 2)
        previous_position, previous = self._sight.get(seat, (None, {}))
        visible = _visible_foes(summary, camp)
        # Only consecutive, explicitly visible observations can establish a trend.
        self._sight[seat] = (position, visible)
        waits = {identity: count for identity, count in self._waits.get(seat, {}).items() if identity in visible}
        self._waits[seat] = waits
        closing: dict[str, float] = {}
        opportunities: set[str] = set()
        for identity, hero in visible.items():
            old = previous.get(identity)
            if hero.share is not None and hero.share <= params["vulnerable_hp"]:
                opportunities.add(identity)
            if old is None:
                continue
            if hero.hp is not None and old.hp is not None and hero.hp <= old.hp * (1 - params["observed_hp_loss"]):
                opportunities.add(identity)
            if previous_position is not None and old.position is not None and hero.position is not None:
                before = math.dist(previous_position, old.position)
                after = math.dist(previous_position, hero.position)
                if before > 0 and after <= before * (1 - params["approach_fraction"]):
                    opportunities.add(identity)
                    closing[identity] = math.dist(position, hero.position) if position is not None else after
        share = _health_share(me)
        if share is not None and share <= 0:
            return _idle(choices) or choices[0]
        if share is not None and share <= params["emergency_hp"] \
                and any(choice.base == "macro_retreat" for choice in choices):
            return _prefer(choices, "macro_retreat")
        attack = next((choice for choice in choices if choice.base == "macro_attack"), None)
        target = _label_hero(attack)
        if target is not None:
            if target in opportunities:
                for skill in self._skill_preference:
                    matching = next((choice for choice in choices if choice.base == skill
                                     and _label_hero(choice) == target), None)
                    if matching is not None:
                        return matching
            return attack
        waiting_for = [identity for identity in closing
                       if waits.get(identity, 0) < params["hold_opportunities_per_visible_target"]]
        idle = _idle(choices)
        if waiting_for and idle is not None:
            identity = min(waiting_for, key=lambda item: (closing[item], item))
            waits[identity] = waits.get(identity, 0) + 1
            return idle
        return _prefer(choices, "macro_attack", "macro_advance", "macro_skill1", "macro_skill2", "macro_skill3")


def make_bot(name: str, seed: int | str = 0,
             clock: Callable[[], float] | None = None) -> ResearchBot:
    """Match factory; clock injection is optional and does not alter turn cadence."""
    if not isinstance(name, str):
        raise ValueError("HoK opponent name must be a string")
    normalized = name.strip().lower().replace("-", "_")
    canonical = _ALIASES.get(normalized, normalized)
    bot_type = HeldoutBot if canonical in HELDOUT_NAMES else ResearchBot
    return bot_type(canonical, seed=seed, clock=clock)
