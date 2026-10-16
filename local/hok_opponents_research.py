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

    def __init__(self, name: str, seed: int | str = 0,
                 clock: Callable[[], float] | None = None) -> None:
        if name not in BOT_NAMES:
            raise ValueError(f"Unknown HoK opponent {name!r}; choose from {', '.join(BOT_NAMES)}")
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


def make_bot(name: str, seed: int | str = 0,
             clock: Callable[[], float] | None = None) -> ResearchBot:
    """Match factory; clock injection is optional and does not alter turn cadence."""
    if not isinstance(name, str):
        raise ValueError("HoK opponent name must be a string")
    normalized = name.strip().lower().replace("-", "_")
    return ResearchBot(_ALIASES.get(normalized, normalized), seed=seed, clock=clock)
