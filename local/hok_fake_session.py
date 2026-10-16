"""Offline HoK contract peer for tests of the *official* runner.

This is not a combat simulator. Frames follow upstream/main:tests/test_hok.py
and GAMES.md; assertions check the server's advertised actions, not our policy.
No transport, credentials, model provider, or production-policy import is used.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re

from altruagent import DecisionContext, GameState
from altruagent.mcp_transport import MCPToolError

GAME = "honor_of_kings_5v5"
SESSION = "offline-hok-contract"
SEATS = tuple(range(5))
CONTEXT = DecisionContext(session_id=SESSION, tournament_id=None, game_type=GAME, agent_id="offline-agent")


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


def hero_summary(seat, *, camp=1, hp=3000):
    return {
        "my_hero": {"hero_id": 100 + seat, "camp": camp, "hp": hp, "max_hp": 3000,
                    "level": 2, "x": -30000 + seat * 1000, "z": -30000},
        "other_heroes": [
            {"runtime_id": 7, "camp": 3 - camp, "visible": True, "hp": 2000,
             "max_hp": 3000, "x": 20000, "z": 20000},
            {"runtime_id": 8, "camp": 3 - camp, "visible": False},
        ],
        "structures": [
            {"camp": 3 - camp, "type": "ACTOR_TOWER", "hp": 6000, "x": 10000, "z": 10000},
            {"camp": camp, "type": "ACTOR_CRYSTAL", "hp": 8000, "x": -25000, "z": -25000},
        ],
    }


def entries(seat, macros=("macro_advance",), *, distance=12000, idle=True):
    out = ([{"action_id": f"s{seat}:button_0", "label": f"[seat {seat}] None",
             "input": {"action": {"which_button": 0, "seat": seat}}}] if idle else [])
    for macro in macros:
        out.append({
            "action_id": f"s{seat}:{macro}",
            "label": f"[seat {seat}] {macro} (nearest: enemy hero 169, {distance} away)",
            "input": {"action": {"which_button": 3, "seat": seat}},
        })
    return out


def payload(version=0, *, seats=SEATS, pending=None, standing=None, mode="realtime", actions=None):
    pending = list(seats if pending is None else pending)
    camp = 1 if min(seats, default=0) < 5 else 2
    return {
        "session_id": SESSION, "game_type": GAME, "status": "in_progress",
        "phase": "moving", "is_current_actor": bool(pending), "is_terminal": False,
        "state_version": version, "pacing": {"mode": mode, "step_interval_s": 0.2},
        "seats": list(seats), "camp": camp, "max_steps": 3000, "seconds_left": 600 - version * 0.2,
        "observation": {"seats": {str(s): {"summary": hero_summary(s, camp=camp)} for s in seats}},
        "simultaneous": {"your_pending_seats": pending, "standing_orders": dict(standing or {})},
        "legal_actions": {"actions": actions if actions is not None else [a for s in pending for a in entries(s)]},
    }


def assert_legal_submission(raw, *, action_id=None, action=None):
    """Independently validate IDs *and* structured actions (the SDK only checks IDs).

    In particular, a legal-looking ID for a non-pending/non-owned hero is not a
    safe fallback. Raw buttons must fill each legal_* field with an offered value.
    Returns (seat, macro-or-button) for invariant checks independent of encoding.
    """
    assert (action_id is None) != (action is None), "send exactly one action encoding"
    offered = raw["legal_actions"]["actions"]
    if action_id is not None:
        match = re.fullmatch(r"s(\d+):(.+)", action_id)
        assert match, f"not a seat-tagged action: {action_id!r}"
        seat, command = int(match[1]), match[2]
        entry = next((a for a in offered if a.get("action_id") == action_id), None)
        assert entry is not None, f"not currently offered: {action_id}"
        template = (entry.get("input") or {}).get("action") or {}
        assert not any(k.startswith("legal_") for k in template), "raw template sent unfilled"
    else:
        assert isinstance(action, dict)
        json.dumps(action, allow_nan=False)
        seat = action.get("seat")
        assert type(seat) is int, f"seat must be an integer, not {seat!r}"
        if "macro" in action:
            assert set(action) == {"seat", "macro"}, f"unexpected macro fields: {action}"
            command = action["macro"]
            assert any(a.get("action_id") == f"s{seat}:{command}" for a in offered)
        else:
            assert type(action.get("which_button")) is int
            command = f"button_{action['which_button']}"
            candidates = [a for a in offered if a.get("action_id") == f"s{seat}:{command}"]
            assert candidates, f"button not offered: {action}"
            template = candidates[0]["input"]["action"]
            fields = {k.removeprefix("legal_") for k in template}
            assert set(action) == fields, f"unfilled or extra fields: {action}, expected {fields}"
            for key, value in template.items():
                if key.startswith("legal_"):
                    assert action[key[6:]] in value, f"value not offered for {key}: {action}"
                else:
                    assert action[key] == value
    owned = raw.get("seats")
    if isinstance(owned, list):
        assert seat in owned, f"acted for unowned seat {seat}: owned={owned}"
    simultaneous = raw.get("simultaneous")
    pending = simultaneous.get("your_pending_seats") if isinstance(simultaneous, dict) else None
    if isinstance(pending, list):
        valid_pending = [s for s in pending if type(s) is int]
        assert seat in valid_pending, f"acted for non-pending seat {seat}: pending={pending}"
    return seat, command


class HokFakeSession:
    """Finite scripted views, accepted-order feedback, and optional server races.

    Each read/wait consumes a frame; exhaustion produces a terminal state.
    Generators may use ``standing`` and ``accepted`` to build the next frame.
    ``reject`` receives the normalized attempted call and returns an MCP error
    code (or None). Failed calls never enter the authoritative standing orders.
    """
    session_id = SESSION
    game_server_url = "http://offline.invalid"

    def __init__(self, frames, *, clock=None, wall_seconds_per_view=0.2, reject=None):
        self.frames = iter(frames)
        self.clock = clock or Clock()
        self.wall_seconds_per_view = wall_seconds_per_view
        self.reject = reject
        self.views = 0
        self.waits = 0
        self.calls = []
        self.accepted = []
        self.standing = {}
        self.current = None

    def get_state(self):
        self.clock.now += self.wall_seconds_per_view
        raw = next(self.frames, None)
        if raw is None:
            raw = payload(self.views, pending=[])
            raw.update(is_terminal=True, status="completed")
        else:
            self.views += 1
        self.current = deepcopy(raw)
        return GameState.from_mcp_state(deepcopy(raw))

    def wait_for_update(self, **kwargs):
        self.waits += 1
        return self.get_state()

    def get_game_config(self, game_type):
        return {}

    def get_legal_actions(self):
        raise AssertionError("frames must embed their legal actions")

    def play_action(self, *, action_id=None, action=None, state_version, reasoning_summary=None):
        assert state_version == self.current["state_version"], "sent a stale state's version"
        seat, command = assert_legal_submission(self.current, action_id=action_id, action=action)
        if reasoning_summary is not None:
            assert isinstance(reasoning_summary, str) and len(reasoning_summary) <= 1024
        call = {"seat": seat, "command": command, "version": state_version, "view": self.views,
                "action_id": action_id, "action": deepcopy(action)}
        self.calls.append(call)
        code = self.reject(call) if self.reject else None
        if code:
            raise MCPToolError(f"scripted {code}", error_code=code, status_code=409)
        self.accepted.append(call)
        if command.startswith("macro_"):
            self.standing[str(seat)] = command
        return {"session_id": SESSION, "status": "in_progress", "state_version": state_version,
                "accepted": True, "stepped": False}

    def get_result(self):
        return {"session_id": SESSION, "is_terminal": True, "status": "completed",
                "returns": {"us": 1.0, "them": 0.0}, "termination_reason": "completed"}

    def resign(self):
        raise AssertionError("HoK must never resign")

    def send_message(self, **kwargs):
        raise AssertionError("HoK has no messaging")
