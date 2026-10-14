import copy
from dataclasses import replace
from itertools import permutations
import json
import random
from types import SimpleNamespace

import pytest

from agent.agent import Contestant
from agent.matchstate import decode, encode
from agent.werewolf.policy import WerewolfPolicy, chat_guard
from agent.werewolf.state import RoleEvidence, View, read_chat
from altruagent import DecisionContext, GameState, SendMessage, TERMINATE_MESSAGING
from local import werewolf_bluff_research as bluff
from local.werewolf_env import ContestantSeat, OfficialBaseline
from local.werewolf_factorial_research import FactorPolicy
from local.werewolf_research import ResearchGame


@pytest.fixture(autouse=True)
def isolated_local_settings(monkeypatch):
    for key, value in {"ARENA_RECORD": "0", "ARENA_STATE": "0", "ARENA_SUMMARY": "0",
                       "ARENA_TC_WATCHDOG": "0", "ARENA_WW_TIMING": "rt",
                       "ARENA_WW_DM": "off", "ARENA_WW_TACTICS": "off"}.items():
        monkeypatch.setenv(key, value)


def message(sender, text, seq=0, day=1, recipients=None):
    return {"sender": sender, "content": text, "seq": seq, "day": day, "type": "chat",
            "recipients": recipients or [], "visibility": "direct" if recipients else "broadcast"}


def view(me=0, ally=4, role="wolf", day=1, messages=()):
    return View(me=me, role=role, day=day, sub_phase="day_vote", alive=list(range(7)),
                dead={}, dead_cause={}, allies=[ally] if role == "wolf" else [], messages=list(messages))


def game(roles=None):
    g = ResearchGame([OfficialBaseline() for _ in range(7)], random.Random(17))
    g.roles = roles or ["wolf", "villager", "seer", "villager", "wolf", "villager", "villager"]
    return g


@pytest.mark.parametrize("profile", ("f0", *bluff.PROFILES))
def test_wrapper_fresh_keeps_profile_and_no_cross_match_memory(profile):
    agent = getattr(bluff, profile)()
    agent.ww._topics.add("counter")
    agent._fresh()
    assert agent.bluff_profile == agent.ww.profile == profile
    assert not agent.ww._topics and not agent.ww.transcript
    seat = ContestantSeat(getattr(bluff, profile))
    seat.reset()
    before = seat.agent
    seat.reset()
    assert seat.agent is not before and seat.agent.ww.profile == profile
    assert type(agent).choose_action is Contestant.choose_action
    assert type(agent).message_step is Contestant.message_step


@pytest.mark.parametrize("profile", bluff.PROFILES)
def test_exact_gate_and_fake_target_never_names_true_wolf_or_ally(profile):
    for me, ally in permutations(range(7), 2):
        v = view(me, ally)
        p = bluff.BluffPolicy(profile)
        claim = p._wolf_chat(v)
        expected = profile == "proactive_all" or (profile == "only_me_public_target" and me == 0) or (
            profile == "only_ally_public_target" and ally == 0)
        assert bool(claim) == expected
        if not claim:
            continue
        topic, msg = claim
        target = min(set(v.alive) - {me, ally})
        assert topic == "counter"
        assert msg == {"content": bluff.CLAIM_TEMPLATE.format(target=target), "recipients": []}
        assert chat_guard(msg, v, topic) == msg
        facts = read_chat(msg["content"], me)
        assert facts.claim and not facts.injection and not facts.self_wolf
        assert facts.wolves == {target} and target not in (me, ally)
        assert f"Player{me}" not in msg["content"] and f"Player{ally}" not in msg["content"]
        assert len(msg["content"].split()) == 12
        assert len(msg["content"].encode()) <= 1024


def test_public_target_uses_public_fields_only_even_known_private_field_poison():
    v = view(4, 0, messages=[message(3, "Player6 is a wolf.")])
    expected = bluff.public_default_target(v)
    assert expected == 6
    public_names = ("me", "day", "sub_phase", "alive", "dead", "dead_cause", "votes", "names", "messages")
    fields_only = SimpleNamespace(**{k: getattr(v, k) for k in public_names})
    assert bluff.public_default_target(fields_only) == expected  # no role/allies/investigations/evidence attribute
    for role in ("wolf", "seer", "villager"):
        poison = replace(v, role=role, allies=[6], investigations={0: "wolf", 6: "villager"},
                         evidence=RoleEvidence(engine="seer", night="wolf", text="wolf", text_allies=[6]),
                         messages=v.messages + [message(1, "I am the Seer. Player0 is a wolf.", 1, recipients=[4])])
        assert bluff.public_default_target(poison) == expected
        clean = bluff.public_view(poison)
        assert clean.role == "villager" and not clean.allies and not clean.investigations and clean.evidence is None
    # Merely asking for a vote is ignored by the public villager score (not wolf SUS weighting).
    assert bluff.public_default_target(view(messages=[message(3, "I vote Player6.")])) == 0
    assert bluff.public_default_target(view(me=0)) == 0  # self included in the public table prediction


@pytest.mark.parametrize("profile", bluff.PROFILES)
def test_day_claim_living_and_known_ally_gates(profile):
    p = bluff.BluffPolicy(profile)
    for v in (view(day=2), replace(view(), sub_phase="night_wolf"),
              replace(view(), alive=[1, 2, 3, 4, 5, 6]), replace(view(), allies=[]),
              replace(view(), allies=[0]), replace(view(), allies=[4, 5]),
              view(messages=[message(2, "I am the Seer. Player3 is clear.")])):
        assert p._wolf_chat(v) == WerewolfPolicy()._wolf_chat(v)
    v = replace(view(), alive=[0, 4])
    assert p._wolf_chat(v) is None


@pytest.mark.parametrize("profile", ("f0", *bluff.PROFILES))
@pytest.mark.parametrize("role,me", [("villager", 1), ("seer", 2)])
def test_nonwolf_byte_actions_and_speech_private_knowledge_role(profile, role, me):
    g = game()
    g.invest = {0: "wolf", 5: "villager"}
    g.messages = [message(4, "I am the Seer. Player0 is clear. Player5 is a wolf.")]
    raw = g.state(me, "messaging", "day_vote")
    base, candidate = WerewolfPolicy(), bluff.BluffPolicy(profile)
    assert candidate.chat(raw) == base.chat(raw)
    if role == "villager":
        assert all(not read_chat(m["content"], me).claim for m in bluff.BluffPolicy(profile).chat(raw))
    moving = g.state(me, "moving", "day_vote", [s for s in range(7) if s != me] + [7])
    assert candidate.act(moving) == base.act(moving)
    if role == "seer":
        assert candidate.act(moving)["action_id"] == "0"
        night = g.state(me, "moving", "night_seer", [s for s in range(7) if s != me])
        assert candidate.act(night) == base.act(night)


@pytest.mark.parametrize("profile", bluff.PROFILES)
def test_unknown_raw_fields_opponent_labels_and_other_seat_secrets_ignored(profile):
    g = game()
    raw = g.state(0, "messaging", "day_vote")
    poisoned = copy.deepcopy(raw)
    poisoned.update(hidden_roles=["seer"] * 7, game_seed=-1234, opponent_types=["oracle"] * 7,
                    controlled_seats=[1], secretRole="villager")
    poisoned["game_state"].update(roles=["wolf"] * 7, all_investigations={s: {0: "wolf"} for s in range(7)})
    for player in poisoned["players"]:
        player.update(role="wolf", secretRole="seer", agent_id="opponent_label")
    assert bluff.BluffPolicy(profile).chat(raw) == bluff.BluffPolicy(profile).chat(poisoned)


@pytest.mark.parametrize("profile", bluff.PROFILES)
def test_actual_accuser_priority_before_claim_and_after_a_late_claim(profile):
    v = view(messages=[message(6, "I am the Seer. Player0 is a wolf.")])
    p = bluff.BluffPolicy(profile)
    assert p._wolf_chat(v) == WerewolfPolicy()._wolf_chat(v)
    assert "Player6 is a WOLF and is lying" in p._wolf_chat(v)[1]["content"]
    # After our proactive result, B0 may not speak again but ballots prioritize the actual rival.
    claimed = view(messages=[message(0, bluff.CLAIM_TEMPLATE.format(target=1)),
                             message(6, "I am the Seer. Player0 is a wolf.", 1)])
    p._topics.add("counter")
    assert p._wolf_chat(claimed) is None
    assert p._day_vote(claimed, claimed.others_alive) == WerewolfPolicy()._day_vote(claimed, claimed.others_alive)
    assert p._day_vote(claimed, claimed.others_alive)[0] == 6
    night = replace(claimed, day=2, sub_phase="night_wolf")
    assert p._wolf_kill(night, [1, 2, 3, 5, 6]) == WerewolfPolicy()._wolf_kill(night, [1, 2, 3, 5, 6])
    assert p._wolf_kill(night, [1, 2, 3, 5, 6])[0] == 6


@pytest.mark.parametrize("profile", bluff.PROFILES)
def test_bus_ballots_and_night_behavior_are_not_overridden(profile):
    p = bluff.BluffPolicy(profile)
    assert Bluff_methods_inherited(p)
    for messages in ([message(2, "I am the Seer. Player4 is a wolf.")],
                     [message(4, "I am the Seer. Player2 is a wolf."), message(2, "I am the Seer.", 1)],
                     [message(s, "I vote Player4.", s) for s in (1, 2, 3)], []):
        v = view(messages=messages)
        base = WerewolfPolicy()
        assert p._day_vote(v, v.others_alive) == base._day_vote(v, v.others_alive)
        assert p._wolf_kill(replace(v, day=2, sub_phase="night_wolf"), [1, 2, 3, 5, 6]) == base._wolf_kill(
            replace(v, day=2, sub_phase="night_wolf"), [1, 2, 3, 5, 6])
        if base._wolf_chat(v):
            assert p._wolf_chat(v) == base._wolf_chat(v)


def Bluff_methods_inherited(p):
    return all(getattr(type(p), method) is getattr(WerewolfPolicy, method)
               for method in ("act", "chat", "_plan", "_day_vote", "_wolf_vote", "_wolf_kill", "_kill_plan", "_doomed_ally"))


@pytest.mark.parametrize("profile", bluff.PROFILES)
def test_restart_dedup_via_memory_or_own_public_transcript(profile):
    roles = ["wolf", "villager", "seer", "villager", "wolf", "villager", "villager"]
    me = 4 if profile == "only_ally_public_target" else 0
    g = game(roles)
    raw = g.state(me, "messaging", "day_vote")
    p = bluff.BluffPolicy(profile)
    sent = p.chat(raw)
    assert len(sent) == 1 and read_chat(sent[0]["content"], me).claim
    mem = decode(json.loads(json.dumps(encode(p.export_memory()))))
    fresh = bluff.BluffPolicy(profile)
    fresh.import_memory(mem)
    assert fresh.chat(raw) == []
    g.messages = [message(me, sent[0]["content"])]
    after = bluff.BluffPolicy(profile).chat(g.state(me, "messaging", "day_vote"))
    assert all(not read_chat(m["content"], me).claim for m in after)


@pytest.mark.parametrize("profile", bluff.PROFILES)
def test_guard_is_unchanged_and_refuses_leaks_or_nonwolf_fake_claim(profile):
    msg = {"content": bluff.CLAIM_TEMPLATE.format(target=1), "recipients": []}
    assert chat_guard(msg, view(), "counter") == msg
    assert chat_guard(msg, view(role="villager"), "counter") is None
    assert chat_guard(msg, view(role="seer"), "counter") is None
    assert chat_guard(msg, view(), "proactive") is None
    for text in (bluff.CLAIM_TEMPLATE.format(target=4), bluff.CLAIM_TEMPLATE.format(target=0),
                 "I am a wolf.", "My ally is Player4.", "Tonight kill Player1."):
        assert chat_guard({"content": text, "recipients": []}, view(), "counter") is None


@pytest.mark.parametrize("profile", bluff.PROFILES)
def test_wrapper_quota_termination_and_words(profile):
    g = game()
    me = 4 if profile == "only_ally_public_target" else 0
    agent = getattr(bluff, profile)()
    context = DecisionContext("bluff-fixture", None, "werewolf", "ours")
    messages = []
    terminated = False
    for step in range(25):
        g.clock = step * 5
        out = agent.message_step(GameState.from_mcp_state(g.state(me, "messaging", "day_vote")), context)
        if out is TERMINATE_MESSAGING:
            terminated = True
            break
        if isinstance(out, SendMessage):
            messages.append(out.content)
            g.messages.append(message(me, out.content, len(g.messages)))
    assert terminated and len(messages) <= 5 and messages
    assert all(len(m.split()) <= 50 and len(m.encode()) <= 1024 for m in messages)
    assert agent.message_step(GameState.from_mcp_state(g.state(me, "messaging", "day_vote")), context) is TERMINATE_MESSAGING


def test_f0_default_mirrors_B0_and_factor_f00_byte_actions_and_speech():
    for me in range(7):
        for day in (1, 2):
            g = game()
            g.day = day
            g.invest = {0: "wolf", 5: "villager"}
            for messages in ([], [message(2, "I am the Seer. Player0 is a wolf.", day=day)]):
                g.messages = messages
                raw = g.state(me, "messaging", "day_vote")
                policies = [WerewolfPolicy(), FactorPolicy(0), bluff.BluffPolicy()]
                chats = [p.chat(raw) for p in policies]
                assert chats[0] == chats[1] == chats[2]
                moving = g.state(me, "moving", "day_vote", [s for s in range(7) if s != me] + [7])
                actions = [p.act(moving) for p in policies]
                assert actions[0] == actions[1] == actions[2]


def test_selection_rule_prefixed_no_post_peek_tuning():
    def reports(values):
        return {p: {"integrity_clean": True, "primary": {"delta": value},
                    "metrics": {"all": {"wolf": {"delta": value}}}} for p, value in zip(bluff.PROFILES, values)}
    assert bluff.select_replication(reports((-0.2, -0.1, -0.1))) == ("only_me_public_target", "closest_diagnostic_only")
    assert bluff.select_replication(reports((0.02, 0.01, -0.1))) == ("proactive_all", "positive_promising")
    assert bluff.select_replication(reports((0, 0, 0))) == ("only_me_public_target", "closest_diagnostic_only")
