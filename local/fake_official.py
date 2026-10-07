"""The OFFICIAL (event) side of local/fake_platform.py (`--official`), for dry runs of `python -m agent --tournament
--match` with a FAKE Official Agent Key, and its 7-seat Werewolf tables (`--werewolf N`). Never the real platform.

Control plane, as altruagent/official.py speaks it (docs/STARTER_README.md "Official tournament"):
  POST /tournament/agent/authenticate {official_agent_key} -> {access_token, expires_at}: an agent session that lives
       --session-ttl seconds (then 401, and the client authenticates again); invalid_official_agent_key (401) for any
       other key.
  GET  /tournament/agent/assignments -> {assignments: [{match_id, seat_id, game_type, seat_position, seat_count,
       match_status, seat_status, context, connect_deadline_at, (tournament games:) tournament_id, tournament_name,
       round_label, opponents}]}: one seat per live match (Pokemon: 1 of 2, Werewolf: 1 of 7, Red Alert: 1 of 2).
       `context` cycles through --contexts per new seat ("tournament", "testing": the event platform's two kinds,
       played by `python -m agent --tournament` / `--match`). The CONNECT WINDOW (04-tournaments.md: 4 minutes, Red
       Alert 4.5): a seat whose runtime has not connected (its first grant) by connect_deadline_at is a NO-SHOW: it
       leaves the listing, its grant answers assignment_not_grantable, `no_shows` counts it and a Pokemon match ends
       as our seat's forfeit. Each seat's `connected_s` (assigned -> first grant) is in the stats.
  POST /tournament/agent/assignments/{seat}/grant {execution_id} -> a SeatGrant (the seat's GameAPI token, which
       lives --token-ttl seconds) and the seat's EXECUTION LEASE for that runtime (--lease-s, 30 s like the backend);
       seat_busy (409) while another execution id holds a live lease; assignment_not_grantable once the match ended;
       assignment_not_found for an unknown seat. A grant to a new execution id revokes the previous one's seat tokens.
  POST /tournament/agent/assignments/{seat}/lease/renew {execution_id} -> 30 s more for the holder; lease_not_held
       for anyone else (or a lapsed lease).
Failure injection (the platform's --outage-file, its first word): `auth503` = authenticate answers 503 and every
agent session counts as expired (so every client must re-authenticate into the 503: the upstream runtime used to end
there), `auth429` = the same with 429 rate_limited; `all` / `sessions` as before.
Every move (play_action, send_message) is attributed to the execution id whose seat token carried it, and counted as
made by the lease holder or NOT (`moves_by_non_holder`: two runtimes playing one seat, which must stay 0).

Werewolf: local/werewolf_platform.Server (the rules of docs/platform/werewolf.md, 120 s idle clock per seat), our seat
over MCP, the six others played by local/werewolf_bots.HeuristicSeat through the real runner in threads (--ww-think
paces them like LLM agents). Our seat's repeated terminate in one window is a no-op that changes nothing (the docs:
terminate is idempotent); the SDK runner sends one after every new chat message of the window.
"""
from __future__ import annotations

import asyncio
import contextvars
import copy
import hashlib
import random
import secrets
import threading
import time
import uuid

from altruagent import DecisionContext
from altruagent.mcp_transport import MCPToolError
from altruagent.runner import run_game

from .werewolf_bots import HeuristicSeat
from .werewolf_env import N as WW_SEATS
from .werewolf_platform import Server, SeatAgent, Session

CURRENT_TOKEN: contextvars.ContextVar = contextvars.ContextVar("fake_platform_token", default=None)
LONG_POLL_MAX_S = 25.0


def _err(code: str, detail: str = "") -> dict:
    return {"error": code, "detail": detail}


def short(execution_id: str | None) -> str:
    """A label for an execution id in stats (a hash prefix; the id itself is never written anywhere)."""
    return hashlib.sha256(str(execution_id).encode()).hexdigest()[:8] if execution_id else "-"


class _Paced:
    """A bot seat that takes 0.5-1.5 x think_s per decision and chat, like an LLM agent, so a table lasts minutes."""

    def __init__(self, agent, think_s: float, rng: random.Random, stopping: threading.Event):
        self.agent, self.think_s, self.rng, self.stopping = agent, think_s, rng, stopping

    def _think(self) -> None:
        self.stopping.wait(self.think_s * self.rng.uniform(0.5, 1.5))

    def choose_action(self, state, context):
        self._think()
        return self.agent.choose_action(state, context)

    def choose_message(self, state, context):
        self._think()
        return self.agent.choose_message(state, context)


class FakeWerewolfMatch:
    """One 7-seat Werewolf table: our seat answered over MCP (the tools below), six HeuristicSeat bots in threads."""
    werewolf = True
    game_type = "werewolf"

    def __init__(self, platform, n: int, seed: int, seat: int | None = None, idle_limit: float = 120.0,
                 think_s: float = 0.0):
        self.platform, self.n, self.seed = platform, n, seed
        self.think_s = think_s
        self.sid = str(uuid.uuid4())
        self.rng = random.Random(seed)
        self.server = Server(seed, idle_limit=idle_limit)
        self.seat = seat if seat is not None else self.rng.randrange(WW_SEATS)
        self.seat_count = WW_SEATS
        self.terminal = False
        self.phase = "moving"
        self.reason = None
        self.started = time.time()
        self.ended = None
        self._acting_since: float | None = None
        self.st = {"decisions": 0, "latency_s": [], "slow": [], "timer": {"idle": 0}, "invalid_actions": 0,
                   "race_errors": 0, "reasoning_lines": 0, "reasoning_max_len": 0, "reasoning_over_cap": 0,
                   "winner_seat": None, "turns": 0, "match_invalid": 0, "error": None, "chats": 0, "dms": 0,
                   "terminates": 0, "tool_errors": [], "lat": []}

    # ---- the table ------------------------------------------------------------------------------------------------
    def run(self) -> None:
        errors = {}

        def bot(i: int) -> None:
            try:
                agent = SeatAgent(HeuristicSeat(), random.Random(self.seed * 10 + i))
                if self.think_s > 0:
                    agent = _Paced(agent, self.think_s, random.Random(self.seed * 100 + i), self.platform.stopping)
                run_game(Session(self.server, i), DecisionContext("local", None, "werewolf", f"id{i}"),
                         agent, wait_seconds=0.05, sleep=lambda s: time.sleep(min(s, 0.05)))
            except BaseException as e:  # noqa: BLE001 - reported, never fatal to the platform
                errors[i] = repr(e)[:200]

        for i in range(WW_SEATS):
            if i != self.seat:
                threading.Thread(target=bot, args=(i,), name=f"ww{self.n}-seat{i}", daemon=True).start()
        try:
            while not self.platform.stopping.is_set():
                self.server.tick()                  # the platform's idle clock (our seat included)
                with self.server.cv:
                    acting = bool(self.server.queue) and self.server.queue[0] == self.seat and not self.server.terminal
                    done = self.server.terminal
                    self.phase = "finished" if done else self.server.phase
                if acting and self._acting_since is None:
                    self._acting_since = time.monotonic()
                elif not acting:
                    self._acting_since = None
                if done:
                    break
                time.sleep(0.1)
        except Exception as e:  # noqa: BLE001
            self.st["error"] = repr(e)[:300]
        with self.server.cv:
            g = self.server.g
            self.st["turns"] = g.day
            self.st["timer"]["idle"] = sum(1 for s, _ in self.server.log["auto"] if s == self.seat)
            self.st["invalid_actions"] = sum(1 for s, *_ in self.server.log["violations"] if s == self.seat)
            side = {"wolf": "wolf", "seer": "villager", "villager": "villager"}
            self.st["role"] = g.roles[self.seat]
            self.st["winner"] = g.winner
            self.st["winner_seat"] = self.seat if g.winner and side[g.roles[self.seat]] == g.winner else None
            if errors:
                self.st["error"] = f"bot seats failed: {errors}"
            self.terminal = True
            self.reason = "completed" if self.server.terminal else "stopped"
        self.ended = time.time()
        self.platform.match_done(self)

    # ---- MCP -------------------------------------------------------------------------------------------------------
    def _fix(self, st: dict) -> dict:
        st = copy.deepcopy(st)
        st["session_id"] = self.sid
        la = st.get("legal_actions")
        if isinstance(la, dict):
            la["session_id"] = self.sid
            for a in la.get("actions") or []:
                if isinstance(a.get("input"), dict):
                    a["input"]["session_id"] = self.sid
        return st

    def view(self) -> dict:
        with self.server.cv:
            return self._fix(self.server.state(self.seat))

    def _changed(self, since_version, since_message_seq, since_is_current_actor, since_phase) -> bool:
        with self.server.cv:
            v, seq, cur, ph, term = self.server._snapshot(self.seat)
        return (term or v != since_version or (seq is not None and seq != since_message_seq)
                or (since_is_current_actor is not None and bool(cur) != bool(since_is_current_actor))
                or (since_phase is not None and ph != since_phase))

    def _tool_error(self, e: MCPToolError) -> dict:
        self.st["race_errors"] += e.error_code != "INVALID_ACTION"
        self.st["tool_errors"] = (self.st["tool_errors"] + [e.error_code])[-20:]
        return _err(e.error_code or "ERROR", str(e))

    async def mcp(self, tool: str, a: dict) -> dict:
        if tool == "get_game_state":
            return self.view()
        if tool == "get_legal_actions":
            v = self.view()
            return {"session_id": self.sid, "state_version": v["state_version"],
                    "actions": (v.get("legal_actions") or {}).get("actions") or []}
        if tool == "wait_for_update":
            end = time.monotonic() + min(LONG_POLL_MAX_S, float(a.get("timeout_seconds") or 20.0))
            while time.monotonic() < end and not self.platform.stopping.is_set():
                if self._changed(a.get("since_version"), a.get("since_message_seq"), a.get("since_is_current_actor"),
                                 a.get("since_phase")):
                    break
                await asyncio.sleep(0.05)
            out = self.view()
            out["updated"] = out["state_version"] != a.get("since_version")
            return out
        if tool == "play_action":
            t0 = self._acting_since
            with self.server.cv:
                sub = self.server.sub                  # night_wolf / night_seer / day_vote
            try:
                res = self.server.play_action(self.seat, a.get("action_id"), a.get("state_version"))
            except MCPToolError as e:
                return self._tool_error(e)
            lat = round(time.monotonic() - t0, 3) if t0 is not None else 0.0
            self._acting_since = None
            self.st["decisions"] += 1
            self.st["latency_s"].append(lat)
            self.st["lat"].append((sub, lat, self.st["decisions"] == 1))
            self.st["slow"] = sorted(self.st["slow"] + [(lat, time.strftime("%H:%M:%S"), "werewolf")], reverse=True)[:3]
            reasoning = a.get("reasoning_summary")
            if reasoning:
                self.st["reasoning_lines"] += 1
                self.st["reasoning_max_len"] = max(self.st["reasoning_max_len"], len(reasoning))
                self.st["reasoning_over_cap"] += len(reasoning) > 280
            out = {"accepted": True, "session_id": self.sid, "status": res.get("status")}
            if res.get("state"):
                out["state"] = self._fix(res["state"])
            return out
        if tool == "send_message":
            kind = a.get("message_type")
            with self.server.cv:                       # a repeated terminate is a no-op (docs: idempotent), not a new
                again = (kind == "terminate" and self.server.phase == "messaging"     # state version the runner's
                         and self.seat in self.server.g.terminated)                   # wait would return on at once
            if again:
                self.st["terminates_repeated"] = self.st.get("terminates_repeated", 0) + 1
                return {"session_id": self.sid, "phase": "messaging", "remaining": {}}
            try:
                res = self.server.send_message(self.seat, kind, a.get("content"), a.get("recipients"))
            except MCPToolError as e:
                return self._tool_error(e)
            if kind == "terminate":
                self.st["terminates"] += 1
            else:
                self.st["dms" if a.get("recipients") else "chats"] += 1
            return dict(res, session_id=self.sid)
        if tool == "get_messages":
            since = a.get("since", -1)
            with self.server.cv:
                msgs = [copy.deepcopy(m) for m in self.server.g.messages if m["seq"] > since and
                        (m["visibility"] == "broadcast" or m["sender"] == self.seat or self.seat in m["recipients"])]
            return {"session_id": self.sid, "messages": msgs}
        if tool == "get_result":
            if not self.terminal and not self.server.terminal:
                return _err("GAME_NOT_COMPLETE", "still running")
            with self.server.cv:
                res = self.server.result(self.seat)
            from .fake_platform import AGENT_ID
            return dict(res, session_id=self.sid, returns={AGENT_ID: res["your_return"], **res["returns"]})
        if tool == "resign":
            return _err("RESIGN_DISABLED", "resign is not available in werewolf")
        return _err("UNKNOWN_TOOL", tool)

    def summary(self) -> dict:
        lat = sorted(self.st["latency_s"])
        row = {k: v for k, v in self.st.items() if k != "latency_s"}
        row.update(session_id=self.sid, n=self.n, seat=self.seat, opponent="werewolf-bots", terminal=self.terminal,
                   phase=self.phase, game_type="werewolf",
                   win=(self.st["winner_seat"] == self.seat) if self.terminal else None, reason=self.reason,
                   started=time.strftime("%H:%M:%S", time.localtime(self.started)),
                   secs=round((self.ended or time.time()) - self.started, 1), bank_used=0.0,
                   latency_p50=lat[len(lat) // 2] if lat else None, latency_max=lat[-1] if lat else None)
        return row


class OfficialPlane:
    """The official tournament control plane for one registered tournament agent (see the module docstring)."""

    def __init__(self, platform, key: str, *, session_ttl: float, seat_token_ttl: float, lease_s: float,
                 contexts: tuple = ("tournament", "testing"), connect_window_s: float = 240.0,
                 ra_connect_window_s: float = 270.0):
        self.p = platform
        self.key = key
        self.session_ttl, self.seat_token_ttl, self.lease_s = session_ttl, seat_token_ttl, lease_s
        self.contexts = tuple(contexts)
        self.connect_window_s, self.ra_connect_window_s = connect_window_s, ra_connect_window_s
        self.lock = threading.Lock()
        self.seats: dict[str, dict] = {}        # seat_id -> {match, holder, lease_until, grants: [label], ...}
        self.counters = {"authenticate": 0, "authenticate_rejected": 0, "authenticate_injected": 0, "assignments": 0,
                         "grants": 0, "seat_busy": 0, "not_grantable": 0, "renewals": 0, "lease_not_held": 0,
                         "takeovers": 0, "moves_by_holder": 0, "moves_by_non_holder": 0, "moves_unattributed": 0,
                         "connected": 0, "no_shows": 0}

    # ---- seats ------------------------------------------------------------------------------------------------------
    def _seat_id(self, fm) -> str:
        return f"seat-{fm.n}-{getattr(fm, 'seat_tag', None) or ('ww' if getattr(fm, 'werewolf', False) else 'vgc')}"

    @staticmethod
    def _game(fm) -> str:
        return getattr(fm, "game_type", None) or ("werewolf" if getattr(fm, "werewolf", False)
                                                  else "pokemon_vgc_doubles_draft")

    def _seats(self) -> dict:
        now = time.time()
        for fm in list(self.p.order):
            sid = self._seat_id(fm)
            if sid not in self.seats:
                window = self.ra_connect_window_s if self._game(fm) == "red_alert" else self.connect_window_s
                ctx = self.contexts[len(self.seats) % len(self.contexts)] if self.contexts else None
                self.seats[sid] = {"match": fm, "holder": None, "lease_until": 0.0, "grants": [], "tokens": set(),
                                   "context": ctx, "created": now, "deadline": now + window, "connected_s": None,
                                   "no_show": False}
        for s in self.seats.values():               # the connect window: a seat nobody connected to is a no-show
            if s["connected_s"] is None and not s["no_show"] and not s["match"].terminal and now > s["deadline"]:
                s["no_show"] = True
                self.counters["no_shows"] += 1
                forfeit = getattr(s["match"], "forfeit_no_show", None)
                if forfeit is not None:
                    forfeit()                       # (a Pokemon match: our seat loses; a table plays on without us)
        return self.seats

    def _row(self, seat_id: str, s: dict) -> dict:
        fm = s["match"]
        ww = getattr(fm, "werewolf", False)
        row = {"match_id": f"match-{fm.n}", "seat_id": seat_id, "game_type": self._game(fm),
               "seat_position": fm.seat, "seat_count": getattr(fm, "seat_count", None) or (WW_SEATS if ww else 2),
               "match_status": "in_progress",
               "seat_status": "running" if s["holder"] else "pending",
               "connect_deadline_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(s["deadline"]))}
        if s["context"]:
            row["context"] = s["context"]
        if s["context"] == "tournament":
            row.update(tournament_id="fake-cup", tournament_name="Local Fake Cup", round_label=f"Swiss round {fm.n + 1}",
                       opponents=[{"name": "local-bot"}])
        return row

    # ---- endpoints -----------------------------------------------------------------------------------------------------
    def authenticate(self, body: dict) -> tuple[int, dict]:
        outage = self.p.outage()
        with self.lock:
            self.counters["authenticate"] += 1
            if outage in ("auth503", "auth429"):
                self.counters["authenticate_injected"] += 1
                if outage == "auth429":
                    return 429, _err("rate_limited", "Too many authentication attempts (injected)")
                return 503, _err("unavailable", "authentication service unavailable (injected)")
            if not secrets.compare_digest(str((body or {}).get("official_agent_key") or ""), self.key):
                self.counters["authenticate_rejected"] += 1
                return 401, _err("invalid_official_agent_key", "Invalid official agent key")
        tok = self.p.issue("session", self.session_ttl)
        return 200, {"access_token": tok, "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                       time.gmtime(time.time() + self.session_ttl))}

    def assignments(self) -> dict:
        with self.lock:
            self.counters["assignments"] += 1
            rows = [self._row(sid, s) for sid, s in self._seats().items()
                    if not s["match"].terminal and not s["no_show"]]
        return {"assignments": rows}

    def grant(self, seat_id: str, execution_id: str | None) -> tuple[int, dict]:
        now = time.monotonic()
        with self.lock:
            s = self._seats().get(seat_id)
            if s is None or not execution_id:
                return 404, _err("assignment_not_found", "Assignment not found")
            fm = s["match"]
            if fm.terminal or s["no_show"]:
                self.counters["not_grantable"] += 1
                return 409, _err("assignment_not_grantable", "The match has ended" if fm.terminal else
                                 "The connect window passed: a no-show")
            if s["connected_s"] is None:                  # the first grant: this runtime connected in time
                s["connected_s"] = round(time.time() - s["created"], 1)
                self.counters["connected"] += 1
            if s["holder"] and s["holder"] != execution_id and s["lease_until"] > now:
                self.counters["seat_busy"] += 1
                return 409, _err("seat_busy", "Another runtime holds this seat's execution lease")
            if s["holder"] != execution_id:
                if s["holder"] is not None:
                    self.counters["takeovers"] += 1
                for tok in s["tokens"]:                   # the previous runtime's GameAPI access ends here
                    self.p.revoke(tok)
                s["tokens"] = set()
                s["grants"].append({"execution": short(execution_id), "at": time.strftime("%H:%M:%S")})
            s["holder"], s["lease_until"] = execution_id, now + self.lease_s
            self.counters["grants"] += 1
            row = self._row(seat_id, s)
        tok = self.p.issue("seat", self.seat_token_ttl, meta=(seat_id, execution_id))
        with self.lock:
            s["tokens"].add(tok)
        return 200, {"access_token": tok, "expires_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + self.seat_token_ttl)), "agent_id": self.p.agent_id,
            "gameapi_server_url": self.p.game_server, "game_session_id": fm.sid, **row}

    def renew(self, seat_id: str, execution_id: str | None) -> tuple[int, dict]:
        now = time.monotonic()
        with self.lock:
            s = self._seats().get(seat_id)
            if s is None:
                return 404, _err("assignment_not_found", "Assignment not found")
            if s["match"].terminal:
                self.counters["not_grantable"] += 1
                return 409, _err("assignment_not_grantable", "The match has ended")
            if s["holder"] != execution_id or s["lease_until"] <= now:
                self.counters["lease_not_held"] += 1
                return 409, _err("lease_not_held", "This runtime does not hold the seat's execution lease")
            s["lease_until"] = now + self.lease_s
            self.counters["renewals"] += 1
        return 200, {"seat_id": seat_id, "execution_lease_expires_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + self.lease_s))}

    def attribute_move(self, session_id: str) -> None:
        """A move just arrived for `session_id`: made by the seat's lease holder, or not (see the module docstring)."""
        meta = self.p.token_meta(CURRENT_TOKEN.get())
        with self.lock:
            if not meta:
                self.counters["moves_unattributed"] += 1
                return
            seat_id, execution_id = meta
            s = self.seats.get(seat_id)
            held = bool(s) and s["holder"] == execution_id and s["lease_until"] > time.monotonic()
            self.counters["moves_by_holder" if held else "moves_by_non_holder"] += 1

    def stats(self) -> dict:
        with self.lock:
            self._seats()                                 # (a connect window can pass while nobody polls)
            return {**self.counters, "seats": {sid: {"match": s["match"].sid, "grants": s["grants"],
                                                     "holder": short(s["holder"]), "context": s["context"],
                                                     "game_type": self._game(s["match"]),
                                                     "connected_s": s["connected_s"], "no_show": s["no_show"]}
                                               for sid, s in self.seats.items()}}

    # ---- routes ------------------------------------------------------------------------------------------------------
    def routes(self, mcp) -> None:
        from starlette.requests import Request
        from starlette.responses import JSONResponse

        async def body_of(request: Request) -> dict:
            try:
                b = await request.json()
            except Exception:  # noqa: BLE001
                b = {}
            return b if isinstance(b, dict) else {}

        @mcp.custom_route("/tournament/agent/authenticate", methods=["POST"])
        async def authenticate(request: Request):
            code, out = self.authenticate(await body_of(request))
            return JSONResponse(out, status_code=code)

        @mcp.custom_route("/tournament/agent/assignments", methods=["GET"])
        async def assignments(request: Request):
            return JSONResponse(self.assignments())

        @mcp.custom_route("/tournament/agent/assignments/{seat_id}/grant", methods=["POST"])
        async def grant(request: Request):
            code, out = self.grant(request.path_params["seat_id"], (await body_of(request)).get("execution_id"))
            return JSONResponse(out, status_code=code)

        @mcp.custom_route("/tournament/agent/assignments/{seat_id}/lease/renew", methods=["POST"])
        async def renew(request: Request):
            code, out = self.renew(request.path_params["seat_id"], (await body_of(request)).get("execution_id"))
            return JSONResponse(out, status_code=code)
