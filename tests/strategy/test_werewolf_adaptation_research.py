from copy import deepcopy
from dataclasses import replace
from itertools import permutations
import random

import pytest
from altruagent import TERMINATE_MESSAGING, DecisionContext, GameState, SendMessage

from agent.agent import HOLD, Contestant
from agent.werewolf.policy import WerewolfPolicy, chat_guard
from agent.werewolf.state import View, read_chat
from local import werewolf_adaptation_research as adaptation
from local.werewolf_env import Game, OfficialBaseline


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for name in ("ARENA_RECORD", "ARENA_STATE", "ARENA_SUMMARY", "ARENA_TC_WATCHDOG"):
        monkeypatch.setenv(name, "0")
    monkeypatch.setenv("ARENA_WW_TACTICS", "off")
    monkeypatch.setenv("ARENA_WW_DM", "off")


def view(me=6, role="villager", ally=0):
    return View(me=me, role=role, day=1, sub_phase="day_vote", alive=list(range(7)), dead={}, dead_cause={},
                allies=[ally] if role == "wolf" else [])


def say(v, sender, content, **extra):
    v.messages.append({"seq": len(v.messages), "sender": sender, "content": content, "day": v.day,
                       "type": "chat", "recipients": [], "visibility": "broadcast", **extra})


def game(me=6, role="villager"):
    g = Game([OfficialBaseline() for _ in range(7)], random.Random(193))
    g.roles = ["villager"] * 7
    others = [s for s in range(7) if s != me]
    wolves = [me, others[0]] if role == "wolf" else others[:2]
    for s in wolves:
        g.roles[s] = "wolf"
    seer = me if role == "seer" else next(s for s in others if s not in wolves)
    g.roles[seer] = "seer"
    g.invest = {4: "villager"}
    g.clock = 0.0
    g.terminated = []
    return g


def chat(g, sender, content):
    g.messages.append({"seq": len(g.messages), "sender": sender, "content": content, "day": g.day,
                       "type": "chat", "recipients": [], "visibility": "broadcast"})


def raw(g, me=6):
    return g.state(me, "messaging", "day_vote")


def step(c, g, me=6):
    return c.message_step(GameState.from_mcp_state(raw(g, me)),
                          DecisionContext("adaptation-test", None, "werewolf", "ours"))


@pytest.mark.parametrize("mode", adaptation.MODES)
def test_factories_fresh_and_instance_independence(mode):
    c, other = getattr(adaptation, mode)(), adaptation.plain()
    c.ww._topics.add("open")
    c._fresh()
    assert type(c.ww) is adaptation.AdaptationPolicy and c.ww.mode == mode and not c.ww._topics
    assert other.ww.mode == "plain" and not other.ww._topics
    assert type(Contestant().ww) is WerewolfPolicy
    assert type(c)._choose_message is Contestant._choose_message
    assert type(c)._may_hold is Contestant._may_hold


def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        adaptation.AdaptationPolicy("wolf_only_neutral")


@pytest.mark.parametrize("me,ally", list(permutations(range(7), 2)))
@pytest.mark.parametrize("mode", ["neutral", "after_speakers", "after_coalition"])
@pytest.mark.parametrize("tactics", ["off", "public_vote"])
def test_same_uninformed_public_opening_across_every_seat_ally_pair_and_role(me, ally, mode, tactics, monkeypatch):
    monkeypatch.setenv("ARENA_WW_TACTICS", tactics)
    outputs = []
    for role in ("villager", "seer", "wolf"):
        v = view(me, role, ally)
        before = deepcopy(v)
        p = adaptation.AdaptationPolicy(mode)
        planned = p._plan(v)
        outputs.append([m["content"] for _, m in planned if not m["recipients"]])
        assert all(chat_guard(m, v, topic) == m for topic, m in planned)
        assert v == before
    assert outputs == [[adaptation.NEUTRAL]] * 3


@pytest.mark.parametrize("role", ["villager", "seer", "wolf"])
@pytest.mark.parametrize("day", [1, 2, 3])
def test_plain_control_is_original_policy(role, day):
    v = replace(view(role=role), day=day)
    candidate, reference = adaptation.AdaptationPolicy("plain"), WerewolfPolicy()
    assert candidate._plan(v) == reference._plan(v)
    assert candidate._day_vote(v, v.others_alive) == reference._day_vote(v, v.others_alive)


@pytest.mark.parametrize("mode", adaptation.MODES)
@pytest.mark.parametrize("role", ["villager", "seer", "wolf"])
def test_later_days_preserve_original_policy(mode, role):
    v = replace(view(role=role), day=2)
    candidate, reference = adaptation.AdaptationPolicy(mode), WerewolfPolicy()
    assert candidate._plan(v) == reference._plan(v)
    assert candidate._day_vote(v, v.others_alive) == reference._day_vote(v, v.others_alive)


@pytest.mark.parametrize("mode", adaptation.MODES)
def test_checked_wolf_overrides_neutral_or_delayed_opening(mode):
    v = replace(view(role="seer"), investigations={4: "wolf"})
    p = adaptation.AdaptationPolicy(mode)
    topic, message = p._plan(v)[0]
    facts = read_chat(message["content"], v.me)
    assert topic == "claim" and facts.claim and facts.wolves == {4}
    assert p._day_vote(v, v.others_alive)[0] == 4
    assert chat_guard(message, v, topic) == message


@pytest.mark.parametrize("mode", adaptation.MODES)
def test_lone_credible_claim_and_private_clear_have_precedence(mode):
    v = view()
    say(v, 3, "I am the Seer. Player4 is a wolf. Vote Player4.")
    p = adaptation.AdaptationPolicy(mode)
    assert p._plan(v) == WerewolfPolicy()._plan(v)
    assert p._day_vote(v, v.others_alive)[0] == 4
    clear = replace(view(role="seer"), investigations={0: "villager"})
    for sender in (1, 2, 3):
        say(clear, sender, "I vote Player0.")
    assert p._day_vote(clear, clear.others_alive)[0] != 0


@pytest.mark.parametrize("mode", ["neutral", "after_speakers", "after_coalition"])
def test_neutral_does_not_suppress_a_seer_defending_a_clear_or_itself(mode):
    v = replace(view(me=0, role="seer"), investigations={4: "villager"})
    p = adaptation.AdaptationPolicy(mode)
    planned = p._plan(v)
    assert planned[0][0] == "claim" and read_chat(planned[0][1]["content"], 0).clears == {4}


@pytest.mark.parametrize("text", [
    "I vote Player4.", "Player4 is suspicious.", "Player4 is a wolf.",
])
def test_after_speakers_requires_three_distinct_living_speakers_not_message_volume(text):
    v = view()
    say(v, v.me, adaptation.NEUTRAL)
    p = adaptation.AdaptationPolicy("after_speakers")
    for _ in range(5):
        say(v, 1, text)
    assert not p._plan(v)
    say(v, 2, text)
    assert not p._plan(v)
    say(v, 3, text)
    assert len(p._plan(v)) == 1


@pytest.mark.parametrize("text", [
    'Player1 said "I vote Player4."', "Do not vote Player4.",
    "If Player4 is a wolf, vote Player4.", "> Vote Player4.", "Should we vote Player4?",
])
def test_quoted_negated_or_conditional_pressure_does_not_trigger_delayed_call(text):
    v = view()
    say(v, v.me, adaptation.NEUTRAL)
    for sender in (1, 2, 3):
        say(v, sender, text)
    for mode in ("after_speakers", "after_coalition"):
        assert not adaptation.AdaptationPolicy(mode)._plan(v)


@pytest.mark.parametrize("change", ["dead", "old", "private", "self"])
def test_nonliving_old_private_and_own_speech_cannot_manufacture_three_speakers(change):
    v = view()
    say(v, v.me, adaptation.NEUTRAL)
    for sender in (1, 2, 3):
        say(v, sender, "I vote Player4.")
    if change == "dead":
        v.alive.remove(3)
    elif change == "old":
        v.messages[-1]["day"] = 0
    elif change == "private":
        v.messages[-1].update(recipients=[6], visibility="direct")
    else:
        v.messages[-1]["sender"] = v.me
    assert adaptation.AdaptationPolicy.heard(v) == {1, 2}
    assert adaptation.AdaptationPolicy("after_coalition").coalition_target(v, v.others_alive) is None


def test_after_coalition_needs_agreement_not_only_three_people_speaking():
    v = view()
    say(v, v.me, adaptation.NEUTRAL)
    for sender, target in ((1, 0), (2, 4), (3, 5)):
        say(v, sender, f"I vote Player{target}.")
    assert adaptation.AdaptationPolicy("after_speakers")._plan(v)
    assert not adaptation.AdaptationPolicy("after_coalition")._plan(v)
    say(v, 1, "I vote Player4.")
    say(v, 3, "I vote Player4.")
    p = adaptation.AdaptationPolicy("after_coalition")
    assert p._day_vote(v, v.others_alive)[0] == 4
    assert p._plan(v)[0][1] == adaptation.vote_message(4)


@pytest.mark.parametrize("role", ["villager", "seer", "wolf"])
def test_public_coalition_target_can_include_wolf_partner_without_new_wolf_template(role):
    v = view(role=role, ally=4)
    say(v, v.me, adaptation.NEUTRAL)
    for sender in (1, 2, 3):
        say(v, sender, "I vote Player4.")
    p = adaptation.AdaptationPolicy("after_coalition")
    planned = p._plan(v)
    assert len(planned) == 1 and planned[0][1] == adaptation.vote_message(4)
    assert chat_guard(planned[0][1], v, planned[0][0]) == planned[0][1]
    assert p._day_vote(v, v.others_alive)[0] == 4


def test_after_speakers_defers_but_does_not_claim_to_remove_partner_exclusion():
    target_by_role = {}
    for role in ("villager", "wolf"):
        v = view(role=role, ally=0)
        say(v, v.me, adaptation.NEUTRAL)
        for sender, target in ((1, 4), (2, 5), (3, 4)):
            say(v, sender, f"I vote Player{target}.")
        p = adaptation.AdaptationPolicy("after_speakers")
        planned = p._plan(v)
        target_by_role[role] = p._day_vote(v, v.others_alive)[0]
        assert planned[0][1] == adaptation.vote_message(target_by_role[role])
    assert target_by_role == {"villager": 0, "wolf": 4}


def test_public_echo_not_an_unsent_plan_preserves_coalition_commitment():
    v = view()
    say(v, v.me, adaptation.NEUTRAL)
    for sender in (1, 2, 3):
        say(v, sender, "I vote Player4.")
    p = adaptation.AdaptationPolicy("after_coalition")
    message = p._plan(v)[0][1]
    assert p.announced(v) is None
    say(v, v.me, message["content"])
    for sender in (1, 2, 3):
        say(v, sender, "I vote Player0.")
    assert p.announced(v) == 4
    assert p._day_vote(v, v.others_alive)[0] == 4
    restored = adaptation.AdaptationPolicy("after_coalition")
    assert not restored._plan(v)
    say(v, 5, "I am the Seer. Player0 is a wolf.")
    assert p._day_vote(v, v.others_alive)[0] == 0


@pytest.mark.parametrize("mode", ["neutral", "after_speakers", "after_coalition"])
def test_neutral_public_text_never_leaks_dm_or_allies(monkeypatch, mode):
    monkeypatch.setenv("ARENA_WW_DM", "on")
    v = view(role="wolf", ally=0)
    p = adaptation.AdaptationPolicy(mode)
    planned = p._plan(v)
    public = [(topic, m) for topic, m in planned if not m["recipients"]]
    private = [(topic, m) for topic, m in planned if m["recipients"]]
    assert public == [("open", {"content": adaptation.NEUTRAL, "recipients": []})]
    assert len(private) == 1 and private[0][1]["recipients"] == [0]
    assert all(chat_guard(message, v, topic) == message for topic, message in planned)


@pytest.mark.parametrize("mode", adaptation.MODES)
@pytest.mark.parametrize("phase,role", [("day_vote", "villager"), ("day_vote", "seer"), ("day_vote", "wolf"),
                                        ("night_wolf", "wolf"), ("night_seer", "seer")])
def test_legal_action_filter_and_no_night_behavior_change(mode, phase, role):
    g = game(role=role)
    p = adaptation.AdaptationPolicy(mode)
    for targets in ([4], [3, 4], [5]):
        state = g.state(6, "moving", phase, targets)
        assert p.act(state)["action_id"] in {str(t) for t in targets}
        if phase != "day_vote":
            assert p.act(state) == WerewolfPolicy().act(state)


@pytest.mark.parametrize("mode", ["after_speakers", "after_coalition"])
def test_runtime_holds_only_while_original_listener_allows_then_spends_its_real_followup(mode):
    c, g = getattr(adaptation, mode)(), game()
    c.listen = True
    first = step(c, g)
    assert isinstance(first, SendMessage) and first.content == adaptation.NEUTRAL
    chat(g, 6, first.content)
    g.clock = 5
    assert step(c, g) is HOLD
    for sender in (1, 2):
        chat(g, sender, "I vote Player4.")
    assert step(c, g) is HOLD
    chat(g, 3, "I vote Player4.")
    follow = step(c, g)
    assert isinstance(follow, SendMessage)
    assert c._chat_sent == 2 and c._followups == 1
    chat(g, 6, follow.content)
    assert step(c, g) is TERMINATE_MESSAGING
    chat(g, 0, "I am the Seer. Player6 is a wolf.")
    assert step(c, g) is TERMINATE_MESSAGING
    assert c._chat_sent == 2


@pytest.mark.parametrize("mode", ["neutral", "after_speakers", "after_coalition"])
def test_runtime_claim_arriving_before_optional_vote_has_priority(mode):
    c, g = getattr(adaptation, mode)(), game()
    c.listen = True
    first = step(c, g)
    chat(g, 6, first.content)
    for sender in (1, 2, 3):
        chat(g, sender, "I vote Player4.")
    chat(g, 5, "I am the Seer. Player0 is a wolf. Vote Player0.")
    answer = step(c, g)
    assert isinstance(answer, SendMessage) and "Following the Seer" in answer.content
    assert "I vote Player0" in answer.content and c._chat_sent == 2 and c._followups == 1


@pytest.mark.parametrize("stop", ["quiet", "all_terminated", "listen_off", "listed_terminated"])
def test_adaptation_does_not_invent_extra_simulator_holds(stop):
    c, g = adaptation.after_speakers(), game()
    c.listen = True
    first = step(c, g)
    chat(g, 6, first.content)
    if stop == "quiet":
        g.clock = 30
    elif stop == "all_terminated":
        g.terminated = list(range(6))
    elif stop == "listen_off":
        c.listen = False
    else:
        g.terminated = [6]
    assert step(c, g) is TERMINATE_MESSAGING


@pytest.mark.parametrize("mode", ["after_speakers", "after_coalition"])
def test_unsent_followup_retry_and_saved_restart_keep_budget(mode):
    c, g = getattr(adaptation, mode)(), game()
    c.listen = True
    first = step(c, g)
    chat(g, 6, first.content)
    for sender in (1, 2, 3):
        chat(g, sender, "I vote Player4.")
    follow = step(c, g)
    restored = getattr(adaptation, mode)()
    restored.listen = True
    restored._import(deepcopy(c._export("werewolf", None)))
    retry = step(restored, g)
    assert isinstance(retry, SendMessage) and retry.content == follow.content
    assert restored.undelivered == 1 and restored._chat_sent == 2 and restored._followups == 1
    chat(g, 6, retry.content)
    assert step(restored, g) is TERMINATE_MESSAGING


@pytest.mark.parametrize("mode", ["after_speakers", "after_coalition"])
def test_cold_restart_recovers_neutral_and_vote_topics_from_real_public_echo(mode):
    g = game()
    chat(g, 6, adaptation.NEUTRAL)
    for sender in (1, 2, 3):
        chat(g, sender, "I vote Player4.")
    c = getattr(adaptation, mode)()
    c.listen = True
    follow = step(c, g)
    assert isinstance(follow, SendMessage) and follow.content != adaptation.NEUTRAL
    chat(g, 6, follow.content)
    new = getattr(adaptation, mode)()
    assert not new.ww.chat(raw(g))
    assert new.ww._topics & {"adaptation-vote", "bus-adaptation-vote"}


@pytest.mark.parametrize("mode", adaptation.MODES)
def test_five_message_quota_and_no_villager_false_claim(mode):
    g = game()
    for i in range(5):
        chat(g, 6, f"Earlier statement {i}.")
    c = getattr(adaptation, mode)()
    assert step(c, g) is TERMINATE_MESSAGING
    assert c._chat_sent == 5
    p = adaptation.AdaptationPolicy(mode)
    v = view()
    messages = p._plan(v)
    for topic, message in messages:
        assert not read_chat(message["content"], v.me).claim
        assert len(message["content"].split()) <= 50 and len(message["content"].encode()) <= 1024
        assert chat_guard(message, v, topic) == message
