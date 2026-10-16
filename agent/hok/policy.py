"""Honor of Kings 5v5 (`honor_of_kings_5v5`): a basic rule-based team, so our five heroes never stand still.

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
  4. fight what is in range: ultimate (skill 3) at an enemy hero only, skills 2 / 1 at anything, else a basic attack;
  5. else advance (toward the nearest enemy tower or crystal).

Only changes are sent: a hero whose standing order already is the rule's choice gets nothing, and an order younger
than HOLD_S is kept unless the hero must retreat (no flapping when a target hovers at the edge of range). An order
whose macro is no longer offered (a skill on cooldown) is replaced, since the hero would otherwise idle on it. Real
time: WAIT when nothing changes. Lockstep: every pending hero gets an action. Orders are sent as the listed action's
own action_id, so the runner checks each against the current legal actions before it goes out.
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
TOWER_RANGE = 8800     # an enemy tower shoots a hero this close (HoK's towers reach about 8800 units)
SKILL_RANGE = 8000     # cast a skill at a target this close
ATTACK_RANGE = 9000    # basic-attack a target this close (the official example's "in range")
HOLD_S = 0.6           # an order younger than this is kept, unless the hero must retreat

_SEAT_ID = re.compile(r"^s(\d+):(.+)$")
_DISTANCE = re.compile(r"(\d+(?:\.\d+)?) away\)")
_NEAREST = re.compile(r"nearest:\s*([^,()]*)")

TAGS = {"macro_retreat": "Falling back to heal", "macro_advance": "Pushing the nearest tower",
        "macro_attack": "Attacking the nearest target", "macro_skill1": "Using a skill", "macro_skill2": "Using a skill",
        "macro_skill3": "Using the ultimate"}


# -- reading the state (pure; never raise) ------------------------------------------------------------------------


def offers_by_seat(actions: list[dict]) -> dict[int, dict[str, Any]]:
    """{seat: {"macros": {macro: label}, "ids": {macro: action_id}, "raw": [action dict, ...]}} from the seat-tagged
    legal actions."""
    out: dict[int, dict[str, Any]] = {}
    for a in actions or []:
        if not isinstance(a, dict):
            continue
        m = _SEAT_ID.match(str(a.get("action_id") or ""))
        if not m:
            continue
        seat, base = int(m.group(1)), m.group(2)
        entry = out.setdefault(seat, {"macros": {}, "ids": {}, "raw": []})
        if base in MACROS:
            entry["macros"][base] = str(a.get("label") or base)
            entry["ids"][base] = a["action_id"]
        else:
            entry["raw"].append(a)
    return out


def is_hok_shape(actions: list[dict]) -> bool:
    """Legal actions shaped like Honor of Kings': every one seat-tagged (`s3:macro_attack`, `s3:button_0`)."""
    ids = [str(a.get("action_id") or "") for a in actions or [] if isinstance(a, dict)]
    return bool(ids) and all(_SEAT_ID.match(i) and (":macro_" in i or ":button_" in i) for i in ids)


def realtime(raw: dict) -> bool:
    pacing = raw.get("pacing")
    return isinstance(pacing, dict) and pacing.get("mode") == "realtime"


def standing_orders(raw: dict) -> dict[int, str]:
    sim = raw.get("simultaneous") if isinstance(raw.get("simultaneous"), dict) else {}
    orders = sim.get("standing_orders") if isinstance(sim.get("standing_orders"), dict) else {}
    return {int(k): v for k, v in orders.items() if str(k).isdigit() and isinstance(v, str)}


def pending_seats(raw: dict, offers: dict[int, Any]) -> list[int]:
    sim = raw.get("simultaneous") if isinstance(raw.get("simultaneous"), dict) else {}
    pending = sim.get("your_pending_seats")
    if isinstance(pending, list):
        return [s for s in pending if isinstance(s, int) and not isinstance(s, bool) and s in offers]
    return sorted(offers)


def summary_of(raw: dict, seat: int) -> dict:
    obs = raw.get("observation") if isinstance(raw.get("observation"), dict) else {}
    seats = obs.get("seats") if isinstance(obs.get("seats"), dict) else {}
    entry = seats.get(str(seat)) or seats.get(seat) or {}
    summary = entry.get("summary") if isinstance(entry, dict) else None
    return summary if isinstance(summary, dict) else {}


def distance_of(label: str) -> float:
    """Macro labels end "(... 4123 away)"; inf without one."""
    m = _DISTANCE.search(label or "")
    return float(m.group(1)) if m else math.inf


def target_is_hero(label: str) -> bool:
    m = _NEAREST.search(label or "")
    return bool(m) and "hero" in m.group(1).lower()


def _num(x) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _pos(d: dict) -> tuple[float, float] | None:
    x, z = _num(d.get("x")), _num(d.get("z"))
    return (x, z) if x is not None and z is not None else None


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def hp_share(me: dict) -> float | None:
    hp, mx = _num(me.get("hp")), _num(me.get("max_hp"))
    return hp / mx if hp is not None and mx and mx > 0 else None


class Team:
    """What the five summaries say together: our live heroes' positions, enemy heroes in sight, enemy towers."""

    def __init__(self, raw: dict, seats: list[int]) -> None:
        self.camp = raw.get("camp")
        self.allies: dict[int, tuple[float, float]] = {}
        self.enemies: dict[Any, tuple[float, float]] = {}
        self.towers: list[tuple[float, float]] = []
        for seat in seats:
            s = summary_of(raw, seat)
            me = s.get("my_hero") if isinstance(s.get("my_hero"), dict) else {}
            if self.camp is None:
                self.camp = me.get("camp")
            p = _pos(me)
            if p is not None and (_num(me.get("hp")) or 0) > 0:
                self.allies[seat] = p
            for o in s.get("other_heroes") or []:
                if not isinstance(o, dict) or not o.get("visible") or o.get("camp") in (None, self.camp):
                    continue
                q = _pos(o)
                if q is not None and (_num(o.get("hp")) is None or o["hp"] > 0):
                    self.enemies[o.get("runtime_id", len(self.enemies))] = q
            if not self.towers:
                for st in s.get("structures") or []:
                    if isinstance(st, dict) and st.get("camp") not in (None, self.camp) \
                            and "TOWER" in str(st.get("type") or "").upper() and (_num(st.get("hp")) or 0) > 0:
                        q = _pos(st)
                        if q is not None:
                            self.towers.append(q)

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
    can_retreat = "macro_retreat" in macros
    if can_retreat and share is not None:
        if share < LOW_HP or (retreating and share < HEALED):
            return "macro_retreat"
        p = _pos(me)
        if team is not None and p is not None and share < HURT and (team.outnumbered(p) or team.under_tower(p)):
            return "macro_retreat"
    for macro in ("macro_skill3", "macro_skill2", "macro_skill1"):
        label = macros.get(macro)
        if label is not None and distance_of(label) <= SKILL_RANGE and (macro != "macro_skill3" or target_is_hero(label)):
            return macro
    if "macro_attack" in macros and distance_of(macros["macro_attack"]) <= ATTACK_RANGE:
        return "macro_attack"
    if "macro_advance" in macros:
        return "macro_advance"
    return next((m for m in MACROS if m in macros and m != "macro_retreat"), next(iter(macros)))


def idle_action(raw_actions: list[dict], seat: int):
    """A playable do-nothing for a hero without macros (lockstep only): the listed `s<seat>:button_0`, else the first
    raw button with the first legal value of each field."""
    for a in raw_actions:
        if str(a.get("action_id")) == f"s{seat}:button_0":
            return a["action_id"]
    for a in raw_actions:
        act = (a.get("input") or {}).get("action") if isinstance(a.get("input"), dict) else None
        if isinstance(act, dict) and act.get("which_button") == 0:
            return a["action_id"]
    if not raw_actions:
        return {"which_button": 0, "seat": seat}
    act = (raw_actions[0].get("input") or {}).get("action") or {}
    out: dict[str, Any] = {"seat": seat}
    for k, v in act.items():
        if k.startswith("legal_"):
            if isinstance(v, list) and v:
                out[k[len("legal_"):]] = v[0]
        elif k != "seat":
            out[k] = v
    return out


# -- one match's decisions -----------------------------------------------------------------------------------------


class HokPolicy:
    """decide(raw) -> (action, tag): a listed action_id (or, for a hero with no listed button_0 in lockstep, a raw
    button dict), or None when nothing needs sending (real time: the caller answers WAIT). `tag` is a short public
    line for the order, or None."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._retreating: set[int] = set()
        self._sent: dict[int, tuple[str, float]] = {}       # seat -> (macro, when) of our last order to it
        self.orders = 0
        self.by_macro: dict[str, int] = {}
        self.idles = 0
        self.refused = 0
        self.views = 0

    def decide(self, raw: dict):
        self.views += 1
        actions = (raw.get("legal_actions") or {}).get("actions") if isinstance(raw.get("legal_actions"), dict) else []
        offers = offers_by_seat(actions or [])
        if not offers:
            return None, None
        rt = realtime(raw)
        standing = standing_orders(raw)
        team = Team(raw, sorted(set(offers) | set(_seat_list(raw))))
        now = self._clock()
        pending = pending_seats(raw, offers)
        for seat in pending:
            macros = offers[seat]["macros"]
            summary = summary_of(raw, seat)
            want = rule_macro(summary, macros, team, seat in self._retreating)
            self._track_retreat(seat, summary, want)
            if want is None:
                if rt:
                    continue                                 # a dead hero: nothing to send
                self.idles += 1
                return idle_action(offers[seat]["raw"], seat), None
            current = standing.get(seat)
            if current is None and seat in self._sent:
                current = self._sent[seat][0]
            if rt and current == want:
                continue
            last = self._sent.get(seat)
            if rt and last is not None and now - last[1] < HOLD_S:
                if last[0] == want:
                    continue                                 # just sent: the server's standing orders lag a step
                if current in macros and last[0] == current and want != "macro_retreat":
                    continue                                 # keep a fresh order a moment (no flapping)
            self._sent[seat] = (want, now)
            self.orders += 1
            self.by_macro[want] = self.by_macro.get(want, 0) + 1
            return offers[seat]["ids"][want], TAGS.get(want)
        if rt:
            return None, None
        # Lockstep and every pending hero answered above, or no pending hero we know: keep the server moving.
        seat = pending[0] if pending else sorted(offers)[0]
        macros = offers[seat]["macros"]
        want = rule_macro(summary_of(raw, seat), macros, team, seat in self._retreating)
        if want is not None:
            return offers[seat]["ids"][want], TAGS.get(want)
        self.idles += 1
        return idle_action(offers[seat]["raw"], seat), None

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
            self._sent.clear()                                # re-read: decide again from the next view

    def summary_text(self) -> str:
        macros = ",".join(f"{k[6:]}:{v}" for k, v in sorted(self.by_macro.items()))
        return f"hok views={self.views} orders={self.orders} ({macros or '-'}) idles={self.idles} refused={self.refused}"


def _seat_list(raw: dict) -> list[int]:
    seats = raw.get("seats")
    return [s for s in seats if isinstance(s, int) and not isinstance(s, bool)] if isinstance(seats, list) else []
