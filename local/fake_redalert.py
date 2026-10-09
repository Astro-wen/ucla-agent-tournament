"""A Red Alert seat for local/fake_platform.py (`--red-alert N`): a REAL-TIME match (docs/platform/redalert.md) played
on the local simulator (local/ra_sim.py world, local/ra_server.py GameAPI), so the deployed agent runs a red_alert
assignment through the real process tree (the SDK's real-time runner path: altruagent.WAIT, context.game_config,
on_action_result). Never the real platform.

Our seat (n % 2) is answered over MCP; the other seat is a scripted opponent (local/ra_bots.py, --ra-opponent: idle by
default) played in-process through altruagent.runner.run_game. The world runs on the realtime clock (a view every
0.2 s / --ra-speed) for --ra-seconds of game time, then ends at the time limit with the platform's tiebreak (or
earlier on victory / resignation). get_game_config("red_alert") answers data/red_alert_config.json (the platform's own
reference). Stats count every batch, refusal, view and config fetch, plus the match's military result.
"""
from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from pathlib import Path

from .ra_rules import CONFIG_FILE, Rules
from .ra_server import MatchServer, SeatSession
from .ra_sim import TICKS_PER_SECOND, World

GAME = "red_alert"
MAPS = ("sidestep", "trapped", "shadowfiend-ii")
_RACES = ("NOT_YOUR_TURN", "STALE_STATE", "GAME_ALREADY_COMPLETE")


def _err(code: str, detail: str = "") -> dict:
    return {"error": code, "detail": detail}


def game_config() -> dict:
    """get_game_config("red_alert"): the platform's reference as saved in data/red_alert_config.json."""
    try:
        return json.loads(Path(CONFIG_FILE).read_text())
    except (OSError, ValueError):
        return {"game_type": GAME, "rules": {}}


class FakeRedAlertMatch:
    """One 1v1 real-time Red Alert match on the simulator: our seat over MCP (the tools below), a scripted bot on
    the other."""
    game_type = GAME
    seat_tag = "ra"
    seat_count = 2

    def __init__(self, platform, n: int, seconds: float = 20.0, opponent: str = "idle", speed: float = 1.0,
                 map_id: str | None = None, seed: int | None = None):
        from .fake_platform import AGENT_ID
        self.platform, self.n = platform, n
        self.seconds = max(1.0, float(seconds))
        self.opponent, self.speed = opponent, max(0.05, float(speed))
        self.seat = n % 2
        seed = (getattr(platform, "seed", 0) or 0) * 1000 + n if seed is None else seed
        self.map_id = map_id or MAPS[n % len(MAPS)]
        self.world = World(Rules(), self.map_id, seed)
        ids = [None, None]
        ids[self.seat], ids[1 - self.seat] = AGENT_ID, "agent-opponent"
        self.server = MatchServer(self.world, agent_ids=tuple(ids), clock="realtime", speed=self.speed,
                                  max_minutes=self.seconds / 60.0)
        self.sid = self.server.session_id
        self.seed = seed
        self.started = time.time()
        self.ended = None
        self.reason = None
        self.returns: dict = {}
        self.no_show = False
        self.st = {"decisions": 0, "latency_s": [], "slow": [], "timer": {}, "invalid_actions": 0, "race_errors": 0,
                   "reasoning_lines": 0, "reasoning_max_len": 0, "reasoning_over_cap": 0, "winner_seat": None,
                   "turns": 0, "match_invalid": 0, "error": None, "lat": [], "batches": 0, "views": 0,
                   "config_calls": 0, "tool_errors": [], "rejected_orders": 0, "opponent_error": None}

    @property
    def terminal(self) -> bool:
        return self.server.terminal

    @property
    def phase(self) -> str:
        return self.server.phase

    # ---- the world ----------------------------------------------------------------------------------------------
    def run(self) -> None:
        from altruagent.models import DecisionContext
        from altruagent.runner import run_game

        from .ra_bots import make_bot
        stop = self.platform.stopping
        other = 1 - self.seat
        world_thread = threading.Thread(target=self.server.run, name=f"ra-world-{self.n}", daemon=True)
        world_thread.start()

        def opponent():
            ctx = DecisionContext(session_id=self.sid, tournament_id=None, game_type=GAME, agent_id="agent-opponent",
                                  seat_position=other)
            try:
                run_game(SeatSession(self.server, other), ctx, make_bot(self.opponent, self.seed),
                         wait_seconds=0.05, sleep=lambda s: time.sleep(min(s, 0.2)))
            except BaseException as e:  # noqa: BLE001 - recorded; the match goes on without the bot
                self.st["opponent_error"] = repr(e)[:300]
            finally:
                self.server.seat_done(other)

        threading.Thread(target=opponent, name=f"ra-opponent-{self.n}", daemon=True).start()
        while not self.server.terminal and not stop.is_set():
            stop.wait(0.2)
        if not self.server.terminal:
            with self.server.cv:
                self.server._finish("stopped", None)
        self.server.stop()
        world_thread.join(5)
        with self.server.cv:
            w = self.world
            self.st["turns"] = w.tick
            self.st["winner_seat"] = self.server.winner
            self.st["military"] = dict(w.players[self.seat].mil)
            self.st["opponent_military"] = dict(w.players[other].mil)
            self.st["rejected_orders"] = self.server.seats[self.seat].st["rejected"]
            self.reason = self.server.reason
            self.returns = dict(self.server.returns)
            self.ended = time.time()
        self.platform.match_done(self)

    def forfeit_no_show(self) -> None:
        """Our runtime never connected within the seat's connect window (local/fake_official.py): our seat loses."""
        with self.server.cv:
            self.no_show = True
            self.server._finish("no_show", 1 - self.seat)

    def view(self) -> dict:
        return self.server.state(self.seat)

    # ---- MCP ------------------------------------------------------------------------------------------------------
    async def mcp(self, tool: str, a: dict) -> dict:
        srv, seat = self.server, self.seat
        if tool == "get_game_state":
            self.st["views"] += 1
            return srv.get_game_state(seat)
        if tool == "wait_for_update":
            self.st["views"] += 1
            out = await asyncio.to_thread(srv.wait_for_update, seat, a.get("since_version"),
                                          a.get("since_is_current_actor"), a.get("since_phase"),
                                          a.get("timeout_seconds"))
            return out
        if tool == "get_legal_actions":
            return srv.get_legal_actions(seat)
        if tool == "play_action":
            t0 = time.monotonic()
            out = await asyncio.to_thread(srv.play_action, seat, a.get("state_version"), a.get("action"),
                                          a.get("action_id"), a.get("reasoning_summary"))
            self._count(out, a, time.monotonic() - t0)
            return out
        if tool == "get_result":
            if not srv.terminal:
                return _err("GAME_NOT_COMPLETE", "still running")
            return srv.result(seat)
        if tool == "resign":
            return srv.resign(seat)
        if tool in ("send_message", "get_messages"):
            return _err("RUNTIME_UNAVAILABLE", "Red Alert has no messaging")
        return _err("UNKNOWN_TOOL", tool)

    def _count(self, out: dict, a: dict, secs: float) -> None:
        if "error" in out:
            code = out["error"]
            if code == "INVALID_ACTION":
                self.st["invalid_actions"] += 1
            elif code in _RACES:
                self.st["race_errors"] += 1
            self.st["tool_errors"] = (self.st["tool_errors"] + [code])[-20:]
            return
        self.st["batches"] += 1
        self.st["decisions"] += 1
        self.st["latency_s"].append(round(secs, 3))
        r = a.get("reasoning_summary")
        if r:
            self.st["reasoning_lines"] += 1
            self.st["reasoning_max_len"] = max(self.st["reasoning_max_len"], len(r))
            self.st["reasoning_over_cap"] += len(r) > 500

    def summary(self) -> dict:
        row = {k: v for k, v in self.st.items() if k != "latency_s"}
        winner = self.server.winner
        row.update(session_id=self.sid, n=self.n, seat=self.seat, opponent=self.opponent, terminal=self.terminal,
                   phase=self.phase, game_type=GAME, map=self.map_id,
                   win=(winner == self.seat) if self.terminal else None, reason=self.reason or self.server.reason,
                   started=time.strftime("%H:%M:%S", time.localtime(self.started)),
                   secs=round((self.ended or time.time()) - self.started, 1), bank_used=0.0,
                   game_seconds=round(max(0, self.world.tick - self.server.start_tick) / TICKS_PER_SECOND, 1),
                   latency_p50=None, latency_max=None, returns=copy.deepcopy(self.returns))
        return row
