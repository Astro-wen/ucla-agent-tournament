"""Loopback-only official assignment + HoK MCP peer for subprocess tests.

Protocol shapes follow local/fake_official.py and the public HoK guide. This
module never starts a live deployment, loads .env, or reads a real key. Imports
stay stdlib-only until build_app(): sitecustomize can install the test boundary
before importing the contestant, SDK, or HTTP clients.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import threading
import time

FAKE_KEY = "eak_live_" + "0" * 64
AGENT_ID = "offline-hok-process-agent"
GAME_TYPE = "honor_of_kings_5v5"


def install_process_boundary():
    """Child-only audit hook: allow one loopback port, forbid dotenv reads.

    Every supervisor, spawn worker, and multiprocessing resource tracker loads
    this through a temporary sitecustomize. Audit events give tests the exact
    owned PIDs; cleanup never searches for or signals unrelated agents.
    """
    port = int(os.environ["HOK_TEST_PORT"])
    audit_path = Path(os.environ["HOK_TEST_AUDIT"])
    fd = os.open(audit_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)

    def note(event, **fields):
        os.write(fd, (json.dumps({"event": event, "pid": os.getpid(), **fields}) + "\n").encode())

    note("start", ppid=os.getppid(), pgid=os.getpgrp(), env_keys=sorted(os.environ))

    def audit(event, args):
        if event in ("socket.connect", "socket.getaddrinfo"):
            address = args[1] if event == "socket.connect" else args[:2]
            # AF_UNIX is used by some multiprocessing implementations, not HTTP.
            if not isinstance(address, tuple) or len(address) < 2:
                return
            host, target_port = address[:2]
            if isinstance(host, bytes):
                host = host.decode("ascii", errors="replace")
            if host not in ("127.0.0.1", "::1", "localhost") or str(target_port) != str(port):
                note("denied-network", host=str(host), port=str(target_port))
                raise PermissionError("HoK subprocess test forbids non-peer network access")
            if event == "socket.connect":
                note("connect", host=host, port=target_port)
        elif event == "open" and isinstance(args[0], (str, bytes)):
            name = os.fsdecode(args[0])
            parts = Path(name).parts
            if Path(name).name.startswith(".env") or any(p in (".ssh", ".aws", ".azure") for p in parts):
                note("denied-file", path=name)
                raise PermissionError("HoK subprocess test forbids credential-file reads")

    sys.addaudithook(audit)


class FakeHokMatch:
    """Small finite protocol peer, not a combat or strength simulator."""

    def __init__(self, mode, *, context, steps, camp=1, reject_first=False, reconnect=False):
        self.mode, self.context, self.steps = mode, context, steps
        self.seats = tuple(range(0, 5)) if camp == 1 else tuple(range(5, 10))
        self.sid = f"hok-{mode}-process"
        self.seat_id = f"seat-{mode}"
        self.version = 0
        self.pending = set(self.seats)
        self.standing = {}
        self.accepted = []
        self.attempts = []
        self.invalid = []
        self.reject_first = reject_first
        self.injected_refusals = 0
        self.reconnect = reconnect
        self.injected_401 = 0
        self.grants = 0
        self.results = 0
        self.connected_s = None
        self.created = time.monotonic()
        self.seen_versions = set()
        self.last_view = None

    @property
    def terminal(self):
        return self.version >= self.steps

    def view(self, *, advance=False):
        from .hok_fake_session import payload

        if self.connected_s is None:
            self.connected_s = time.monotonic() - self.created
        if advance and self.mode == "realtime" and self.last_view is not None:
            self.version = min(self.steps, self.version + 1)
            self.pending = set(self.seats)
        raw = payload(self.version, mode=self.mode, seats=self.seats,
                      pending=sorted(self.pending), standing=self.standing)
        raw.update(session_id=self.sid, max_steps=self.steps, time_limit_s=600)
        if self.terminal:
            raw.update(is_terminal=True, is_current_actor=False, status="completed", phase="finished")
            raw["legal_actions"]["actions"] = []
        self.seen_versions.add(self.version)
        self.last_view = raw
        return raw

    def play(self, state_version, action_id, action, reasoning_summary):
        from .hok_fake_session import assert_legal_submission

        if state_version not in self.seen_versions or (self.mode == "lockstep" and state_version != self.version):
            return {"error": "STALE_STATE", "detail": "read the current version"}
        try:
            seat, command = assert_legal_submission(self.last_view, action_id=action_id, action=action)
        except (AssertionError, ValueError, TypeError) as exc:
            self.invalid.append(str(exc))
            return {"error": "INVALID_ACTION", "detail": str(exc)}
        row = {"seat": seat, "command": command, "version": self.version,
               "reasoning_summary": reasoning_summary}
        self.attempts.append(row)
        if self.reject_first and not self.injected_refusals:
            self.injected_refusals += 1
            return {"error": "INVALID_ACTION", "detail": "injected target disappearance after a valid read"}
        self.accepted.append(row)
        if command.startswith("macro_"):
            self.standing[str(seat)] = command
        else:
            self.standing.pop(str(seat), None)
        self.pending.discard(seat)
        if self.mode == "lockstep" and not self.pending:
            self.version += 1
            self.pending = set(self.seats)
        return {"session_id": self.sid, "accepted": True, "state_version": self.version,
                "status": "completed" if self.terminal else "in_progress"}


class FakeHokOfficial:
    """One synthetic agent, at most two assignments, one loopback origin."""

    def __init__(self, matches):
        assert 1 <= len(matches) <= 2
        self.matches = {m.sid: m for m in matches}
        self.by_seat = {m.seat_id: m for m in matches}
        self.tokens = {}
        self.holders = {}
        self.counters = Counter()
        self.lock = threading.RLock()
        self.base_url = None
        self.deadline = datetime.now(timezone.utc) + timedelta(seconds=45)

    def assignment(self, match):
        row = {"match_id": match.sid, "seat_id": match.seat_id, "game_type": GAME_TYPE,
               "seat_position": 0, "seat_count": 2, "match_status": "in_progress",
               "seat_status": "running" if match.grants else "pending", "context": match.context,
               "opponents": [{"name": "offline-peer"}]}
        if match.context == "tournament":
            row.update(tournament_id="offline-hok-cup", tournament_name="Offline HoK",
                       connect_deadline_at=self.deadline.isoformat())
        return row

    def build_app(self, port):
        from mcp.server.fastmcp import FastMCP
        from starlette.requests import Request
        from starlette.responses import JSONResponse

        self.base_url = f"http://127.0.0.1:{port}"
        mcp = FastMCP("offline-hok", host="127.0.0.1", port=port, stateless_http=True,
                      json_response=True, log_level="ERROR")

        @mcp.custom_route("/tournament/agent/authenticate", methods=["POST"])
        async def authenticate(request: Request):
            body = await request.json()
            with self.lock:
                self.counters["authenticate"] += 1
                if body.get("official_agent_key") != FAKE_KEY:
                    self.counters["bad_auth"] += 1
                    return JSONResponse({"error": "invalid_official_agent_key"}, status_code=401)
            return JSONResponse({"access_token": "offline-agent-session"})

        @mcp.custom_route("/tournament/agent/assignments", methods=["GET"])
        async def assignments(request: Request):
            with self.lock:
                self.counters["assignments"] += 1
                return JSONResponse({"assignments": [self.assignment(m) for m in self.matches.values()
                                                      if not m.terminal]})

        @mcp.custom_route("/tournament/agent/assignments/{seat_id}/grant", methods=["POST"])
        async def grant(request: Request):
            body = await request.json()
            with self.lock:
                match = self.by_seat[request.path_params["seat_id"]]
                execution = body.get("execution_id")
                if not execution:
                    return JSONResponse({"error": "assignment_not_grantable"}, status_code=409)
                if match.seat_id in self.holders and self.holders[match.seat_id] != execution:
                    self.counters["seat_busy"] += 1
                    return JSONResponse({"error": "seat_busy"}, status_code=409)
                self.holders[match.seat_id] = execution
                match.grants += 1
                token = f"offline-seat-{match.mode}-{match.grants}"
                self.tokens[token] = match
                return JSONResponse({**self.assignment(match), "access_token": token, "agent_id": AGENT_ID,
                                     "game_session_id": match.sid, "gameapi_server_url": self.base_url})

        @mcp.custom_route("/tournament/agent/assignments/{seat_id}/lease/renew", methods=["POST"])
        async def renew(request: Request):
            body = await request.json()
            seat = request.path_params["seat_id"]
            with self.lock:
                if body.get("execution_id") != self.holders.get(seat):
                    return JSONResponse({"error": "lease_not_held"}, status_code=409)
                self.counters["renewals"] += 1
            return JSONResponse({"seat_id": seat})

        @mcp.tool()
        async def get_game_state(session_id: str) -> dict:
            with self.lock:
                self.counters["get_game_state"] += 1
                return self.matches[session_id].view(advance=True)

        @mcp.tool()
        async def wait_for_update(session_id: str, since_version: int, since_message_seq: int | None = None,
                                  since_is_current_actor: bool | None = None, since_phase: str | None = None,
                                  timeout_seconds: float | None = None) -> dict:
            import asyncio
            await asyncio.sleep(0.005)
            with self.lock:
                self.counters["wait_for_update"] += 1
                return self.matches[session_id].view(advance=True)

        @mcp.tool()
        async def get_legal_actions(session_id: str) -> dict:
            with self.lock:
                match = self.matches[session_id]
                return {"state_version": match.version, **match.view()["legal_actions"]}

        @mcp.tool()
        async def get_game_config(game_type: str) -> dict:
            return {"game_type": game_type}

        @mcp.tool()
        async def play_action(session_id: str, state_version: int, action_id: str | None = None,
                              action: dict | None = None, reasoning_summary: str | None = None) -> dict:
            with self.lock:
                return self.matches[session_id].play(state_version, action_id, action, reasoning_summary)

        @mcp.tool()
        async def get_result(session_id: str) -> dict:
            with self.lock:
                match = self.matches[session_id]
                if not match.terminal:
                    return {"error": "GAME_NOT_COMPLETE"}
                match.results += 1
                return {"session_id": session_id, "is_terminal": True, "status": "completed",
                        "termination_reason": "step_limit", "returns": {AGENT_ID: 1.0}, "your_return": 1.0}

        app = mcp.streamable_http_app()
        peer = self

        class AuthBoundary:
            async def __call__(self, scope, receive, send):
                if scope["type"] == "http":
                    path = scope["path"]
                    rpc = {}
                    if path.startswith("/mcp") and scope["method"] == "POST":
                        # Inject expiry before a *read* is dispatched. Expiring
                        # an unrelated streaming/cleanup request after a move
                        # landed makes delivery ambiguous, not an exactly-once
                        # policy test.
                        messages, chunks = [], []
                        while True:
                            message = await receive()
                            messages.append(message)
                            chunks.append(message.get("body", b""))
                            if not message.get("more_body"):
                                break
                        try:
                            rpc = json.loads(b"".join(chunks))
                        except (ValueError, TypeError):
                            rpc = {}
                        original_receive = receive

                        async def replay_receive():
                            return messages.pop(0) if messages else await original_receive()

                        receive = replay_receive
                    headers = dict(scope.get("headers") or [])
                    token = headers.get(b"authorization", b"").decode().removeprefix("Bearer ")
                    if path == "/tournament/agent/authenticate":
                        allowed = True
                    elif path.startswith("/tournament/agent/"):
                        allowed = token == "offline-agent-session"
                    else:
                        with peer.lock:
                            match = peer.tokens.get(token)
                            allowed = match is not None
                            is_read = (isinstance(rpc, dict) and rpc.get("method") == "tools/call"
                                       and rpc.get("params", {}).get("name") == "get_game_state")
                            if match and match.reconnect and is_read and len(match.accepted) >= 2 \
                                    and not match.injected_401:
                                match.injected_401 += 1
                                del peer.tokens[token]
                                allowed = False
                    if not allowed:
                        await JSONResponse({"error": "token_expired"}, status_code=401)(scope, receive, send)
                        return
                await app(scope, receive, send)

        return AuthBoundary()


def isolated_environment(root, temporary, port, *, record=False):
    """Explicit allowlist; never copy or inspect the invoking shell's secrets."""
    temporary = Path(temporary)
    for name in ("home", "config", "cache", "state", "logs", "tmp", "bootstrap"):
        (temporary / name).mkdir()
    (temporary / "bootstrap" / "sitecustomize.py").write_text(
        "from local.hok_fake_official import install_process_boundary\ninstall_process_boundary()\n")
    return {
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), "/usr/bin", "/bin")),
        "HOME": str(temporary / "home"), "USER": "offline-hok", "LOGNAME": "offline-hok",
        "XDG_CONFIG_HOME": str(temporary / "config"), "XDG_CACHE_HOME": str(temporary / "cache"),
        "TMPDIR": str(temporary / "tmp"), "PYTHONPATH": os.pathsep.join((str(temporary / "bootstrap"), str(root))),
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
        "PYTHONUTF8": "1", "PYTHON_DOTENV_DISABLED": "1",
        "ALTRUAGENT_OFFICIAL_AGENT_KEY": FAKE_KEY, "ALTRUAGENT_CONTROL_URL": f"http://127.0.0.1:{port}",
        "HOK_TEST_PORT": str(port), "HOK_TEST_AUDIT": str(temporary / "process-boundary.jsonl"),
        "ARENA_RUN_ID": "offline-hok-contract",  # omit unrelated Pokemon/Node preflight, not the actual runtime
        "ARENA_HOK": "on", "ARENA_BATTLE": "classic", "ARENA_TC_WATCHDOG": "0",
        "ARENA_RECORD": "1" if record else "0", "ARENA_RECORD_DIR": str(temporary / "logs"),
        "ARENA_STATE": "0", "ARENA_STATE_DIR": str(temporary / "state"), "ARENA_SUMMARY": "1",
        "ARENA_REASONING": "safe", "ARENA_DISCOVERY_S": "2", "ARENA_WORKER_COOLDOWN_S": "1",
    }
