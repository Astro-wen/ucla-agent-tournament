"""v4.2.4 (SDK PR #7): a move or chat the runner could not send for a temporary reason (a dropped connection, a 5xx,
RUNTIME_TEMPORARILY_UNAVAILABLE, TOO_MANY_WAITS) no longer ends the match process. The runner never sends it again
blindly (it may have landed): it pauses, re-reads the state and asks the agent again. Driven here through the real
altruagent.runner.run_game and a scripted game session:

  * the agent decides again from the re-read state (a Werewolf vote, a Pokemon doubles turn), and the move goes out
    once more with that state's version;
  * a Werewolf chat that did not reach the server (the re-read state's new_messages does not show it) is not counted:
    it is sent again (the never-twice rule and the 5-chat quota do not hold it back), and when it can no longer be
    sent the saved match state (agent/matchstate.py, what a restarted worker resumes from) does not count it either;
  * a chat that did land (the re-read state shows it) is never sent twice.
"""
import json
from pathlib import Path

import pytest
from altruagent import DecisionContext, GameState, LegalAction, SendMessage
from altruagent.mcp_transport import MCPToolError
from altruagent.runner import run_game

from agent import matchstate
from agent.agent import Contestant, doubles_ok
from tests.test_runner import FakeMCPGameSession, result_dict

ME = 3
NAMES = ["alice", "bob", "carl", "dave", "erin", "fran", "gus"]
CTX = DecisionContext(session_id="ww-hiccup", tournament_id=None, game_type="werewolf", agent_id="a1",
                      seat_position=ME)
ROOT = Path(__file__).resolve().parents[2]

# the temporary failures the runner retries in place (altruagent.runner._is_transient)
HICCUPS = {
    "connection_dropped": lambda: MCPToolError("MCP request failed: connection reset", status_code=None),
    "gateway_503": lambda: MCPToolError("MCP request failed with HTTP 503", status_code=503),
    "engine_busy": lambda: MCPToolError("RUNTIME_TEMPORARILY_UNAVAILABLE: busy", status_code=None,
                                        error_code="RUNTIME_TEMPORARILY_UNAVAILABLE"),
    "too_many_waits": lambda: MCPToolError("TOO_MANY_WAITS: 2 waits", status_code=None, error_code="TOO_MANY_WAITS"),
}


class Game(FakeMCPGameSession):
    """The runner's scripted game session, taking play_action's reasoning_summary as MCPGameSession does (our moves
    carry a public one)."""

    def play_action(self, *, action_id=None, action=None, state_version, reasoning_summary=None) -> dict:
        self.play_action_calls.append({"action_id": action_id, "action": action, "state_version": state_version,
                                       "reasoning_summary": reasoning_summary})
        return self._pop(self._play_action_queue)


@pytest.fixture(autouse=True)
def _not_listening(monkeypatch):
    """These games script every state the runner reads; Werewolf listening (on by default since v4.3) would read more
    of them. Listening's own runner tests, a lost follow-up included, are in test_werewolf_listen.py."""
    monkeypatch.setenv("ARENA_WW_LISTEN", "off")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("ARENA_STATE", "1")
    monkeypatch.setenv("ARENA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ARENA_RECORD", "0")
    monkeypatch.setattr(matchstate, "_dir", None)
    return tmp_path / "state"


def ww_raw(*, version, phase="messaging", sub="day_vote", msgs=(), actions=(), terminated=(), terminal=False):
    """A Werewolf state of seat ME (a villager), shaped like the platform's get_game_state payload: day 1, our own
    chats in new_messages like everyone's (the whole open window)."""
    new = [{"seq": i, "sender": s, "recipients": [], "visibility": "broadcast", "type": "chat", "content": t}
           for i, (s, t) in enumerate(msgs)]
    return {"session_id": "ww-hiccup", "game_type": "werewolf", "status": "completed" if terminal else "in_progress",
            "state_version": version, "phase": phase, "is_terminal": terminal,
            "is_current_actor": bool(actions) and not terminal, "current_actor": None,
            "observation": f"=== Werewolf ===\nYour role: VILLAGER\nDay 1 | phase {sub}", "your_position": ME,
            "players": [{"position": i, "name": n, "agent_id": f"id{i}"} for i, n in enumerate(NAMES)],
            "game_state": {"day": 1, "phase": sub, "alive": list(NAMES), "dead": [], "vote_history": [],
                           "winner": None},
            "eliminated": False, "messaging_enabled": True, "new_messages": new if phase == "messaging" else [],
            "terminated_messaging": list(terminated),
            "legal_actions": {"state_version": version, "actions": [
                {"action_id": str(a), "label": f"Vote to eliminate Player{a}" if a != 7 else "Abstain",
                 "input": {"action_id": str(a), "state_version": version}} for a in actions]} if actions else None,
            "next_actions": []}


def ww_state(**kw) -> GameState:
    return GameState.from_mcp_state(ww_raw(**kw))


class Spy:
    """The Contestant as the runner sees it, plus what it was asked with and what it saved after each decision."""

    def __init__(self, agent, ctx=CTX):
        self.agent, self.ctx = agent, ctx
        self.asked: list[GameState] = []
        self.answers: list = []
        self.saved: list = []

    def _after(self, state, out):
        self.asked.append(state)
        self.answers.append(out)
        self.saved.append(matchstate.load(self.ctx.agent_id, self.ctx.session_id, ME) if matchstate.enabled() else None)
        return out

    def choose_action(self, state, context):
        return self._after(state, self.agent.choose_action(state, context))

    def choose_message(self, state, context):
        return self._after(state, self.agent.choose_message(state, context))


def talky(lines):
    """Our Contestant with a chat policy that opens the day with `lines` (the real WerewolfPolicy otherwise)."""
    c = Contestant()
    c.ww.chat = lambda st, _lines=list(lines): [{"content": t, "recipients": []} for t in _lines]
    c.ww.reactive_chat = False
    return c


def chats(game) -> list[str]:
    return [c["content"] for c in game.send_message_calls if c["message_type"] == "chat"]


OTHER = (5, "hello table, any seer here?")
A = "I am a plain villager. Player5 asked first, so I am watching Player5."
B = "Let us not vote blind today: whoever claims seer, tell us your check."


@pytest.mark.parametrize("hiccup", sorted(HICCUPS))
def test_a_chat_that_did_not_reach_the_server_is_sent_again_and_counted_once(hiccup, store):
    game = Game(session_id="ww-hiccup")
    game.queue_state(
        ww_state(version=10, msgs=[OTHER]),                                # 1st read: the window opens
        ww_state(version=11, msgs=[OTHER, (ME, A)]),                       # the wait after A: A shows (delivered)
        ww_state(version=11, msgs=[OTHER, (ME, A)]),                       # the re-read after B failed: B is not there
        ww_state(version=12, msgs=[OTHER, (ME, A), (ME, B)]),              # the wait after B (again): B shows
        ww_state(version=13, phase="moving", terminal=True),               # after our terminate: the game ends
    )
    game.queue_send_message({"phase": "messaging", "message": {"seq": 1, "sender": ME, "type": "chat", "content": A}},
                            HICCUPS[hiccup](),
                            {"phase": "messaging", "message": {"seq": 2, "sender": ME, "type": "chat", "content": B}},
                            {"phase": "moving"})
    game.queue_result(result_dict())
    agent = talky([A, B])
    spy = Spy(agent)
    log: list[str] = []

    run_game(game, CTX, spy, sleep=lambda s: None, log=log.append)

    assert chats(game) == [A, B, B]                    # B decided again from the re-read state and sent once more
    assert game.send_message_calls[-1]["message_type"] == "terminate"
    assert spy.asked[2].state_version == 11 and spy.answers[2].content == B
    assert agent._chat_sent == 2 and agent.undelivered == 1          # counted: what reached the server
    assert agent._chat_said == {(A, ()), (B, ())}
    saved = spy.saved[-1]["chat"]
    assert saved["sent"] == 2 and saved["undelivered"] == 1 and saved["pending"] is None
    assert any("retrying for up to 90s" in line for line in log)    # the runner's own line, once
    assert any("game goes on" in line for line in log)


def test_an_undelivered_chat_that_can_no_longer_be_sent_is_not_counted_in_the_saved_state(store):
    """The re-read state lists us as terminated (the platform's idle auto-terminate during a long outage): nothing more
    may be said, so B is never sent again, and the match state a restarted worker would resume from counts A only."""
    game = Game(session_id="ww-hiccup")
    game.queue_state(
        ww_state(version=10, msgs=[OTHER]),
        ww_state(version=11, msgs=[OTHER, (ME, A)]),
        ww_state(version=12, msgs=[OTHER, (ME, A)], terminated=[ME]),      # B never landed; we were terminated
        ww_state(version=13, phase="moving", terminal=True),
    )
    game.queue_send_message({"phase": "messaging", "message": {"seq": 1}}, HICCUPS["connection_dropped"](),
                            {"phase": "moving"})
    game.queue_result(result_dict())
    agent = talky([A, B])
    spy = Spy(agent)

    run_game(game, CTX, spy, sleep=lambda s: None, log=lambda m: None)

    assert chats(game) == [A, B]                       # B was tried once and never again
    assert game.send_message_calls[-1]["message_type"] == "terminate"
    after_b = spy.saved[1]["chat"]                     # saved right after deciding B: counted while in flight
    assert after_b["sent"] == 2 and after_b["pending"]["text"] == B
    after = spy.saved[2]["chat"]                       # saved after the re-read: B not counted, not "said"
    assert after["sent"] == 1 and after["said"] == {(A, ())} and after["undelivered"] == 1
    assert after["done"] is True and after["pending"] is None
    assert agent._chat_sent == 1


def test_a_chat_that_landed_although_its_answer_was_lost_is_never_sent_twice(store):
    """The send failed on our side (the answer was lost), but the server had taken it: the re-read state shows B, so
    B counts and the next answer is the terminate."""
    game = Game(session_id="ww-hiccup")
    game.queue_state(
        ww_state(version=10, msgs=[OTHER]),
        ww_state(version=11, msgs=[OTHER, (ME, A)]),
        ww_state(version=12, msgs=[OTHER, (ME, A), (ME, B)]),              # the re-read: B is there
        ww_state(version=13, phase="moving", terminal=True),
    )
    game.queue_send_message({"phase": "messaging", "message": {"seq": 1}}, HICCUPS["gateway_503"](),
                            {"phase": "moving"})
    game.queue_result(result_dict())
    agent = talky([A, B])

    run_game(game, CTX, Spy(agent), sleep=lambda s: None, log=lambda m: None)

    assert chats(game) == [A, B]
    assert game.send_message_calls[-1]["message_type"] == "terminate"
    assert agent._chat_sent == 2 and agent.undelivered == 0


def test_before_any_chat_of_ours_came_back_a_failed_chat_stays_counted(store):
    """Our own chats are only trusted to show in new_messages once this match has shown one (a platform that left
    them out would otherwise get every chat again until the quota refused one): the very first chat failing stays
    counted, as before v4.2.4, and nothing is sent twice."""
    game = Game(session_id="ww-hiccup")
    game.queue_state(
        ww_state(version=10, msgs=[OTHER]),
        ww_state(version=10, msgs=[OTHER]),                                # the re-read after A failed
        ww_state(version=11, msgs=[OTHER, (ME, B)]),
        ww_state(version=12, phase="moving", terminal=True),
    )
    game.queue_send_message(HICCUPS["engine_busy"](), {"phase": "messaging", "message": {"seq": 1}},
                            {"phase": "moving"})
    game.queue_result(result_dict())
    agent = talky([A, B])

    run_game(game, CTX, Spy(agent), sleep=lambda s: None, log=lambda m: None)

    assert chats(game) == [A, B]
    assert agent._chat_sent == 2 and agent.undelivered == 0


@pytest.mark.parametrize("hiccup", ["connection_dropped", "engine_busy"])
def test_a_vote_that_could_not_be_sent_is_decided_again_from_the_re_read_state(hiccup, store):
    game = Game(session_id="ww-hiccup")
    vote = dict(phase="moving", sub="day_vote", actions=[0, 1, 2, 4, 5, 6, 7])
    game.queue_state(ww_state(version=20, **vote),
                     ww_state(version=20, **vote),                         # the re-read: still our vote to cast
                     ww_state(version=21, phase="moving", terminal=True))
    game.queue_play_action(HICCUPS[hiccup](), {"accepted": True, "status": "in_progress"})
    game.queue_result(result_dict())
    agent = Contestant()
    spy = Spy(agent)

    run_game(game, CTX, spy, sleep=lambda s: None, log=lambda m: None)

    assert len(spy.asked) == 2 and spy.asked[1] is not spy.asked[0]  # asked again, with the re-read state
    first, second = (a.action if hasattr(a, "action") else a for a in spy.answers)
    assert isinstance(first, LegalAction) and isinstance(second, LegalAction)
    assert first.action_id == second.action_id         # the same state, the same vote
    assert [c["action_id"] for c in game.play_action_calls] == [first.action_id] * 2
    assert [c["state_version"] for c in game.play_action_calls] == [20, 20]
    assert agent.decisions == 2 and spy.saved[-1]["counters"]["decisions"] == 2


def _recorded_doubles_turn() -> dict:
    """A real platform doubles battle state we answered (logs/yoriko, the v4 match the match-state tests use)."""
    for line in open(ROOT / "logs" / "yoriko" / "0495293f-a47a-4bf6-bdc3-00acaba14709.jsonl"):
        rec = json.loads(line)
        acts = ((rec.get("raw") or {}).get("legal_actions") or {}).get("actions") or []
        if rec.get("kind") == "action" and any(((a.get("input") or {}).get("action") or {}).get("type")
                                               == "doubles_turn" for a in acts):
            return rec["raw"]
    raise AssertionError("no recorded doubles turn")


def test_a_pokemon_turn_that_could_not_be_sent_is_decided_again_and_sent_with_the_re_read_version(store):
    raw = _recorded_doubles_turn()
    again = {**raw, "state_version": raw.get("state_version")}
    done = {**raw, "is_terminal": True, "is_current_actor": False, "legal_actions": None, "status": "completed"}
    ctx = DecisionContext(session_id=raw.get("session_id") or "pk-hiccup", tournament_id=None,
                          game_type=raw.get("game_type") or "pokemon_vgc_doubles_draft", agent_id="a1", seat_position=0)
    game = Game(session_id=ctx.session_id)
    game.queue_state(GameState.from_mcp_state(raw), GameState.from_mcp_state(again), GameState.from_mcp_state(done))
    game.queue_play_action(HICCUPS["gateway_503"](), {"accepted": True, "status": "in_progress"})
    game.queue_result(result_dict())
    agent = Contestant()

    run_game(game, ctx, agent, sleep=lambda s: None, log=lambda m: None)

    assert len(game.play_action_calls) == 2 and agent.decisions == 2
    for call in game.play_action_calls:                # each answer a legal pair for the state it was decided on
        assert call["state_version"] == raw.get("state_version")
        assert call["action"]["type"] == "doubles_turn" and doubles_ok(raw, call["action"])
