"""Local 7-player Werewolf engine, written to docs/platform/werewolf.md.

Rules implemented (all from that document): 2 wolves / 1 seer / 4 villagers;
night 1 peaceful; wolves disagree -> lower seat dies; seer sees wolf/villager;
deaths at dawn reveal the role; one open discussion window per day (5 chats,
50 words, private message to one seat allowed); sequential hidden votes,
plurality lynches, tie lynches nobody, "7" = abstain; three consecutive days
without an elimination -> wolves win; wolves win at parity; +1/-1 per side.

State payloads are built in the platform's shape (observation text,
game_state, players, legal_actions, new_messages, ...) so the deployed
`agent.agent.Contestant` runs here unchanged.

Failure model (docs/platform/werewolf.md section 9 and altruagent/runner.py): a
seat whose decision raises, answers an illegal action, sends a chat the
platform would reject (empty, over 50 words, bad recipients, a 6th chat) or
takes longer than the 120 s idle limit loses its process: from then on the
platform auto-plays it (day vote -> abstain, night -> first legal target, chat
-> terminate). Every such event is counted in `Game.stats` per seat.

Like the real runner, a seat that already terminated this window is still asked
`message()` whenever the window is open; answering a chat then is counted as a
violation (`chat_after_terminate`) and dropped.
"""
from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field

from altruagent import TERMINATE_MESSAGING, DecisionContext, GameState, LegalAction, SendMessage, WithReasoning
from altruagent.runner import _validate_decision

os.environ["ARENA_RECORD"] = "0"      # never record simulated games (agent/agent.py record())
N = 7
ABSTAIN = 7
IDLE_LIMIT_S = 120.0                  # werewolf.md section 9
MAX_CHATS = 5
MAX_WORDS = 50
ROUNDS = 6                            # discussion rounds before the idle timeout would close the window


class Seat:
    """Agent interface: reset(); vote/kill/investigate via choose(raw) -> action_id str; chat via message(raw).

    `rng` (a random.Random) is assigned by the Game before reset(), one per seat, derived from the game seed."""
    name = "seat"
    rng: random.Random | None = None

    def reset(self):
        pass

    def choose(self, raw: dict) -> str:
        raise NotImplementedError

    def message(self, raw: dict):
        return None            # None = terminate; else {"content": str, "recipients": [] | [seat]}


class ContestantSeat(Seat):
    def __init__(self, factory, name="ours"):
        self.factory, self.name = factory, name

    def reset(self):
        self.agent = self.factory()

    def _ctx(self):
        return DecisionContext(session_id="local", tournament_id=None, game_type="werewolf", agent_id="ours")

    def choose(self, raw: dict) -> str:
        gs = GameState.from_mcp_state(raw)
        out = self.agent.choose_action(gs, self._ctx())
        if isinstance(out, WithReasoning):          # public reasoning: checked like the real runner, then unwrapped
            _validate_decision(out, gs.legal_actions)
            out = out.action
        return out.action_id if isinstance(out, LegalAction) else str(out)

    def message(self, raw: dict):
        out = self.agent.choose_message(GameState.from_mcp_state(raw), self._ctx())
        if out is TERMINATE_MESSAGING or out is None:
            return None
        if isinstance(out, SendMessage):
            return {"content": out.content, "recipients": list(out.recipients or [])}
        raise TypeError(f"choose_message answered {out!r}")   # the real runner raises DecisionError


class OfficialBaseline(Seat):
    """`legal_actions[0]` and the runner's default auto-terminate for chat (examples/basic_agent.py, smoke_agent.py)."""
    name = "official_baseline"

    def choose(self, raw: dict) -> str:
        return raw["legal_actions"]["actions"][0]["action_id"]


class RandomSeat(Seat):
    name = "random"

    def choose(self, raw: dict) -> str:
        return (self.rng or random).choice(raw["legal_actions"]["actions"])["action_id"]


@dataclass
class Game:
    seats: list[Seat]
    rng: random.Random
    roles: list[str] = field(default_factory=list)
    alive: list[bool] = field(default_factory=lambda: [True] * N)
    day: int = 1
    dead: list[dict] = field(default_factory=list)
    votes: list[dict] = field(default_factory=list)
    invest: dict[int, str] = field(default_factory=dict)
    messages: list[dict] = field(default_factory=list)
    stall: int = 0
    winner: str | None = None
    version: int = 0
    window_start_seq: int = 0
    terminated: list[int] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    broken: list[bool] = field(default_factory=lambda: [False] * N)     # process gone: platform auto-plays the seat
    chat_order: str = "shuffle"       # "shuffle": a fresh random speaking order every round (agents run concurrently
    #                                   on the platform); "seat": seat order
    stats: dict = field(default_factory=lambda: {k: [0] * N for k in (
        "decisions", "crash", "illegal", "bad_chat", "timeout", "chat_after_terminate", "chats", "dms")}
        | {"max_ms": [0.0] * N})

    # ---- setup -----------------------------------------------------------
    def __post_init__(self):
        self.roles = deal_roles(self.rng)
        base = self.rng.getrandbits(64)
        self.order_seed = self.rng.getrandbits(64)
        for i, s in enumerate(self.seats):
            s.rng = random.Random(base * 8 + i)
            s.reset()

    @property
    def names(self):
        return [f"agent{i}" for i in range(N)]

    def wolves(self):
        return [i for i in range(N) if self.roles[i] == "wolf" and self.alive[i]]

    def living(self):
        return [i for i in range(N) if self.alive[i]]

    # ---- state payload -----------------------------------------------------
    def state(self, seat: int, phase: str, sub: str | None, actions: list[int] | None = None) -> dict:
        role = self.roles[seat]
        lines = ["=== Werewolf ===", f"Your role: {role.upper()}"]
        if role == "wolf":
            allies = [i for i in range(N) if self.roles[i] == "wolf" and i != seat]
            lines.append("Your fellow wolves: " + ", ".join(f"Player{i}" for i in allies))
        elif role == "seer":
            inv = ", ".join(f"Player{t} is {r}" for t, r in self.invest.items()) or "none yet"
            lines.append(f"Your investigations: {inv}")
        else:
            lines.append("You do not know anyone else's role.")
        if not self.alive[seat]:
            lines.append("You are DEAD. You may watch, but not act.")
        lines.append(f"Day {self.day} | phase {sub} | alive {len(self.living())}/{N}")
        lines.append("Living: " + ", ".join(f"Player{i}" for i in self.living()))
        lines.append("Dead:")
        for d in self.dead:
            lines.append(f"  day {d['day']}: Player{d['seat']} was {d['role']} ({d['cause']})")
        for v in self.votes:
            vs = ", ".join(f"Player{a} -> " + (f"Player{b}" if b is not None else "abstain") for a, b in v["votes"].items())
            lines.append(f"Day {v['day']} vote: {vs} — " + (f"Player{v['eliminated']} lynched" if v["eliminated"] is not None else "nobody lynched"))
        legal = [{"action_id": str(a), "label": _label(sub, a),
                  "input": {"session_id": "local", "action_id": str(a), "state_version": self.version}}
                 for a in (actions or [])]
        visible = [m for m in self.messages[self.window_start_seq:]
                   if m["visibility"] == "broadcast" or m["sender"] == seat or seat in m["recipients"]]
        return {
            "session_id": "local", "game_type": "werewolf", "runtime_adapter": "openspiel", "status": "in_progress",
            "state_version": self.version, "phase": phase, "observation": "\n".join(lines),
            "is_terminal": False, "is_current_actor": bool(actions), "current_actor": None,
            "legal_actions": {"session_id": "local", "state_version": self.version, "actions": legal} if actions else None,
            "next_actions": [], "your_position": seat,
            "players": [{"position": i, "name": self.names[i], "agent_id": f"id{i}"} for i in range(N)],
            "game_state": {"day": self.day, "phase": sub,
                           "alive": [self.names[i] for i in self.living()],
                           "dead": [{"day": d["day"], "player": self.names[d["seat"]], "role": d["role"], "cause": d["cause"]} for d in self.dead],
                           "vote_history": [{"day": v["day"],
                                             "votes": {self.names[a]: (self.names[b] if b is not None else "abstain") for a, b in v["votes"].items()},
                                             "eliminated": self.names[v["eliminated"]] if v["eliminated"] is not None else None} for v in self.votes],
                           "winner": self.winner},
            "eliminated": not self.alive[seat], "messaging_enabled": True, "new_messages": visible,
            "terminated_messaging": list(self.terminated),
        }

    def _timed(self, seat: int, fn):
        t0 = time.perf_counter()
        try:
            return fn(), None
        except Exception as e:                 # noqa: BLE001 - a crashed seat is data here
            return None, e
        finally:
            ms = 1000 * (time.perf_counter() - t0)
            self.stats["decisions"][seat] += 1
            self.stats["max_ms"][seat] = max(self.stats["max_ms"][seat], ms)
            if ms > 1000 * IDLE_LIMIT_S:
                self.stats["timeout"][seat] += 1
                self.broken[seat] = True

    def _choose(self, seat: int, sub: str, legal: list[int]) -> int:
        auto = ABSTAIN if sub == "day_vote" and ABSTAIN in legal else legal[0]   # the platform's idle auto-action
        if self.broken[seat]:
            self.version += 1
            return auto
        raw = self.state(seat, "moving", sub, legal)
        out, err = self._timed(seat, lambda: self.seats[seat].choose(raw))
        a = auto
        if err is not None:
            self.stats["crash"][seat] += 1
            self.broken[seat] = True
        elif not self.broken[seat]:
            try:
                a = int(out)
            except (TypeError, ValueError):
                a = -1
            if a not in legal:                  # the runner raises on an illegal id: process gone
                self.stats["illegal"][seat] += 1
                self.broken[seat] = True
                a = auto
        self.version += 1
        return a

    # ---- phases ----------------------------------------------------------
    def night(self) -> None:
        kills = []
        for w in self.wolves():
            legal = [i for i in self.living() if self.roles[i] != "wolf"]
            kills.append(self._choose(w, "night_wolf", legal))
        seer = next((i for i in range(N) if self.roles[i] == "seer" and self.alive[i]), None)
        if seer is not None:
            legal = [i for i in self.living() if i != seer]
            t = self._choose(seer, "night_seer", legal)
            self.invest[t] = "wolf" if self.roles[t] == "wolf" else "villager"
        if self.day > 1 and kills:
            victim = min(kills)                      # disagreement -> lower seat
            self._kill(victim, "night_kill")

    def _chat_ok(self, seat: int, m, sent: int) -> bool:
        if not isinstance(m, dict):
            return False
        words = str(m.get("content", "")).split()
        rec = list(m.get("recipients") or [])
        return bool(words) and len(words) <= MAX_WORDS and sent < MAX_CHATS and len(rec) <= 1 \
            and all(isinstance(r, int) and 0 <= r < N and r != seat for r in rec)

    def discussion(self) -> None:
        self.window_start_seq = len(self.messages)
        self.terminated = [i for i in range(N) if not self.alive[i]]
        sent = {i: 0 for i in range(N)}
        for _round in range(ROUNDS):
            order = self.living()
            if self.chat_order == "shuffle":        # same order for every agent under test (paired comparisons)
                random.Random(f"{self.order_seed}:{self.day}:{_round}").shuffle(order)
            for seat in order:
                done = seat in self.terminated
                if self.broken[seat]:
                    if not done:
                        self.terminated.append(seat)
                    continue
                if done and not isinstance(self.seats[seat], ContestantSeat):
                    continue                           # reference seats: the runner's idempotent re-terminate
                raw = self.state(seat, "messaging", "day_vote")
                m, err = self._timed(seat, lambda: self.seats[seat].message(raw))
                if err is not None:
                    self.stats["crash"][seat] += 1
                    self.broken[seat] = True
                    m = None
                if m is None:
                    if not done:
                        self.terminated.append(seat)
                    continue
                if done:
                    self.stats["chat_after_terminate"][seat] += 1
                    continue
                if not self._chat_ok(seat, m, sent[seat]):
                    self.stats["bad_chat"][seat] += 1      # the platform rejects it; the runner raises: process gone
                    self.broken[seat] = True
                    self.terminated.append(seat)
                    continue
                rec = list(m.get("recipients") or [])
                self.messages.append({"seq": len(self.messages), "sender": seat, "recipients": rec,
                                      "visibility": "direct" if rec else "broadcast", "type": "chat",
                                      "content": " ".join(str(m.get("content", "")).split()), "day": self.day})
                sent[seat] += 1
                self.stats["dms" if rec else "chats"][seat] += 1
            if all(i in self.terminated for i in self.living()):
                break
        for seat in self.living():                     # idle timeout closes the window
            if seat not in self.terminated:
                self.terminated.append(seat)

    def vote(self) -> None:
        ballots: dict[int, int | None] = {}
        for seat in self.living():
            legal = [i for i in self.living() if i != seat] + [ABSTAIN]
            a = self._choose(seat, "day_vote", legal)
            ballots[seat] = None if a == ABSTAIN else a
        tally: dict[int, int] = {}
        for t in ballots.values():
            if t is not None:
                tally[t] = tally.get(t, 0) + 1
        eliminated = None
        if tally:
            top = max(tally.values())
            leaders = [s for s, c in tally.items() if c == top]
            if len(leaders) == 1:
                eliminated = leaders[0]
        self.votes.append({"day": self.day, "votes": ballots, "eliminated": eliminated})
        if eliminated is None:
            self.stall += 1
        else:
            self.stall = 0
            self._kill(eliminated, "lynch")

    def _kill(self, seat: int, cause: str) -> None:
        self.alive[seat] = False
        self.dead.append({"day": self.day, "seat": seat, "role": self.roles[seat], "cause": cause})

    def check(self) -> bool:
        w = len(self.wolves())
        v = len(self.living()) - w
        if w == 0:
            self.winner = "villager"
        elif w >= v or self.stall >= 3:
            self.winner = "wolf"
        return self.winner is not None

    def play(self) -> dict[int, float]:
        while True:
            self.night()
            if self.check():
                break
            self.discussion()
            self.vote()
            if self.check():
                break
            self.day += 1
        side = {"wolf": "wolf", "seer": "villager", "villager": "villager"}
        return {i: (1.0 if side[self.roles[i]] == self.winner else -1.0) for i in range(N)}


def deal_roles(rng: random.Random) -> list[str]:
    """The seat -> role deal a Game built on `rng` gets (call it on a fresh copy to know the roles in advance)."""
    roles = ["wolf", "wolf", "seer", "villager", "villager", "villager", "villager"]
    rng.shuffle(roles)
    return roles


def _label(sub: str | None, a: int) -> str:
    if a == ABSTAIN:
        return "Abstain"
    return {"night_wolf": "Kill", "night_seer": "Investigate", "day_vote": "Vote to lynch"}.get(sub or "", "") + f" Player{a}"
