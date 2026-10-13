"""Replay a late competing claim after the real seer already disclosed only clears."""
import copy
import random

import pytest
from altruagent import TERMINATE_MESSAGING, DecisionContext, GameState, SendMessage

from agent.agent import Contestant
from agent.werewolf.policy import WerewolfPolicy, chat_guard
from agent.werewolf.state import View, parse, read_chat
from local.werewolf_env import Game, OfficialBaseline


def game():
    g = Game([OfficialBaseline() for _ in range(7)], random.Random(1))
    g.roles = ["seer", "villager", "wolf", "villager", "villager", "wolf", "villager"]
    g.invest = {3: "villager"}
    g.clock = 0.0
    g.terminated = []
    return g


def say(g, sender, text):
    g.messages.append({"seq": len(g.messages), "sender": sender, "recipients": [], "visibility": "broadcast",
                       "type": "chat", "content": text, "day": g.day})


def raw(g):
    return g.state(0, "messaging", "day_vote")


def accuse(g, sender=2):
    g.clock = 10
    say(g, sender, f"I am the Seer. Player0 is a wolf. Vote Player0.")


def test_clear_only_claim_can_answer_one_late_false_claim_without_inventing_a_result():
    p, g = WerewolfPolicy(), game()
    initial = p.chat(raw(g))
    assert len(initial) == 1 and "Player3 is clear" in initial[0]["content"]
    say(g, 0, initial[0]["content"])
    accuse(g)
    reply = p.chat(raw(g))
    assert len(reply) == 1
    text = reply[0]["content"]
    assert "Player2 is not the Seer" in text and "Vote Player2" in text
    facts = read_chat(text, sender=0)
    assert facts.claim and facts.clears == {3} and not facts.wolves
    assert len(text.split()) <= 50 and len(text.encode()) <= 1024 and reply[0]["recipients"] == []
    assert p._day_vote(parse(raw(g)), list(range(1, 7)))[0] == 2
    say(g, 0, text)
    assert p.chat(raw(g)) == []
    accuse(g, sender=5)
    assert p.chat(raw(g)) == []  # One bounded rebuttal, even with another new claimant.
    restored = WerewolfPolicy()
    restored.import_memory(copy.deepcopy(p.export_memory()))
    assert restored.chat(raw(g)) == []


def test_private_investigation_still_overrides_an_unchecked_rival_claim():
    p, g = WerewolfPolicy(), game()
    g.invest[5] = "wolf"
    initial = p.chat(raw(g))
    say(g, 0, initial[0]["content"])
    accuse(g)
    assert p.chat(raw(g)) == []
    assert p._day_vote(parse(raw(g)), list(range(1, 7)))[0] == 5


def test_a_new_day_resets_the_bounded_rebuttal_topic():
    p, g = WerewolfPolicy(), game()
    initial = p.chat(raw(g))
    say(g, 0, initial[0]["content"])
    accuse(g)
    reply = p.chat(raw(g))
    say(g, 0, reply[0]["content"])
    assert p.chat(raw(g)) == []

    g.alive[2] = False
    g.dead.append({"day": g.day, "seat": 2, "role": "wolf", "cause": "vote"})
    g.day += 1
    g.clock = 0
    g.window_start_seq = len(g.messages)
    g.terminated = [2]
    initial = p.chat(raw(g))
    assert len(initial) == 1 and "Player3 is clear" in initial[0]["content"]
    say(g, 0, initial[0]["content"])
    accuse(g, sender=5)
    reply = p.chat(raw(g))
    assert len(reply) == 1 and "Player5 is not the Seer" in reply[0]["content"]
    say(g, 0, reply[0]["content"])
    assert p.chat(raw(g)) == []


def test_rebuttal_and_ballot_agree_when_rival_claims_arrive_out_of_seat_order():
    p, g = WerewolfPolicy(), game()
    initial = p.chat(raw(g))
    say(g, 0, initial[0]["content"])
    accuse(g, sender=5)
    accuse(g, sender=2)
    reply = p.chat(raw(g))
    assert len(reply) == 1
    assert "Player2 is not the Seer" in reply[0]["content"]
    assert "Vote Player2" in reply[0]["content"]
    assert p._day_vote(parse(raw(g)), list(range(1, 7)))[0] == 2


def test_a_cleared_villagers_false_role_claim_does_not_make_them_a_wolf():
    p, g = WerewolfPolicy(), game()
    initial = p.chat(raw(g))
    say(g, 0, initial[0]["content"])
    accuse(g, sender=3)
    assert p.chat(raw(g)) == []
    assert p._day_vote(parse(raw(g)), list(range(1, 7)))[0] != 3


@pytest.mark.parametrize("role,allowed", [("seer", True), ("villager", False), ("wolf", False)])
def test_rebuttal_topic_is_only_available_to_the_actual_seer(role, allowed):
    v = View(me=0, role=role, day=1, sub_phase="day_vote", alive=list(range(7)), dead={}, dead_cause={})
    msg = {"content": "I am the Seer. Player2 is not the Seer; their claim is false. Vote Player2.", "recipients": []}
    assert (chat_guard(msg, v, "claim-rebuttal") is not None) == allowed


def test_runtime_sends_one_rebuttal_and_keeps_its_budget_across_restart(monkeypatch):
    monkeypatch.setenv("ARENA_RECORD", "0")
    monkeypatch.setenv("ARENA_STATE", "0")
    monkeypatch.setenv("ARENA_TC_WATCHDOG", "0")
    c, g = Contestant(), game()
    c.listen = True
    ctx = DecisionContext("rebuttal-test", None, "werewolf", "ours")
    initial = c.message_step(GameState.from_mcp_state(raw(g)), ctx)
    assert isinstance(initial, SendMessage)
    say(g, 0, initial.content)
    accuse(g)
    reply = c.message_step(GameState.from_mcp_state(raw(g)), ctx)
    assert isinstance(reply, SendMessage) and "Player2 is not the Seer" in reply.content
    say(g, 0, reply.content)
    assert c._chat_sent == 2 and c._followups == 1
    restored = Contestant()
    restored.listen = True
    restored._import(copy.deepcopy(c._export("werewolf", None)))
    assert restored.message_step(GameState.from_mcp_state(raw(g)), ctx) is TERMINATE_MESSAGING
    accuse(g, sender=5)
    assert restored.message_step(GameState.from_mcp_state(raw(g)), ctx) is TERMINATE_MESSAGING
    assert restored._chat_sent == 2


@pytest.mark.parametrize("stop", ["terminated", "quota"])
def test_late_fake_claim_cannot_reopen_a_closed_or_exhausted_window(monkeypatch, stop):
    monkeypatch.setenv("ARENA_RECORD", "0")
    monkeypatch.setenv("ARENA_STATE", "0")
    monkeypatch.setenv("ARENA_TC_WATCHDOG", "0")
    c, g = Contestant(), game()
    c.listen = True
    ctx = DecisionContext("rebuttal-stop-test", None, "werewolf", "ours")
    first = c.message_step(GameState.from_mcp_state(raw(g)), ctx)
    assert isinstance(first, SendMessage)
    say(g, 0, first.content)
    if stop == "terminated":
        g.terminated = [0]
    else:
        for i in range(4):
            say(g, 0, f"Earlier table discussion {i}.")
        c._chat_sent = 5
    accuse(g)
    assert c.message_step(GameState.from_mcp_state(raw(g)), ctx) is TERMINATE_MESSAGING
