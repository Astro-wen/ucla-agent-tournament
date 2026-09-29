"""A local stand-in for the AltruAgent platform, for unattended rehearsals of the REAL `python -m agent` process tree
(scripts/run_agent.sh -> supervisor -> one worker process per match -> node damage calculator) without ever touching
the real platform. Binds 127.0.0.1 only; the API key is a local test value.

    python -m local.fake_platform --port 8765 --concurrent 2 --stats logs/rehearsal/platform.json
    ALTRUAGENT_CONTROL_URL=http://127.0.0.1:8765 ALTRUAGENT_API_KEY=local-rehearsal-key python -m agent

Control plane (REST): POST /auth/agent/login, GET /auth/agent/me, GET /agents/me/sessions, GET /competitions/{id}.
GameAPI (MCP streamable HTTP, stateless, at /mcp): get_game_state, wait_for_update, get_legal_actions, play_action,
get_result, resign. Same payload shapes as the platform (built by local/pokemon_env.Match, see docs/real_payloads.md).

Matches: pokemon_vgc_doubles_draft on the local Showdown engine. Our seat is answered over MCP; the other seat is a
local bot played in-process (--opponent random | baseline | mixed). The match state lives HERE, like on the
platform: an agent that is killed and restarted rediscovers its active matches and resumes them. Platform clocks:
90 s team preview and 55 s per battle request (then Showdown's own default choice, counted as a timer decision),
a 420 s + 90 s grace battle bank (empty = our seat loses), and a 300 s draft limit (the platform has no draft timer;
a dead agent must not hang the rehearsal). Bearer tokens expire after --token-ttl seconds, so the client's
401 -> re-login path runs too.

Stats (--stats, rewritten every few seconds): per match (seat, winner, turns, decisions, answer latencies, timer
decisions, invalid actions, race errors, public-reasoning lines and their longest length) and totals. The slowest
answers carry wall-clock timestamps, so a recovery after `kill -9` shows up as one slow answer at that time.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import random
import secrets
import signal
import threading
import time
import uuid
from pathlib import Path

from altruagent import RESIGN

from .pokemon_env import GAME, TIMER_DEFAULT, Match, OfficialBaseline, Player, RandomPlayer, _Forfeit
from .showdown import Showdown

AGENT_ID = "agent-rehearsal"
AGENT_NAME = "Rehearsal_Local"
DEFAULT_KEY = "local-rehearsal-key"          # a local test value; the fake platform accepts nothing else
TIMERS = {"draft": 300.0, "team_preview": 90.0, "battle": 55.0}
BANK_S = 420.0 + 90.0
LONG_POLL_MAX_S = 25.0
REASONING_CAP = 280
_NONE = object()


def _err(code: str, detail: str) -> dict:
    return {"error": code, "detail": detail}


class RemoteSeat(Player):
    """Our seat: every decision is published as this agent's MCP state and answered by play_action."""
    name = "remote"

    def __init__(self, fm: "FakeMatch"):
        self.fm = fm

    def reset(self):
        pass

    def choose(self, raw: dict):
        return self.fm.ask(raw)


class FakeMatch:
    def __init__(self, platform: "FakePlatform", n: int, opponent: str, seed: int):
        self.platform, self.n, self.seed = platform, n, seed
        self.sid = str(uuid.uuid4())
        self.seat = n % 2
        self.opponent = opponent
        self.cond = threading.Condition()
        self.version = 1
        self.pending: dict | None = None
        self.pending_at = 0.0
        self.answer = _NONE
        self.last_obs: dict = {}
        self.phase = "draft"
        self.terminal = False
        self.returns: dict = {}
        self.reason = None
        self.bank_used = 0.0
        self.started = time.time()
        self.ended = None
        self.st = {"decisions": 0, "latency_s": [], "slow": [], "timer": {"draft": 0, "team_preview": 0, "battle": 0},
                   "invalid_actions": 0, "race_errors": 0, "reasoning_lines": 0, "reasoning_max_len": 0,
                   "reasoning_over_cap": 0, "winner_seat": None, "turns": 0, "match_invalid": 0, "error": None}

    # ---- match thread side -------------------------------------------------------------------------------------
    def ask(self, raw: dict):
        raw = copy.deepcopy(raw)
        phase = raw.get("phase") if raw.get("phase") in ("draft", "team_preview") else "battle"   # battle = "moving"
        with self.cond:
            self.version += 1
            raw["session_id"] = self.sid
            raw["state_version"] = self.version
            la = raw.get("legal_actions") or {}
            la["session_id"], la["state_version"] = self.sid, self.version
            for a in la.get("actions") or []:
                inp = a.get("input")
                if isinstance(inp, dict):
                    inp["session_id"], inp["state_version"] = self.sid, self.version
            raw["current_actor"] = {"agent_id": AGENT_ID, "position": self.seat}
            self.pending, self.pending_at, self.answer, self.phase = raw, time.monotonic(), _NONE, raw.get("phase")
            self.last_obs = raw.get("observation") or {}
            self.cond.notify_all()
            limit = TIMERS.get(phase, 55.0)
            if phase == "battle":
                limit = max(0.0, min(limit, BANK_S - self.bank_used))
            deadline = self.pending_at + limit
            while self.answer is _NONE and not self.platform.stopping.is_set():
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.cond.wait(min(left, 1.0))
            ans, waited = self.answer, time.monotonic() - self.pending_at
            self.pending = None
            self.version += 1
            self.cond.notify_all()
        if phase == "battle":
            self.bank_used += min(waited, limit)
        if ans is _NONE:
            self.st["timer"][phase] = self.st["timer"].get(phase, 0) + 1
            if phase == "battle" and self.bank_used >= BANK_S:
                return RESIGN                     # bank empty: Showdown's "lost due to inactivity"
            return TIMER_DEFAULT if phase in ("team_preview", "battle") else None
        self.st["decisions"] += 1
        self.st["latency_s"].append(round(waited, 3))
        self.st["slow"].append((round(waited, 2), time.strftime("%H:%M:%S"), phase))
        self.st["slow"] = sorted(self.st["slow"], reverse=True)[:3]
        return ans

    def run(self) -> None:
        opp = {"random": RandomPlayer, "baseline": OfficialBaseline}[self.opponent]()
        me = RemoteSeat(self)
        players = [me, opp] if self.seat == 0 else [opp, me]
        rng = random.Random(self.seed)
        winner = None
        try:
            sd = Showdown()
            try:
                m = Match(sd, players, rng)
                try:
                    m.draft()
                    r = m.battle(m.preview())
                except _Forfeit as f:
                    r = m.forfeited(f.seat)
                winner, self.st["turns"] = r.winner, r.turns
                self.st["match_invalid"] = r.invalid[self.seat] - self.st["timer"]["draft"]
                self.reason = "forfeit" if r.forfeit is not None else "game_over"
            finally:
                sd.close()
                try:
                    sd.proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001 - a simulator failure ends this match, never the platform
            self.st["error"] = repr(e)[:300]
            self.reason = "simulator_error"
        with self.cond:
            self.st["winner_seat"] = winner
            ours = 0.0 if winner is None else (1.0 if winner == self.seat else -1.0)
            self.returns = {AGENT_ID: ours, "agent-opponent": -ours}
            self.terminal = True
            self.ended = time.time()
            self.version += 1
            self.cond.notify_all()
        self.platform.match_done(self)

    # ---- MCP side ----------------------------------------------------------------------------------------------
    def view(self) -> dict:
        with self.cond:
            if self.pending is not None:
                out = copy.deepcopy(self.pending)
                out["is_current_actor"] = True
                return out
            return {"session_id": self.sid, "game_type": GAME, "runtime_adapter": "pokemon",
                    "status": "completed" if self.terminal else "in_progress", "state_version": self.version,
                    "phase": "finished" if self.terminal else self.phase, "observation": copy.deepcopy(self.last_obs),
                    "is_terminal": self.terminal, "is_current_actor": False,
                    "current_actor": {"agent_id": "agent-opponent", "position": 1 - self.seat}}

    def submit(self, state_version, action_id, action, reasoning) -> dict | None:
        with self.cond:
            if self.terminal:
                self.st["race_errors"] += 1
                return _err("GAME_ALREADY_COMPLETE", "the match is over")
            if self.pending is None:
                self.st["race_errors"] += 1
                return _err("NOT_YOUR_TURN", "not this agent's turn")
            if state_version != self.version:
                self.st["race_errors"] += 1
                return _err("STALE_STATE", f"state_version {state_version} != {self.version}")
            if action is not None:
                out = action
            else:
                la = next((a for a in (self.pending.get("legal_actions") or {}).get("actions") or []
                           if a.get("action_id") == action_id), None)
                if la is None:
                    self.st["invalid_actions"] += 1
                    return _err("INVALID_ACTION", f"unknown action_id {action_id!r}")
                out = (la.get("input") or {}).get("action") or action_id
            if reasoning:
                self.st["reasoning_lines"] += 1
                self.st["reasoning_max_len"] = max(self.st["reasoning_max_len"], len(reasoning))
                self.st["reasoning_over_cap"] += len(reasoning) > REASONING_CAP
            self.answer = out
            self.cond.notify_all()
            return None

    def summary(self) -> dict:
        lat = sorted(self.st["latency_s"])
        row = {k: v for k, v in self.st.items() if k != "latency_s"}
        row.update(session_id=self.sid, n=self.n, seat=self.seat, opponent=self.opponent, terminal=self.terminal,
                   phase=self.phase,
                   win=(self.st["winner_seat"] == self.seat) if self.terminal else None, reason=self.reason,
                   started=time.strftime("%H:%M:%S", time.localtime(self.started)),
                   secs=round((self.ended or time.time()) - self.started, 1), bank_used=round(self.bank_used, 1),
                   latency_p50=lat[len(lat) // 2] if lat else None, latency_max=lat[-1] if lat else None)
        return row


class FakePlatform:
    def __init__(self, *, concurrent: int, opponent: str, max_matches: int | None, api_key: str, token_ttl: float,
                 seed: int, stats: Path | None, game_server: str):
        self.concurrent, self.opponent, self.max_matches = concurrent, opponent, max_matches
        self.api_key, self.token_ttl, self.seed, self.stats_path = api_key, token_ttl, seed, stats
        self.game_server = game_server
        self.gap = 0.0
        self.matches: dict[str, FakeMatch] = {}
        self.order: list[FakeMatch] = []
        self.lock = threading.Lock()
        self.stats_lock = threading.Lock()
        self.stopping = threading.Event()
        self.tokens: dict[str, float] = {}
        self.counters = {"logins": 0, "unauthorized": 0, "sessions_calls": 0, "mcp_calls": 0, "outage_503": 0}
        self.t0 = time.time()
        self.outage_file: Path | None = None
        self._outage = (0.0, None)

    def outage(self) -> str | None:
        """'sessions' / 'all' while the --outage-file exists (its first word), else None: a rehearsed network blip
        (503 on the discovery poll only, or on every request)."""
        if self.outage_file is None:
            return None
        now = time.monotonic()
        if now - self._outage[0] > 0.5:
            try:
                word = (self.outage_file.read_text().split() or ["all"])[0]
            except OSError:
                word = None
            self._outage = (now, word)
        return self._outage[1]

    # ---- auth --------------------------------------------------------------------------------------------------
    def login(self, key: str) -> str | None:
        if not key or not secrets.compare_digest(str(key), self.api_key):
            return None
        tok = secrets.token_hex(16)
        with self.lock:
            self.counters["logins"] += 1
            now = time.time()
            self.tokens = {t: exp for t, exp in self.tokens.items() if exp > now}
            self.tokens[tok] = now + self.token_ttl
        return tok

    def token_ok(self, header: str) -> bool:
        tok = header[7:] if header.startswith("Bearer ") else ""
        with self.lock:
            ok = self.tokens.get(tok, 0) > time.time()
            if not ok:
                self.counters["unauthorized"] += 1
            return ok

    # ---- matches -----------------------------------------------------------------------------------------------
    def _new_match(self) -> None:
        n = len(self.order)
        opp = self.opponent if self.opponent != "mixed" else ("random", "baseline")[n % 2]
        fm = FakeMatch(self, n, opp, self.seed * 100000 + n)
        with self.lock:
            self.matches[fm.sid] = fm
            self.order.append(fm)
        threading.Thread(target=fm.run, name=f"match-{n}", daemon=True).start()

    def match_done(self, fm: FakeMatch) -> None:
        self.write_stats()

    def scheduler(self) -> None:
        last = 0.0
        while not self.stopping.is_set():
            try:
                active = [m for m in self.order if not m.terminal]
                ended = [m.ended for m in self.order if m.ended]
                idle = not ended or time.time() - max(ended) >= self.gap
                if len(active) < self.concurrent and idle and \
                        (self.max_matches is None or len(self.order) < self.max_matches):
                    self._new_match()
                    continue
                if time.time() - last > 10:
                    self.write_stats()
                    last = time.time()
            except Exception as e:  # noqa: BLE001 - the scheduler must outlive any single failure
                print(f"scheduler error: {e!r}", flush=True)
            self.stopping.wait(0.5)

    def sessions(self) -> dict:
        with self.lock:
            self.counters["sessions_calls"] += 1
            active = [m for m in self.order if not m.terminal]
            done = [m for m in self.order if m.terminal][-20:]

        def row(m, status):
            return {"session_id": m.sid, "status": status, "game_type": GAME, "tournament_id": "rehearsal",
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(m.started))}
        return {"joined_sessions": [], "active_sessions": [row(m, "in_progress") for m in active],
                "completed_sessions": [row(m, "completed") for m in done]}

    def write_stats(self) -> None:
        if self.stats_path is None:
            return
        with self.stats_lock:                  # (match threads and the scheduler both write it)
            try:
                self._write_stats()
            except Exception as e:  # noqa: BLE001 - stats must never take the platform down
                print(f"stats write failed: {e!r}", flush=True)

    def _write_stats(self) -> None:
        with self.lock:
            rows = [m.summary() for m in self.order]
            counters = dict(self.counters)
        done = [r for r in rows if r["terminal"]]
        lat = sorted(x for m in self.order for x in m.st["latency_s"])
        tot = {"elapsed_s": round(time.time() - self.t0), "matches_started": len(rows), "matches_done": len(done),
               "wins": sum(1 for r in done if r["win"]), "decisions": sum(r["decisions"] for r in rows),
               "timer_decisions": sum(sum(r["timer"].values()) for r in rows),
               "invalid_actions": sum(r["invalid_actions"] for r in rows),
               "match_invalid": sum(r["match_invalid"] for r in done),
               "race_errors": sum(r["race_errors"] for r in rows),
               "forfeits": sum(1 for r in done if r["reason"] == "forfeit"),
               "simulator_errors": sum(1 for r in done if r["reason"] == "simulator_error"),
               "reasoning_lines": sum(r["reasoning_lines"] for r in rows),
               "reasoning_over_cap": sum(r["reasoning_over_cap"] for r in rows),
               "latency_p50": lat[len(lat) // 2] if lat else None,
               "latency_p99": lat[min(len(lat) - 1, int(0.99 * len(lat)))] if lat else None,
               "latency_max": lat[-1] if lat else None,
               "slowest": sorted((s for m in self.order for s in m.st["slow"]), reverse=True)[:8], **counters}
        tmp = self.stats_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"totals": tot, "matches": rows}, indent=1))
        os.replace(tmp, self.stats_path)


def build_app(p: FakePlatform, port: int):
    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    mcp = FastMCP("fake-gameapi", host="127.0.0.1", port=port, stateless_http=True, json_response=True,
                  log_level="WARNING")

    def match(session_id: str) -> FakeMatch | None:
        with p.lock:
            p.counters["mcp_calls"] += 1
            return p.matches.get(session_id)

    @mcp.custom_route("/auth/agent/login", methods=["POST"])
    async def login(request: Request):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        tok = p.login((body or {}).get("api_key"))
        if tok is None:
            return JSONResponse({"error": "invalid_api_key", "detail": "unknown API key"}, status_code=401)
        return JSONResponse({"access_token": tok, "token_type": "bearer", "expires_in": int(p.token_ttl)})

    @mcp.custom_route("/auth/agent/me", methods=["GET"])
    async def me(request: Request):
        return JSONResponse({"id": AGENT_ID, "name": AGENT_NAME, "status": "claimed"})

    @mcp.custom_route("/agents/me/sessions", methods=["GET"])
    async def sessions(request: Request):
        return JSONResponse(p.sessions())

    @mcp.custom_route("/competitions/{session_id}", methods=["GET"])
    async def competition(request: Request):
        sid = request.path_params["session_id"]
        if match(sid) is None:
            return JSONResponse({"error": "not_found", "detail": "no such competition"}, status_code=404)
        return JSONResponse({"session_id": sid, "status": "in_progress", "game_server_url": p.game_server})

    @mcp.tool()
    async def get_game_state(session_id: str) -> dict:
        fm = match(session_id)
        return fm.view() if fm else _err("SESSION_NOT_FOUND", session_id)

    @mcp.tool()
    async def get_legal_actions(session_id: str) -> dict:
        fm = match(session_id)
        if fm is None:
            return _err("SESSION_NOT_FOUND", session_id)
        v = fm.view()
        return {"session_id": session_id, "state_version": v["state_version"],
                "actions": (v.get("legal_actions") or {}).get("actions") or []}

    @mcp.tool()
    async def wait_for_update(session_id: str, since_version: int, since_message_seq: int | None = None,
                              since_is_current_actor: bool | None = None, since_phase: str | None = None,
                              timeout_seconds: float | None = None) -> dict:
        fm = match(session_id)
        if fm is None:
            return _err("SESSION_NOT_FOUND", session_id)
        end = time.monotonic() + min(LONG_POLL_MAX_S, float(timeout_seconds or 20.0))
        while time.monotonic() < end and not p.stopping.is_set():
            if fm.version != since_version or fm.terminal or \
                    (since_is_current_actor is not None and (fm.pending is not None) != since_is_current_actor):
                break
            await asyncio.sleep(0.05)
        out = fm.view()
        out["updated"] = out["state_version"] != since_version
        return out

    @mcp.tool()
    async def play_action(session_id: str, state_version: int, action_id: str | None = None,
                          action: dict | None = None, reasoning_summary: str | None = None) -> dict:
        fm = match(session_id)
        if fm is None:
            return _err("SESSION_NOT_FOUND", session_id)
        err = fm.submit(state_version, action_id, action, reasoning_summary)
        if err is not None:
            return err
        end = time.monotonic() + 0.5                  # let the match thread take it (the platform answers post-move)
        while time.monotonic() < end and fm.version == state_version:
            await asyncio.sleep(0.01)
        v = fm.view()
        out = {"accepted": True, "session_id": session_id, "state_version": v["state_version"], "status": v["status"]}
        if not v["is_terminal"]:
            out["state"] = v
        return out

    @mcp.tool()
    async def get_result(session_id: str) -> dict:
        fm = match(session_id)
        if fm is None:
            return _err("SESSION_NOT_FOUND", session_id)
        if not fm.terminal:
            return _err("GAME_NOT_COMPLETE", "still running")
        return {"session_id": session_id, "is_terminal": True, "status": "completed", "returns": fm.returns,
                "termination_reason": fm.reason}

    @mcp.tool()
    async def resign(session_id: str) -> dict:
        fm = match(session_id)
        if fm is None:
            return _err("SESSION_NOT_FOUND", session_id)
        with fm.cond:
            if fm.pending is not None and fm.answer is _NONE:
                fm.answer = RESIGN
                fm.cond.notify_all()
        end = time.monotonic() + 5
        while time.monotonic() < end and not fm.terminal:
            await asyncio.sleep(0.05)
        return {"session_id": session_id, "is_terminal": fm.terminal, "status": "completed", "returns": fm.returns,
                "termination_reason": "resign"}

    app = mcp.streamable_http_app()

    class Auth:
        """Bearer check on everything but login (the platform's JWT; expired -> 401 -> the client logs in again)."""

        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            outage = p.outage() if scope["type"] == "http" else None
            if outage == "all" or (outage == "sessions" and scope["path"] == "/agents/me/sessions"):
                with p.lock:
                    p.counters["outage_503"] += 1
                await JSONResponse({"error": "unavailable", "detail": "rehearsal outage"},
                                   status_code=503)(scope, receive, send)
                return
            if scope["type"] == "http" and scope["path"] not in ("/auth/agent/login", "/healthz"):
                hdr = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
                if not p.token_ok(hdr):
                    await JSONResponse({"error": "unauthorized", "detail": "missing or expired token"},
                                       status_code=401)(scope, receive, send)
                    return
            await self.inner(scope, receive, send)

    return Auth(app)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--concurrent", type=int, default=2, help="matches in progress at once (default 2)")
    ap.add_argument("--opponent", choices=("random", "baseline", "mixed"), default="mixed")
    ap.add_argument("--max-matches", type=int, default=None, help="stop creating matches after this many")
    ap.add_argument("--api-key", default=os.environ.get("FAKE_PLATFORM_KEY", DEFAULT_KEY))
    ap.add_argument("--token-ttl", type=float, default=600.0, help="seconds a bearer token lives (default 600)")
    ap.add_argument("--seed", type=int, default=5)
    ap.add_argument("--stats", type=Path, default=None, help="JSON stats file, rewritten every ~10 s")
    ap.add_argument("--gap", type=float, default=0.0,
                    help="idle seconds after a match ends before the next one starts (default 0)")
    ap.add_argument("--outage-file", type=Path, default=None,
                    help="while this file exists: 503 on the discovery poll ('sessions') or on everything ('all')")
    a = ap.parse_args(argv)
    import uvicorn

    os.environ["ARENA_RECORD"] = "0"
    p = FakePlatform(concurrent=a.concurrent, opponent=a.opponent, max_matches=a.max_matches, api_key=a.api_key,
                     token_ttl=a.token_ttl, seed=a.seed, stats=a.stats, game_server=f"127.0.0.1:{a.port}")
    p.outage_file, p.gap = a.outage_file, a.gap
    threading.Thread(target=p.scheduler, name="scheduler", daemon=True).start()
    server = uvicorn.Server(uvicorn.Config(build_app(p, a.port), host="127.0.0.1", port=a.port, log_level="warning",
                                           access_log=False))

    def stop(*_):
        p.stopping.set()
        server.should_exit = True
    signal.signal(signal.SIGTERM, stop)
    print(f"fake platform on http://127.0.0.1:{a.port} ({a.concurrent} concurrent, opponent {a.opponent})", flush=True)
    try:
        server.run()
    finally:
        p.stopping.set()
        p.write_stats()


if __name__ == "__main__":
    main()
