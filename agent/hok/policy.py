"""Honor of Kings 5v5 (`honor_of_kings_5v5`): legal, visible-state team rules.

The platform had not switched the game on by the submission deadline; this follows the contract published in the
official starter's GAMES.md ("Honor of Kings") and the payload shapes of its tests (tests/test_hok.py upstream). No
LLM: every order comes from the rules below.

One agent commands a whole team (seats 0-4 or 5-9). Each hero is offered macros as seat-tagged legal actions
(`s3:macro_advance`, `macro_retreat`, `macro_attack`, `macro_skill1`-`3`, each only while possible, labels such as
"Basic attack (nearest: enemy hero 169, 4123 away)") plus raw buttons (`s3:button_0` = do nothing). A macro becomes
that hero's standing order: the server keeps re-playing it every step until the hero gets another order. One order
goes out per choose_action call; the runner calls again at once with the next view.

Per hero, in order:
  1. dead (no macros): nothing (lockstep: the do-nothing button, the server needs an answer);
  2. retreat home below LOW_HP of its HP, and keep retreating until HEALED (hysteresis: no back-and-forth at the
     threshold);
  3. retreat when hurt and outnumbered (visible enemy heroes near it at least two more than our live heroes near it),
     or hurt under an enemy tower;
  4. basic-attack an offered nearby structure; otherwise use skills in range (ultimate at a hero only), then attack;
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
from typing import Any, Callable

GAME_TYPE = "honor_of_kings_5v5"
MACROS = ("macro_advance", "macro_retreat", "macro_attack", "macro_skill1", "macro_skill2", "macro_skill3")

LOW_HP = 0.30          # retreat below this share of max HP ...
HEALED = 0.85          # ... and go back out only above this one
HURT = 0.60            # "hurt" for the outnumbered / tower rules
NEAR = 10000           # game units: heroes this close count for the outnumbered rule
TOWER_RANGE = 8800     # cautious proximity heuristic, not an asserted tower weapon range
SKILL_RANGE = 8000     # conservative skill-distance heuristic; exact hero ranges are not published
ATTACK_RANGE = 9000    # distance heuristic from the official example, not a universal weapon range
HOLD_S = 0.6           # an order younger than this is kept, unless the hero must retreat
RETRY_S = 0.4          # give a refused macro two nominal steps before reconsidering on a fresh view
MAX_BYPASS = 4         # a continuously waiting hero gets priority after four other dispatches

_SEAT_ID = re.compile(r"^s([0-9]):([^:\s]+)$")
_DISTANCE = re.compile(r",\s*(\d+(?:\.\d+)?)\s+away\)", re.IGNORECASE)
_NEAREST = re.compile(r"nearest:\s*([^,()]*)", re.IGNORECASE)

TAGS = {"macro_retreat": "Falling back to heal", "macro_advance": "Pushing the nearest tower",
        "macro_attack": "Attacking the nearest target", "macro_skill1": "Using a skill", "macro_skill2": "Using a skill",
        "macro_skill3": "Using the ultimate"}


# -- reading the state (pure; never raise) ------------------------------------------------------------------------


def offers_by_seat(actions: list[dict]) -> dict[int, dict[str, Any]]:
    """{seat: {"macros": {macro: label}, "ids": {macro: action_id}, "raw": [action dict, ...]}} from the seat-tagged
    legal actions."""
    out: dict[int, dict[str, Any]] = {}
    for a in actions if isinstance(actions, list) else []:
        if not isinstance(a, dict):
            continue
        action_id = a.get("action_id")
        m = _SEAT_ID.fullmatch(action_id) if isinstance(action_id, str) else None
        if not m:
            continue
        seat, base = int(m.group(1)), m.group(2)
        if base not in MACROS and not re.fullmatch(r"button_[0-9]+", base):
            continue
        payload = a.get("input")
        act = payload.get("action") if isinstance(payload, dict) else None
        if isinstance(act, dict) and "seat" in act and (type(act["seat"]) is not int or act["seat"] != seat):
            continue
        entry = out.setdefault(seat, {"macros": {}, "ids": {}, "raw": []})
        if base in MACROS:
            entry["macros"][base] = str(a.get("label") or base)
            entry["ids"][base] = a["action_id"]
        else:
            entry["raw"].append(a)
    return out


def is_hok_shape(actions: list[dict]) -> bool:
    """Legal actions shaped like Honor of Kings': every one seat-tagged (`s3:macro_attack`, `s3:button_0`)."""
    if not isinstance(actions, list) or not actions:
        return False
    return all(isinstance(a, dict) and isinstance(a.get("action_id"), str)
               and re.fullmatch(r"s[0-9]:(?:macro_(?:advance|retreat|attack|skill[123])|button_[0-9]+)",
                                a["action_id"]) for a in actions)


def realtime(raw: dict) -> bool:
    pacing = raw.get("pacing")
    return isinstance(pacing, dict) and pacing.get("mode") == "realtime"


def standing_orders(raw: dict) -> dict[int, str]:
    sim = raw.get("simultaneous") if isinstance(raw.get("simultaneous"), dict) else {}
    orders = sim.get("standing_orders") if isinstance(sim.get("standing_orders"), dict) else {}
    return {int(k): v for k, v in orders.items() if re.fullmatch(r"[0-9]", str(k)) and isinstance(v, str)}


def pending_seats(raw: dict, offers: dict[int, Any]) -> list[int]:
    sim = raw.get("simultaneous") if isinstance(raw.get("simultaneous"), dict) else {}
    pending = sim.get("your_pending_seats")
    if isinstance(pending, list):
        return sorted({s for s in pending if type(s) is int and s in offers})
    return sorted(offers)


def summary_of(raw: dict, seat: int) -> dict:
    obs = raw.get("observation") if isinstance(raw.get("observation"), dict) else {}
    seats = obs.get("seats") if isinstance(obs.get("seats"), dict) else {}
    entry = seats.get(str(seat)) or seats.get(seat) or {}
    summary = entry.get("summary") if isinstance(entry, dict) else None
    return summary if isinstance(summary, dict) else {}


def distance_of(label: str) -> float:
    """Macro labels end "(... 4123 away)"; inf without one."""
    m = _DISTANCE.search(label) if isinstance(label, str) else None
    return float(m.group(1)) if m else math.inf


def target_is_hero(label: str) -> bool:
    return target_kind(label) == "hero"


def target_kind(label: str) -> str | None:
    """Only classify the offered target description, never hero IDs or unseen world state."""
    m = _NEAREST.search(label) if isinstance(label, str) else None
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
    return not isinstance(label, str) or _DISTANCE.search(label) is None or distance_of(label) <= limit


def _num(x) -> float | None:
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        try:
            value = float(x)
            return value if math.isfinite(value) else None
        except (OverflowError, ValueError):
            pass
    return None


def _pos(d: dict) -> tuple[float, float] | None:
    x, z = _num(d.get("x")), _num(d.get("z"))
    return (x, z) if x is not None and z is not None else None


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def hp_share(me: dict) -> float | None:
    hp, mx = _num(me.get("hp")), _num(me.get("max_hp"))
    return hp / mx if hp is not None and mx and mx > 0 else None


def _dict_list(value: Any) -> list[dict]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


class Team:
    """What the five summaries say together: our live heroes' positions, enemy heroes in sight, enemy towers."""

    def __init__(self, raw: dict, seats: list[int]) -> None:
        self.camp = raw.get("camp") if type(raw.get("camp")) is int and raw["camp"] in (1, 2) else None
        self.allies: dict[int, tuple[float, float]] = {}
        self.enemies: dict[Any, tuple[float, float]] = {}
        self.towers: list[tuple[float, float]] = []
        summaries = [summary_of(raw, seat) for seat in seats]
        if self.camp is None:
            camps = {s["my_hero"].get("camp") for s in summaries if isinstance(s.get("my_hero"), dict)
                     and type(s["my_hero"].get("camp")) is int and s["my_hero"]["camp"] in (1, 2)}
            self.camp = next(iter(camps)) if len(camps) == 1 else None
        towers: set[tuple[float, float]] = set()
        for seat, s in zip(seats, summaries):
            me = s.get("my_hero") if isinstance(s.get("my_hero"), dict) else {}
            p = _pos(me)
            if p is not None and (_num(me.get("hp")) or 0) > 0:
                self.allies[seat] = p
            for o in _dict_list(s.get("other_heroes")):
                if o.get("visible") is not True or self.camp is None \
                        or type(o.get("camp")) is not int or o["camp"] != 3 - self.camp:
                    continue
                q = _pos(o)
                if q is not None and (_num(o.get("hp")) is None or o["hp"] > 0):
                    identity = o.get("runtime_id")
                    key = ("id", identity) if type(identity) in (int, str) else ("position", q)
                    self.enemies[key] = q
            for st in _dict_list(s.get("structures")):
                if self.camp is not None and type(st.get("camp")) is int and st["camp"] == 3 - self.camp \
                        and st.get("visible") is not False \
                        and "TOWER" in str(st.get("type") or "").upper() and (_num(st.get("hp")) or 0) > 0:
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
    if not macros:
        return None
    me = summary.get("my_hero") if isinstance(summary.get("my_hero"), dict) else {}
    share = hp_share(me)
    hp = _num(me.get("hp"))
    if hp is not None and hp <= 0:
        return None
    can_retreat = "macro_retreat" in macros
    if can_retreat and share is not None:
        if share < LOW_HP or (retreating and share < HEALED):
            return "macro_retreat"
        p = _pos(me)
        if team is not None and p is not None and share < HURT and (team.outnumbered(p) or team.under_tower(p)):
            return "macro_retreat"
    attack = macros.get("macro_attack")
    if attack is not None and target_kind(attack) == "structure" and _near_enough(attack, ATTACK_RANGE):
        return "macro_attack"
    for macro in ("macro_skill3", "macro_skill2", "macro_skill1"):
        label = macros.get(macro)
        if label is not None and _near_enough(label, SKILL_RANGE) and (macro != "macro_skill3" or target_is_hero(label)):
            return macro
    if "macro_attack" in macros and _near_enough(macros["macro_attack"], ATTACK_RANGE):
        return "macro_attack"
    if "macro_advance" in macros:
        return "macro_advance"
    return next((m for m in MACROS if m in macros and m != "macro_retreat"), next(iter(macros)))


def idle_action(raw_actions: list[dict], seat: int):
    """A playable do-nothing for a hero without macros (lockstep only): the listed `s<seat>:button_0`, else the first
    raw button with the first legal value of each field."""
    listed = offers_by_seat(raw_actions).get(seat, {}).get("raw", [])
    for a in listed:
        if a["action_id"] == f"s{seat}:button_0":
            payload = a.get("input")
            act = payload.get("action") if isinstance(payload, dict) else None
            if not isinstance(act, dict) or (act.get("which_button", 0) == 0
                                             and not any(str(k).startswith("legal_") for k in act)):
                return a["action_id"]
    for a in listed:
        payload = a.get("input")
        act = payload.get("action") if isinstance(payload, dict) else None
        if not isinstance(act, dict) or type(act.get("which_button")) is not int:
            continue
        if a["action_id"] != f"s{seat}:button_{act['which_button']}":
            continue
        out: dict[str, Any] = {"seat": seat}
        valid = True
        for k, v in act.items():
            if not isinstance(k, str):
                valid = False
                break
            if k.startswith("legal_"):
                if not isinstance(v, list) or not v or not k[len("legal_"):]:
                    valid = False
                    break
                field = k[len("legal_"):]
                if field == "seat":
                    valid = seat in v
                    if not valid:
                        break
                    continue
                if field in act and act[field] not in v:
                    valid = False
                    break
                out[field] = act.get(field, v[0])
            elif k != "seat":
                out[k] = v
        if valid:
            return out
    return None  # No listed, fillable action: do not invent a button or a coordinate.


# -- one match's decisions -----------------------------------------------------------------------------------------


class HokPolicy:
    """decide(raw) -> (action, tag): a listed action_id (or, for a hero with no listed button_0 in lockstep, a raw
    button dict), or None when nothing needs sending (real time: the caller answers WAIT). `tag` is a short public
    line for the order, or None."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._retreating: set[int] = set()
        self._sent: dict[int, tuple[str, float]] = {}       # seat -> (macro, when) of our last order to it
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
        actions = (raw.get("legal_actions") or {}).get("actions") if isinstance(raw.get("legal_actions"), dict) else []
        offers = offers_by_seat(actions or [])
        owned = _seat_list(raw)
        if isinstance(raw.get("seats"), list):
            offers = {seat: entry for seat, entry in offers.items() if seat in owned}
        if not offers:
            return None, None
        rt = realtime(raw)
        standing = standing_orders(raw)
        team = Team(raw, sorted(set(offers) | set(owned)))
        now = self._clock()
        version = raw.get("state_version")
        version = version if type(version) is int and version >= 0 else None
        pending = pending_seats(raw, offers)
        candidates: dict[int, tuple[str | None, Any, bool]] = {}
        for seat in pending:
            macros = offers[seat]["macros"]
            summary = summary_of(raw, seat)
            want = rule_macro(summary, macros, team, seat in self._retreating)
            self._track_retreat(seat, summary, want)
            if rt and want is None:
                self._sent.pop(seat, None)                   # dead heroes must be commanded again after respawn
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
            current = standing.get(seat)
            if current is None and seat in self._sent:
                current = self._sent[seat][0]
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
            self.orders += 1
            self.by_macro[want] = self.by_macro.get(want, 0) + 1
        else:
            self._sent.pop(seat, None)                       # raw actions clear the standing order (public guide)
            self.idles += 1
        return action, TAGS.get(want)

    def _blocked(self, seat: int, macro: str, version: int | None, now: float) -> bool:
        rejected = self._rejected.get((seat, macro))
        if rejected is None:
            return False
        old_version, until = rejected
        return now < until or (version is not None and old_version == version)

    def _track_retreat(self, seat: int, summary: dict, want: str | None) -> None:
        me = summary.get("my_hero") if isinstance(summary.get("my_hero"), dict) else {}
        share = hp_share(me)
        if want == "macro_retreat" and share is not None and share < LOW_HP:
            self._retreating.add(seat)
        elif want is None or (share is not None and share >= HEALED):
            self._retreating.discard(seat)                    # healed, or dead (revives at home with full HP)

    def on_result(self, result) -> None:
        if isinstance(result, dict) and result.get("error"):
            self.refused += 1
            if self._inflight is not None:
                seat, macro, version = self._inflight
                self._sent.pop(seat, None)                   # a refusal is not evidence about the other four heroes
                if macro is not None:
                    self._rejected[(seat, macro)] = (version, self._clock() + RETRY_S)
        self._inflight = None

    def summary_text(self) -> str:
        macros = ",".join(f"{k[6:]}:{v}" for k, v in sorted(self.by_macro.items()))
        return f"hok views={self.views} orders={self.orders} ({macros or '-'}) idles={self.idles} refused={self.refused}"


def _seat_list(raw: dict) -> list[int]:
    seats = raw.get("seats")
    return sorted({s for s in seats if type(s) is int and 0 <= s <= 9}) if isinstance(seats, list) else []
