"""A local stand-in for the AltruAgent platform, for unattended rehearsals of the REAL `python -m agent` process tree
(scripts/run_agent.sh -> supervisor -> one worker process per match -> node damage calculator) without ever touching
the real platform. Binds 127.0.0.1 only; the API key is a local test value.

    python -m local.fake_platform --port 8765 --official --werewolf 1 --stats logs/rehearsal/platform.json
    ALTRUAGENT_CONTROL_URL=http://127.0.0.1:8765 ALTRUAGENT_OFFICIAL_AGENT_KEY=eak_live_fa4e...fa4e \
        python -m agent --tournament --match      # (the FAKE key DEFAULT_OFFICIAL_KEY below; the event flow)

Control plane (REST), without --official: the RETIRED platform-agent API (POST /auth/agent/login, GET /auth/agent/me,
GET /agents/me/sessions, GET /competitions/{id}), kept for the SDK's own client tests; `python -m agent` no longer
speaks it (the platform turned it off on 10-06).
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
answers carry wall-clock timestamps, so a recovery after `kill -9` shows up as one slow answer at that time. Every
answered decision is also kept as (phase, latency s, first of the match) in `lat`, so a report can give the battle
decisions' p99 on their own.

Payloads are shaped like the REAL platform's recordings (logs/yoriko, logs/apocalypse; docs/real_payloads.md) unless
--plain: `team` lists all six of the roster with item / ability / moves / revealed / is_terastallized
(ARENA_SIX_TEAM=1), `protocol_log` is always present (ARENA_PLOG=1) and ends with Showdown's
`|inactive|Time left: T sec this turn | B sec total` line, `fields` / `weather` are dicts, and the observation carries
battle_tag, can_tera, finished, lost, won, waiting_for_action and target_legend. Every --blind-every'th battle
request (default 25; 0 = never) is BLIND like the recorded 4b85cc32 turn 8: the doubles_turn template next to an
observation that is only {"message": "No pending decision is currently available.", ...}.

The official tournament (`--official`, local/fake_official.py): the /tournament/agent/* control plane with seat
execution leases, for `python -m agent --tournament` with a FAKE Official Agent Key (--official-key, or
FAKE_OFFICIAL_KEY; the default is a fixed local test value). `--werewolf N` adds N 7-seat Werewolf tables (our seat
over MCP, six local bots), in either mode. `--red-alert N` adds N real-time Red Alert matches, one after another
(local/fake_redalert.py: the local simulator, --ra-seconds of game time at --ra-speed, a scripted --ra-opponent in
the other seat; the MCP tool get_game_config answers too).
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import random
import re
import secrets
import signal
import threading
import time
import uuid
from pathlib import Path

from altruagent import RESIGN

from .pokemon_env import GAME, TIMER_DEFAULT, Match, OfficialBaseline, Player, RandomPlayer, _Forfeit, _sid
from .showdown import Showdown

AGENT_ID = "agent-rehearsal"
AGENT_NAME = "Rehearsal_Local"
DEFAULT_KEY = "local-rehearsal-key"          # a local test value; the fake platform accepts nothing else
# The FAKE Official Agent Key: shaped like a real one (eak_live_ + 64 hex, so the SDK's local format check passes),
# obviously fake ("fa4e" = FAKE) and accepted by this local fake platform only.
DEFAULT_OFFICIAL_KEY = "eak_live_" + "fa4e" * 16
TIMERS = {"draft": 300.0, "team_preview": 90.0, "battle": 55.0}
BANK_S = 420.0 + 90.0
LONG_POLL_MAX_S = 25.0
REASONING_CAP = 280
_NONE = object()

TARGET_LEGEND = {"-1": "board position A on your side (always slot 0's position)",
                 "-2": "board position B on your side (always slot 1's position)",
                 "1": "board position A on the opponent's side", "2": "board position B on the opponent's side",
                 "0": "no target (self / spread / field moves)"}
BLIND_OBS = {"message": "No pending decision is currently available.", "waiting_for_action": False, "finished": False}
_SWITCH = re.compile(r"^\|(?:switch|drag|replace)\|(p[12])[ab]: [^|]*\|([^,|]+)")


def _err(code: str, detail: str) -> dict:
    return {"error": code, "detail": detail}


def _own_tools(fm) -> bool:
    """A match that answers the MCP tools itself (`mcp(tool, args)`: Werewolf tables, Red Alert), not FakeMatch."""
    return callable(getattr(fm, "mcp", None))


def _dictify(x) -> dict:
    if isinstance(x, dict):
        return x
    if not x:
        return {}
    items = x if isinstance(x, list) else [x]
    return {re.sub(r"[^A-Z0-9]+", "_", str(i).upper()).strip("_"): 0 for i in items}


def realistic(raw: dict, fm: "FakeMatch") -> dict:
    """A battle state (phase "moving") shaped like the real platform's (see the module docstring); others unchanged.
    From the exp20 deployment audit's fake_platform_real.py (checked against 1366 recorded moving states)."""
    obs = raw.get("observation")
    if raw.get("phase") != "moving" or not isinstance(obs, dict):
        return raw
    obs.pop("phase", None)                           # (local only)
    obs["fields"] = _dictify(obs.get("fields"))
    obs["weather"] = _dictify(obs.get("weather"))
    match, seat = fm.match, fm.seat
    cards = {_sid(c["species"]): c for c in (match.rosters[seat] if match else [])}
    plog = obs.get("protocol_log")
    if not isinstance(plog, list):
        plog = []
    side = "p1" if seat == 0 else "p2"
    seen = {_sid(m.group(2)) for m in map(_SWITCH.match, plog) if m and m.group(1) == side}
    for v in (obs.get("team") or {}).values():
        if not isinstance(v, dict):
            continue
        c = cards.get(_sid(v.get("species") or ""))
        if c is not None:
            v.setdefault("ability", _sid(c["ability"]))
            v.setdefault("item", _sid(c["item"]))
            v.setdefault("moves", [_sid(m) for m in c["moves"]])
        v.setdefault("is_terastallized", False)
        if "revealed" not in v:
            v["revealed"] = bool(v.get("fainted")) or _sid(v.get("species") or "") in seen
    turn = int(obs.get("turn") or 0)
    per_turn = 90 if turn <= 1 else 55
    bank = max(0, int(420 - max(0.0, fm.bank_used - 90)))
    obs["protocol_log"] = list(plog) + [f"|inactive|Time left: {per_turn} sec this turn | {bank} sec total"
                                        + (" | 90 sec grace" if turn <= 1 else "")]
    obs.setdefault("battle_tag", f"battle-gen9vgc2025regi-{fm.n}")
    obs.setdefault("can_tera", [False, False])
    obs.setdefault("finished", False)
    obs.setdefault("lost", None)
    obs.setdefault("won", None)
    obs.setdefault("waiting_for_action", True)
    obs.setdefault("target_legend", dict(TARGET_LEGEND))
    return raw


class RemoteSeat(Player):
    """Our seat: every decision is published as this agent's MCP state and answered by play_action."""
    name = "remote"

    def __init__(self, fm: "FakeMatch"):
        self.fm = fm

    def reset(self):
        pass

    def bind(self, match, seat):                    # (pokemon_env.Match hands every player its match)
        self.fm.match = match

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
        self.match = None                    # the pokemon_env.Match (RemoteSeat.bind)
        self.battle_asks = 0
        self.no_show = False                 # the official plane's connect window passed unconnected (forfeit_no_show)
        self.st = {"decisions": 0, "latency_s": [], "slow": [], "timer": {"draft": 0, "team_preview": 0, "battle": 0},
                   "invalid_actions": 0, "race_errors": 0, "reasoning_lines": 0, "reasoning_max_len": 0,
                   "reasoning_over_cap": 0, "winner_seat": None, "turns": 0, "match_invalid": 0, "error": None,
                   "blind": 0, "lat": []}

    # ---- match thread side -------------------------------------------------------------------------------------
    def shape(self, raw: dict) -> dict:
        """The platform's payload shape (realistic(), a blind state now and then) unless the platform is --plain."""
        if not self.platform.realistic:
            return raw
        try:
            raw = realistic(raw, self)
            every = self.platform.blind_every
            if raw.get("phase") == "moving" and isinstance(raw.get("observation"), dict):
                self.battle_asks += 1
                if every and self.battle_asks % every == 0 and int(raw["observation"].get("turn") or 0) > 1:
                    raw["observation"] = dict(BLIND_OBS)
                    self.st["blind"] += 1
        except Exception as e:  # noqa: BLE001 - the shaping must never end a rehearsal match
            print(f"realistic() failed: {e!r}", flush=True)
        return raw

    def forfeit_no_show(self) -> None:
        """Our runtime never connected within the seat's connect window (local/fake_official.py): our seat loses, as
        the platform scores a no-show; the match ends at our seat's next request (or the one pending now)."""
        with self.cond:
            self.no_show = True
            self.cond.notify_all()

    def ask(self, raw: dict):
        if self.no_show:
            return RESIGN
        raw = self.shape(copy.deepcopy(raw))
        first = self.st["decisions"] == 0 and not any(self.st["timer"].values())
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
            while self.answer is _NONE and not self.platform.stopping.is_set() and not self.no_show:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.cond.wait(min(left, 1.0))
            ans, waited = self.answer, time.monotonic() - self.pending_at
            self.pending = None
            self.version += 1
            self.cond.notify_all()
        if ans is _NONE and self.no_show:
            return RESIGN                         # (a no-show: not a timer decision)
        if phase == "battle":
            self.bank_used += min(waited, limit)
        if ans is _NONE:
            self.st["timer"][phase] = self.st["timer"].get(phase, 0) + 1
            if phase == "battle" and self.bank_used >= BANK_S:
                return RESIGN                     # bank empty: Showdown's "lost due to inactivity"
            return TIMER_DEFAULT if phase in ("team_preview", "battle") else None
        self.st["decisions"] += 1
        self.st["latency_s"].append(round(waited, 3))
        self.st["lat"].append((raw.get("phase"), round(waited, 3), first))
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
                self.reason = "no_show" if self.no_show else "forfeit" if r.forfeit is not None else "game_over"
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
                   phase=self.phase, game_type=GAME,
                   win=(self.st["winner_seat"] == self.seat) if self.terminal else None, reason=self.reason,
                   started=time.strftime("%H:%M:%S", time.localtime(self.started)),
                   secs=round((self.ended or time.time()) - self.started, 1), bank_used=round(self.bank_used, 1),
                   latency_p50=lat[len(lat) // 2] if lat else None, latency_max=lat[-1] if lat else None)
        return row


class FakePlatform:
    def __init__(self, *, concurrent: int, opponent: str, max_matches: int | None, api_key: str, token_ttl: float,
                 seed: int, stats: Path | None, game_server: str, realistic: bool = True, blind_every: int = 25,
                 werewolf: int = 0, max_werewolf: int | None = None, ww_seat: int | None = None,
                 ww_idle: float = 120.0, ww_think: float = 0.0, red_alert: int = 0, ra_seconds: float = 20.0,
                 ra_opponent: str = "idle", ra_speed: float = 1.0):
        self.concurrent, self.opponent, self.max_matches = concurrent, opponent, max_matches
        self.red_alert, self.ra_seconds = red_alert, ra_seconds     # Red Alert matches in all, one at a time
        self.ra_opponent, self.ra_speed = ra_opponent, ra_speed
        self._ra_started = 0
        self.api_key, self.token_ttl, self.seed, self.stats_path = api_key, token_ttl, seed, stats
        self.game_server = game_server
        self.realistic, self.blind_every = realistic, blind_every
        self.werewolf, self.ww_seat, self.ww_idle, self.ww_think = werewolf, ww_seat, ww_idle, ww_think
        self.max_werewolf = werewolf if max_werewolf is None else max_werewolf
        self.agent_id = AGENT_ID
        self.official = None                   # local.fake_official.OfficialPlane with --official
        self.gap = 0.0
        self.matches: dict[str, FakeMatch] = {}
        self.order: list[FakeMatch] = []
        self.lock = threading.Lock()
        self.stats_lock = threading.Lock()
        self.stopping = threading.Event()
        self.tokens: dict[str, float] = {}
        self.token_kind: dict[str, str] = {}   # login (discovery) | session (official agent) | seat (a SeatGrant)
        self.token_seat: dict[str, tuple] = {} # seat token -> (seat_id, execution_id)
        self.counters = {"logins": 0, "unauthorized": 0, "sessions_calls": 0, "mcp_calls": 0, "outage_503": 0}
        self.t0 = time.time()
        self.outage_file: Path | None = None
        self.drain_file: Path | None = None    # while it exists no new match starts (the live ones play on)
        self.stats_every = 10.0
        self._outage = (0.0, None)
        self._ww_started = 0

    def outage(self) -> str | None:
        """The --outage-file's first word while it exists, else None: a rehearsed network blip ('assignments': 503 on
        the assignment listing only, 'sessions': on the retired discovery poll, 'all': on every request) or a failing
        official authentication ('auth503' / 'auth429', every agent session expired)."""
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
    def issue(self, kind: str, ttl: float, meta: tuple | None = None) -> str:
        tok = secrets.token_hex(16)
        with self.lock:
            now = time.time()
            for t in [t for t, exp in self.tokens.items() if exp <= now]:
                self.tokens.pop(t, None)
                self.token_kind.pop(t, None)
                self.token_seat.pop(t, None)
            self.tokens[tok] = now + ttl
            self.token_kind[tok] = kind
            if meta is not None:
                self.token_seat[tok] = meta
        return tok

    def revoke(self, tok: str) -> None:
        with self.lock:
            self.tokens.pop(tok, None)

    def token_meta(self, tok: str | None) -> tuple | None:
        with self.lock:
            return self.token_seat.get(tok or "")

    def login(self, key: str) -> str | None:
        if not key or not secrets.compare_digest(str(key), self.api_key):
            return None
        with self.lock:
            self.counters["logins"] += 1
        return self.issue("login", self.token_ttl)

    def token_ok(self, header: str, kinds: tuple = ("login",)) -> bool:
        tok = header[7:] if header.startswith("Bearer ") else ""
        stale_sessions = self.outage() in ("auth503", "auth429")     # (the injected re-authentication failure)
        with self.lock:
            kind = self.token_kind.get(tok)
            ok = self.tokens.get(tok, 0) > time.time() and kind in kinds and not (stale_sessions and kind == "session")
            if not ok:
                self.counters["unauthorized"] += 1
            return ok

    # ---- matches -----------------------------------------------------------------------------------------------
    def _new_match(self) -> None:
        n = len(self.order)
        opp = self.opponent if self.opponent != "mixed" else ("random", "baseline")[n % 2]
        fm = FakeMatch(self, n, opp, self.seed * 100000 + n)
        self._start(fm)

    def _new_werewolf(self) -> None:
        from .fake_official import FakeWerewolfMatch
        n = len(self.order)
        self._ww_started += 1
        self._start(FakeWerewolfMatch(self, n, self.seed * 100000 + n, seat=self.ww_seat, idle_limit=self.ww_idle,
                                      think_s=self.ww_think))

    def _new_red_alert(self) -> None:
        from .fake_redalert import FakeRedAlertMatch
        self._ra_started += 1
        self._start(FakeRedAlertMatch(self, len(self.order), seconds=self.ra_seconds, opponent=self.ra_opponent,
                                      speed=self.ra_speed))

    def _start(self, fm) -> None:
        with self.lock:
            self.matches[fm.sid] = fm
            self.order.append(fm)
        threading.Thread(target=fm.run, name=f"match-{fm.n}", daemon=True).start()

    def match_done(self, fm: FakeMatch) -> None:
        self.write_stats()

    def scheduler(self) -> None:
        last = 0.0
        while not self.stopping.is_set():
            try:
                draining = self.drain_file is not None and self.drain_file.exists()
                live_ww = sum(1 for m in self.order if getattr(m, "werewolf", False) and not m.terminal)
                if not draining and live_ww < self.werewolf and self._ww_started < self.max_werewolf:
                    self._new_werewolf()                                             # (next to the VGC matches)
                    continue
                live_ra = sum(1 for m in self.order if getattr(m, "game_type", GAME) == "red_alert" and not m.terminal)
                if not draining and live_ra == 0 and self._ra_started < self.red_alert:
                    self._new_red_alert()                                            # (one at a time)
                    continue
                vgc = [m for m in self.order if getattr(m, "game_type", GAME) == GAME]
                active = [m for m in vgc if not m.terminal]
                ended = [m.ended for m in vgc if m.ended]
                idle = not ended or time.time() - max(ended) >= self.gap
                if not draining and len(active) < self.concurrent and idle and \
                        (self.max_matches is None or len(vgc) < self.max_matches):
                    self._new_match()
                    continue
                if time.time() - last > self.stats_every:
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
            return {"session_id": m.sid, "status": status, "game_type": getattr(m, "game_type", GAME),
                    "tournament_id": "rehearsal", "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(m.started))}
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
               "slowest": sorted((s for m in self.order for s in m.st["slow"]), reverse=True)[:8],
               "blind_states": sum(r.get("blind", 0) for r in rows),
               "werewolf_tables": sum(1 for r in rows if r.get("game_type") == "werewolf"),
               "red_alert_matches": sum(1 for r in rows if r.get("game_type") == "red_alert"), **counters}
        out = {"totals": tot, "matches": rows}
        if self.official is not None:
            out["official"] = self.official.stats()
        tmp = self.stats_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, indent=1))
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

    if p.official is not None:
        p.official.routes(mcp)

    def attribute(session_id: str) -> None:
        if p.official is not None:
            p.official.attribute_move(session_id)

    @mcp.tool()
    async def get_game_state(session_id: str) -> dict:
        fm = match(session_id)
        if _own_tools(fm):
            return await fm.mcp("get_game_state", {})
        return fm.view() if fm else _err("SESSION_NOT_FOUND", session_id)

    @mcp.tool()
    async def get_legal_actions(session_id: str) -> dict:
        fm = match(session_id)
        if fm is None:
            return _err("SESSION_NOT_FOUND", session_id)
        if _own_tools(fm):
            return await fm.mcp("get_legal_actions", {})
        v = fm.view()
        return {"session_id": session_id, "state_version": v["state_version"],
                "actions": (v.get("legal_actions") or {}).get("actions") or []}

    @mcp.tool()
    async def send_message(session_id: str, message_type: str, content: str | None = None,
                           recipients: list[int] | None = None) -> dict:
        fm = match(session_id)
        if not _own_tools(fm):
            return _err("SESSION_NOT_FOUND" if fm is None else "MESSAGING_NOT_ENABLED", session_id)
        out = await fm.mcp("send_message", {"message_type": message_type, "content": content, "recipients": recipients})
        if "error" not in out:
            attribute(session_id)
        return out

    @mcp.tool()
    async def get_messages(session_id: str, since: int = -1) -> dict:
        fm = match(session_id)
        if not _own_tools(fm):
            return _err("SESSION_NOT_FOUND" if fm is None else "MESSAGING_NOT_ENABLED", session_id)
        return await fm.mcp("get_messages", {"since": since})

    @mcp.tool()
    async def wait_for_update(session_id: str, since_version: int, since_message_seq: int | None = None,
                              since_is_current_actor: bool | None = None, since_phase: str | None = None,
                              timeout_seconds: float | None = None) -> dict:
        fm = match(session_id)
        if fm is None:
            return _err("SESSION_NOT_FOUND", session_id)
        if _own_tools(fm):
            return await fm.mcp("wait_for_update", {"since_version": since_version, "since_message_seq": since_message_seq,
                                                    "since_is_current_actor": since_is_current_actor,
                                                    "since_phase": since_phase, "timeout_seconds": timeout_seconds})
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
        if _own_tools(fm):
            out = await fm.mcp("play_action", {"state_version": state_version, "action_id": action_id,
                                               "action": action, "reasoning_summary": reasoning_summary})
            if "error" not in out:
                attribute(session_id)
            return out
        err = fm.submit(state_version, action_id, action, reasoning_summary)
        if err is not None:
            return err
        attribute(session_id)
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
        if _own_tools(fm):
            return await fm.mcp("get_result", {})
        if not fm.terminal:
            return _err("GAME_NOT_COMPLETE", "still running")
        return {"session_id": session_id, "is_terminal": True, "status": "completed", "returns": fm.returns,
                "termination_reason": fm.reason}

    @mcp.tool()
    async def resign(session_id: str) -> dict:
        fm = match(session_id)
        if fm is None:
            return _err("SESSION_NOT_FOUND", session_id)
        if _own_tools(fm):
            return await fm.mcp("resign", {})
        with fm.cond:
            if fm.pending is not None and fm.answer is _NONE:
                fm.answer = RESIGN
                fm.cond.notify_all()
        end = time.monotonic() + 5
        while time.monotonic() < end and not fm.terminal:
            await asyncio.sleep(0.05)
        return {"session_id": session_id, "is_terminal": fm.terminal, "status": "completed", "returns": fm.returns,
                "termination_reason": "resign"}

    @mcp.tool()
    async def get_game_config(game_type: str) -> dict:
        """The game's static reference (the SDK runner fetches it once per real-time match): Red Alert only here."""
        with p.lock:
            p.counters["mcp_calls"] += 1
            p.counters["game_config_calls"] = p.counters.get("game_config_calls", 0) + 1
        if game_type == "red_alert":
            from .fake_redalert import game_config
            return game_config()
        return _err("UNKNOWN_GAME", f"no reference for {game_type!r} on this fake platform")

    app = mcp.streamable_http_app()

    from .fake_official import CURRENT_TOKEN

    class Auth:
        """Bearer check on everything but login / authenticate (the platform's JWT; expired -> 401 -> the client logs
        in again). Which token opens what: the official agent session the /tournament/agent/assignments endpoints, a
        seat's SeatGrant token or a discovery login the GameAPI (/mcp), a discovery login the rest."""

        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            outage = p.outage() if scope["type"] == "http" else None
            if outage == "all" or (outage == "sessions" and scope["path"] == "/agents/me/sessions") or \
                    (outage == "assignments" and scope["path"] == "/tournament/agent/assignments"):
                with p.lock:
                    p.counters["outage_503"] += 1
                await JSONResponse({"error": "unavailable", "detail": "rehearsal outage"},
                                   status_code=503)(scope, receive, send)
                return
            path = scope.get("path") or ""
            if scope["type"] == "http" and path not in ("/auth/agent/login", "/healthz", "/tournament/agent/authenticate"):
                hdr = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
                kinds = ("session",) if path.startswith("/tournament/agent/") else \
                    ("login", "seat") if path.startswith("/mcp") else ("login",)
                if not p.token_ok(hdr, kinds):
                    code = "invalid_agent_session" if kinds == ("session",) else "unauthorized"
                    await JSONResponse({"error": code, "detail": "missing or expired token"},
                                       status_code=401)(scope, receive, send)
                    return
                CURRENT_TOKEN.set(hdr[7:] if hdr.startswith("Bearer ") else None)   # (whose move: fake_official)
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
    ap.add_argument("--stats", type=Path, default=None, help="JSON stats file, rewritten every --stats-every s")
    ap.add_argument("--stats-every", type=float, default=10.0, help="seconds between stats rewrites (default 10)")
    ap.add_argument("--drain-file", type=Path, default=None,
                    help="while this file exists no new match (or Werewolf table) starts; the live ones play on")
    ap.add_argument("--gap", type=float, default=0.0,
                    help="idle seconds after a match ends before the next one starts (default 0)")
    ap.add_argument("--outage-file", type=Path, default=None,
                    help="while this file exists: 503 on the assignment listing ('assignments'; the retired discovery "
                         "poll: 'sessions'), on everything ('all'), or on official authentication with every agent "
                         "session expired ('auth503' / 'auth429')")
    ap.add_argument("--plain", action="store_true",
                    help="the old local payloads (four-entry team, no protocol_log, no timer line, no blind states)")
    ap.add_argument("--blind-every", type=int, default=25,
                    help="every Nth battle request of a match is a blind state (default 25; 0 = never)")
    ap.add_argument("--werewolf", type=int, default=0, help="7-seat Werewolf tables in progress at once (default 0)")
    ap.add_argument("--max-werewolf", type=int, default=None,
                    help="Werewolf tables in all (default: --werewolf; more = a new table when one ends)")
    ap.add_argument("--ww-seat", type=int, default=None, help="our Werewolf seat (default: random per table)")
    ap.add_argument("--ww-idle", type=float, default=120.0, help="Werewolf idle clock per seat (default 120 s)")
    ap.add_argument("--ww-think", type=float, default=0.0,
                    help="the bots' mean seconds per decision / chat (default 0; a few s = LLM-paced tables)")
    ap.add_argument("--red-alert", type=int, default=0,
                    help="real-time Red Alert matches in all, one at a time (default 0; local/fake_redalert.py)")
    ap.add_argument("--ra-seconds", type=float, default=20.0,
                    help="game seconds each Red Alert match plays before its time limit (default 20)")
    ap.add_argument("--ra-opponent", choices=("idle", "turtle", "rusher", "llm"), default="idle",
                    help="the scripted bot in the other Red Alert seat (local/ra_bots.py; default idle)")
    ap.add_argument("--ra-speed", type=float, default=1.0,
                    help="Red Alert game seconds per wall second (default 1, the platform's pace)")
    ap.add_argument("--official", action="store_true",
                    help="the official tournament control plane (/tournament/agent/*, local/fake_official.py)")
    ap.add_argument("--official-key", default=os.environ.get("FAKE_OFFICIAL_KEY", DEFAULT_OFFICIAL_KEY),
                    help="the Official Agent Key this fake accepts (a FAKE test value)")
    ap.add_argument("--session-ttl", type=float, default=600.0, help="seconds an official agent session lives")
    ap.add_argument("--lease-s", type=float, default=30.0, help="a seat's execution lease (default 30 s)")
    ap.add_argument("--contexts", default="tournament,testing",
                    help="the assignments' context, cycled per new seat (default tournament,testing; '' = none)")
    ap.add_argument("--connect-window", type=float, default=240.0,
                    help="seconds a runtime has to connect to a new seat before it is a no-show (default 240)")
    ap.add_argument("--ra-connect-window", type=float, default=270.0,
                    help="the same for a Red Alert seat (default 270)")
    a = ap.parse_args(argv)
    import uvicorn

    os.environ["ARENA_RECORD"] = "0"
    if not a.plain:                                   # (read by pokemon_env.Match when a match starts)
        os.environ["ARENA_SIX_TEAM"] = "1"
        os.environ["ARENA_PLOG"] = "1"
    p = FakePlatform(concurrent=a.concurrent, opponent=a.opponent, max_matches=a.max_matches, api_key=a.api_key,
                     token_ttl=a.token_ttl, seed=a.seed, stats=a.stats, game_server=f"127.0.0.1:{a.port}",
                     realistic=not a.plain, blind_every=max(0, a.blind_every), werewolf=max(0, a.werewolf),
                     max_werewolf=a.max_werewolf, ww_seat=a.ww_seat, ww_idle=a.ww_idle, ww_think=max(0.0, a.ww_think),
                     red_alert=max(0, a.red_alert), ra_seconds=a.ra_seconds, ra_opponent=a.ra_opponent,
                     ra_speed=a.ra_speed)
    p.outage_file, p.gap = a.outage_file, a.gap
    p.drain_file, p.stats_every = a.drain_file, max(0.5, a.stats_every)
    if a.official:
        from .fake_official import OfficialPlane
        p.official = OfficialPlane(p, a.official_key, session_ttl=a.session_ttl, seat_token_ttl=a.token_ttl,
                                   lease_s=a.lease_s, contexts=tuple(c for c in a.contexts.split(",") if c.strip()),
                                   connect_window_s=a.connect_window, ra_connect_window_s=a.ra_connect_window)
    threading.Thread(target=p.scheduler, name="scheduler", daemon=True).start()
    server = uvicorn.Server(uvicorn.Config(build_app(p, a.port), host="127.0.0.1", port=a.port, log_level="warning",
                                           access_log=False))

    def stop(*_):
        p.stopping.set()
        server.should_exit = True
    signal.signal(signal.SIGTERM, stop)
    print(f"fake platform on http://127.0.0.1:{a.port} ({a.concurrent} concurrent, opponent {a.opponent}, "
          f"{'plain' if a.plain else 'real-shaped'} payloads, {a.werewolf} werewolf, {a.red_alert} red alert, "
          f"{'official (event) ' if a.official else 'retired platform-agent '}control plane)", flush=True)
    try:
        server.run()
    finally:
        p.stopping.set()
        p.write_stats()


if __name__ == "__main__":
    main()
