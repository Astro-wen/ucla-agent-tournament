"""A Red Alert seat for local/fake_platform.py (`--red-alert N`): a REAL-TIME match (docs/platform/redalert.md), for
checking that the deployed agent, which has no Red Alert policy, sits a red_alert assignment out cleanly through the
real process tree (the SDK's real-time runner path: altruagent.WAIT, context.game_config). Never the real platform.

No engine: our seat sees a scripted world that advances 5 ticks every 0.2 s (25 ticks a second, a new view about 5
times a second, like the platform) for --ra-seconds, after a short `waiting_for_agents` phase, then ends
(`time_limit`; our seat scores 0.0). The state is shaped like the platform's (pacing.mode "realtime", an observation
with units / buildings / production, legal_actions as a dict of short lists, no `actions` list). play_action takes
`{"type": "orders", "orders": [...]}` and refuses anything else as a whole (INVALID_ACTION), like the platform;
get_game_config("red_alert") answers data/red_alert_config.json (the platform's own reference). Stats count every
batch, refusal, view and config fetch.
"""
from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
import uuid
from pathlib import Path

GAME = "red_alert"
TICKS_PER_VIEW = 5                 # 25 ticks per second, a view every 0.2 s
VIEW_S = 0.2
WAITING_S = 1.0                    # the `waiting_for_agents` phase before `playing`
LONG_POLL_MAX_S = 25.0
PACING = {"mode": "realtime", "min_batch_interval_s": 0.2, "stale_seconds": 120, "next_batch_allowed_in_ms": 0}
LIMITS = {"max_orders_per_batch": 20, "max_units_per_order": 50, "max_train_count": 10, "min_batch_interval_s": 0.2,
          "stale_seconds": 120}
CONFIG_FILE = Path(__file__).resolve().parents[1] / "data" / "red_alert_config.json"


def _err(code: str, detail: str = "") -> dict:
    return {"error": code, "detail": detail}


def game_config() -> dict:
    """get_game_config("red_alert"): the platform's reference as saved in data/red_alert_config.json."""
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return {"game_type": GAME, "rules": {}}


class FakeRedAlertMatch:
    """One 1v1 real-time Red Alert match: our seat over MCP (the tools below), nobody on the other side."""
    game_type = GAME
    seat_tag = "ra"
    seat_count = 2

    def __init__(self, platform, n: int, seconds: float = 20.0):
        self.platform, self.n = platform, n
        self.seconds = max(1.0, float(seconds))
        self.sid = str(uuid.uuid4())
        self.seat = n % 2
        self.cond = threading.Condition()
        self.tick = 0
        self.phase = "waiting_for_agents"
        self.terminal = False
        self.reason = None
        self.returns: dict = {}
        self.started = time.time()
        self.ended = None
        self.st = {"decisions": 0, "latency_s": [], "slow": [], "timer": {}, "invalid_actions": 0, "race_errors": 0,
                   "reasoning_lines": 0, "reasoning_max_len": 0, "reasoning_over_cap": 0, "winner_seat": None,
                   "turns": 0, "match_invalid": 0, "error": None, "lat": [], "batches": 0, "views": 0,
                   "config_calls": 0, "tool_errors": []}

    # ---- the world ----------------------------------------------------------------------------------------------
    def run(self) -> None:
        stop = self.platform.stopping
        stop.wait(WAITING_S)
        with self.cond:
            self.phase = "playing"
            self.tick = TICKS_PER_VIEW
            self.cond.notify_all()
        end = time.monotonic() + self.seconds
        while time.monotonic() < end and not stop.is_set():
            stop.wait(VIEW_S)
            with self.cond:
                self.tick += TICKS_PER_VIEW
                self.cond.notify_all()
        from .fake_platform import AGENT_ID
        with self.cond:
            self.st["turns"] = self.tick
            self.returns = {AGENT_ID: 0.0, "agent-opponent": 1.0}
            self.reason = "time_limit" if not stop.is_set() else "stopped"
            self.phase = "finished"
            self.terminal = True
            self.ended = time.time()
            self.tick += 1
            self.cond.notify_all()
        self.platform.match_done(self)

    def view(self) -> dict:
        with self.cond:
            playing = self.phase == "playing"
            out = {"session_id": self.sid, "game_type": GAME, "runtime_adapter": "openra",
                   "status": "completed" if self.terminal else "in_progress", "phase": self.phase,
                   "state_version": self.tick if (playing or self.terminal) else 0,
                   "is_current_actor": playing, "is_terminal": self.terminal, "pacing": dict(PACING),
                   "limits": dict(LIMITS), "your_seat": f"Multi{self.seat}", "window": None,
                   "map": {"id": "singles", "width": 112, "height": 54, "bounds": [2, 2, 108, 50]},
                   "observation": None, "last_orders": [], "recent_order_problems": []}
            if playing:
                out["observation"] = {
                    "tick": self.tick, "faction": "england", "enemy_faction": "russia",
                    "economy": {"cash": 5000, "ore": 0, "power_provided": 0, "power_drained": 0,
                                "resource_capacity": 0, "harvesters": 0},
                    "units": [{"id": 102, "type": "mcv", "x": 14, "y": 17, "hp": 100, "act": None, "idle": True,
                               "stance": "defend"}],
                    "buildings": [], "production": [], "available_production": [], "visible_enemies": [],
                    "visible_enemy_buildings": [], "base_center": [14, 17], "explored_percent": 3, "events": []}
                out["legal_actions"] = {"build": [], "train": [], "place": [], "deploy": [102], "repair": [],
                                        "attack": [], "attack_now": None, "enemy_base": [95, 11],
                                        "combat_units": [], "idle_units": [102], "harvesters": [],
                                        "production_buildings": [], "how_to_send": "play_action(...)"}
            if self.terminal:
                out["result"] = {"outcome": "loss", "reason": self.reason, "winner_seat": f"Multi{1 - self.seat}",
                                 "returns": copy.deepcopy(self.returns)}
            return out

    # ---- MCP ------------------------------------------------------------------------------------------------------
    async def mcp(self, tool: str, a: dict) -> dict:
        if tool in ("get_game_state", "wait_for_update"):
            if tool == "wait_for_update":
                since = a.get("since_version")
                since_phase, since_cur = a.get("since_phase"), a.get("since_is_current_actor")
                end = time.monotonic() + min(LONG_POLL_MAX_S, float(a.get("timeout_seconds") or 20.0))
                while time.monotonic() < end and not self.platform.stopping.is_set():
                    v = self.view()
                    if (v["is_terminal"] or v["state_version"] != since or (since_phase and v["phase"] != since_phase)
                            or (since_cur is not None and bool(v["is_current_actor"]) != bool(since_cur))):
                        break
                    await asyncio.sleep(0.05)
            out = self.view()
            with self.cond:
                self.st["views"] += 1
            if tool == "wait_for_update":
                out["updated"] = True
            return out
        if tool == "get_legal_actions":
            v = self.view()
            return dict(v.get("legal_actions") or {}, session_id=self.sid, state_version=v["state_version"])
        if tool == "play_action":
            return self._play(a)
        if tool == "get_result":
            if not self.terminal:
                return _err("GAME_NOT_COMPLETE", "still running")
            return {"session_id": self.sid, "is_terminal": True, "status": "completed",
                    "returns": copy.deepcopy(self.returns), "termination_reason": self.reason}
        if tool == "resign":
            with self.cond:
                if not self.terminal:
                    self.reason, self.terminal, self.phase = "resign", True, "finished"
                    from .fake_platform import AGENT_ID
                    self.returns = {AGENT_ID: 0.0, "agent-opponent": 1.0}
                    self.ended = time.time()
                    self.cond.notify_all()
            return {"session_id": self.sid, "is_terminal": True, "status": "completed",
                    "returns": copy.deepcopy(self.returns), "termination_reason": "resign"}
        if tool in ("send_message", "get_messages"):
            return _err("MESSAGING_NOT_ENABLED", "Red Alert has no chat")
        return _err("UNKNOWN_TOOL", tool)

    def _play(self, a: dict) -> dict:
        with self.cond:
            if self.terminal:
                self.st["race_errors"] += 1
                return _err("GAME_ALREADY_COMPLETE", "the match is over")
            if self.phase != "playing":
                self.st["race_errors"] += 1
                return _err("NOT_YOUR_TURN", "The game has not started yet")
            self.st["batches"] += 1
            action = a.get("action")
            orders = action.get("orders") if isinstance(action, dict) and action.get("type") == "orders" else None
            if not isinstance(orders, list) or not 1 <= len(orders) <= LIMITS["max_orders_per_batch"]:
                self.st["invalid_actions"] += 1
                self.st["tool_errors"] = (self.st["tool_errors"] + ["INVALID_ACTION"])[-20:]
                return _err("INVALID_ACTION", f"No order was valid: not an orders batch: {str(action)[:80]}")
            self.st["decisions"] += 1
            results = [{"index": i, "cmd": (o or {}).get("cmd") if isinstance(o, dict) else None,
                        "status": "pending", "reason": "pending"} for i, o in enumerate(orders)]
            return {"accepted": True, "session_id": self.sid, "status": "in_progress", "pending": True,
                    "results": results}

    def summary(self) -> dict:
        row = {k: v for k, v in self.st.items() if k != "latency_s"}
        row.update(session_id=self.sid, n=self.n, seat=self.seat, opponent="none", terminal=self.terminal,
                   phase=self.phase, game_type=GAME, win=False if self.terminal else None, reason=self.reason,
                   started=time.strftime("%H:%M:%S", time.localtime(self.started)),
                   secs=round((self.ended or time.time()) - self.started, 1), bank_used=0.0,
                   latency_p50=None, latency_max=None)
        return row
