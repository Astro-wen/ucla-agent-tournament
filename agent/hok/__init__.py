"""Our rule-based Honor of Kings 5v5 team (`honor_of_kings_5v5`). No LLM: deterministic rules over each view.

The contract is the official SDK's GAMES.md "Honor of Kings" section (upstream 686e756; the platform's own guide is
published when the game is switched on). One agent commands all five heroes of a camp (seats 0-4 or 5-9). Each hero
is offered macros in `state.legal_actions` (`s<seat>:macro_advance`, `macro_retreat`, `macro_attack`,
`macro_skill1`-`3`, each only while possible, labels ending "(nearest: <target>, <d> away)") plus raw buttons
(`s<seat>:button_0` = do nothing). One hero's order goes out per choose_action call, as `{"macro": id, "seat": n}`;
a macro stays that hero's standing order (the server re-aims and replays it every step) until it gets another.

`HokPolicy.decide(raw, legal_actions)` answers one view with (decision, public tag):

  * real time (`pacing.mode == "realtime"`): the order of the first pending hero whose wanted macro differs from its
    standing order, or None (the Contestant answers altruagent.WAIT). Breaking off a fight to push waits until the
    current fight has played MIN_HOLD_STEPS steps (no flapping at a range threshold); an order just sent is not sent
    again while the server has not shown it yet (RESEND_STEPS).
  * lockstep (each step waits for every hero): an action for the first pending hero, always one the state lists
    (an INVALID_ACTION ends a turn-based match): its macro, or the do-nothing button for a hero with no macro (dead).

What a hero wants (`want`), in order:
  1. retreat when HP < RETREAT_HP, and keep retreating (hysteresis) until HP >= RETURN_HP;
  2. retreat when outnumbered by visible enemy heroes nearby (>= 2 more, or more while HP < OUTNUMBERED_HP), or when
     standing in an enemy tower's range with HP < TOWER_HP;
  3. the ultimate (skill 3) on an enemy hero in range; skill 2 / skill 1 on anything in range; a basic attack on
     anything in range (ranges get ENGAGE_SLACK more once the hero is already fighting);
  4. else push (macro_advance).

One policy per match; nothing is persisted (a restarted worker rebuilds from the next view). `decide` never raises:
an exception is counted and answered with a safe action (real time: None; lockstep: the do-nothing button of the
first pending hero, else the first listed action). ARENA_HOK=off restores the old behaviour (WAIT in real time).
"""
from __future__ import annotations

import logging
import math
import re
from typing import Any

log = logging.getLogger("agent.hok")

GAME_TYPE = "honor_of_kings_5v5"
MACROS = ("macro_advance", "macro_retreat", "macro_attack", "macro_skill1", "macro_skill2", "macro_skill3")
FIGHT = ("macro_skill3", "macro_skill2", "macro_skill1", "macro_attack")

RETREAT_HP = 0.30          # start walking home below this share of max HP ...
RETURN_HP = 0.90           # ... and keep going until healed to this share
OUTNUMBERED_HP = 0.60      # outnumbered by one: retreat only below this share
TOWER_HP = 0.45            # inside an enemy tower's range: retreat below this share
NEAR = 12000               # enemy / ally heroes this close count for "outnumbered" (game units)
TOWER_RANGE = 9000         # an enemy tower this close is shooting range (with a margin)
ATTACK_RANGE = 9000        # a basic attack / skill is worth it on a target this close (the example agent's IN_RANGE)
SKILL_RANGE = 8000         # skills are a little more selective than basic attacks
ENGAGE_SLACK = 2000        # a hero already fighting keeps fighting up to this much further
MIN_HOLD_STEPS = 3         # real time: breaking off a fight to push waits until the fight played 0.6 s
RESEND_STEPS = 5           # real time: an order sent but not yet shown as standing is resent after 1 s
REFUSED_STEPS = 5          # a refused (seat, macro) is not offered again for this many steps

_SEAT_ID = re.compile(r"^s(\d+):(.+)$")
_NEAREST = re.compile(r"nearest:\s*([^,()]*?)\s*,\s*(\d+(?:\.\d+)?)\s*away", re.I)
_DISTANCE = re.compile(r"(\d+(?:\.\d+)?)\s*away\)")


def _num(x) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _int(x) -> int | None:
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x
    if isinstance(x, str) and x.strip().lstrip("-").isdigit():
        return int(x)
    return None


def target_of(label: str) -> tuple[str, float]:
    """(target kind, distance) from a macro label "... (nearest: enemy hero 169, 4123 away)"; ("", inf) without."""
    text = label or ""
    m = _NEAREST.search(text)
    if m:
        return m.group(1).strip().lower(), float(m.group(2))
    m = _DISTANCE.search(text)
    return ("", float(m.group(1))) if m else ("", math.inf)


def offers_by_seat(legal_actions) -> dict[int, dict[str, Any]]:
    """{seat: {"macros": {macro: label}, "raw": [LegalAction, ...]}} from the seat-tagged legal actions."""
    out: dict[int, dict[str, Any]] = {}
    for a in legal_actions or []:
        m = _SEAT_ID.match(str(getattr(a, "action_id", "") or ""))
        if not m:
            continue
        seat, base = int(m.group(1)), m.group(2)
        entry = out.setdefault(seat, {"macros": {}, "raw": []})
        if base in MACROS:
            entry["macros"][base] = str(getattr(a, "label", None) or base)
        else:
            entry["raw"].append(a)
    return out


def is_hok_shape(legal_actions) -> bool:
    """A state whose legal actions are HoK's seat-tagged macros / buttons (`s3:macro_attack`, `s3:button_0`)."""
    for a in legal_actions or []:
        m = _SEAT_ID.match(str(getattr(a, "action_id", "") or ""))
        if m and (m.group(2) in MACROS or m.group(2).startswith("button_")):
            return True
    return False


class Hero:
    """One of our heroes as this view shows it."""

    __slots__ = ("seat", "hp", "max_hp", "x", "z", "alive")

    def __init__(self, seat: int, me: dict):
        self.seat = seat
        self.hp, self.max_hp = _num(me.get("hp")), _num(me.get("max_hp"))
        self.x, self.z = _num(me.get("x")), _num(me.get("z"))
        revive = _num(me.get("revive_time")) or 0.0
        self.alive = not (revive > 0 or (self.hp is not None and self.hp <= 0))

    @property
    def frac(self) -> float | None:
        if self.hp is None or not self.max_hp:
            return None
        return max(0.0, min(1.0, self.hp / self.max_hp))

    def dist(self, x, z) -> float:
        if self.x is None or self.z is None or x is None or z is None:
            return math.inf
        return math.hypot(self.x - x, self.z - z)


class View:
    """The parts of one state the rules read: our heroes, the enemy heroes in sight, the enemy towers."""

    def __init__(self, raw: dict):
        self.raw = raw
        pacing = raw.get("pacing")
        self.realtime = isinstance(pacing, dict) and pacing.get("mode") == "realtime"
        self.step = _int(raw.get("state_version")) or 0
        sim = raw.get("simultaneous") if isinstance(raw.get("simultaneous"), dict) else {}
        pend = sim.get("your_pending_seats")
        self.pending = [s for s in (_int(p) for p in pend) if s is not None] if isinstance(pend, list) else None
        orders = sim.get("standing_orders") if isinstance(sim.get("standing_orders"), dict) else {}
        self.standing = {s: v for k, v in orders.items() if (s := _int(k)) is not None and isinstance(v, str)}
        obs = raw.get("observation") if isinstance(raw.get("observation"), dict) else {}
        seats = obs.get("seats") if isinstance(obs.get("seats"), dict) else {}
        self.summaries: dict[int, dict] = {}
        for k, v in seats.items():
            s = _int(k)
            summ = v.get("summary") if isinstance(v, dict) else None
            if s is not None and isinstance(summ, dict):
                self.summaries[s] = summ
        self.camp = _int(raw.get("camp"))
        self.heroes: dict[int, Hero] = {}
        enemies: dict[Any, tuple] = {}
        towers: dict[tuple, tuple] = {}
        for s, summ in self.summaries.items():
            me = summ.get("my_hero") if isinstance(summ.get("my_hero"), dict) else {}
            self.heroes[s] = Hero(s, me)
            if self.camp is None:
                self.camp = _int(me.get("camp"))
        for s, summ in self.summaries.items():
            for o in summ.get("other_heroes") or []:
                if not isinstance(o, dict) or not o.get("visible") or _int(o.get("camp")) in (None, self.camp):
                    continue
                hp = _num(o.get("hp"))
                if hp is not None and hp <= 0:
                    continue
                x, z = _num(o.get("x")), _num(o.get("z"))
                if x is None or z is None:
                    continue
                enemies[o.get("runtime_id", (x, z))] = (x, z)
            for st in summ.get("structures") or []:
                if not isinstance(st, dict) or _int(st.get("camp")) in (None, self.camp):
                    continue
                if "TOWER" not in str(st.get("type") or "").upper():
                    continue
                hp = _num(st.get("hp"))
                x, z = _num(st.get("x")), _num(st.get("z"))
                if (hp is not None and hp <= 0) or x is None or z is None:
                    continue
                towers[(x, z)] = (x, z)
        self.enemies = list(enemies.values())
        self.towers = list(towers.values())

    def enemies_near(self, h: Hero, r: float = NEAR) -> int:
        return sum(1 for x, z in self.enemies if h.dist(x, z) <= r)

    def allies_near(self, h: Hero, r: float = NEAR) -> int:
        return sum(1 for o in self.heroes.values() if o.alive and (o is h or h.dist(o.x, o.z) <= r))

    def under_tower(self, h: Hero) -> bool:
        return any(h.dist(x, z) <= TOWER_RANGE for x, z in self.towers)


class HokPolicy:
    """One Honor of Kings match's decisions (see the module docstring)."""

    def __init__(self):
        self.retreating: set[int] = set()
        self.sent: dict[int, tuple[str, int]] = {}      # seat -> (macro, step) of our last order to it
        self.since: dict[int, tuple[str, int]] = {}     # seat -> (standing macro, first step we saw it)
        self.refused: dict[tuple[int, str], int] = {}   # (seat, macro) -> step until it is not offered again
        self._last: tuple[int, str] | None = None
        self.views = 0
        self.orders = 0
        self.by_macro: dict[str, int] = {}
        self.retreats = 0
        self.refusals = 0
        self.errors = 0
        self.buttons = 0
        self.want_record = True                          # the first view is recorded (a real payload sample)
        self._err_kinds: set[str] = set()

    # ---- the runner's hooks -------------------------------------------------------------------------------------
    def decide(self, raw: dict, legal_actions) -> tuple[Any, str | None]:
        """(decision, public tag): decision is an order dict, a listed LegalAction, or None (nothing to send)."""
        self.views += 1
        self.want_record = self.views == 1
        realtime = False
        try:
            raw = raw if isinstance(raw, dict) else {}
            v = View(raw)
            realtime = v.realtime
            return self._decide(v, offers_by_seat(legal_actions))
        except Exception as e:                           # noqa: BLE001 - never raise to the runner
            self.errors += 1
            self.want_record = True
            kind = type(e).__name__
            if kind not in self._err_kinds:
                self._err_kinds.add(kind)
                log.exception("hok decision failed (%s); answering safely", kind)
            return (None if realtime else safe_lockstep(raw, legal_actions)), None

    def on_result(self, result) -> None:
        """A play_action answer: a refused order (a race: the macro went away) is not offered again for a while and the
        hero is decided afresh at the next view."""
        if not isinstance(result, dict) or not result.get("error") or self._last is None:
            return
        seat, macro = self._last
        self.refusals += 1
        step = self.sent.get(seat, (macro, 0))[1]
        self.refused[(seat, macro)] = step + REFUSED_STEPS
        self.sent.pop(seat, None)
        self._last = None

    def summary_text(self) -> str:
        macros = ",".join(f"{k[6:]}:{v}" for k, v in sorted(self.by_macro.items()))
        return (f"hok views={self.views} orders={self.orders} ({macros or '-'}) retreats={self.retreats} "
                f"buttons={self.buttons} refused={self.refusals} errors={self.errors}")

    # ---- deciding -------------------------------------------------------------------------------------------------
    def _decide(self, v: View, offers: dict[int, dict[str, Any]]):
        if not offers:
            if v.realtime:
                return None, None
            raise ValueError("lockstep view with no seat-tagged legal actions")
        for s, m in v.standing.items():                  # how long each standing order has played
            if self.since.get(s, (None,))[0] != m:
                self.since[s] = (m, v.step)
        pending = [s for s in v.pending if s in offers] if v.pending is not None else sorted(offers)
        if not pending:
            if v.realtime:
                return None, None
            pending = sorted(offers)
        for seat in pending:
            macros = {m: lab for m, lab in offers[seat]["macros"].items()
                      if self.refused.get((seat, m), -1) < v.step}
            hero = v.heroes.get(seat) or Hero(seat, {})
            want, urgent, why = self.want(v, hero, macros)
            if want is None:
                if v.realtime:
                    continue                             # a dead hero: nothing to send
                self.buttons += 1
                return _button0(offers[seat]["raw"], seat), None
            if v.realtime and not self._should_send(v, seat, want, urgent, macros):
                continue
            return self._send(v, seat, want, why), why
        if v.realtime:
            return None, None
        seat = pending[0]                                # (unreachable: a pending hero always gets an answer above)
        self.buttons += 1
        return _button0(offers[seat]["raw"], seat), None

    def want(self, v: View, h: Hero, macros: dict[str, str]) -> tuple[str | None, bool, str | None]:
        """(macro, urgent, public tag) for one hero; macro None when it has none (dead)."""
        if not macros:
            self.retreating.discard(h.seat)
            return None, False, None
        frac = h.frac
        if frac is not None and frac >= RETURN_HP:
            self.retreating.discard(h.seat)
        if "macro_retreat" in macros:
            if frac is not None and frac < RETREAT_HP:
                if h.seat not in self.retreating:
                    self.retreating.add(h.seat)
                    self.retreats += 1
                return "macro_retreat", True, f"Hero {h.seat} falls back to heal."
            if h.seat in self.retreating:
                return "macro_retreat", False, f"Hero {h.seat} falls back to heal."
            foes = v.enemies_near(h)
            if foes:
                friends = v.allies_near(h)
                if foes >= friends + 2 or (foes > friends and frac is not None and frac < OUTNUMBERED_HP):
                    return "macro_retreat", True, f"Hero {h.seat} backs off: outnumbered."
            if frac is not None and frac < TOWER_HP and v.under_tower(h):
                return "macro_retreat", True, f"Hero {h.seat} leaves tower range."
        fighting = v.standing.get(h.seat) in FIGHT
        slack = ENGAGE_SLACK if fighting else 0
        for m in FIGHT:
            if m not in macros:
                continue
            kind, d = target_of(macros[m])
            if m == "macro_skill3" and "hero" not in kind:
                continue                                 # the ultimate is kept for enemy heroes
            if d < (ATTACK_RANGE if m == "macro_attack" else SKILL_RANGE) + slack:
                what = "an enemy hero" if "hero" in kind else "the nearest target"
                return m, False, f"Hero {h.seat} fights {what}."
        if "macro_advance" in macros:
            return "macro_advance", False, f"Hero {h.seat} pushes."
        if "macro_attack" in macros:
            return "macro_attack", False, f"Hero {h.seat} fights the nearest target."
        return next(iter(macros)), False, None

    def _should_send(self, v: View, seat: int, want: str, urgent: bool, macros: dict) -> bool:
        standing = v.standing.get(seat)
        if standing == want:
            return False
        last = self.sent.get(seat)
        if last is not None and last[0] == want and v.step - last[1] < RESEND_STEPS:
            return False                                 # sent, the server has not shown it yet
        if urgent or standing is None or standing not in macros or not (standing in FIGHT and want == "macro_advance"):
            return True                                  # (only breaking off a fight waits: the flapping case)
        held = self.since.get(seat)
        return held is None or held[0] != standing or v.step - held[1] >= MIN_HOLD_STEPS

    def _send(self, v: View, seat: int, macro: str, why: str | None) -> dict:
        self.sent[seat] = (macro, v.step)
        self._last = (seat, macro)
        self.orders += 1
        self.by_macro[macro] = self.by_macro.get(macro, 0) + 1
        return {"macro": macro, "seat": seat}


def _button0(raw_actions, seat: int):
    """The do-nothing button of a hero (lockstep, a hero with no macro): its listed LegalAction when there is one."""
    for a in raw_actions or []:
        if str(getattr(a, "action_id", "")) == f"s{seat}:button_0":
            return a
    return {"which_button": 0, "seat": seat}


def safe_lockstep(raw: dict, legal_actions):
    """The last resort in lockstep: a listed action for the first pending hero (its do-nothing button, else its first
    listed action), else the first listed action of all."""
    acts = [a for a in legal_actions or [] if getattr(a, "action_id", None)]
    pending = None
    try:
        sim = raw.get("simultaneous") if isinstance(raw, dict) else None
        pend = sim.get("your_pending_seats") if isinstance(sim, dict) else None
        pending = [s for s in (_int(p) for p in pend) if s is not None] if isinstance(pend, list) else None
    except Exception:                                    # noqa: BLE001
        pending = None
    for seat in pending or []:
        mine = [a for a in acts if str(a.action_id).startswith(f"s{seat}:")]
        for a in mine:
            if a.action_id == f"s{seat}:button_0":
                return a
        if mine:
            return mine[0]
    return acts[0] if acts else None
