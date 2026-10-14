import copy
import itertools
import random

import pytest

import agent.agent as runtime
from altruagent import DecisionContext, GameState, SendMessage, TERMINATE_MESSAGING
from local.werewolf_benchmark import Scenario
from local.werewolf_env import OfficialBaseline
from local.werewolf_factorial_research import FACTORS, FactorContestant, FactorPolicy
from local.werewolf_research import ResearchGame, run_task


@pytest.mark.parametrize("background", ["first", "random", "heuristic", "llmq", "rt_talker", "rt_detective", "rt_chaos", "guard_abstain"])
def test_all_factors_off_is_a_true_mirror_of_previous_policy(background):
    for game in range(5):
        rows = run_task((Scenario(201, game, background, 1),
                         [("reference", "agent", {}), ("zero", "local.werewolf_factorial_research:f00", {})],
                         {"ARENA_WW_TIMING": "rt", "ARENA_WW_TACTICS": "off"}, True, True))
        for key in ("win", "role", "seat", "days", "trace", "failures"):
            assert rows[0][key] == rows[1][key]


@pytest.mark.parametrize("mask", range(64))
def test_factor_switches_are_independent_and_fresh_preserves_them(mask):
    agent = FactorContestant(mask)
    assert [agent.ww.enabled(f) for f in FACTORS] == [bool(mask & (1 << i)) for i in range(6)]
    agent._fresh()
    assert agent.ww.mask == mask
    assert agent._choose_message.__func__.__globals__["LISTEN_FOLLOWUPS"] == (3 if mask & 32 else 1)
    assert runtime.LISTEN_FOLLOWUPS == 1


@pytest.mark.parametrize("mask", range(64))
@pytest.mark.parametrize("role,me,phase,legal", [
    ("wolf", 0, "night_wolf", [1, 2, 3, 5, 6]),
    ("seer", 2, "night_seer", [0, 1, 3, 4, 5, 6]),
    ("villager", 1, "day_vote", [0, 2, 3, 4, 5, 6, 7]),
])
def test_every_combination_uses_only_own_observation_and_legal_actions(mask, role, me, phase, legal):
    game = ResearchGame([OfficialBaseline() for _ in range(7)], random.Random(201))
    game.roles = ["wolf", "villager", "seer", "villager", "wolf", "villager", "villager"]
    raw = game.state(me, "moving", phase, legal)
    polluted = copy.deepcopy(raw)
    polluted.update(hidden_roles=["wolf"] * 7, opponent_types=["first"] * 7, game_seed=33101, controlled_seats=[me])
    polluted["game_state"]["all_investigations"] = {s: {6: "wolf"} for s in range(7)}
    ordinary = FactorPolicy(mask).act(raw)
    assert ordinary == FactorPolicy(mask).act(polluted)
    assert ordinary["action_id"] in {str(s) for s in legal}


def test_extended_listening_is_per_instance_and_obeys_protocol_quota():
    outputs = []
    for mask in (0, 32):
        agent = FactorContestant(mask)
        counter = itertools.cycle(range(1, 7))
        agent._ask_chat = lambda raw: [{"content": f"I vote Player{next(counter)}.", "recipients": []}]
        game = ResearchGame([OfficialBaseline() for _ in range(7)], random.Random(201))
        game.roles = ["villager", "villager", "seer", "wolf", "villager", "wolf", "villager"]
        context = DecisionContext("local", None, "werewolf", "ours")
        messages = []
        for step in range(8):
            game.clock = 2 * step
            result = agent.message_step(GameState.from_mcp_state(game.state(0, "messaging", "day_vote")), context)
            if result is TERMINATE_MESSAGING:
                break
            if isinstance(result, SendMessage):
                messages.append(result.content)
                game.messages.append({"seq": len(game.messages), "sender": 0, "content": result.content,
                                      "recipients": [], "visibility": "broadcast", "type": "chat", "day": 1})
        outputs.append(messages)
        assert agent._chat_sent <= 5
        assert all(len(message.split()) <= 50 for message in messages)
        assert agent.message_step(GameState.from_mcp_state(game.state(0, "messaging", "day_vote")), context) is TERMINATE_MESSAGING
    assert len(outputs[0]) == 2
    assert len(outputs[1]) == 4
    assert runtime.LISTEN_FOLLOWUPS == 1
