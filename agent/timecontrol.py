"""Per-decision time budgets: never lose a game on time.

The platform's clocks (docs/competition_brief.md, checked against logs/*/):
  * Pokémon VGC runs Showdown's VGC timer: 90 s at team preview, 55 s per turn (90 s on the first), and a 420 s bank for
    the whole battle that is never refilled (plus 90 s of first-turn grace). A turn that runs out is auto-played; a bank
    that runs out loses the battle. The bank is drawn down in real time while the server waits for us, so it pays for
    our thinking AND for the platform's round trip (about 0.7 s per decision in the recordings).
  * The draft and teambuild phases have no idle timer (pokemon.md); team preview is covered by the 90 s.
  * Werewolf / Avalon auto-play a seat after 120 s idle.
What a battle decision can see: Showdown sends `|inactive|Time left: T sec this turn | B sec total [| G sec grace]` with
every request, and the platform forwards the whole protocol in `observation.protocol_log` (the last such line is the
current request's). Nothing is visible during the draft or at team preview.

`budget_for(raw, game_type)` turns that into one `Deadline` per decision:
  * draft pick DRAFT_S (20 s), teambuild TEAMBUILD_S (20 s), team preview PREVIEW_S (30 s of the 90 s), Werewolf /
    unknown games OTHER_S (20 s of the 120 s), a chat turn MESSAGE_S;
  * a battle decision: BATTLE_S (10 s), and when the timer line is visible also at most
    TURN_SHARE x (T - LATENCY_S)   (half of what is left of this turn after the platform's latency) and
    (bank - BANK_RESERVE_S) / BANK_SPREAD   (the bank left, spread over the next 20 decisions, 30 s kept in reserve),
    never below MIN_S (a heuristic answer takes milliseconds).
`Contestant.choose_action` (agent/agent.py) enters `decision(deadline)` for the whole decision, and everything that can
take time bounds itself by `current()` / `cap()`: DamageCalc round trips (calc.py), the drafter's search wall
(policy._drafter_pick), the preview solver's wall cap (policy._preview_matrix) and the lookahead's TIME_CAP
(policy._doubles). The deadlines only bind under pathological load or a hung calculator: every component already
finishes far inside them (v3: draft 1.4 s, preview 2.7 s, battle 84 ms worst case on half a CPU), so play is unchanged
otherwise. As a last line of defence the Contestant runs the policy under a watchdog that answers with a safe legal
action once `Deadline.hard` (the soft deadline + a small grace) passes.

Every knob can be overridden for experiments with ARENA_TC_<NAME> (e.g. ARENA_TC_BATTLE_S=3); deployed runs never set
them.
"""
from __future__ import annotations

import contextvars
import math
import os
import re
import time
from contextlib import contextmanager


def _knob(name: str, default: float) -> float:
    """ARENA_TC_<NAME> override (experiments / tests only); anything unparsable or negative keeps the default."""
    try:
        v = os.environ.get(f"ARENA_TC_{name}")
        if v in (None, ""):
            return default
        x = float(v)
        return x if math.isfinite(x) and x >= 0 else default
    except (TypeError, ValueError):
        return default


# ---- budgets (seconds) -------------------------------------------------------------------------------------------
DRAFT_S = 20.0          # a draft pick (no platform timer; v3 worst case 1.4 s)
TEAMBUILD_S = 20.0
PREVIEW_S = 30.0        # team preview: 90 s on the platform (v3 worst case 2.7 s)
BATTLE_S = 10.0         # a battle decision at most (55 s per turn; v3 worst case 84 ms)
OTHER_S = 20.0          # Werewolf / Avalon / an unknown game or phase (auto-played after 120 s idle)
MESSAGE_S = 20.0        # a chat turn
MIN_S = 0.5             # floor of every budget
LATENCY_S = 3.0         # platform + network round trip kept out of the visible per-turn time
TURN_SHARE = 0.5        # never plan to use more than half of what is left of the turn
BANK_RESERVE_S = 30.0   # bank never planned for
BANK_SPREAD = 20        # the rest of the bank is spread over this many future decisions
HARD_GRACE_S = 2.0      # the watchdog fires this long after the soft deadline (at most 25% of the budget)
RESERVE_SHARE = 0.1     # cap(): time kept back for the work after a bounded step (scoring, validation) ...
RESERVE_MIN, RESERVE_MAX = 0.05, 1.0   # ... clamped to these

# Showdown's timer line (room-battle.ts): per-turn time, bank, and grace that is spent before the bank.
_TIME_LEFT = re.compile(r"Time left: (\d+) sec this turn \| (\d+) sec total(?: \| (\d+) sec grace)?")
BATTLE_PHASES = ("moving",)
PRE_BATTLE = {"draft": "draft", "teambuild": "teambuild", "team_preview": "preview"}


class Deadline:
    """One decision's time budget. `end` is the soft deadline every bounded step aims for, `hard` is when the watchdog
    gives up on the policy and answers with the safe fallback."""

    __slots__ = ("kind", "seconds", "source", "start", "end", "hard", "_clock")

    def __init__(self, seconds: float, kind: str = "decision", source: str = "default", clock=time.monotonic,
                 grace: float | None = None):
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            seconds = BATTLE_S
        if not math.isfinite(seconds) or seconds < 0:
            seconds = BATTLE_S
        self.kind, self.seconds, self.source, self._clock = kind, seconds, source, clock
        self.start = clock()
        self.end = self.start + seconds
        g = min(_knob("HARD_GRACE_S", HARD_GRACE_S), 0.25 * seconds) if grace is None else max(0.0, float(grace))
        self.hard = self.end + g

    def left(self) -> float:
        """Seconds to the soft deadline (0 once it passed)."""
        return max(0.0, self.end - self._clock())

    def hard_left(self) -> float:
        return max(0.0, self.hard - self._clock())

    def elapsed(self) -> float:
        return self._clock() - self.start

    def expired(self) -> bool:
        return self._clock() >= self.end

    def reserve(self) -> float:
        return min(RESERVE_MAX, max(RESERVE_MIN, RESERVE_SHARE * self.seconds))

    def cap(self, limit: float, share: float = 1.0) -> float:
        """How long a bounded step may run: its own `limit`, but never past share x (time left) minus the reserve."""
        return max(0.0, min(float(limit), self.left() * share - self.reserve()))

    def __repr__(self) -> str:
        return f"Deadline({self.kind}, {self.seconds:.2f}s from {self.source}, {self.left():.2f}s left)"


_CURRENT: contextvars.ContextVar = contextvars.ContextVar("arena_decision_deadline", default=None)


def current() -> Deadline | None:
    """The deadline of the decision in progress (None outside `decision()`: tests, scripts, tools)."""
    return _CURRENT.get()


def left() -> float | None:
    d = _CURRENT.get()
    return None if d is None else d.left()


def cap(limit: float, share: float = 1.0) -> float:
    """`limit`, bounded by the current decision's deadline (unchanged outside a decision)."""
    d = _CURRENT.get()
    return float(limit) if d is None else d.cap(limit, share)


@contextmanager
def decision(deadline: Deadline | None):
    """Make `deadline` the current one for the code inside (and for threads started with a copy of this context)."""
    token = _CURRENT.set(deadline)
    try:
        yield deadline
    finally:
        _CURRENT.reset(token)


def parse_timer(raw) -> dict | None:
    """The newest Showdown timer line of a battle state: {"turn": s, "total": s, "grace": s}, or None."""
    try:
        obs = raw.get("observation") if isinstance(raw, dict) else None
        lines = obs.get("protocol_log") if isinstance(obs, dict) else None
        if not isinstance(lines, list):
            return None
        for line in reversed(lines[-400:]):
            if isinstance(line, str) and line.startswith("|inactive|") and "Time left" in line:
                m = _TIME_LEFT.search(line)
                if m:
                    return {"turn": int(m.group(1)), "total": int(m.group(2)), "grace": int(m.group(3) or 0)}
    except Exception:           # a malformed log is not worth a decision
        return None
    return None


def battle_seconds(timer: dict | None) -> tuple[float, str]:
    """(budget, source) of a battle decision from its timer line (None = not visible: the default)."""
    secs = _knob("BATTLE_S", BATTLE_S)
    if not timer:
        return max(_knob("MIN_S", MIN_S), secs), "default"
    turn_cap = (timer["turn"] - _knob("LATENCY_S", LATENCY_S)) * _knob("TURN_SHARE", TURN_SHARE)
    bank = timer["total"] + timer.get("grace", 0)
    bank_cap = (bank - _knob("BANK_RESERVE_S", BANK_RESERVE_S)) / max(1.0, _knob("BANK_SPREAD", BANK_SPREAD))
    return max(_knob("MIN_S", MIN_S), min(secs, turn_cap, bank_cap)), "timer"


def budget_for(raw, game_type: str | None = None) -> Deadline:
    """The Deadline of one decision on this state (see the module docstring). Never raises."""
    try:
        raw = raw if isinstance(raw, dict) else {}
        game = str(game_type or raw.get("game_type") or "")
        phase = str(raw.get("phase") or "")
        if phase == "messaging":
            return Deadline(_knob("MESSAGE_S", MESSAGE_S), "message")
        if game.startswith("pokemon"):
            if phase in PRE_BATTLE:
                kind = PRE_BATTLE[phase]
                default = {"draft": DRAFT_S, "teambuild": TEAMBUILD_S, "preview": PREVIEW_S}[kind]
                return Deadline(_knob(f"{kind.upper()}_S", default), kind)
            secs, source = battle_seconds(parse_timer(raw))
            return Deadline(secs, "battle" if phase in BATTLE_PHASES else f"pokemon:{phase or '?'}", source)
        return Deadline(_knob("OTHER_S", OTHER_S), game or "unknown")
    except Exception:
        return Deadline(BATTLE_S, "unknown", "error")
