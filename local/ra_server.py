"""The platform's GameAPI for one simulated Red Alert match (docs/platform/redalert.md sections 3-7 and 11) around a
local/ra_sim.World, plus a runner session (`SeatSession`) so `altruagent.runner.run_game` plays it exactly like the
real MCP session (local/ra_platform.py runs whole matches; local/fake_redalert.py serves it over HTTP / MCP).

Per seat: check-in by reading the state (phase waiting_for_agents -> playing once both are in, or 120 s after the
first), `get_game_state` / `wait_for_update` (long-poll on version, phase, is_current_actor, the end) /
`get_legal_actions` / `play_action` / `get_result` / `resign` / `get_game_config`.

play_action follows section 5: `{"type": "orders", "orders": [1-20]}` else INVALID_ACTION (also for an action_id,
for more than 200 engine commands, and for a batch where no order is valid, with "[i] cmd: reason (detail)" rows);
STALE_STATE for a state_version from the future or more than 3000 ticks old; NOT_YOUR_TURN before the start or while
the seat's previous batch is still being answered; a batch sent less than 0.2 s after the last accepted one is held
for the rest of the gap. Each order is checked on its own (rejected / pending / ok for an idempotent no-op); pending
orders reach the engine 5-10 ticks later and their verdicts (ok / partial / rejected with the engine's reason, `lost`
when --lost-rate drops them) land in a later state's `last_orders` and `recent_order_problems` (last 5 settled
batches, 10 rows). The match ends on victory (or a draw), at the time limit, on the idle rule (no engine-accepted
command from either seat for 10 minutes) with the (kills_cost - deaths_cost, assets_value) tiebreak, or a resignation.

Two clocks:
- lockstep (deterministic, as fast as the agents): the world advances one view (5 ticks) once every live seat is
  waiting (in wait_for_update, or in play_action's 0.2 s hold); a seat that keeps calling without ever waiting (more
  than SPIN_CALLS calls in one view) counts as waiting. `latency_ticks` holds each batch until that many ticks after
  the state it was based on (a fixed think + network delay). The same seeds and the same deterministic agents give
  the same match.
- realtime: a view every 0.2 s / speed of wall time, whatever the agents do (thinking time costs game time, scaled).
"""
from __future__ import annotations

import copy
import json
import random
import threading
import time
import uuid

from altruagent.mcp_transport import MCPToolError
from altruagent.models import GameState

from .ra_sim import STEP, TICKS_PER_SECOND, World

GAME = "red_alert"
MIN_BATCH_INTERVAL_S = 0.2
STALE_TICKS = 3000
MAX_ORDERS = 20
MAX_COMMANDS = 200
LONG_POLL_DEFAULT_S, LONG_POLL_MAX_S = 20.0, 25.0
AUTO_START_S = 120.0
NO_AGENTS_S = 300.0
IDLE_S = 600.0
SPIN_CALLS = 12
SETTLED_KEPT, PROBLEM_ROWS = 5, 10
LIMITS = {"max_orders_per_batch": MAX_ORDERS, "max_units_per_order": 50, "max_train_count": 10,
          "min_batch_interval_s": MIN_BATCH_INTERVAL_S, "stale_seconds": 120}


def _err(code: str, detail: str, tool: str | None = None, hint: str | None = None) -> dict:
    out = {"error": code, "detail": detail}
    if tool:
        out["next_actions"] = [{"tool": tool, "hint": hint or detail}]
    return out


class Seat:
    def __init__(self, n: int, agent_id: str, name: str):
        self.n, self.agent_id, self.name = n, agent_id, name
        self.checked_in = False
        self.checkin_wall = None
        self.waiting = 0                   # threads of this seat blocked in wait_for_update / the batch hold
        self.done = False                  # its runner has stopped (match over, or the agent crashed)
        self.in_flight = False
        self.batch_seq = 0
        self.last_accept_tick = -10 ** 9
        self.last_accept_wall = -10 ** 9
        self.last_active_tick = 0          # last batch with an engine-accepted command (idle rule)
        self.last_orders = None
        self.open: dict[int, dict] = {}    # batch -> record, verdicts still pending
        self.settled: list[dict] = []
        self.calls_this_view = 0
        self.wait_gen = -1
        self.resigned = False
        self.invalid_details: list[str] = []
        self.st = {"views": 0, "waits": 0, "batches": 0, "orders": 0, "commands": 0, "invalid_actions": 0,
                   "rejected": 0, "partial": 0, "lost": 0, "noop": 0, "errors": {}, "first_batch_tick": None,
                   "reasoning": 0, "held_s": 0.0, "config_calls": 0}


class MatchServer:
    def __init__(self, world: World, *, agent_ids=("agent-a", "agent-b"), names=None, clock: str = "lockstep",
                 speed: float = 1.0, max_minutes: float = 20.0, idle_s: float = IDLE_S,
                 auto_start_s: float = AUTO_START_S, no_agents_s: float = NO_AGENTS_S, session_id: str | None = None,
                 latency_ticks: int = 0, lost_rate: float = 0.0, lockstep_timeout_s: float = 10.0,
                 start_tick: int = 25, match_rules: dict | None = None):
        if clock not in ("lockstep", "realtime"):
            raise ValueError("clock must be lockstep or realtime")
        self.world = world
        self.clock, self.speed = clock, max(0.01, float(speed))
        self.max_minutes = float(max_minutes)
        self.idle_ticks = int(idle_s * TICKS_PER_SECOND)
        self.auto_start_s, self.no_agents_s = auto_start_s, no_agents_s
        self.session_id = session_id or str(uuid.uuid4())
        self.latency_ticks = int(latency_ticks)
        self.lost_rate = float(lost_rate)
        self.lost_rng = random.Random(f"lost:{world.seed}")
        self.lockstep_timeout_s = lockstep_timeout_s
        names = names or agent_ids
        self.seats = [Seat(i, agent_ids[i], names[i]) for i in (0, 1)]
        self.cv = threading.Condition()
        self.phase = "waiting_for_agents"
        self.start_tick = int(start_tick)
        self.end_tick = self.start_tick + int(self.max_minutes * 60 * TICKS_PER_SECOND)
        self.created_wall = time.monotonic()
        self.terminal = False
        self.reason = None
        self.failure = None
        self.winner = None                 # seat, None = draw / no result
        self.returns: dict = {}
        self.stopping = threading.Event()
        self.forced_advances = 0
        self.views_built = 0
        self.gen = 0                       # world advances so far (lockstep bookkeeping)
        self._cache: dict = {}
        self.match_rules = {"starting_cash": world.starting_cash, "starting_units": world.starting_units,
                            "explored_map": world.explored_map, "kill_bounties": False, "crates": False,
                            **(match_rules or {})}
        self.started_wall = None
        self.ended_wall = None

    # ---- the clock -----------------------------------------------------------------------------------------------
    def run(self) -> None:
        """The world thread: wait for the check-ins, then advance until the match ends."""
        with self.cv:
            while self.phase == "waiting_for_agents" and not self.stopping.is_set():
                now = time.monotonic()
                ins = [s for s in self.seats if s.checked_in]
                if len(ins) == 2 or (ins and now - min(s.checkin_wall for s in ins) >= self.auto_start_s):
                    self._start()
                    break
                if not ins and now - self.created_wall >= self.no_agents_s:
                    self._fail("no_agents_connected")
                    return
                self.cv.wait(0.05)
        if self.clock == "realtime":
            self._run_realtime()
        else:
            self._run_lockstep()

    def _start(self) -> None:
        w = self.world
        while w.tick < self.start_tick:
            w.step()
        w.verdicts.clear()
        for s in self.seats:
            s.last_active_tick = w.tick
        self.phase = "playing"
        self.started_wall = time.monotonic()
        self.cv.notify_all()

    def _blocked(self, s: Seat) -> bool:
        """Lockstep: whether this seat is waiting for the next view. A seat that was waiting when the world last
        advanced is not, until it has looked at the new view and gone back to waiting (wait_gen)."""
        return s.done or not s.checked_in or s.calls_this_view > SPIN_CALLS or \
            (s.waiting > 0 and s.wait_gen == self.gen)

    def _run_lockstep(self) -> None:
        while True:
            with self.cv:
                deadline = time.monotonic() + self.lockstep_timeout_s
                while not self.terminal and not self.stopping.is_set() and \
                        not all(self._blocked(s) for s in self.seats):
                    left = deadline - time.monotonic()
                    if left <= 0:
                        self.forced_advances += 1
                        break
                    self.cv.wait(min(left, 0.5))
                if self.terminal or self.stopping.is_set():
                    return
                self._advance()

    def _run_realtime(self) -> None:
        period = STEP / TICKS_PER_SECOND / self.speed
        nxt = time.monotonic()
        while True:
            nxt += period
            with self.cv:
                while not self.terminal and not self.stopping.is_set():
                    left = nxt - time.monotonic()
                    if left <= 0:
                        break
                    self.cv.wait(left)
                if self.terminal or self.stopping.is_set():
                    return
                self._advance()
            if time.monotonic() - nxt > 1.0:              # fell behind (a slow machine): no burst of steps
                nxt = time.monotonic()

    def _advance(self) -> None:
        w = self.world
        w.step()
        self.gen += 1
        for s in self.seats:
            s.calls_this_view = 0
        for seat, batch, index, status, reason, detail, failed, accepted in w.verdicts:
            self._verdict(seat, batch, index, status, reason, detail, failed, accepted)
        w.verdicts.clear()
        if w.over:
            self._finish("draw" if w.winner == -1 else "victory", None if w.winner == -1 else w.winner)
        elif w.tick >= self.end_tick:
            self._tiebreak("time_limit")
        elif w.tick - max(s.last_active_tick for s in self.seats) >= self.idle_ticks:
            self._tiebreak("idle_timeout")
        self.cv.notify_all()

    # ---- the end ---------------------------------------------------------------------------------------------------
    def _tiebreak(self, reason: str) -> None:
        a, b = self.world.tiebreak(0), self.world.tiebreak(1)
        self._finish(reason, None if a == b else (0 if a > b else 1))

    def _finish(self, reason: str, winner: int | None) -> None:
        if self.terminal:
            return
        self.terminal, self.reason, self.winner = True, reason, winner
        ids = [s.agent_id for s in self.seats]
        if winner is None:
            self.returns = {ids[0]: 0.5, ids[1]: 0.5}
        else:
            self.returns = {ids[winner]: 1.0, ids[1 - winner]: 0.0}
        self.phase = "finished"
        self.ended_wall = time.monotonic()
        self.cv.notify_all()

    def _fail(self, reason: str) -> None:
        self.terminal, self.reason, self.failure, self.winner, self.returns = True, reason, reason, None, {}
        self.phase = "finished"
        self.ended_wall = time.monotonic()
        self.cv.notify_all()

    def stop(self) -> None:
        self.stopping.set()
        with self.cv:
            self.cv.notify_all()

    # ---- verdicts ----------------------------------------------------------------------------------------------------
    def _verdict(self, seat: int, batch: int, index: int, status: str, reason: str, detail, failed, accepted):
        s = self.seats[seat]
        rec = s.open.get(batch)
        if rec is None:
            return
        row = rec["results"][index]
        row.update(status=status, reason=reason, detail=detail, failed=failed)
        if accepted > 0:
            s.last_active_tick = self.world.tick
        if status == "rejected":
            s.st["rejected"] += 1
        elif status == "partial":
            s.st["partial"] += 1
        rec["_left"] -= 1
        if rec["_left"] <= 0:
            self._settle(s, rec)

    def _settle(self, s: Seat, rec: dict) -> None:
        rec["pending"] = False
        rec["applied_at_tick"] = self.world.tick
        rec["accepted_orders"] = sum(1 for r in rec["results"] if r["status"] in ("ok", "partial"))
        rec["rejected_orders"] = sum(1 for r in rec["results"] if r["status"] in ("rejected", "lost"))
        s.open.pop(rec["batch"], None)
        s.settled = (s.settled + [rec])[-SETTLED_KEPT:]

    def _problems(self, s: Seat) -> list[dict]:
        rows = [{"batch": rec["batch"], "index": r["index"], "cmd": r["cmd"], "status": r["status"],
                 "reason": r["reason"], "detail": r["detail"]}
                for rec in s.settled for r in rec["results"] if r["status"] in ("rejected", "partial", "lost")]
        return rows[-PROBLEM_ROWS:]

    # ---- the state ---------------------------------------------------------------------------------------------------
    def _checkin(self, seat: int) -> None:
        s = self.seats[seat]
        if not s.checked_in:
            s.checked_in, s.checkin_wall = True, time.monotonic()
            self.cv.notify_all()

    def _obs(self, seat: int) -> tuple[dict, dict]:
        """This seat's observation and legal_actions for the current tick (built once per view, fresh copies)."""
        key = (seat, self.world.tick)
        hit = self._cache.get(seat)
        if hit is None or hit[0] != key:
            obs = self.world.observation(seat)
            la = self.world.legal_actions(seat, obs)
            hit = (key, json.dumps(obs), json.dumps(la))
            self._cache[seat] = hit
            self.views_built += 1
        return json.loads(hit[1]), json.loads(hit[2])

    def _pacing_ms(self, s: Seat) -> int:
        if self.clock == "lockstep":
            left = s.last_accept_tick + STEP - self.world.tick
            return max(0, int(left * 1000 / TICKS_PER_SECOND))
        left = s.last_accept_wall + MIN_BATCH_INTERVAL_S / self.speed - time.monotonic()
        return max(0, int(left * 1000 * self.speed))

    def state(self, seat: int) -> dict:
        w = self.world
        s = self.seats[seat]
        playing = self.phase == "playing"
        version = w.tick if (playing or (self.terminal and self.started_wall is not None)) else 0
        game_s = max(0, w.tick - self.start_tick) / TICKS_PER_SECOND if self.started_wall else 0.0
        out = {
            "session_id": self.session_id, "game_type": GAME, "runtime_adapter": "openra",
            "status": "completed" if self.terminal else "in_progress", "phase": self.phase,
            "state_version": version, "is_current_actor": playing and not s.in_flight,
            "is_terminal": self.terminal, "your_seat": f"Multi{seat}",
            "players": [{"seat": f"Multi{x.n}", "agent_id": x.agent_id, "name": x.name,
                         "faction": w.players[x.n].country if self.started_wall else None,
                         "checked_in": x.checked_in} for x in self.seats],
            "pacing": {"mode": "realtime", "min_batch_interval_s": MIN_BATCH_INTERVAL_S, "stale_seconds": 120,
                       "next_batch_allowed_in_ms": self._pacing_ms(s)},
            "limits": dict(LIMITS),
            "time": {"max_minutes": self.max_minutes, "game_seconds": round(game_s, 1),
                     "remaining_seconds": round(max(0.0, self.max_minutes * 60 - game_s), 1)},
            "match_rules": dict(self.match_rules), "queue_position": None,
            "map": {"id": w.map_id, "width": w.width, "height": w.height, "bounds": list(w.bounds),
                    "layout": f"get_game_config('red_alert').rules.maps.{w.map_id}.layout"},
            "window": None, "observation": None,
            "last_orders": copy.deepcopy(self._public(s.last_orders)),
            "recent_order_problems": self._problems(s),
        }
        if self.phase == "waiting_for_agents":
            missing = [x.n for x in self.seats if not x.checked_in]
            out["waiting_for_players"] = missing
            out["hint"] = "Waiting for the other agent to check in; call wait_for_update."
            out["next_actions"] = [{"tool": "wait_for_update", "hint": "returns when the game starts"}]
        elif playing:
            obs, la = self._obs(seat)
            out["observation"] = obs
            out["legal_actions"] = la
            out["hint"] = "The game is running: send orders with play_action, or wait_for_update for the next view."
            out["next_actions"] = [{"tool": "play_action", "hint": "send a batch of 1-20 orders"}]
        if self.terminal:
            if self.started_wall is not None:
                out["observation"], _ = self._obs(seat)
            out["result"] = self._result_row(seat)
            out["hint"] = "The match is over; call get_result."
            out["next_actions"] = [{"tool": "get_result", "hint": "the final result"}]
        return out

    @staticmethod
    def _public(rec):
        if rec is None:
            return None
        return {k: v for k, v in rec.items() if not k.startswith("_")}

    def _result_row(self, seat: int) -> dict:
        if self.failure:
            outcome = "no_result"
        elif self.winner is None:
            outcome = "draw"
        else:
            outcome = "win" if self.winner == seat else "loss"
        return {"outcome": outcome, "reason": self.reason,
                "winner_seat": None if self.winner is None else f"Multi{self.winner}",
                "returns": dict(self.returns)}

    # ---- tools -------------------------------------------------------------------------------------------------------
    def get_game_state(self, seat: int) -> dict:
        with self.cv:
            self._checkin(seat)
            s = self.seats[seat]
            s.st["views"] += 1
            s.calls_this_view += 1
            if s.calls_this_view > SPIN_CALLS:
                self.cv.notify_all()
            return self.state(seat)

    def get_legal_actions(self, seat: int) -> dict:
        with self.cv:
            self._checkin(seat)
            st = self.state(seat)
            return dict(st.get("legal_actions") or {}, session_id=self.session_id, state_version=st["state_version"])

    def wait_for_update(self, seat: int, since_version=None, since_is_current_actor=None, since_phase=None,
                        timeout_seconds=None) -> dict:
        timeout = min(LONG_POLL_MAX_S, float(timeout_seconds or LONG_POLL_DEFAULT_S))
        end = time.monotonic() + timeout
        with self.cv:
            self._checkin(seat)
            s = self.seats[seat]
            s.st["waits"] += 1
            s.waiting += 1
            self.cv.notify_all()
            try:
                while not self.stopping.is_set():
                    playing = self.phase == "playing"
                    ver = self.world.tick if playing or self.terminal else 0
                    cur = playing and not s.in_flight
                    if (self.terminal or ver != since_version or (since_phase is not None and self.phase != since_phase)
                            or (since_is_current_actor is not None and bool(cur) != bool(since_is_current_actor))):
                        break
                    left = end - time.monotonic()
                    if left <= 0:
                        break
                    s.wait_gen = self.gen
                    self.cv.notify_all()
                    self.cv.wait(min(left, 1.0))
            finally:
                s.waiting -= 1
            out = self.state(seat)
            out["updated"] = out["state_version"] != since_version or out["phase"] != since_phase
            return out

    def get_game_config(self, seat: int, game_type: str) -> dict:
        with self.cv:
            self.seats[seat].st["config_calls"] += 1
        if game_type != GAME:
            return _err("UNKNOWN_GAME", f"no reference for {game_type!r}")
        return copy.deepcopy(self.world.rules.config)

    def resign(self, seat: int) -> dict:
        with self.cv:
            if not self.terminal:
                self.seats[seat].resigned = True
                self._finish("resignation", 1 - seat)
            elif self.reason != "resignation" or not self.seats[seat].resigned:
                return _err("GAME_ALREADY_COMPLETE", "the match is over", "get_result")
            return self.result(seat, locked=True)

    def result(self, seat: int, locked: bool = False) -> dict:
        def build():
            ids = [s.agent_id for s in self.seats]
            if not self.terminal:
                return {"session_id": self.session_id, "is_terminal": False, "status": "in_progress", "returns": None,
                        "your_return": None, "winner_agent_id": None, "termination_reason": None,
                        "failure_reason": None}
            return {"session_id": self.session_id, "is_terminal": True, "status": "completed",
                    "returns": dict(self.returns), "your_return": self.returns.get(ids[seat]),
                    "winner_agent_id": None if self.winner is None else ids[self.winner],
                    "termination_reason": self.reason, "failure_reason": self.failure}
        if locked:
            return build()
        with self.cv:
            return build()

    def play_action(self, seat: int, state_version=None, action=None, action_id=None, reasoning_summary=None) -> dict:
        s = self.seats[seat]
        with self.cv:
            self._checkin(seat)
            s.calls_this_view += 1
            if self.terminal:
                return self._count(s, _err("GAME_ALREADY_COMPLETE", "the match is over", "get_result"))
            if self.phase != "playing":
                return self._count(s, _err("NOT_YOUR_TURN", "The game has not started yet", "wait_for_update"))
            if s.in_flight:
                return self._count(s, _err("NOT_YOUR_TURN", "Your previous play_action is still being answered",
                                           "get_game_state"))
            if action is None and action_id is not None:
                return self._invalid(s, "Red Alert has no action ids: send action={\"type\": \"orders\", ...}")
            orders = action.get("orders") if isinstance(action, dict) and action.get("type") == "orders" else None
            if not isinstance(orders, list):
                return self._invalid(s, "action must be {\"type\": \"orders\", \"orders\": [...]}")
            if not 1 <= len(orders) <= MAX_ORDERS:
                return self._invalid(s, f"a batch is 1-{MAX_ORDERS} orders, got {len(orders)}")
            stale = self._stale(state_version)
            if stale:
                return self._count(s, _err("STALE_STATE", stale, "get_game_state"))
            s.in_flight = True
            try:
                held = self._hold(s, state_version)
                if held is not None:
                    return self._count(s, held)
                return self._accept(s, state_version, orders, reasoning_summary)
            finally:
                s.in_flight = False
                self.cv.notify_all()

    def _stale(self, sv) -> str | None:
        t = self.world.tick
        if not isinstance(sv, int) or isinstance(sv, bool):
            return "state_version must be the tick of the state you acted on"
        if sv > t:
            return f"state_version {sv} is in the future (now {t})"
        if sv < t - STALE_TICKS:
            return f"state_version {sv} is more than {STALE_TICKS} ticks old (now {t})"
        return None

    def _hold(self, s: Seat, sv: int) -> dict | None:
        """The 0.2 s gap after the last accepted batch (and, in lockstep, the modelled latency). Called with the
        lock held; returns an error if the match ended meanwhile."""
        t0 = time.monotonic()
        s.waiting += 1
        self.cv.notify_all()
        try:
            while not self.stopping.is_set():
                if self.terminal:
                    return _err("GAME_ALREADY_COMPLETE", "the match is over", "get_result")
                if self.clock == "lockstep":
                    if self.world.tick >= max(s.last_accept_tick + STEP, sv + self.latency_ticks):
                        return None
                    s.wait_gen = self.gen
                    self.cv.notify_all()
                    self.cv.wait(1.0)
                else:
                    left = s.last_accept_wall + MIN_BATCH_INTERVAL_S / self.speed - time.monotonic()
                    if left <= 0:
                        return None
                    self.cv.wait(left)
            return _err("RUNTIME_TEMPORARILY_UNAVAILABLE", "the game server is stopping")
        finally:
            s.waiting -= 1
            s.st["held_s"] += time.monotonic() - t0

    def _count(self, s: Seat, err: dict) -> dict:
        s.st["errors"][err["error"]] = s.st["errors"].get(err["error"], 0) + 1
        return err

    def _invalid(self, s: Seat, detail: str) -> dict:
        s.st["invalid_actions"] += 1
        s.invalid_details = (s.invalid_details + [f"t{self.world.tick}: {detail}"[:300]])[-5:]
        return self._count(s, _err("INVALID_ACTION", detail, "get_game_state", "fix the batch and resend at once"))

    def _accept(self, s: Seat, sv: int, orders: list, reasoning) -> dict:
        w = self.world
        checks = [w.check_order(s.n, o) for o in orders]
        commands = sum(c["commands"] for c in checks if c["status"] == "pending")
        if commands > MAX_COMMANDS:
            return self._invalid(s, f"the batch expands to {commands} engine commands (at most {MAX_COMMANDS}); "
                                    "nothing was sent")
        if not any(c["status"] in ("pending", "ok") for c in checks):
            rows = "; ".join(f"[{i}] {c.get('cmd') or (o.get('cmd') if isinstance(o, dict) else None)}: "
                             f"{c['reason']}" + (f" ({c['detail']})" if c.get("detail") else "")
                             for i, (o, c) in enumerate(zip(orders, checks)))
            return self._invalid(s, f"No order was valid: {rows}")
        s.batch_seq += 1
        batch = s.batch_seq
        delay = w.order_rng[s.n].choice(w.apply_delay_ticks)
        apply_tick = w.tick + max(1, delay)
        results, left = [], 0
        for i, (o, c) in enumerate(zip(orders, checks)):
            cmd = c.get("cmd") or (o.get("cmd") if isinstance(o, dict) else None)
            row = {"index": i, "cmd": cmd, "status": c["status"], "reason": c["reason"], "detail": c.get("detail"),
                   "commands": c["commands"], "failed": list(c.get("failed") or [])}
            if c["status"] == "pending":
                if self.lost_rate and self.lost_rng.random() < self.lost_rate:
                    row["_lost"] = True
                else:
                    w.enqueue(s.n, batch, i, o, c, apply_tick)
                    left += 1
            elif c["status"] == "rejected":
                s.st["rejected"] += 1
            else:
                s.st["noop"] += 1
            results.append(row)
        answer_rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in results]
        for r in results:                              # lost orders: their verdict comes "later", as lost
            if r.pop("_lost", False):
                r.update(status="lost", reason="not_applied", detail="the orders never reached the engine")
                s.st["lost"] += 1
        rec = {"state_version": sv, "batch": batch, "sent_at_tick": w.tick, "carried": left, "pending": left > 0,
               "applied_at_tick": None, "results": results,
               "accepted_orders": sum(1 for r in results if r["status"] in ("pending", "ok")),
               "rejected_orders": sum(1 for r in results if r["status"] == "rejected"), "_left": left}
        s.last_orders = rec
        if left:
            s.open[batch] = rec
        else:
            self._settle(s, rec)
        s.last_accept_tick, s.last_accept_wall = w.tick, time.monotonic()
        s.st["batches"] += 1
        s.st["orders"] += len(orders)
        s.st["commands"] += commands
        if s.st["first_batch_tick"] is None:
            s.st["first_batch_tick"] = w.tick
        if reasoning:
            s.st["reasoning"] += 1
        acc = sum(1 for r in answer_rows if r["status"] in ("pending", "ok"))
        return {"accepted": True, "session_id": self.session_id, "status": "in_progress", "state_version": sv,
                "pending": left > 0, "applied_at_tick": None, "accepted_orders": acc,
                "rejected_orders": len(answer_rows) - acc, "next_batch_allowed_in_ms": 200, "results": answer_rows,
                "hint": f"Orders sent at tick {w.tick}; the engine applies them in a moment. Read the new state and "
                        "act again.",
                "next_actions": [{"tool": "get_game_state",
                                  "hint": "Your orders' verdicts appear in the next state's last_orders."}]}

    def seat_done(self, seat: int) -> None:
        with self.cv:
            self.seats[seat].done = True
            self.cv.notify_all()


class SeatSession:
    """The MCPGameSession surface `altruagent.runner.run_game` uses, for one seat of a MatchServer: tool errors
    come back as MCPToolError with the platform's error codes, like the real session."""

    def __init__(self, server: MatchServer, seat: int):
        self.server, self.seat = server, seat
        self.session_id = server.session_id
        self.game_server_url = "local-ra-sim"

    @staticmethod
    def _raise(out: dict) -> dict:
        if isinstance(out, dict) and "error" in out:
            nxt = (out.get("next_actions") or [None])[0]
            raise MCPToolError(out.get("detail") or out["error"], status_code=None, error_code=out["error"],
                               detail=out.get("detail"), next_action=nxt)
        return out

    def get_state(self) -> GameState:
        return GameState.from_mcp_state(self._raise(self.server.get_game_state(self.seat)))

    def wait_for_update(self, *, since_version, since_message_seq=None, since_is_current_actor=None,
                        since_phase=None, timeout_seconds=None) -> GameState:
        return GameState.from_mcp_state(self._raise(self.server.wait_for_update(
            self.seat, since_version, since_is_current_actor, since_phase, timeout_seconds)))

    def get_game_config(self, game_type: str) -> dict:
        return self._raise(self.server.get_game_config(self.seat, game_type))

    def get_legal_actions(self) -> dict:
        return self._raise(self.server.get_legal_actions(self.seat))

    def play_action(self, *, action_id=None, action=None, state_version, reasoning_summary=None) -> dict:
        return self._raise(self.server.play_action(self.seat, state_version, action, action_id, reasoning_summary))

    def send_message(self, **_kw) -> dict:
        return self._raise(_err("RUNTIME_UNAVAILABLE", "Red Alert has no messaging"))

    def get_messages(self, **_kw) -> dict:
        return self._raise(_err("RUNTIME_UNAVAILABLE", "Red Alert has no messaging"))

    def resign(self) -> dict:
        return self._raise(self.server.resign(self.seat))

    def get_result(self) -> dict:
        return self._raise(self.server.result(self.seat))
