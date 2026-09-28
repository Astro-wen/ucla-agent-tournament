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
"""
from __future__ import annotations

import os
import random
from dataclasses import dataclass, field

from altruagent import TERMINATE_MESSAGING, DecisionContext, GameState, LegalAction, SendMessage, WithReasoning
from altruagent.runner import _validate_decision

os.environ["ARENA_RECORD"] = "0"      # never record simulated games (agent/agent.py record())
N = 7
ABSTAIN = 7


class Seat:
    """Agent interface: reset(); vote/kill/investigate via choose(raw) -> action_id str; chat via message(raw)."""
    name = "seat"

    def reset(self):
        pass

    def choose(self, raw: dict) -> str:
        raise NotImplementedError

    def message(self, raw: dict):
        return None            # None = terminate


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
        return None


class OfficialBaseline(Seat):
    """`legal_actions[0]` and the runner's default auto-terminate for chat."""
    name = "official_baseline"

    def choose(self, raw: dict) -> str:
        return raw["legal_actions"]["actions"][0]["action_id"]


class RandomSeat(Seat):
    name = "random"

    def choose(self, raw: dict) -> str:
        return random.choice(raw["legal_actions"]["actions"])["action_id"]


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

    # ---- setup -----------------------------------------------------------
    def __post_init__(self):
        roles = ["wolf", "wolf", "seer", "villager", "villager", "villager", "villager"]
        self.rng.shuffle(roles)
        self.roles = roles
        for s in self.seats:
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

    def _choose(self, seat: int, sub: str, legal: list[int]) -> int:
        raw = self.state(seat, "moving", sub, legal)
        try:
            a = int(self.seats[seat].choose(raw))
        except Exception:
            a = legal[0]
        if a not in legal:
            a = legal[0]
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

    def discussion(self) -> None:
        self.window_start_seq = len(self.messages)
        self.terminated = [i for i in range(N) if not self.alive[i]]
        sent = {i: 0 for i in range(N)}
        for _round in range(6):
            for seat in self.living():
                if seat in self.terminated:
                    continue
                raw = self.state(seat, "messaging", "day_vote")
                try:
                    m = self.seats[seat].message(raw)
                except Exception:
                    m = None
                if m is None or sent[seat] >= 5:
                    self.terminated.append(seat)
                    continue
                words = str(m.get("content", "")).split()
                rec = list(m.get("recipients") or [])
                if not words or len(words) > 50 or len(rec) > 1 or (rec and (rec[0] == seat or not 0 <= rec[0] < N)):
                    self.terminated.append(seat)      # platform would reject; runner would die -> auto-terminate
                    continue
                self.messages.append({"seq": len(self.messages), "sender": seat, "recipients": rec,
                                      "visibility": "direct" if rec else "broadcast", "type": "chat",
                                      "content": " ".join(words), "day": self.day})
                sent[seat] += 1
            if all(i in self.terminated for i in self.living()):
                break

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


def _label(sub: str | None, a: int) -> str:
    if a == ABSTAIN:
        return "Abstain"
    return {"night_wolf": "Kill", "night_seer": "Investigate", "day_vote": "Vote to lynch"}.get(sub or "", "") + f" Player{a}"
