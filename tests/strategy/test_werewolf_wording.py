"""Werewolf wording 10-04/05 (docs/platform/werewolf.md): "lynch" became "eliminate" / "vote".

Both wordings must be read (we have never seen a real Werewolf payload; the docs may lead or lag the server), a death's
cause is one internal value, the local simulators speak the new wording by default (ARENA_WW_WORDING=old for the old
one), new_messages is empty outside a discussion window in the platform stand-in, a game may have no discussion at
all (messaging_enabled false), and no chat of ours is ever over the 1024-character cap.
"""
import logging
import random

import pytest
from altruagent import TERMINATE_MESSAGING, DecisionContext, GameState, SendMessage

from agent.agent import MAX_MESSAGE_CHARS, Contestant, clip_chat, create_agent
from agent.werewolf.policy import WerewolfPolicy, chat_guard
from agent.werewolf.state import CAUSE_NIGHT, CAUSE_VOTE, night_evidence, norm_cause, parse
from local import werewolf_env as we
from local.werewolf_bots import HeuristicSeat, LLMQwenSeat, LLMSeat
from local.werewolf_platform import SeatAgent, Server, play_table


def _record(wording: str, same_names: bool = False, winner=None):
    """A day-2 vote state of a game with one elimination by vote and one night kill, in `wording`."""
    g = we.Game([we.OfficialBaseline() for _ in range(7)], random.Random(3), wording=wording)
    g._kill(1, "vote")
    g.votes.append({"day": 1, "votes": {0: 1, 1: None, 2: 1, 3: 1, 4: 6, 5: 1, 6: 0}, "eliminated": 1})
    g.day = 2
    g._kill(5, "night_kill")
    g.votes.append({"day": 2, "votes": {0: 6, 2: 6, 3: 4, 4: 3, 6: 2}, "eliminated": None})
    g.winner = winner
    raw = g.state(3, "moving", "day_vote", [0, 2, 4, 6, 7])
    return g, (we.same_names(raw) if same_names else raw)


# ---------------------------------------------------------------- the deployed parser reads both wordings
@pytest.mark.parametrize("wording", ["new", "old"])
@pytest.mark.parametrize("same_names", [False, True])
def test_public_record_reads_alike_in_both_wordings(wording, same_names):
    g, raw = _record(wording, same_names)
    v = parse(raw)
    assert v.dead == {1: g.roles[1], 5: g.roles[5]}
    assert v.dead_cause == {1: CAUSE_VOTE, 5: CAUSE_NIGHT}
    assert v.votes == [(1, {0: 1, 1: None, 2: 1, 3: 1, 4: 6, 5: 1, 6: 0}, 1),
                       (2, {0: 6, 2: 6, 3: 4, 4: 3, 6: 2}, None)]
    assert v.alive == [0, 2, 3, 4, 6] and (v.day, v.sub_phase) == (2, "day_vote")


def test_new_wording_is_what_the_docs_show():
    _, raw = _record("new", winner="wolf")
    obs = raw["observation"]
    assert "  day 1: Player1 was" in obs and "(eliminated by vote)" in obs and "(killed at night)" in obs
    assert "— Player1 eliminated by vote" in obs and obs.rstrip().endswith("Game over — WOLVES win")
    assert {d["cause"] for d in raw["game_state"]["dead"]} == {"vote", "night_kill"}
    assert [a["label"] for a in raw["legal_actions"]["actions"]][:2] == ["Vote to eliminate Player0",
                                                                         "Vote to eliminate Player2"]
    _, old = _record("old", winner="wolf")
    assert "(lynch)" in old["observation"] and "Player1 lynched" in old["observation"]
    assert "Game over" not in old["observation"] and "Vote to lynch Player0" in str(old["legal_actions"])
    assert {d["cause"] for d in old["game_state"]["dead"]} == {"lynch", "night_kill"}


@pytest.mark.parametrize("cause,want", [
    ("lynch", CAUSE_VOTE), ("vote", CAUSE_VOTE), ("LYNCH", CAUSE_VOTE), ("eliminated by vote", CAUSE_VOTE),
    ("lynched", CAUSE_VOTE), ("voted out", CAUSE_VOTE), ("night_kill", CAUSE_NIGHT), ("killed at night", CAUSE_NIGHT),
    ("Night_Kill", CAUSE_NIGHT), ("night kill", CAUSE_NIGHT), ("killed by wolves", CAUSE_NIGHT),
    ("resignation", "resignation"), (None, "?"), ("", "?"), (3, "?"),
])
def test_cause_is_one_internal_value(cause, want):
    assert norm_cause(cause) == want


@pytest.mark.parametrize("cause", ["lynch", "vote"])
def test_structured_cause_in_either_wording_gives_the_same_view_and_decision(cause):
    """The structured path: only the cause's spelling differs, the view and every decision are the same."""
    names = ["alice", "bob", "carl", "dave", "erin", "fran", "gus"]

    def st(c):
        return {"observation": "=== Werewolf ===\nYour role: VILLAGER\nDay 3 | phase day_vote | alive 4/7",
                "your_position": 3, "phase": "moving", "state_version": 9, "new_messages": [],
                "players": [{"position": i, "name": n} for i, n in enumerate(names)],
                "game_state": {"day": 3, "phase": "day_vote", "alive": ["alice", "dave", "erin", "gus"],
                               "dead": [{"day": 1, "player": "bob", "role": "villager", "cause": c},
                                        {"day": 2, "player": "carl", "role": "seer", "cause": "night_kill"},
                                        {"day": 2, "player": "fran", "role": "villager", "cause": c}],
                               "vote_history": [{"day": 1, "votes": {"alice": "bob", "dave": "bob", "erin": "gus",
                                                                     "gus": "fran"}, "eliminated": "bob"},
                                                {"day": 2, "votes": {"alice": "fran", "erin": "fran", "gus": "carl",
                                                                     "dave": "alice"}, "eliminated": "fran"}],
                               "winner": None},
                "legal_actions": {"actions": [{"action_id": str(a), "label": f"Vote to eliminate Player{a}"}
                                              for a in (0, 4, 6)] + [{"action_id": "7", "label": "Abstain"}]}}
    v = parse(st(cause))
    assert v.dead_cause == {1: CAUSE_VOTE, 2: CAUSE_NIGHT, 5: CAUSE_VOTE}
    assert WerewolfPolicy().act(st(cause)) == WerewolfPolicy().act(st("lynch"))


def test_seer_before_its_first_check():
    obs = "=== Werewolf ===\nYour role: SEER\nYou have not investigated anyone yet.\nDay 1 | phase night_seer | alive 7/7"
    raw = {"observation": obs, "your_position": 2, "game_state": {"day": 1, "phase": "night_seer"},
           "legal_actions": {"actions": [{"action_id": str(a), "label": f"Investigate Player{a}"}
                                         for a in (0, 1, 3, 4, 5, 6)]}}
    v = parse(raw)
    assert (v.role, v.investigations, v.evidence.hint) == ("seer", {}, "seer")
    # without a recognised role line the sentence alone is a seer cue, like an investigations line
    v = parse({"observation": "=== Werewolf ===\nYou have not investigated anyone yet.\nDay 1 | phase night_seer",
               "your_position": 2})
    assert v.role == "seer"


def test_game_over_line_and_eliminate_labels_change_no_role(caplog):
    _, raw = _record("new", winner="villager")
    assert raw["observation"].rstrip().endswith("Game over — VILLAGERS win")
    with caplog.at_level(logging.ERROR, logger="agent.werewolf"):
        v = WerewolfPolicy()._view(raw)
    assert v.role == "villager" and not caplog.records
    # "Vote to eliminate" is a day label: no night evidence
    assert night_evidence(raw, "day_vote", v.alive, 3) == (None, None)


# ---------------------------------------------------------------- simulators: new wording by default
def test_wording_switch(monkeypatch):
    monkeypatch.delenv(we.WORDING_ENV, raising=False)
    assert we.Game([we.Seat() for _ in range(7)], random.Random(1)).wording == "new"
    monkeypatch.setenv(we.WORDING_ENV, "old")
    assert we.Game([we.Seat() for _ in range(7)], random.Random(1)).wording == "old"
    assert Server(1).g.wording == "old"
    monkeypatch.setenv(we.WORDING_ENV, "new")
    assert Server(1).g.wording == "new"


def _play(wording, seed, same=False, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setenv(we.SAME_NAMES_ENV, "on" if same else "off")
    seats = [we.ContestantSeat(create_agent), HeuristicSeat(), we.ContestantSeat(create_agent), LLMSeat(),
             LLMQwenSeat(), we.ContestantSeat(create_agent), we.RandomSeat()]
    g = we.Game(seats, random.Random(seed), wording=wording)
    g.play()
    fails = sum(g.stats[k][i] for k in ("crash", "illegal", "bad_chat", "timeout", "chat_after_terminate")
                for i in range(7))
    return (g.winner, g.day, [(d["seat"], d["cause"]) for d in g.dead], g.votes,
            [(m["sender"], m["content"], m["recipients"]) for m in g.messages]), fails


@pytest.mark.parametrize("same", [False, True])
def test_whole_games_are_identical_in_both_wordings(monkeypatch, caplog, same):
    """End to end: our agent (and the stand-ins) play every game the same in the old and new wording, also when it has
    to read the seat-labelled text (one display name for every seat)."""
    with caplog.at_level(logging.ERROR, logger="agent.werewolf"):
        for seed in range(6):
            new, f_new = _play("new", seed, same, monkeypatch)
            old, f_old = _play("old", seed, same, monkeypatch)
            assert new == old and f_new == f_old == 0
    assert not [r for r in caplog.records if "role check" in r.getMessage()]


def test_platform_stand_in_empties_new_messages_outside_the_window():
    s = Server(4)
    seen = {"messaging": 0, "moving": 0}
    with s.cv:
        # play the night through: the first state of the day is the open window
        while s.phase != "messaging" and not s.terminal:
            seat = s.queue[0]
            s.play_action(seat, s._legal(seat)[0], s.g.version)
        assert s.state(0)["messaging_seconds_left"] in (119, 120) and s.state(0)["messaging_deadline"]
        s.send_message(0, "chat", "Player3 looks suspicious to me.", [])
        assert [m["content"] for m in s.state(1)["new_messages"]] == ["Player3 looks suspicious to me."]
        seen["messaging"] += 1
        for i in s.g.living():
            s.send_message(i, "terminate")
        assert s.phase == "moving" and s.sub == "day_vote"
        st = s.state(1)
        assert st["new_messages"] == [] and st["messaging_seconds_left"] is None and st["messaging_deadline"] is None
        assert len(s.g.messages) == 1                        # the transcript itself is kept (get_messages)


def test_platform_stand_in_window_closes_at_its_time_limit():
    now = [0.0]
    s = Server(4, clock=lambda: now[0], window_limit=120.0)
    with s.cv:
        while s.phase != "messaging" and not s.terminal:
            seat = s.queue[0]
            s.play_action(seat, s._legal(seat)[0], s.g.version)
    now[0] = 60.0
    with s.cv:
        s.send_message(0, "chat", "hello", [])                  # activity: no idle terminate before 120 s
    now[0] = 100.0
    s.tick()
    assert s.phase == "messaging"
    now[0] = 121.0
    s.tick()
    assert s.phase == "moving" and all(a[1] == "window_limit" for a in s.log["auto"])


# ---------------------------------------------------------------- no discussion window (messaging_enabled false)
def test_game_without_discussion_our_agent_votes_and_never_messages():
    agents = [create_agent() for _ in range(4)] + [SeatAgent(LLMSeat(), random.Random(1)),
                                                   SeatAgent(HeuristicSeat(), random.Random(2)),
                                                   SeatAgent(we.OfficialBaseline(), random.Random(3))]
    calls = []
    for a in agents[:4]:
        orig = a.choose_message
        a.choose_message = lambda st, ctx, orig=orig: calls.append(1) or orig(st, ctx)
    out = play_table(agents, seed=5, idle_limit=120.0, timeout=60.0, messaging=False)
    assert out["terminal"] and not out["hung"] and not out["errors"]
    assert not out["log"]["violations"] and not out["messages"] and not calls
    assert not [a for a in out["log"]["auto"] if a[0] < 4]
    ballots = [t for v in out["votes"] for s, t in v["votes"].items() if s < 4]
    assert ballots and None not in ballots                        # our seats vote, never abstain


def test_env_without_discussion(monkeypatch):
    monkeypatch.setenv(we.MESSAGING_ENV, "off")
    g = we.Game([we.ContestantSeat(create_agent) for _ in range(7)], random.Random(2))
    assert g.messaging is False
    g.play()
    assert g.winner and not g.messages and g.votes
    assert all(t is not None for v in g.votes for t in v["votes"].values())
    assert g.state(0, "moving", "day_vote")["messaging_enabled"] is False


def test_messaging_disabled_send_is_refused_by_the_stand_in():
    s = Server(4, messaging=False)
    from altruagent.mcp_transport import MCPToolError
    with pytest.raises(MCPToolError) as e:
        s.send_message(0, "terminate")
    assert e.value.error_code == "MESSAGING_DISABLED"


# ---------------------------------------------------------------- MESSAGE_TOO_LONG (1024 characters)
def test_clip_chat_never_exceeds_the_character_cap():
    assert clip_chat("x" * 1100) == ""                                       # one word over the cap: nothing
    long = " ".join(["abcdefghijklmnopqrstuvwxyz"] * 40)                     # 40 words, 1079 characters
    assert len(long) > 1024 and len(clip_chat(long)) <= MAX_MESSAGE_CHARS
    assert clip_chat(long) == " ".join(["abcdefghijklmnopqrstuvwxyz"] * 37)
    wide = " ".join(["é" * 30] * 40)                                         # 2 bytes a character
    assert len(clip_chat(wide).encode("utf-8")) <= MAX_MESSAGE_CHARS
    assert clip_chat("  I am   the Seer.  ") == "I am the Seer."             # ordinary chat: unchanged


def test_chat_guard_drops_an_over_long_message():
    v = parse({"observation": "Your role: VILLAGER", "your_position": 3})
    assert chat_guard({"content": "a" * 1100, "recipients": []}, v, "open") is None
    assert chat_guard({"content": " ".join(["word" * 30] * 10), "recipients": []}, v, "open") is None
    assert chat_guard({"content": "No hard info yet.", "recipients": []}, v, "open") is not None


class _LongTalker(Contestant):
    """Our Contestant with a chat policy gone wrong: 1100-character messages."""

    def __init__(self):
        super().__init__()
        self.ww.chat = lambda st: [{"content": "y" * 1100, "recipients": []},
                                   {"content": " ".join(["abcdefghijklmnopqrstuvwxyz"] * 40), "recipients": []}]


def test_an_1100_character_message_never_leaves_our_agent():
    c = _LongTalker()
    st = {"observation": "=== Werewolf ===\nYour role: VILLAGER\nDay 1 | phase day_vote", "game_type": "werewolf",
          "your_position": 3, "phase": "messaging", "terminated_messaging": [], "new_messages": [],
          "game_state": {"day": 1, "phase": "day_vote"}}
    ctx = DecisionContext("sid", None, "werewolf", "me")
    out = c.choose_message(GameState.from_mcp_state(st), ctx)
    assert isinstance(out, SendMessage) and len(out.content.encode("utf-8")) <= MAX_MESSAGE_CHARS
    assert c.choose_message(GameState.from_mcp_state(st), ctx) is TERMINATE_MESSAGING


def test_over_long_chats_through_the_real_runner_are_never_rejected():
    agents = [_LongTalker() for _ in range(3)] + [create_agent()] + [SeatAgent(LLMSeat(), random.Random(i))
                                                                    for i in range(3)]
    out = play_table(agents, seed=7, idle_limit=120.0, timeout=60.0, window_limit=10.0)   # (listening: see test_werewolf_eval)
    assert out["terminal"] and not out["hung"] and not out["errors"]
    assert not [v for v in out["log"]["violations"] if v[0] < 4]
    assert all(len(m["content"]) <= 1024 for m in out["messages"])
    assert any(m["sender"] < 3 for m in out["messages"])                     # the clipped chats went out
