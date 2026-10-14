from copy import deepcopy
from dataclasses import replace
import random

import pytest
from altruagent import TERMINATE_MESSAGING, DecisionContext, GameState, SendMessage

from agent.werewolf.policy import WerewolfPolicy, chat_guard
from agent.werewolf.state import View, read_chat
from local import werewolf_coordination_research as research
from local.werewolf_env import Game, OfficialBaseline


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for name in ("ARENA_RECORD", "ARENA_STATE", "ARENA_SUMMARY", "ARENA_TC_WATCHDOG"):
        monkeypatch.setenv(name, "0")
    monkeypatch.setenv("ARENA_WW_TACTICS", "off")
    monkeypatch.setenv("ARENA_WW_DM", "off")


def view(role="villager", me=6):
    return View(me=me, role=role, day=2, sub_phase="day_vote", alive=[0, 1, 2, 3, 4, 6],
                dead={5: "villager"}, dead_cause={5: "night_kill"})


def say(v, sender, text, day=None, **kwargs):
    v.messages.append({"seq": len(v.messages), "sender": sender, "content": text,
                       "recipients": [], "type": "chat", "day": v.day if day is None else day, **kwargs})


def prior_commitment(v, speaker=1, target=5):
    say(v, speaker, f"I vote Player{target}.", day=1)
    v.votes = [(1, {speaker: target, 2: target, 3: 0, 4: 0, 6: 0, 0: 2, 5: 0}, 0)]


def game(role="seer", me=0):
    g = Game([OfficialBaseline() for _ in range(7)], random.Random(191))
    g.roles = ["villager"] * 7
    g.roles[2] = g.roles[5] = "wolf"
    g.roles[me] = role
    if role != "seer":
        g.roles[3] = "seer"
    g.invest = {3: "villager"} if role == "seer" else {}
    g.clock = 0.0
    g.terminated = []
    return g


def raw(g, me=0):
    return g.state(me, "messaging", "day_vote")


def chat(g, sender, text):
    g.messages.append({"seq": len(g.messages), "sender": sender, "recipients": [], "visibility": "broadcast",
                       "type": "chat", "content": text, "day": g.day})


def step(c, g):
    return c.message_step(GameState.from_mcp_state(raw(g)),
                          DecisionContext("coordination-test", None, "werewolf", "ours"))


@pytest.mark.parametrize("mode", research.MODES)
def test_factory_survives_fresh_and_does_not_change_plain_contestant(mode):
    c = getattr(research, mode)()
    assert isinstance(c, research.CoordinationContestant)
    assert c.ww.mode == mode
    c.ww._topics.add("claim")
    c._fresh()
    assert isinstance(c.ww, research.CoordinationPolicy) and c.ww.mode == mode and not c.ww._topics
    assert type(research.Contestant().ww) is WerewolfPolicy


def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        research.CoordinationPolicy("unconditional_pressure")


def test_first_false_claim_is_not_reported_as_a_checked_wolf():
    v = replace(view("seer", 0), investigations={6: "villager"})
    say(v, 2, "I am the Seer. Player0 is a wolf.")
    base = WerewolfPolicy()
    assert read_chat(base._seer_chat(v, base.claims(v))[1]["content"], 0).wolves == {2}
    p = research.CoordinationPolicy("transmit")
    topic, message = p._seer_chat(v, p.claims(v))
    facts = read_chat(message["content"], 0)
    assert facts.claim and not facts.wolves and facts.clears == {6}
    assert "Player2 is not the Seer" in message["content"]
    assert "vote Player2" in message["content"]
    assert chat_guard(message, v, topic) == message


def test_checked_and_deduced_wolves_are_labelled_differently():
    v = replace(view("seer", 0), alive=[0, 1, 2, 3, 4], dead={5: "villager", 6: "villager"},
                investigations={1: "wolf", 2: "villager", 3: "villager"})
    p = research.CoordinationPolicy("transmit")
    topic, message = p._seer_chat(v, p.claims(v))
    text = message["content"]
    assert "I checked Player1: wolf." in text
    assert "remaining roles imply Player4 is a WOLF" in text
    assert "I checked Player4" not in text
    facts = read_chat(text, 0)
    assert facts.wolves == {1, 4} and facts.clears == {2, 3}
    assert len(text.split()) <= 50 and len(text.encode()) <= 1024
    assert chat_guard(message, v, topic)


def test_no_unconditioned_first_day_clear_disclosure():
    v = replace(view("seer", 4), day=1, alive=list(range(7)), dead={}, investigations={6: "villager"})
    p = research.CoordinationPolicy("transmit")
    assert p._seer_chat(v, p.claims(v)) is None
    for sender in (1, 2):
        say(v, sender, "I vote Player6.")
    assert p._seer_chat(v, p.claims(v)) is None
    say(v, 3, "I vote Player6.")
    message = p._seer_chat(v, p.claims(v))[1]
    assert read_chat(message["content"], 4).clears == {6}
    assert "vote Player6" not in message["content"]


def test_known_wolf_switch_gets_one_actionable_followup():
    p, g = research.CoordinationPolicy("transmit"), game()
    g.invest = {2: "wolf", 5: "wolf"}
    first = p.chat(raw(g))[0]
    assert "vote Player2" in first["content"]
    chat(g, 0, first["content"])
    for sender in (1, 3, 4):
        chat(g, sender, "I vote Player5.")
    reply = p.chat(raw(g))
    assert len(reply) == 1 and "vote Player5" in reply[0]["content"]
    assert p._called(p._view(raw(g))) == 5
    chat(g, 0, reply[0]["content"])
    assert not p.chat(raw(g))
    restored = research.CoordinationPolicy("transmit")
    restored.import_memory(deepcopy(p.export_memory()))
    assert not restored.chat(raw(g)) and restored._call == [g.day, 5]


def test_concrete_emergency_coalition_changes_near_tie_not_role_evidence():
    v = view("seer")
    v.investigations = {1: "villager"}
    for sender in (1, 2):
        say(v, sender, "I vote Player4.")
    p = research.CoordinationPolicy("coalition")
    assert WerewolfPolicy()._day_vote(v, v.others_alive)[0] == 0
    assert p._day_vote(v, v.others_alive)[0] == 4
    assert "not a verified role" in p._day_vote(v, v.others_alive)[1]
    assert v.investigations == {1: "villager"}


def test_two_unknown_speakers_do_not_make_a_credible_coalition():
    v = view()
    for sender in (1, 2):
        say(v, sender, "I vote Player4.")
    p = research.CoordinationPolicy("coalition")
    assert p._day_vote(v, v.others_alive)[0] == 0
    say(v, 3, "I vote Player4.")
    assert p._day_vote(v, v.others_alive)[0] == 4


def test_past_kept_call_counts_as_limited_credibility_only_while_alive():
    v = view()
    say(v, 1, "I vote Player3.", day=1)
    v.votes = [(1, {1: 3, 2: 4, 3: 2, 4: 3, 0: 4, 6: 2}, None)]
    for sender in (1, 2):
        say(v, sender, "I vote Player4.")
    p = research.CoordinationPolicy("coalition")
    assert 1 in p.credible(v) and p._day_vote(v, v.others_alive)[0] == 4
    v.alive.remove(1)
    v.dead[1] = "villager"
    assert 1 not in p.credible(v) and p._day_vote(v, v.others_alive)[0] == 0


@pytest.mark.parametrize("count", [1, 5, 30])
def test_repetition_is_one_speaker_not_a_coalition(count):
    v = replace(view("seer"), investigations={1: "villager"})
    for _ in range(count):
        say(v, 1, "I vote Player4.")
    p = research.CoordinationPolicy("combined")
    assert p.supporters(v)[4] == {1}
    assert p._day_vote(v, v.others_alive)[0] == 0


@pytest.mark.parametrize("text", [
    'Player2 said "I vote Player4."', "'Vote Player4.' is what Player2 said.",
    "Do not vote Player4.", "I won't vote Player4.", "If Player4 is a wolf, vote Player4.",
    "Player2: I vote Player4.", "> Vote Player4.", "Should we vote Player4?",
    "Player4 is suspicious.", "Player4 is a wolf.",
    "Player4 is suspicious. I won't vote Player4.",
])
def test_quotes_negation_allegations_are_not_personal_vote_commitments(text):
    v = view()
    say(v, 1, text)
    assert research.CoordinationPolicy.commitments(v) == {}


def test_latest_call_replaces_old_call_and_ambiguity_removes_support():
    v = view()
    say(v, 1, "I vote Player0.")
    say(v, 1, "I vote Player4.")
    assert research.CoordinationPolicy.commitments(v) == {1: 4}
    say(v, 1, "Vote Player2. Vote Player3.")
    assert research.CoordinationPolicy.commitments(v) == {}


@pytest.mark.parametrize("withdrawal", ["I will not vote Player4.", "I won't vote Player4.",
                                       "I no longer vote Player4."])
def test_explicit_withdrawal_removes_old_support(withdrawal):
    v = view()
    say(v, 1, "I vote Player4.")
    say(v, 1, withdrawal)
    assert research.CoordinationPolicy.commitments(v) == {}


def test_dead_vote_target_does_not_erase_a_living_speakers_kept_promise():
    v = view()
    prior_commitment(v)
    assert research.CoordinationPolicy("coalition").credible(v) == {1}


def test_two_stalls_are_an_emergency_without_overriding_hard_truth():
    v = replace(view("seer"), alive=list(range(7)), dead={}, investigations={1: "villager"})
    for sender in (1, 2):
        say(v, sender, "I vote Player4.")
    p = research.CoordinationPolicy("coalition")
    assert p._day_vote(v, v.others_alive)[0] == 0
    v.votes = [(1, {1: 4, 2: 4, 3: 0, 4: 0}, None), (2, {1: 4, 2: 4, 3: 0, 4: 0}, None)]
    assert p._day_vote(v, v.others_alive)[0] == 4
    v.investigations[0] = "wolf"
    assert p._day_vote(v, v.others_alive)[0] == 0


@pytest.mark.parametrize("change", ["dead", "old", "private", "liar", "self", "checked_wolf"])
def test_only_distinct_living_noncontradicted_public_speakers_count(change):
    v = replace(view("seer"), investigations={1: "villager"})
    for sender in (1, 2):
        say(v, sender, "I vote Player4.")
    if change == "dead":
        v.alive.remove(2)
        v.dead[2] = "wolf"
    elif change == "old":
        v.messages[-1]["day"] = 1
    elif change == "private":
        v.messages[-1].update(recipients=[6], visibility="direct")
    elif change == "liar":
        say(v, 2, "I am the Seer.")
    elif change == "self":
        v.messages[-1]["sender"] = v.me
    else:
        v.investigations[2] = "wolf"
    assert research.CoordinationPolicy("coalition").supporters(v)[4] == {1}


@pytest.mark.parametrize("truth", ["checked_wolf", "checked_clear", "trusted_wolf", "trusted_clear", "dead_seer", "liar"])
def test_hard_truth_and_established_claim_precedence(truth):
    v = view("seer" if truth.startswith("checked") else "villager")
    for sender in (1, 2, 3):
        say(v, sender, "I vote Player4.")
    if truth == "checked_wolf":
        v.investigations = {0: "wolf"}
    elif truth == "checked_clear":
        v.investigations = {4: "villager"}
    elif truth in ("trusted_wolf", "trusted_clear"):
        say(v, 1, "I am the Seer. Player0 is a wolf." if truth == "trusted_wolf"
            else "I am the Seer. Player4 is clear.")
    elif truth == "dead_seer":
        v.dead[5] = "seer"
        say(v, 5, "I am the Seer. Player0 is a wolf.", day=1)
    else:
        say(v, 0, "I am the Seer. Player6 is a wolf.")
    p = research.CoordinationPolicy("coalition")
    assert p._day_vote(v, v.others_alive) == WerewolfPolicy()._day_vote(v, v.others_alive)
    assert p._day_vote(v, v.others_alive)[0] != 4


def test_revealed_vote_record_is_not_overridden_by_bare_calls():
    v = view()
    v.dead[5] = "villager"
    v.votes = [(1, {0: 5, 1: 3, 2: 3, 3: 2, 4: 1, 6: 2}, 5)]
    for sender in (1, 2, 3):
        say(v, sender, "I vote Player4.")
    assert research.CoordinationPolicy("coalition")._day_vote(v, v.others_alive)[0] == 0


def test_information_investigation_does_not_reward_repeated_speech():
    v = replace(view("seer", 0), sub_phase="night_seer")
    say(v, 4, "Let's discuss the vote.", day=1)
    say(v, 6, "Let's discuss the vote.", day=1)
    p = research.CoordinationPolicy("information")
    assert p._investigate(v, v.others_alive)[0] == 6
    for _ in range(4):
        say(v, 4, "Let's discuss the vote.", day=1)
    assert WerewolfPolicy()._investigate(v, v.others_alive)[0] == 4
    assert p._investigate(v, v.others_alive)[0] == 6


def test_information_values_demonstrated_voting_pivot_not_message_volume():
    v = replace(view("seer", 0), alive=[0, 1, 2, 3, 4, 6], sub_phase="night_seer")
    say(v, 3, "I vote Player4.", day=1)
    v.votes = [(1, {0: 1, 1: 4, 2: 4, 3: 4, 4: 1, 6: 1}, None)]
    p = research.CoordinationPolicy("information")
    assert p._investigate(v, v.others_alive)[0] == 3
    v.investigations = {3: "villager"}
    assert p._investigate(v, v.others_alive)[0] != 3


@pytest.mark.parametrize("mode", research.MODES)
@pytest.mark.parametrize("phase,role", [("day_vote", "villager"), ("day_vote", "seer"),
                                        ("day_vote", "wolf"), ("night_seer", "seer"), ("night_wolf", "wolf")])
def test_actions_remain_legal_with_restricted_targets(mode, phase, role):
    g = game(role)
    for targets in ([1], [4], [1, 4], [7] if phase == "day_vote" else [6]):
        state = g.state(0, "moving", phase, targets)
        action = research.CoordinationPolicy(mode).act(state)
        assert action["action_id"] in {str(t) for t in targets}


@pytest.mark.parametrize("mode", research.MODES)
@pytest.mark.parametrize("role", ["seer", "villager"])
def test_three_survivor_endgame_keeps_deduced_or_trusted_wolf_above_stall_pressure(mode, role):
    v = replace(view(role), day=4, alive=[0, 4, 6],
                dead={1: "wolf", 2: "villager", 3: "villager", 5: "villager"},
                investigations={4: "villager"} if role == "seer" else {},
                votes=[(1, {0: 4, 1: 4, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1}, 1),
                       (2, {0: 4, 3: 0, 4: 0, 5: 4, 6: None}, None),
                       (3, {0: 4, 4: 0, 5: 0, 6: 4}, None)])
    if role == "villager":
        say(v, 4, "I am the Seer. Player0 is a wolf. Vote Player0.")
    for _ in range(5):
        say(v, 0, "Vote Player4.")
    assert research.CoordinationPolicy(mode)._day_vote(v, v.others_alive)[0] == 0


@pytest.mark.parametrize("mode", research.MODES)
def test_wolf_messages_and_night_targets_keep_original_guard_and_no_private_leak(mode):
    v = replace(view("wolf", 3), allies=[4])
    p, base = research.CoordinationPolicy(mode), WerewolfPolicy()
    before = deepcopy(v)
    assert p._plan(v) == base._plan(v)
    assert p._kill_plan(v, [0, 1, 2, 6]) == base._kill_plan(v, [0, 1, 2, 6])
    for topic, message in p._plan(v):
        assert chat_guard(message, v, topic)
        assert "Player4" not in message["content"]
        assert not any(term in message["content"].lower() for term in ("ally", "tonight", "our kill"))
    assert v == before


@pytest.mark.parametrize("mode", research.MODES)
def test_villager_never_claims_seer_or_invents_check(mode):
    v = view()
    for sender in (1, 2, 3):
        say(v, sender, "I vote Player4.")
    p = research.CoordinationPolicy(mode)
    for topic, message in p._plan(v):
        assert not read_chat(message["content"], v.me).claim
        assert "I checked" not in message["content"]
        assert chat_guard(message, v, topic)


def test_runtime_followup_retry_restart_and_sent_bookkeeping():
    c, g = research.transmit(), game()
    c.listen = True
    initial = step(c, g)
    assert isinstance(initial, SendMessage)
    chat(g, 0, initial.content)
    chat(g, 2, "I am the Seer. Player0 is a wolf.")
    g.clock = 10
    reply = step(c, g)
    assert isinstance(reply, SendMessage) and "Player2 is not the Seer" in reply.content
    assert c._chat_sent == 2 and c._followups == 1
    restored = research.transmit()
    restored.listen = True
    restored._import(deepcopy(c._export("werewolf", None)))
    retried = step(restored, g)
    assert isinstance(retried, SendMessage) and retried.content == reply.content
    assert restored.undelivered == 1 and restored._chat_sent == 2 and restored._followups == 1
    chat(g, 0, retried.content)
    assert step(restored, g) is TERMINATE_MESSAGING
    assert restored._chat_sent == 2


@pytest.mark.parametrize("saved", [False, True])
def test_runtime_five_message_quota_survives_cold_or_saved_restart(saved):
    g = game()
    for index in range(5):
        chat(g, 0, f"Earlier discussion point {index}.")
    chat(g, 2, "I am the Seer. Player0 is a wolf.")
    c = research.combined()
    if saved:
        original = research.combined()
        step(original, g)
        c._import(deepcopy(original._export("werewolf", None)))
    assert step(c, g) is TERMINATE_MESSAGING
    assert c._chat_sent == 5


def test_policy_new_day_topics_reset_and_direct_count_stays_bounded():
    p, g = research.CoordinationPolicy("combined"), game()
    output = p.chat(raw(g))
    for message in output:
        chat(g, 0, message["content"])
    for sender in (2, 5, 1, 4, 6):
        chat(g, sender, "I am the Seer. Player0 is a wolf.")
        new = p.chat(raw(g))
        output.extend(new)
        for message in new:
            chat(g, 0, message["content"])
    assert len(output) <= 5
    assert all(len(message["content"].split()) <= 50 for message in output)
    assert not p.chat(raw(g))
    g.day += 1
    g.window_start_seq = len(g.messages)
    assert p.chat(raw(g))
