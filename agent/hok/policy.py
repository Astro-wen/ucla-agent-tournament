"""Honor of Kings 5v5 (`honor_of_kings_5v5`): legal, visible-state team rules.

This follows the official starter's GAMES.md ("Honor of Kings"), the payload shapes in tests/test_hok.py upstream,
and the public /skill/hok contract. It does not assume unobserved engine mechanics or access to a live match.
No LLM: every order comes from the rules below.

One agent commands a whole team (seats 0-4 or 5-9). Each hero is offered macros as seat-tagged legal actions
(`s3:macro_advance`, `macro_retreat`, `macro_attack`, `macro_skill1`-`3`, each only while possible, labels such as
"Basic attack (nearest: enemy hero 169, 4123 away)") plus raw buttons (`s3:button_0` = do nothing). A macro becomes
that hero's standing order: the server keeps re-playing it every step until the hero gets another order. One order
goes out per choose_action call; the runner calls again at once with the next view.

Per hero, in order:
  1. dead (no macros): nothing (lockstep: the do-nothing button, the server needs an answer);
  2. retreat home below LOW_HP of its HP, and finish a wounded hero's retreat until HEALED (hysteresis: no
     back-and-forth at the HP threshold or tower boundary);
  3. retreat when hurt and outnumbered (visible enemy heroes near it at least two more than our live heroes near it),
     or hurt under an enemy tower;
  4. basic-attack an offered structure; otherwise use skills in range (ultimate at a hero only), then offered attack;
  5. else advance (toward the nearest enemy tower or crystal).

Only changes are sent: a hero whose standing order already is the rule's choice gets nothing, and an order younger
than HOLD_S is kept unless the hero must retreat (no flapping when a target hovers at the edge of range). An order
whose macro is no longer offered (a skill on cooldown) is replaced, since the hero would otherwise idle on it. Real
time: WAIT when nothing changes. Lockstep: every pending hero gets an action. Orders are sent as the listed action's
own action_id, so the runner checks each against the current legal actions before it goes out. All pending heroes
are considered before dispatch: emergencies first, with ageing to prevent one busy hero starving its teammates.
Rejected orders invalidate only that hero's cache, and are not retried against the same rejected state.

Contract: upstream GAMES.md / examples/llm/hok.py and the public /skill/hok guide. Distance cutoffs below are
conservative policy heuristics, NOT verified hero/tower ranges. We never infer a hero's abilities from its ID,
invent movement coordinates, or retain fogged enemy positions.
"""

from __future__ import annotations

import math
import re
import time
from itertools import islice
from typing import Any, Callable

GAME_TYPE = "honor_of_kings_5v5"
MACROS = ("macro_advance", "macro_retreat", "macro_attack", "macro_skill1", "macro_skill2", "macro_skill3")

LOW_HP = 0.30          # retreat below this share of max HP ...
HEALED = 0.85          # ... and go back out only above this one
HURT = 0.60            # "hurt" for the outnumbered / tower rules
NEAR = 10000           # game units: heroes this close count for the outnumbered rule
TOWER_RANGE = 8800     # cautious proximity heuristic, not an asserted tower weapon range
SKILL_RANGE = 8000     # conservative skill-distance heuristic; exact hero ranges are not published
HOLD_S = 0.6           # an order younger than this is kept, unless the hero must retreat
ACK_GRACE_S = 2.0      # tolerate a few lagging snapshots, not an indefinitely missing standing order
RETRY_S = 0.4          # give a refused macro two nominal steps before reconsidering on a fresh view
MAX_BYPASS = 4         # a continuously waiting hero gets priority after four other dispatches
MAX_LABEL = 512        # labels and identifiers are untrusted metadata, not arbitrary-length instructions
MAX_ACTIONS = 1024
MAX_ACTORS = 256

_SEAT_ID = re.compile(r"^s([0-9]):([^:\s]+)$")
_DISTANCE = re.compile(r",\s*(\d+(?:\.\d+)?)\s+away\)", re.IGNORECASE)
_NEAREST = re.compile(r"nearest:\s*([^,()]*)", re.IGNORECASE)

TAGS = {"macro_retreat": "Falling back to heal", "macro_advance": "Pushing the nearest tower",
        "macro_attack": "Attacking the nearest target", "macro_skill1": "Using a skill", "macro_skill2": "Using a skill",
        "macro_skill3": "Using the ultimate"}


# -- reading the state (pure; never raise) ------------------------------------------------------------------------


def _small_int(value: Any, low: int, high: int) -> int | None:
    """Bounded normalization for integer identifiers; never coerce booleans or arbitrary objects."""
    if type(value) is int:
        return value if low <= value <= high else None
    if isinstance(value, str) and len(value) <= 8:
        text = value.strip()
        if text and all("0" <= c <= "9" for c in text):
            number = int(text)
            return number if low <= number <= high else None
    return None


def _seat(value: Any) -> int | None:
    return _small_int(value, 0, 9)


def _camp(value: Any) -> int | None:
    return _small_int(value, 1, 2)


def _actions(raw: dict) -> list[dict]:
    if not isinstance(raw, dict):
        return []
    legal = raw.get("legal_actions")
    actions = legal.get("actions") if isinstance(legal, dict) else legal
    return actions[:MAX_ACTIONS] if isinstance(actions, list) else []


def offers_by_seat(actions: list[dict]) -> dict[int, dict[str, Any]]:
    """{seat: {"macros": {macro: label}, "ids": {macro: action_id}, "raw": [action dict, ...]}} from the seat-tagged
    legal actions."""
    out: dict[int, dict[str, Any]] = {}
    for a in actions[:MAX_ACTIONS] if isinstance(actions, list) else []:
        if not isinstance(a, dict):
            continue
        action_id = a.get("action_id")
        m = _SEAT_ID.fullmatch(action_id) if isinstance(action_id, str) and len(action_id) <= 64 else None
        if not m:
            continue
        seat, base = int(m.group(1)), m.group(2)
        if base not in MACROS and not re.fullmatch(r"button_[0-9]+", base):
            continue
        payload = a.get("input")
        act = payload.get("action") if isinstance(payload, dict) else None
        if isinstance(act, dict) and len(act) > 64:
            continue
        if isinstance(act, dict) and "seat" in act and _seat(act["seat"]) != seat:
            continue
        if isinstance(act, dict) and "macro" in act and act["macro"] != base:
            continue
        if base.startswith("button_") and isinstance(act, dict) and "which_button" in act \
                and base != f"button_{_small_int(act['which_button'], 0, 999999)}":
            continue
        if base in MACROS and isinstance(act, dict) \
                and any(isinstance(k, str) and k.startswith("legal_") for k in act):
            continue  # macros are complete offers; a contradictory raw template is not a safe macro ID
        entry = out.setdefault(seat, {"macros": {}, "ids": {}, "raw": []})
        if base in MACROS:
            entry["macros"][base] = a["label"][:MAX_LABEL] if isinstance(a.get("label"), str) else base
            entry["ids"][base] = a["action_id"]
        else:
            entry["raw"].append(a)
    return out


def is_hok_shape(actions: list[dict]) -> bool:
    """Legal actions shaped like Honor of Kings': every one seat-tagged (`s3:macro_attack`, `s3:button_0`)."""
    if not isinstance(actions, list) or not actions or len(actions) > MAX_ACTIONS:
        return False
    return all(isinstance(a, dict) and isinstance(a.get("action_id"), str) and len(a["action_id"]) <= 64
               and re.fullmatch(r"s[0-9]:(?:macro_(?:advance|retreat|attack|skill[123])|button_[0-9]+)",
                                a["action_id"]) for a in actions)


def realtime(raw: dict) -> bool:
    pacing = raw.get("pacing") if isinstance(raw, dict) else None
    return isinstance(pacing, dict) and pacing.get("mode") == "realtime"


def _standing_snapshot(raw: dict) -> dict[int, str | None] | None:
    if not isinstance(raw, dict):
        return None
    sim = raw.get("simultaneous") if isinstance(raw.get("simultaneous"), dict) else {}
    orders = sim.get("standing_orders")
    if not isinstance(orders, dict):
        return None
    out = {}
    for k, v in islice(orders.items(), 64):
        seat = _seat(k)
        if seat is not None:
            if v is None or v == "":
                out[seat] = None
            elif isinstance(v, str) and len(v) <= 64:
                out[seat] = v
    return out


def standing_orders(raw: dict) -> dict[int, str]:
    return {seat: order for seat, order in (_standing_snapshot(raw) or {}).items() if order is not None}


def _pending_only(raw: dict, offers: dict[int, Any]) -> list[int]:
    """Strict pending eligibility for lockstep and the exception fallback, without reading telemetry."""
    if not isinstance(raw, dict):
        return []
    owned = set(_seat_list(raw))
    eligible = owned.intersection(offers)
    sim = raw.get("simultaneous") if isinstance(raw.get("simultaneous"), dict) else {}
    pending = sim.get("your_pending_seats")
    if isinstance(pending, list):
        return sorted({_seat(s) for s in pending[:64]}.intersection(eligible))
    return sorted(eligible)  # absent metadata: the contract's owned legal offers are our only pending evidence


def pending_seats(raw: dict, offers: dict[int, Any]) -> list[int]:
    """Policy eligibility: real time can replace any owned legal order; lockstep answers pending seats only."""
    if realtime(raw):
        return sorted(set(_seat_list(raw)).intersection(offers))
    return _pending_only(raw, offers)


def summary_of(raw: dict, seat: int) -> dict:
    if not isinstance(raw, dict):
        return {}
    seat = _seat(seat)
    if seat is None:
        return {}
    obs = raw.get("observation") if isinstance(raw.get("observation"), dict) else {}
    seats = obs.get("seats") if isinstance(obs.get("seats"), dict) else {}
    if str(seat) in seats:
        entry = seats[str(seat)]
    else:
        entry = next((v for k, v in islice(seats.items(), 64) if _seat(k) == seat), {})
    summary = entry.get("summary") if isinstance(entry, dict) else None
    return summary if isinstance(summary, dict) else {}


def distance_of(label: str) -> float:
    """Macro labels end "(... 4123 away)"; inf without one."""
    m = _DISTANCE.search(label[:MAX_LABEL]) if isinstance(label, str) else None
    return float(m.group(1)) if m else math.inf


def target_is_hero(label: str) -> bool:
    return target_kind(label) == "hero"


def target_kind(label: str) -> str | None:
    """Only classify the offered target description, never hero IDs or unseen world state."""
    m = _NEAREST.search(label[:MAX_LABEL]) if isinstance(label, str) else None
    if not m:
        return None
    words = set(re.findall(r"[a-z]+", m.group(1).lower()))
    if words & {"tower", "crystal", "structure"}:
        return "structure"
    if "hero" in words:
        return "hero"
    return None


def _near_enough(label: str, limit: float) -> bool:
    # The official guide guarantees a listed macro is legal. If the optional distance text is missing,
    # trust that offer instead of silently leaving a hero advancing forever. Explicit distances still
    # use the starter's conservative heuristic; they are not a claim about a particular hero's range.
    return not isinstance(label, str) or _DISTANCE.search(label[:MAX_LABEL]) is None or distance_of(label) <= limit


def _num(x) -> float | None:
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        try:
            value = float(x)
            return value if math.isfinite(value) else None
        except (OverflowError, ValueError):
            pass
    return None


def _pos(d: dict) -> tuple[float, float] | None:
    if not isinstance(d, dict):
        return None
    x, z = _num(d.get("x")), _num(d.get("z"))
    return (x, z) if x is not None and z is not None else None


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def hp_share(me: dict) -> float | None:
    if not isinstance(me, dict):
        return None
    hp, mx = _num(me.get("hp")), _num(me.get("max_hp"))
    return hp / mx if hp is not None and mx and mx > 0 else None


def _dict_list(value: Any) -> list[dict]:
    return [x for x in value[:MAX_ACTORS] if isinstance(x, dict)] if isinstance(value, list) else []


def _actor_id(value: Any):
    if type(value) is int and -(2 ** 63) <= value < 2 ** 63:
        return value
    if isinstance(value, str) and len(value) <= 64:
        if 0 < len(value) <= 20 and all("0" <= c <= "9" for c in value):
            return int(value)
        return value or None
    return None


class Team:
    """What the five summaries say together: our live heroes' positions, enemy heroes in sight, enemy towers."""

    def __init__(self, raw: dict, seats: list[int]) -> None:
        raw = raw if isinstance(raw, dict) else {}
        requested = {_seat(s) for s in seats[:64]} if isinstance(seats, (list, tuple)) else set()
        seats = sorted(requested.intersection(_seat_list(raw)))
        self.camp = _camp(raw.get("camp"))
        self.allies: dict[int, tuple[float, float]] = {}
        self.enemies: dict[Any, tuple[float, float]] = {}
        self.towers: list[tuple[float, float]] = []
        summaries = [summary_of(raw, seat) for seat in seats]
        if self.camp is None:
            camps = {_camp(s["my_hero"].get("camp")) for s in summaries if isinstance(s.get("my_hero"), dict)}
            camps.discard(None)
            self.camp = next(iter(camps)) if len(camps) == 1 else None
        towers: set[tuple[float, float]] = set()
        for seat, s in zip(seats, summaries):
            me = s.get("my_hero") if isinstance(s.get("my_hero"), dict) else {}
            p = _pos(me)
            if p is not None and (_num(me.get("hp")) or 0) > 0:
                self.allies[seat] = p
            for o in _dict_list(s.get("other_heroes")):
                if o.get("visible") is not True or self.camp is None \
                        or _camp(o.get("camp")) != 3 - self.camp:
                    continue
                q = _pos(o)
                if q is not None and (_num(o.get("hp")) is None or o["hp"] > 0):
                    # IDs may be absent or collide. Distinct currently visible positions must not collapse;
                    # repeated observations with identical IDs and coordinates must not multiply threats.
                    key = (_actor_id(o.get("runtime_id")), _actor_id(o.get("hero_id")), q)
                    self.enemies[key] = q
            for st in _dict_list(s.get("structures")):
                kind = st.get("type")
                if self.camp is not None and _camp(st.get("camp")) == 3 - self.camp \
                        and st.get("visible") is not False \
                        and isinstance(kind, str) and "TOWER" in kind[:64].upper() and (_num(st.get("hp")) or 0) > 0:
                    q = _pos(st)
                    if q is not None:
                        towers.add(q)
        self.towers = sorted(towers)

    def outnumbered(self, p) -> bool:
        foes = sum(1 for q in self.enemies.values() if _dist(p, q) <= NEAR)
        friends = sum(1 for q in self.allies.values() if _dist(p, q) <= NEAR)
        return foes >= max(friends, 1) + 2

    def under_tower(self, p) -> bool:
        return any(_dist(p, q) <= TOWER_RANGE for q in self.towers)


def rule_macro(summary: dict, macros: dict[str, str], team: Team | None = None, retreating: bool = False) -> str | None:
    """The rules for one hero (see the module docstring); None for a dead hero (no macros)."""
    if not isinstance(macros, dict) or not macros:
        return None
    summary = summary if isinstance(summary, dict) else {}
    me = summary.get("my_hero") if isinstance(summary.get("my_hero"), dict) else {}
    share = hp_share(me)
    hp = _num(me.get("hp"))
    if hp is not None and hp <= 0:
        return None
    can_retreat = "macro_retreat" in macros
    if can_retreat and retreating and (share is None or share < HEALED):
        return "macro_retreat"  # missing HP is not evidence of recovery
    if can_retreat and share is not None:
        if share < LOW_HP:
            return "macro_retreat"
        p = _pos(me)
        if team is not None and p is not None and share < HURT and (team.outnumbered(p) or team.under_tower(p)):
            return "macro_retreat"
    attack = macros.get("macro_attack")
    if attack is not None and target_kind(attack) == "structure":
        return "macro_attack"
    for macro in ("macro_skill3", "macro_skill2", "macro_skill1"):
        label = macros.get(macro)
        if label is not None and _near_enough(label, SKILL_RANGE) and (macro != "macro_skill3" or target_is_hero(label)):
            return macro
    if "macro_attack" in macros:
        # The live contract explicitly says no macro_attack is offered without a target in range.
        # A generic label cutoff is not evidence that an offered basic attack cannot hit.
        return "macro_attack"
    if "macro_advance" in macros:
        return "macro_advance"
    return next((m for m in MACROS if m in macros and m != "macro_retreat"), next(iter(macros)))


def _wire_scalar(value: Any) -> bool:
    if value is None or type(value) is bool:
        return True
    if isinstance(value, str):
        return len(value) <= 256
    if type(value) is int:
        return -(2 ** 63) <= value < 2 ** 63
    return type(value) is float and math.isfinite(value)


def _complete_raw(entry: dict, seat: int):
    """Complete only listed scalar domains. A raw template's ID is never returned unfilled."""
    action_id = entry["action_id"]
    payload = entry.get("input")
    act = payload.get("action") if isinstance(payload, dict) else None
    if act is None and action_id == f"s{seat}:button_0":
        return action_id
    if not isinstance(act, dict) or len(act) > 64:
        return None
    button = _small_int(act.get("which_button", 0 if action_id == f"s{seat}:button_0" else None), 0, 999999)
    if button is None or action_id != f"s{seat}:button_{button}":
        return None
    out: dict[str, Any] = {"seat": seat, "which_button": button}
    has_domains = False
    for k, v in act.items():
        if not isinstance(k, str) or len(k) > 64:
            return None
        if k.startswith("legal_"):
            has_domains = True
            field = k[6:]
            if not field or field == "macro" or field.startswith("legal_") or not isinstance(v, list) or not v:
                return None
            values = v[:256]
            if field in ("seat", "which_button"):
                target = seat if field == "seat" else button
                limit = 9 if field == "seat" else 999999
                if not any(_small_int(x, 0, limit) == target for x in values):
                    return None
                continue
            if field in act:
                fixed = act[field]
                if not _wire_scalar(fixed) or not any(type(x) is type(fixed) and x == fixed for x in values):
                    return None
                out[field] = fixed
            else:
                usable = next((x for x in values if _wire_scalar(x)), ...)
                if usable is ...:
                    return None
                out[field] = usable
        elif k not in ("seat", "which_button"):
            if not _wire_scalar(v):
                return None
            out[k] = v
    if button == 0 and not has_domains:
        return action_id
    return out


def idle_action(raw_actions: list[dict], seat: int):
    """A listed button_0, else a fully populated raw button, else None; never invent movement coordinates."""
    listed = offers_by_seat(raw_actions).get(seat, {}).get("raw", [])
    for entry in sorted(listed, key=lambda a: a["action_id"] != f"s{seat}:button_0"):
        action = _complete_raw(entry, seat)
        if action is not None:
            return action
    return None


def safe_fallback_action(raw: dict):
    """Pure exception fallback: current owned pending seat only; no observation, Team, clock, or policy state.

    Prefer a playable button_0 anywhere on the team, then another fillable raw button, then a complete macro.
    Unlike the real-time tactical policy, this emergency helper deliberately honors an explicit pending list.
    """
    offers = offers_by_seat(_actions(raw))
    pending = _pending_only(raw, offers)
    for idle_only in (True, False):
        for seat in pending:
            for entry in offers[seat]["raw"]:
                if (entry["action_id"] == f"s{seat}:button_0") != idle_only:
                    continue
                action = _complete_raw(entry, seat)
                if action is not None:
                    return action
    for seat in pending:
        for macro in MACROS:
            if macro in offers[seat]["ids"]:
                return offers[seat]["ids"][macro]
    return None


# -- one match's decisions -----------------------------------------------------------------------------------------


class HokPolicy:
    """decide(raw) -> (action, tag): a listed action_id (or, for a hero with no listed button_0 in lockstep, a raw
    button dict), or None when nothing needs sending (real time: the caller answers WAIT). `tag` is a short public
    line for the order, or None."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._retreating: set[int] = set()
        self._sent: dict[int, tuple[str, float]] = {}       # seat -> (macro, when) of our last order to it
        self._acknowledged: set[int] = set()
        self._deaths: dict[int, int] = {}
        self._inflight: tuple[int, str | None, int | None] | None = None
        self._rejected: dict[tuple[int, str], tuple[int | None, float]] = {}
        self._dispatches = 0
        self._last_service: dict[int, int] = {}
        self._waiting_since: dict[int, int] = {}
        self.orders = 0
        self.by_macro: dict[str, int] = {}
        self.idles = 0
        self.refused = 0
        self.views = 0

    def decide(self, raw: dict):
        self.views += 1
        if not isinstance(raw, dict):
            return None, None
        owned = _seat_list(raw)
        self._observe_life(raw, owned)                       # death may be visible even with no legal offers
        offers = offers_by_seat(_actions(raw))
        offers = {seat: entry for seat, entry in offers.items() if seat in owned}
        if not offers:
            return None, None
        rt = realtime(raw)
        snapshot = _standing_snapshot(raw)
        team = Team(raw, sorted(set(offers) | set(owned)))
        now = self._clock()
        version = _small_int(raw.get("state_version"), 0, 2 ** 63 - 1)
        pending = pending_seats(raw, offers)
        candidates: dict[int, tuple[str | None, Any, bool]] = {}
        for seat in pending:
            macros = offers[seat]["macros"]
            summary = summary_of(raw, seat)
            want = rule_macro(summary, macros, team, seat in self._retreating)
            self._track_retreat(seat, summary, want)
            if rt and want is None:
                self._forget_order(seat)
                continue
            if want is not None and self._blocked(seat, want, version, now):
                if want == "macro_retreat":
                    if rt:
                        continue                            # never turn a refused emergency retreat into a dive
                    want = None
                else:
                    available = {m: label for m, label in macros.items()
                                 if not self._blocked(seat, m, version, now)}
                    want = rule_macro(summary, available, team, seat in self._retreating)
                    if want == "macro_retreat":
                        want = None                         # do not retreat a healthy hero just because push was refused
            if want is None:
                if rt:
                    continue
                action = idle_action(offers[seat]["raw"], seat)
                if action is not None:
                    candidates[seat] = (None, action, False)
                continue
            current = self._current_order(seat, snapshot, now)
            if rt and current == want:
                continue
            last = self._sent.get(seat)
            if rt and last is not None and now - last[1] < HOLD_S:
                if last[0] == want:
                    continue                                 # just sent: the server's standing orders lag a step
                opportunity = (want.startswith("macro_skill") and target_is_hero(macros[want])) \
                    or (want == "macro_attack" and target_kind(macros[want]) == "structure")
                if current in macros and last[0] == current and want != "macro_retreat" and not opportunity:
                    continue                                 # keep a fresh order a moment (no flapping)
            emergency = want == "macro_retreat" and current != want
            candidates[seat] = (want, offers[seat]["ids"][want], emergency)
        self._waiting_since = {s: self._waiting_since.get(s, self._dispatches) for s in candidates}
        if not candidates:
            return None, None

        def priority(seat: int) -> tuple:
            overdue = self._dispatches - self._waiting_since[seat] >= MAX_BYPASS
            return (not overdue, not candidates[seat][2], self._last_service.get(seat, -1), seat)

        seat = min(candidates, key=priority)
        want, action, _ = candidates[seat]
        self._dispatches += 1
        self._last_service[seat] = self._dispatches
        self._waiting_since.pop(seat, None)
        self._inflight = (seat, want, version)
        if want is not None:
            self._sent[seat] = (want, now)
            self._acknowledged.discard(seat)
            self.orders += 1
            self.by_macro[want] = self.by_macro.get(want, 0) + 1
        else:
            self._forget_order(seat)                         # raw actions clear the standing order (public guide)
            self.idles += 1
        return action, TAGS.get(want)

    def _forget_order(self, seat: int) -> None:
        self._sent.pop(seat, None)
        self._acknowledged.discard(seat)

    def _observe_life(self, raw: dict, owned: list[int]) -> None:
        for seat in owned:
            summary = summary_of(raw, seat)
            me = summary.get("my_hero") if isinstance(summary.get("my_hero"), dict) else {}
            hp = _num(me.get("hp"))
            deaths = _small_int(me.get("deaths"), 0, 999999)
            died = hp is not None and hp <= 0
            if deaths is not None:
                previous = self._deaths.get(seat)
                died = died or (previous is not None and deaths > previous)
                self._deaths[seat] = deaths
            if died:
                self._forget_order(seat)
                self._retreating.discard(seat)
                self._rejected = {key: value for key, value in self._rejected.items() if key[0] != seat}

    def _current_order(self, seat: int, snapshot: dict[int, str | None] | None, now: float) -> str | None:
        current = snapshot.get(seat) if snapshot is not None else None
        last = self._sent.get(seat)
        if current is not None:
            if last is not None and current == last[0]:
                self._acknowledged.add(seat)
            return current
        if last is None:
            return None
        # Explicit null/empty is a clear. A previously acknowledged entry disappearing is also a clear.
        # Preserve the short send/ack grace; never let an optimistic cache override clear evidence forever.
        cleared = snapshot is not None and (seat in snapshot or seat in self._acknowledged)
        grace = HOLD_S if cleared else ACK_GRACE_S
        if now - last[1] < grace:
            return last[0]
        self._forget_order(seat)
        return None

    def _blocked(self, seat: int, macro: str, version: int | None, now: float) -> bool:
        rejected = self._rejected.get((seat, macro))
        if rejected is None:
            return False
        old_version, until = rejected
        return now < until or (version is not None and old_version == version)

    def _track_retreat(self, seat: int, summary: dict, want: str | None) -> None:
        me = summary.get("my_hero") if isinstance(summary.get("my_hero"), dict) else {}
        share = hp_share(me)
        if want == "macro_retreat" and share is not None and share < HURT:
            self._retreating.add(seat)
        elif (share is not None and share >= HEALED) or (_num(me.get("hp")) is not None and me["hp"] <= 0):
            self._retreating.discard(seat)                    # positively observed recovery or death

    def on_result(self, result) -> None:
        if isinstance(result, dict) and result.get("error"):
            self.refused += 1
            if self._inflight is not None:
                seat, macro, version = self._inflight
                self._forget_order(seat)                     # a refusal is not evidence about the other four heroes
                if macro is not None:
                    self._rejected[(seat, macro)] = (version, self._clock() + RETRY_S)
        self._inflight = None

    def summary_text(self) -> str:
        macros = ",".join(f"{k[6:]}:{v}" for k, v in sorted(self.by_macro.items()))
        return f"hok views={self.views} orders={self.orders} ({macros or '-'}) idles={self.idles} refused={self.refused}"


def _seat_list(raw: dict) -> list[int]:
    seats = raw.get("seats") if isinstance(raw, dict) else None
    normalized = {_seat(s) for s in seats[:64]} if isinstance(seats, list) else set()
    normalized.discard(None)
    camp = _camp(raw.get("camp")) if isinstance(raw, dict) else None
    if camp is not None:
        normalized.intersection_update(range(0, 5) if camp == 1 else range(5, 10))
    return sorted(normalized)
