from copy import deepcopy
from dataclasses import replace
from itertools import product
import random

import pytest
from altruagent import TERMINATE_MESSAGING, DecisionContext, GameState, SendMessage

from agent.agent import Contestant
from agent.werewolf.policy import WerewolfPolicy, chat_guard
from agent.werewolf.state import View, read_chat
from local import werewolf_seer_design_research as design
from local.werewolf_benchmark import Scenario
from local.werewolf_env import Game, OfficialBaseline
from local.werewolf_research import run_task


LEVELS = tuple(product(design.INVESTIGATIONS, design.DISCLOSURES))


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for key in ("ARENA_RECORD", "ARENA_STATE", "ARENA_SUMMARY", "ARENA_TC_WATCHDOG"):
        monkeypatch.setenv(key, "0")
    monkeypatch.setenv("ARENA_WW_TACTICS", "off")
    monkeypatch.setenv("ARENA_WW_DM", "off")


def game():
    g = Game([OfficialBaseline() for _ in range(7)], random.Random(206))
    g.roles = ["villager", "wolf", "villager", "villager", "seer", "wolf", "villager"]
    g.invest = {0: "villager"}
    g.clock, g.terminated = 0.0, []
    return g


def raw(g):
    return g.state(4, "messaging", "day_vote")


def say(container, sender, text, day=1, **extra):
    container.messages.append({"seq": len(container.messages), "sender": sender, "content": text,
                               "day": day, "type": "chat", "recipients": [], "visibility": "broadcast", **extra})


def step(c, g):
    return c.message_step(GameState.from_mcp_state(raw(g)),
                          DecisionContext("seer-design", None, "werewolf", "ours"))


def view(me=4):
    return View(me=me, role="seer", day=1, sub_phase="night_seer", alive=list(range(7)), dead={}, dead_cause={})


def test_joint_target_and_risk_disclosure_addresses_a_clear_at_risk():
    v = view()
    base = WerewolfPolicy()
    assert base._investigate(v, v.others_alive)[0] == 6
    p = design.SeerDesignPolicy("intended", "at_risk")
    assert p._investigate(v, v.others_alive)[0] == 0
    dawn = replace(v, sub_phase="day_vote", investigations={0: "villager"})
    assert design.SeerDesignPolicy("intended", "baseline")._seer_chat(dawn, base.claims(dawn)) is None
    irrelevant = replace(dawn, investigations={6: "villager"})
    assert design.SeerDesignPolicy("baseline", "at_risk")._seer_chat(irrelevant, base.claims(irrelevant)) is None
    topic, message = p._seer_chat(dawn, p.claims(dawn))
    assert read_chat(message["content"], v.me).clears == {0}
    assert "I vote Player1" in message["content"]
    assert chat_guard(message, dawn, topic) == message


def test_first_clear_factor_is_independent_of_the_target_factor():
    v = replace(view(), sub_phase="day_vote", investigations={6: "villager"})
    assert WerewolfPolicy()._seer_chat(v, WerewolfPolicy.claims(v)) is None
    p = design.SeerDesignPolicy("baseline", "first_clear")
    topic, message = p._seer_chat(v, p.claims(v))
    assert topic == "claim" and read_chat(message["content"], v.me).clears == {6}


@pytest.mark.parametrize("me", range(7))
def test_first_night_has_no_information_to_break_role_uncertainty_ties(me):
    v = view(me)
    p = design.SeerDesignPolicy("intended", "at_risk")
    assert p.intended_public_target(v) == min(v.others_alive)
    chosen, reason = p._investigate(v, v.others_alive)
    assert chosen == min(v.others_alive)
    assert "anticipated" in reason and "not a known future vote" in reason
    assert "wolf" not in reason.lower()
    assert v.investigations == {} and v.messages == []


@pytest.mark.parametrize("investigation,disclosure", LEVELS)
@pytest.mark.parametrize("day", [1, 2])
def test_already_checked_and_dead_targets_are_skipped_when_an_unread_target_exists(investigation, disclosure, day):
    v = replace(view(), day=day, alive=[0, 2, 3, 4, 5, 6], dead={1: "villager"}, investigations={0: "villager"})
    p = design.SeerDesignPolicy(investigation, disclosure)
    assert p._investigate(v, [0, 1, 2])[0] == 2


@pytest.mark.parametrize("investigation,disclosure", LEVELS)
def test_known_wolf_and_own_emergency_keep_original_priority(investigation, disclosure):
    p = design.SeerDesignPolicy(investigation, disclosure)
    v = replace(view(), investigations={0: "villager", 6: "wolf"}, sub_phase="day_vote")
    before = deepcopy(v)
    topic, msg = p._seer_chat(v, p.claims(v))
    assert "Player6 is a WOLF" in msg["content"] and "vote Player6" in msg["content"]
    assert p._day_vote(v, v.others_alive)[0] == 6
    assert chat_guard(msg, v, topic) == msg and v == before
    own = replace(view(0), sub_phase="day_vote", investigations={6: "villager"})
    assert p._seer_chat(own, p.claims(own)) == WerewolfPolicy()._seer_chat(own, WerewolfPolicy.claims(own))


@pytest.mark.parametrize("investigation,disclosure", LEVELS)
def test_clear_is_never_voted_even_when_falsely_claiming_seer(investigation, disclosure):
    p = design.SeerDesignPolicy(investigation, disclosure)
    v = replace(view(), investigations={0: "villager"}, sub_phase="day_vote")
    say(v, 0, "I am the Seer. Player4 is a wolf.")
    assert p._day_vote(v, v.others_alive)[0] != 0
    assert p._seer_chat(v, p.claims(v)) == WerewolfPolicy()._seer_chat(v, WerewolfPolicy.claims(v))


@pytest.mark.parametrize("text", [
    'Player2 said "Vote Player6."', "Do not vote Player6.", "I won't vote Player6.",
    "If Player6 is a wolf, vote Player6.", "> Vote Player6.", "Should we vote Player6?",
])
def test_quoted_negated_or_conditional_pressure_cannot_force_a_clear_claim(text):
    v = replace(view(), sub_phase="day_vote", investigations={6: "villager"})
    for sender in (1, 2, 3):
        say(v, sender, text)
    p = design.SeerDesignPolicy("baseline", "at_risk")
    assert p._seer_chat(v, p.claims(v)) is None


@pytest.mark.parametrize("text", ["I vote Player6.", "Player6 is a wolf.", "Player6 is suspicious."])
@pytest.mark.parametrize("count", [1, 5, 30])
def test_one_speaker_cannot_replace_the_saved_night_intent_or_force_disclosure_by_spam(text, count):
    p = design.SeerDesignPolicy("baseline", "at_risk")
    p._investigate(view(), view().others_alive)
    v = replace(view(), sub_phase="day_vote", investigations={6: "villager"})
    for _ in range(count):
        say(v, 1, text)
    assert p.first_intent(v) == 0
    assert p._seer_chat(v, p.claims(v)) is None
    restored = design.SeerDesignPolicy("baseline", "at_risk")
    restored.import_memory(deepcopy(p.export_memory()))
    assert restored.first_intent(v) == 0 and restored._seer_chat(v, restored.claims(v)) is None


def test_two_distinct_living_public_voters_can_create_a_new_clear_risk():
    v = replace(view(), sub_phase="day_vote", investigations={6: "villager"})
    p = design.SeerDesignPolicy("baseline", "at_risk")
    say(v, 1, "I vote Player6.")
    say(v, 2, "I vote Player6.")
    assert read_chat(p._seer_chat(v, p.claims(v))[1]["content"], 4).clears == {6}
    v.messages[-1]["recipients"] = [4]
    assert p._seer_chat(v, p.claims(v)) is None
    v.messages[-1]["recipients"] = []
    v.alive.remove(2)
    v.dead[2] = "villager"
    assert p._seer_chat(v, p.claims(v)) is None


@pytest.mark.parametrize("investigation,disclosure", LEVELS)
@pytest.mark.parametrize("stop", ["eliminated", "retired", "is_terminal", "dead"])
def test_dead_or_retired_seer_cannot_publish_the_final_completed_night_check(investigation, disclosure, stop):
    p, g = design.SeerDesignPolicy(investigation, disclosure), game()
    g.day, g.invest = 2, {0: "villager", 1: "wolf"}
    state = raw(g)
    if stop == "dead":
        g.alive[4] = False
        g.dead.append({"day": 2, "seat": 4, "role": "seer", "cause": "night_kill"})
        state = raw(g)
    else:
        state[stop] = True
    assert p.chat(state) == []


@pytest.mark.parametrize("investigation,disclosure", LEVELS)
@pytest.mark.parametrize("role", ["wolf", "villager"])
def test_nonseer_behavior_and_private_knowledge_are_unchanged(investigation, disclosure, role, caplog):
    p, g = design.SeerDesignPolicy(investigation, disclosure), game()
    g.roles[4], g.roles[2] = role, "seer"
    if role == "wolf":
        g.roles[1] = "villager"
    state = raw(g)
    v = p._view(state)
    assert v.role == role and v.allies == ([5] if role == "wolf" else [])
    assert p._seer_chat(v, p.claims(v)) is None
    messages = p.chat(state)
    assert messages == WerewolfPolicy().chat(state)
    assert all("Player5" not in m["content"] and "ally" not in m["content"].lower() for m in messages)
    assert not caplog.records


@pytest.mark.parametrize("factory", design.FACTORIES)
def test_factories_preserve_factor_levels_through_fresh(factory):
    c = getattr(design, factory)()
    expected = c.design
    c.ww._first_intent = [1, 6]
    c._fresh()
    assert isinstance(c.ww, design.SeerDesignPolicy) and c.design == expected
    assert (c.ww.investigation, c.ww.disclosure) == expected and c.ww._first_intent is None
    assert type(Contestant().ww) is WerewolfPolicy


@pytest.mark.parametrize("factory", ["clear_only", "joint_clear", "risk_only", "joint_risk"])
def test_new_disclosure_uses_existing_guard_and_one_daily_topic(factory):
    p, g = getattr(design, factory)().ww, game()
    messages = p.chat(raw(g))
    assert len(messages) == 1
    for m in messages:
        say(g, 4, m["content"])
        assert len(m["content"].split()) <= 50 and len(m["content"].encode()) <= 1024
        assert read_chat(m["content"], 4).clears == {0}
        assert m["recipients"] == []
    assert p.chat(raw(g)) == []
    restored = getattr(design, factory)().ww
    restored.import_memory(deepcopy(p.export_memory()))
    assert restored.chat(raw(g)) == []


def test_real_runtime_late_clear_followup_retry_and_restart_bookkeeping():
    c, g = design.risk_only(), game()
    g.invest = {6: "villager"}
    c.listen = True
    first = step(c, g)
    assert isinstance(first, SendMessage) and "I am the Seer" not in first.content
    say(g, 4, first.content)
    for sender in (1, 2):
        say(g, sender, "I vote Player6.")
    follow = step(c, g)
    assert isinstance(follow, SendMessage) and "Player6 is clear" in follow.content
    restored = design.risk_only()
    restored.listen = True
    restored._import(deepcopy(c._export("werewolf", None)))
    retried = step(restored, g)
    assert isinstance(retried, SendMessage) and retried.content == follow.content
    assert restored._chat_sent == 2 and restored._followups == 1 and restored.undelivered == 1
    say(g, 4, retried.content)
    assert step(restored, g) is TERMINATE_MESSAGING


@pytest.mark.parametrize("factory", design.FACTORIES)
def test_real_runtime_five_chat_cap(factory):
    c, g = getattr(design, factory)(), game()
    for i in range(5):
        say(g, 4, f"Earlier discussion point {i}.")
    assert step(c, g) is TERMINATE_MESSAGING and c._chat_sent == 5


@pytest.mark.parametrize("bg", ["first", "llmq", "rt_wolfpack", "pop_late_claim"])
@pytest.mark.parametrize("index", range(4))
def test_alloff_wholegame_mirror_on_identical_deals(bg, index):
    records = run_task((Scenario(206, index, bg, 1),
                        [("reference", "agent", {}), ("baseline", "local.werewolf_seer_design_research:baseline", {})],
                        {"ARENA_WW_TIMING": "rt", "ARENA_WW_TACTICS": "off", "ARENA_WW_DM": "off"}, True, True))
    reference, candidate = records
    assert not any("harness_error" in r for r in records)
    for key in ("seat", "role", "deal_seed", "win", "winner", "days", "chats", "dms", "trace"):
        assert candidate[key] == reference[key]
    assert not any(candidate["failures"].values())


def test_unpublished_check_metric_does_not_invent_a_posthumous_message():
    record = {"role": "seer", "seat": 4}
    trace = {"roles": ["villager", "wolf", "villager", "villager", "seer", "wolf", "villager"],
             "dead": [{"day": 2, "seat": 4, "role": "seer", "cause": "night_kill"}],
             "votes": [{"day": 1, "eliminated": 2}],
             "actions": [{"seat": 4, "phase": "night_seer", "day": 1, "choice": "0"},
                         {"seat": 4, "phase": "night_seer", "day": 2, "choice": "1"}],
             "messages": [{"day": 1, "sender": 4, "content": "I am the Seer. Player0 is clear.",
                           "recipients": [], "visibility": "broadcast"}]}
    metrics = design.seer_design_metrics(record, trace)
    assert metrics["first_day_innocent_elimination"] == 1
    assert metrics["night_two_exposure"] == metrics["night_two_seer_death"] == 1
    assert metrics["completed_checks_at_night_two_death"] == 2
    assert metrics["unpublished_checks_at_night_two_death"] == 1
    trace["messages"] = []
    assert design.seer_design_metrics(record, trace)["unpublished_checks_at_night_two_death"] == 2


def test_interaction_is_difference_of_differences_not_a_single_factor_gain():
    records = []
    metrics = {"first_day_innocent_elimination": 0, "night_two_seer_death": 0,
               "unpublished_checks_at_night_two_death": 0}
    for i in range(4):
        for name in ("baseline", "target_only", "clear_only", "joint_clear"):
            records.append({"id": f"206/first/k1/{i}", "profile": name, "seat": 4, "role": "seer",
                            "deal_seed": i, "scenario": {"seed": 206, "game": i, "background": "first", "k": 1},
                            "win": name == "joint_clear", "seer_design": metrics})
    report = design.interaction_report(records, "first_clear")
    effect = report["win"]["scopes"]["seer"]["effects"]["intended_target:disclosure"]
    assert effect["mean"] == 1 and effect["n"] == effect["clusters"] == 4
    records[-1]["deal_seed"] += 20
    with pytest.raises(ValueError, match="mismatch"):
        design.interaction_report(records, "first_clear")


def test_evaluator_drops_private_traces_before_records_are_written():
    import json
    profiles = [("reference", "agent", {}), ("baseline", "local.werewolf_seer_design_research:baseline", {}),
                ("joint_clear", "local.werewolf_seer_design_research:joint_clear", {})]
    records = design.study_task((Scenario(206, 0, "llmq", 1), profiles,
                                 {"ARENA_WW_TIMING": "rt", "ARENA_WW_TACTICS": "off", "ARENA_WW_DM": "off"}))
    for record in records:
        assert record["alloff_mirror"]
        assert not {"trace", "allies", "roles", "observation", "investigations", "messages"} & record.keys()
        encoded = json.dumps(record)
        assert "Your role:" not in encoded and "Your fellow wolves:" not in encoded


@pytest.mark.parametrize("factory", design.FACTORIES)
@pytest.mark.parametrize("phase", ["night_seer", "day_vote"])
def test_restricted_action_sets_are_respected(factory, phase):
    p, g = getattr(design, factory)().ww, game()
    for targets in ([0], [1], [2], [0, 2]):
        action = p.act(g.state(4, "moving", phase, targets))
        assert action["action_id"] in {str(t) for t in targets}
