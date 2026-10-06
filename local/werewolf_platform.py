"""A threaded stand-in for a platform `werewolf` session, written to docs/platform/werewolf.md, driven by the real runner.

    python -m local.werewolf_platform --games 30 --idle 120        # our agent in every seat + example agents mixed in

Seven threads each run `altruagent.runner.run_game` (the loop the deployed agent uses) against `Session`, which
exposes the MCPGameSession methods over one shared `Server`. The server follows the document: phase "messaging"
vs "moving" with the engine sub-phase in game_state.phase (the discussion window already sits on day_vote, and the
first voter sees is_current_actor while phase == "messaging", section 3); state_version checks (STALE_STATE),
NOT_YOUR_TURN, WRONG_PHASE, PLAYER_ELIMINATED, INVALID_ACTION; 5 chats / 50 words / one recipient per window,
idempotent terminate, the window closing when every living seat has terminated; an idle clock per seat (section 9:
only play_action / send_message count as activity; idle day voter -> abstain, idle night actor -> first legal target,
idle talker -> empty terminate); `wait_for_update` long-polling on version / message seq / actor / phase.

Unknown platform behaviour is modelled strictly: a chat from a seat that already terminated this window is rejected
with a message-scoped error, which makes the runner raise (the match process would die). Every such event, every
error returned to a seat, every idle auto-action and every decision's latency is recorded in `Server.log`.

Since the 10-04/05 docs: `new_messages` is reset at every phase change, so it is empty outside a discussion window
(during the vote too); a window also closes for everyone `window_limit` (120 s) after it opens, and while it is open
the state carries `messaging_seconds_left` / `messaging_deadline` (null otherwise); a chat over 1024 characters is
MESSAGE_TOO_LONG; the wording follows local/werewolf_env.py (ARENA_WW_WORDING, new by default). `messaging=False`
(ARENA_WW_MESSAGING=off, `--no-messaging`) is a game without discussion (messaging_enabled false: the day goes straight
to the vote, and any send_message is MESSAGING_DISABLED).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import random
import threading
import time

from altruagent import TERMINATE_MESSAGING, DecisionContext, GameState, LegalAction, SendMessage
from altruagent.mcp_transport import MCPToolError
from altruagent.runner import run_game

from .werewolf_env import ABSTAIN, MAX_CHATS, MAX_WORDS, N, Game, Seat


def _err(code: str, detail: str = "") -> MCPToolError:
    return MCPToolError(f"{code}: {detail}", status_code=None, error_code=code)


MAX_CHARS = 1024                       # MESSAGE_TOO_LONG above it (werewolf.md section 8)
WINDOW_LIMIT_S = 120.0                 # a discussion window closes 2 minutes after it opens (section 8)


class Server:
    def __init__(self, seed: int, idle_limit: float = 120.0, clock=time.monotonic, messaging: bool | None = None,
                 window_limit: float = WINDOW_LIMIT_S):
        self.cv = threading.Condition()
        self.clock = clock
        self.idle_limit = idle_limit
        self.window_limit = window_limit
        self.window_opened = None          # clock() / time.time() when the open window opened
        self.window_opened_wall = None
        self.g = Game([Seat() for _ in range(N)], random.Random(seed))
        self.g.chat_order = "seat"
        if messaging is not None:
            self.g.messaging = messaging
        self.phase = "moving"              # platform phase: moving | messaging
        self.sub = "night_wolf"
        self.queue: list[int] = []         # seats still to act in this sub-phase, in order
        self.kills: list[int] = []
        self.ballots: dict[int, int | None] = {}
        self.sent = {i: 0 for i in range(N)}
        self.terminal = False
        self.last_active = {i: clock() for i in range(N)}
        self.log = {"errors": [], "auto": [], "violations": [], "chats": 0, "dms": 0, "actions": 0}
        self._start_night()

    # ---- phase machine ---------------------------------------------------------------------------------------
    def _bump(self):
        self.g.version += 1
        self.cv.notify_all()

    def _start_night(self):
        self.phase, self.sub, self.kills = "moving", "night_wolf", []
        self.queue = [w for w in self.g.wolves()]
        if not self.queue:
            self._start_seer()
        self._touch_actor()

    def _start_seer(self):
        seer = next((i for i in range(N) if self.g.roles[i] == "seer" and self.g.alive[i]), None)
        self.sub = "night_seer"
        self.queue = [seer] if seer is not None else []
        if not self.queue:
            self._dawn()

    def _dawn(self):
        if self.g.day > 1 and self.kills:
            self.g._kill(min(self.kills), "night_kill")
        if self._check():
            return
        if not self.g.messaging:                 # no discussion window: straight to the vote
            self.sub = "day_vote"
            self._close_window()
            return
        self.phase, self.sub = "messaging", "day_vote"
        self.g.window_start_seq = len(self.g.messages)
        self.g.terminated = [i for i in range(N) if not self.g.alive[i]]
        self.sent = {i: 0 for i in range(N)}
        now = self.clock()
        self.window_opened, self.window_opened_wall = now, time.time()
        for i in self.g.living():
            self.last_active[i] = now
        self.queue = list(self.g.living())       # voters, in seat order, once the window closes

    def _close_window(self):
        self.phase, self.sub, self.ballots = "moving", "day_vote", {}
        self.window_opened = self.window_opened_wall = None
        self.queue = list(self.g.living())
        self._touch_actor()

    def _resolve_vote(self):
        tally: dict[int, int] = {}
        for t in self.ballots.values():
            if t is not None:
                tally[t] = tally.get(t, 0) + 1
        elim = None
        if tally:
            top = max(tally.values())
            leaders = [s for s, c in tally.items() if c == top]
            elim = leaders[0] if len(leaders) == 1 else None
        self.g.votes.append({"day": self.g.day, "votes": dict(self.ballots), "eliminated": elim})
        if elim is None:
            self.g.stall += 1
        else:
            self.g.stall = 0
            self.g._kill(elim, "vote")
        if self._check():
            return
        self.g.day += 1
        self._start_night()

    def _check(self) -> bool:
        if self.g.check():
            self.terminal, self.phase, self.sub, self.queue = True, "moving", None, []
            return True
        return False

    def _touch_actor(self):
        if self.queue:
            self.last_active[self.queue[0]] = self.clock()

    def _legal(self, seat: int) -> list[int]:
        if self.sub == "night_wolf":
            return [i for i in self.g.living() if self.g.roles[i] != "wolf"]
        if self.sub == "night_seer":
            return [i for i in self.g.living() if i != seat]
        return [i for i in self.g.living() if i != seat] + [ABSTAIN]

    def _apply(self, seat: int, a: int):
        self.queue.pop(0)
        self.log["actions"] += 1
        if self.sub == "night_wolf":
            self.kills.append(a)
            if not self.queue:
                self._start_seer()
        elif self.sub == "night_seer":
            self.g.invest[a] = "wolf" if self.g.roles[a] == "wolf" else "villager"
            self._dawn()
        else:
            self.ballots[seat] = None if a == ABSTAIN else a
            if not self.queue:
                self._resolve_vote()
        self._touch_actor()
        self._bump()

    # ---- idle timeout (section 9) ----------------------------------------------------------------------------
    def tick(self):
        with self.cv:
            if self.terminal:
                return
            now = self.clock()
            if self.phase == "messaging":
                over = self.window_opened is not None and now - self.window_opened > self.window_limit
                for i in self.g.living():
                    if self.phase != "messaging":
                        break
                    if i not in self.g.terminated and (over or now - self.last_active[i] > self.idle_limit):
                        self.log["auto"].append((i, "window_limit" if over else "terminate"))
                        self._terminate(i)
            elif self.queue and now - self.last_active[self.queue[0]] > self.idle_limit:
                seat = self.queue[0]
                legal = self._legal(seat)
                a = ABSTAIN if self.sub == "day_vote" else legal[0]
                self.log["auto"].append((seat, self.sub))
                self._apply(seat, a)

    def _terminate(self, seat: int):
        if seat not in self.g.terminated:
            self.g.terminated.append(seat)
        if all(i in self.g.terminated for i in self.g.living()):
            self._close_window()
        self._bump()

    # ---- tools -----------------------------------------------------------------------------------------------
    def state(self, seat: int) -> dict:
        actor = self.queue[0] if self.queue else None
        acting = actor == seat and not self.terminal and self.g.alive[seat]
        legal = self._legal(seat) if acting else None
        st = self.g.state(seat, self.phase, self.sub, legal)
        st["is_current_actor"] = acting              # true for the first voter even during messaging (section 3)
        if self.phase != "messaging":
            st["new_messages"] = []                  # reset at every phase change: empty during the vote (section 8)
        if self.phase == "messaging" and self.window_opened is not None:
            left = max(0.0, self.window_limit - (self.clock() - self.window_opened))
            st["messaging_seconds_left"] = int(left)
            st["messaging_deadline"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.window_opened_wall + self.window_limit))
        else:
            st["messaging_seconds_left"] = st["messaging_deadline"] = None
        if self.terminal:
            st.update(is_terminal=True, status="completed", legal_actions=None, is_current_actor=False)
            st["game_state"]["winner"] = self.g.winner
            st["game_state"]["phase"] = None
            st["game_state"]["day"] = None
        return st

    def _guard(self, seat: int, need_phase: str | None):
        if self.terminal:
            raise _err("GAME_ALREADY_COMPLETE")
        if not self.g.alive[seat]:
            raise _err("PLAYER_ELIMINATED")
        if need_phase and self.phase != need_phase:
            raise _err("WRONG_PHASE", f"phase is {self.phase}")

    def play_action(self, seat: int, action_id, state_version) -> dict:
        with self.cv:
            try:
                self._guard(seat, "moving")
                if not self.queue or self.queue[0] != seat:
                    raise _err("NOT_YOUR_TURN")
                if state_version != self.g.version:
                    raise _err("STALE_STATE")
                try:
                    a = int(action_id)
                except (TypeError, ValueError):
                    a = -1
                if a not in self._legal(seat):
                    self.log["violations"].append((seat, "invalid_action", action_id))
                    raise _err("INVALID_ACTION", str(action_id))
            except MCPToolError as e:
                self.log["errors"].append((seat, e.error_code))
                raise
            self.last_active[seat] = self.clock()
            self._apply(seat, a)
            return {"status": "in_progress" if not self.terminal else "completed",
                    "state": self.state(seat) if not self.terminal else None}

    def send_message(self, seat: int, message_type: str, content=None, recipients=None) -> dict:
        with self.cv:
            try:
                if not self.g.messaging:
                    self.log["violations"].append((seat, "messaging_disabled", message_type))
                    raise _err("MESSAGING_DISABLED", "this game has no discussion")
                self._guard(seat, "messaging")
                if message_type == "terminate":
                    self.last_active[seat] = self.clock()
                    self._terminate(seat)
                    return {"phase": self.phase, "remaining": {"this_turn": MAX_CHATS - self.sent[seat]}}
                if seat in self.g.terminated:
                    self.log["violations"].append((seat, "chat_after_terminate", content))
                    raise _err("MESSAGES_QUOTA_EXCEEDED", "already terminated")
                if self.sent[seat] >= MAX_CHATS:
                    self.log["violations"].append((seat, "quota", content))
                    raise _err("MESSAGES_QUOTA_EXCEEDED")
                if not isinstance(content, str) or not content.strip():
                    self.log["violations"].append((seat, "empty", content))
                    raise _err("INVALID_CONTENT")
                if len(content) > MAX_CHARS:
                    self.log["violations"].append((seat, "too_long", content))
                    raise _err("MESSAGE_TOO_LONG")
                if len(content.split()) > MAX_WORDS:
                    self.log["violations"].append((seat, "too_many_words", content))
                    raise _err("TOO_MANY_WORDS")
                rec = list(recipients or [])
                if len(rec) > 1 or any(not isinstance(r, int) or r == seat or not 0 <= r < N for r in rec):
                    self.log["violations"].append((seat, "recipients", rec))
                    raise _err("INVALID_RECIPIENTS")
            except MCPToolError as e:
                self.log["errors"].append((seat, e.error_code))
                raise
            env = {"seq": len(self.g.messages), "sender": seat, "recipients": rec,
                   "visibility": "direct" if rec else "broadcast", "type": "chat", "content": content,
                   "day": self.g.day}
            self.g.messages.append(env)
            self.sent[seat] += 1
            self.log["dms" if rec else "chats"] += 1
            self.last_active[seat] = self.clock()
            self._bump()
            return {"phase": self.phase, "message": env, "remaining": {"this_turn": MAX_CHATS - self.sent[seat]}}

    def _snapshot(self, seat: int):
        st = self.state(seat)
        seqs = [m["seq"] for m in st.get("new_messages") or []]
        return st["state_version"], max(seqs, default=None), st["is_current_actor"], st["phase"], st["is_terminal"]

    def wait_for_update(self, seat: int, since_version, since_message_seq, since_is_current_actor, since_phase,
                        timeout_seconds) -> dict:
        deadline = time.monotonic() + min(float(timeout_seconds or 1.0), 1.0)
        with self.cv:
            while True:
                v, seq, cur, ph, term = self._snapshot(seat)
                if (term or v != since_version or (seq is not None and seq != since_message_seq)
                        or bool(cur) != bool(since_is_current_actor) or ph != since_phase):
                    return self.state(seat)
                left = deadline - time.monotonic()
                if left <= 0:
                    return self.state(seat)
                self.cv.wait(timeout=min(left, 0.05))

    def result(self, seat: int) -> dict:
        with self.cv:
            side = {"wolf": "wolf", "seer": "villager", "villager": "villager"}
            returns = {self.g.names[i]: (1.0 if side[self.g.roles[i]] == self.g.winner else -1.0) for i in range(N)}
            return {"session_id": "local", "is_terminal": True, "status": "completed", "returns": returns,
                    "your_return": returns[self.g.names[seat]], "termination_reason": "completed"}


class Session:
    """The MCPGameSession surface run_game uses, for one seat."""

    def __init__(self, server: Server, seat: int):
        self.server, self.seat = server, seat
        self.session_id = "local"
        self.game_server_url = "local"

    def get_state(self) -> GameState:
        with self.server.cv:
            return GameState.from_mcp_state(self.server.state(self.seat))

    def wait_for_update(self, *, since_version, since_message_seq=None, since_is_current_actor=None,
                        since_phase=None, timeout_seconds=None) -> GameState:
        return GameState.from_mcp_state(self.server.wait_for_update(
            self.seat, since_version, since_message_seq, since_is_current_actor, since_phase, timeout_seconds))

    def get_legal_actions(self) -> dict:
        with self.server.cv:
            st = self.server.state(self.seat)
            return st.get("legal_actions") or {"state_version": st["state_version"], "actions": []}

    def play_action(self, *, action_id=None, action=None, state_version, reasoning_summary=None, **_kw) -> dict:
        if reasoning_summary is not None:
            self.server.log.setdefault("reasoning", []).append((self.seat, reasoning_summary))
        return self.server.play_action(self.seat, action_id, state_version)

    def send_message(self, *, message_type: str, content=None, recipients=None) -> dict:
        return self.server.send_message(self.seat, message_type, content, recipients)

    def get_messages(self, *, since: int = -1) -> dict:
        with self.server.cv:
            return {"messages": [m for m in self.server.g.messages if m["seq"] > since]}

    def resign(self) -> dict:
        raise _err("RESIGN_DISABLED", "not used locally")

    def get_result(self) -> dict:
        return self.server.result(self.seat)


class SeatAgent:
    """Wrap a local/werewolf_env Seat (choose(raw) / message(raw)) as a runner contestant; terminates once a day."""

    def __init__(self, seat: Seat, rng: random.Random):
        self.seat = seat
        seat.rng = rng
        seat.reset()
        self.done_day = None

    def choose_action(self, state: GameState, context):
        aid = self.seat.choose(state.raw)
        return next(a for a in state.legal_actions if a.action_id == str(aid))

    def choose_message(self, state: GameState, context):
        day = (state.raw.get("game_state") or {}).get("day")
        if self.done_day == day:
            # still reading the window's later messages, as a runner-driven agent does (new_messages is empty by
            # the vote, so a stand-in that stopped reading here would vote without them)
            read = getattr(self.seat, "_read", None)
            if callable(read):
                read(state.raw)
            return TERMINATE_MESSAGING
        m = self.seat.message(state.raw)
        if m is None:
            self.done_day = day
            return TERMINATE_MESSAGING
        return SendMessage(m["content"], recipients=m.get("recipients") or None)


class Timed:
    """Measure every decision of a contestant (the idle limit is per decision)."""

    def __init__(self, agent, sink: list):
        self.agent, self.sink = agent, sink

    def choose_action(self, state, context):
        t0 = time.perf_counter()
        try:
            return self.agent.choose_action(state, context)
        finally:
            self.sink.append(time.perf_counter() - t0)

    def choose_message(self, state, context):
        t0 = time.perf_counter()
        try:
            fn = getattr(self.agent, "choose_message", None)
            return fn(state, context) if fn else TERMINATE_MESSAGING
        finally:
            self.sink.append(time.perf_counter() - t0)


def play_table(agents: list, seed: int, idle_limit: float = 120.0, timeout: float = 120.0,
               messaging: bool | None = None) -> dict:
    """Run one 7-seat game with run_game per seat. agents[i]: a contestant object. Returns the server log + result.
    `messaging`: False = a game without discussion windows (None: ARENA_WW_MESSAGING, on by default)."""
    server = Server(seed, idle_limit=idle_limit, messaging=messaging)
    errors: dict[int, BaseException] = {}
    lat: dict[int, list] = {i: [] for i in range(N)}
    finals: dict[int, GameState] = {}

    def seat_main(i):
        try:
            finals[i] = run_game(Session(server, i), DecisionContext("local", None, "werewolf", f"id{i}"),
                                 Timed(agents[i], lat[i]), wait_seconds=0.05, sleep=lambda s: time.sleep(min(s, 0.05)))
        except BaseException as e:      # noqa: BLE001 - reported
            errors[i] = e

    threads = [threading.Thread(target=seat_main, args=(i,), daemon=True) for i in range(N)]
    for t in threads:
        t.start()
    t_end = time.monotonic() + timeout
    while any(t.is_alive() for t in threads) and time.monotonic() < t_end:
        server.tick()
        time.sleep(0.02)
    hung = [i for i, t in enumerate(threads) if t.is_alive()]
    return {"roles": list(server.g.roles), "winner": server.g.winner, "terminal": server.terminal, "log": server.log,
            "errors": {i: repr(e) for i, e in errors.items()}, "hung": hung, "days": server.g.day,
            "max_s": {i: max(v, default=0.0) for i, v in lat.items()}, "messages": list(server.g.messages),
            "votes": [dict(v) for v in server.g.votes], "returns": {i: finals[i].returns for i in finals}}


def main(argv=None) -> None:
    import os
    os.environ.setdefault("ARENA_RECORD", "0")
    from agent.agent import create_agent
    from examples.messaging_agent import WerewolfAgent

    from .werewolf_bots import HeuristicSeat, LLMSeat

    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=20)
    ap.add_argument("--seed", type=int, default=5)
    ap.add_argument("--idle", type=float, default=120.0)
    ap.add_argument("--no-messaging", action="store_true", help="games without discussion (messaging_enabled false)")
    a = ap.parse_args(argv)
    from .werewolf_env import wording_from_env
    print(f"wording {wording_from_env()} (ARENA_WW_WORDING), discussion {'off' if a.no_messaging else 'on'}")
    kinds = ["ours", "ours", "ours", "llm", "heuristic", "messaging_example", "basic"]
    tot = {"errors": 0, "hung": 0, "auto_ours": 0, "violations_ours": 0, "games": 0, "ours_wins": 0, "ours_seats": 0,
           "votes_ours": 0, "abstain_ours": 0, "chats_ours": 0}
    worst = 0.0
    for g in range(a.games):
        r = random.Random(a.seed * 1000 + g)
        seats = [r.choice(kinds) for _ in range(N)]
        if "ours" not in seats:
            seats[r.randrange(N)] = "ours"
        agents = []
        for i, kind in enumerate(seats):
            if kind == "ours":
                agents.append(create_agent())
            elif kind == "llm":
                agents.append(SeatAgent(LLMSeat(), random.Random(r.random())))
            elif kind == "heuristic":
                agents.append(SeatAgent(HeuristicSeat(), random.Random(r.random())))
            elif kind == "messaging_example":
                agents.append(WerewolfAgent())
            else:
                agents.append(SeatAgent(_First(), random.Random(0)))
        with contextlib.redirect_stdout(io.StringIO()):          # examples/messaging_agent.py prints every message
            out = play_table(agents, a.seed * 1000 + g, idle_limit=a.idle, messaging=False if a.no_messaging else None)
        ours = [i for i, k in enumerate(seats) if k == "ours"]
        tot["games"] += 1
        tot["errors"] += sum(1 for i in out["errors"] if i in ours)
        tot["hung"] += len(out["hung"])
        tot["auto_ours"] += sum(1 for s, _ in out["log"]["auto"] if s in ours)
        tot["violations_ours"] += sum(1 for s, *_ in out["log"]["violations"] if s in ours)
        side = {"wolf": "wolf", "seer": "villager", "villager": "villager"}
        tot["ours_seats"] += len(ours)
        tot["ours_wins"] += sum(1 for i in ours if side[out["roles"][i]] == out["winner"])
        worst = max([worst] + [out["max_s"][i] for i in ours])
        ballots = [(s, t) for v in out["votes"] for s, t in v["votes"].items() if s in ours]
        tot["votes_ours"] += len(ballots)
        tot["abstain_ours"] += sum(1 for _, t in ballots if t is None)
        tot["chats_ours"] += sum(1 for m in out["messages"] if m["sender"] in ours)
        if out["errors"] or out["hung"]:
            print(f"game {g}: seats {seats} errors {out['errors']} hung {out['hung']}")
    print(f"{tot['games']} games via altruagent.runner.run_game: our seat errors {tot['errors']}, hung seats "
          f"{tot['hung']}, idle auto-actions on our seats {tot['auto_ours']}, rejected moves/chats of our seats "
          f"{tot['violations_ours']}, slowest decision of our seats {1000 * worst:.1f} ms (limit {a.idle:.0f} s); "
          f"our seats won {tot['ours_wins']}/{tot['ours_seats']}; our ballots {tot['votes_ours']} "
          f"({tot['abstain_ours']} abstain), our chats {tot['chats_ours']}")


class _First(Seat):
    name = "basic"

    def choose(self, raw):
        return raw["legal_actions"]["actions"][0]["action_id"]


if __name__ == "__main__":
    main()
