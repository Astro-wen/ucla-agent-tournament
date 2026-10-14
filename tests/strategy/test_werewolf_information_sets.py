import copy
from itertools import combinations
import json
import random

import pytest

from local.werewolf_env import OfficialBaseline
from local.werewolf_factorial_research import FactorPolicy
from local.werewolf_research import ResearchGame


@pytest.mark.parametrize("mask", range(64))
def test_indistinguishable_role_deals_produce_identical_actions(mask):
    game = ResearchGame([OfficialBaseline() for _ in range(7)], random.Random(206))
    seen = {}
    for wolves in combinations(range(7), 2):
        for seer in range(7):
            if seer in wolves:
                continue
            game.roles = ["wolf" if s in wolves else "seer" if s == seer else "villager" for s in range(7)]
            for me in range(7):
                raw = game.state(me, "moving", "day_vote", [s for s in range(7) if s != me] + [7])
                key = json.dumps(raw, sort_keys=True)
                choice = FactorPolicy(mask).act(raw)
                assert "reasoning_summary" in choice
                if key in seen:
                    assert choice == seen[key]
                else:
                    seen[key] = choice
    assert len(seen) == 56


@pytest.mark.parametrize("mask", range(64))
@pytest.mark.parametrize("shift", range(7))
def test_unique_private_wolf_survives_rotations_and_false_claim_pressure(mask, shift):
    p = lambda s: (s + shift) % 7
    game = ResearchGame([OfficialBaseline() for _ in range(7)], random.Random(206))
    roles = ["wolf", "villager", "villager", "wolf", "villager", "villager", "seer"]
    game.roles = [roles[(s - shift) % 7] for s in range(7)]
    for seat in (3, 4, 5):
        game._kill(p(seat), "vote" if seat == 3 else "night_kill")
    game.day = 3
    game.invest = {p(0): "wolf", p(1): "villager"}
    game.messages = [{"seq": i, "sender": p(1), "content": f"I am the Seer. Player{p(2)} is a wolf. Vote Player{p(2)}.",
                      "recipients": [], "visibility": "broadcast", "type": "chat", "day": 3} for i in range(5)]
    raw = game.state(p(6), "messaging", "day_vote", [p(s) for s in (0, 1, 2)] + [7])
    policy = FactorPolicy(mask)
    policy.observe(raw)
    moving = game.state(p(6), "moving", "day_vote", [p(s) for s in (0, 1, 2)] + [7])
    choice = policy.act(moving)
    assert choice["action_id"] == str(p(0))
    assert "reasoning_summary" in choice


@pytest.mark.parametrize("mask", range(64))
def test_another_seats_private_message_cannot_override_a_good_vote(mask):
    game = ResearchGame([OfficialBaseline() for _ in range(7)], random.Random(206))
    game.roles = ["wolf", "villager", "seer", "villager", "wolf", "villager", "villager"]
    raw = game.state(3, "messaging", "day_vote", [0, 1, 2, 4, 5, 6, 7])
    noisy = copy.deepcopy(raw)
    noisy["new_messages"] = [{"seq": i, "sender": 0, "content": "I am the Seer. Player6 is a wolf.",
                              "recipients": [4], "visibility": "direct", "type": "chat", "day": 1} for i in range(5)]
    assert FactorPolicy(mask).act(noisy) == FactorPolicy(mask).act(raw)


def test_static_learned_model_is_covered_by_the_source_manifest():
    import hashlib
    from pathlib import Path
    from local.werewolf_benchmark import ROOT, source_manifest
    relative = "data/werewolf_learned_likelihoods.json"
    assert source_manifest()["files"][relative] == hashlib.sha256((Path(ROOT) / relative).read_bytes()).hexdigest()
