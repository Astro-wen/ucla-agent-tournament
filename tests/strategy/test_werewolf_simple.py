from copy import deepcopy
from dataclasses import replace

import pytest

from agent.werewolf.policy import WerewolfPolicy
from agent.werewolf.state import View
from local.werewolf_simple import SimplePolicy


def view(role="wolf"):
    return View(me=6, role=role, day=2, sub_phase="night_wolf", alive=[1, 2, 3, 4, 5, 6],
                dead={0: "villager"}, dead_cause={0: "vote"}, allies=[5] if role == "wolf" else [],
                votes=[(1, {i: 0 if i else 1 for i in range(7)}, 0)])


def policy(mode):
    p = SimplePolicy()
    p.mode = mode
    return p


@pytest.mark.parametrize("mode", ["night", "inference", "combined"])
@pytest.mark.parametrize("role", ["wolf", "villager", "seer"])
def test_first_day_is_identical_without_observed_behavior(mode, role):
    v = replace(view(role), day=1, votes=[], alive=list(range(7)), dead={}, dead_cause={})
    targets = [s for s in v.alive if s != v.me]
    p, reference = policy(mode), WerewolfPolicy()
    assert p._day_vote(v, targets) == reference._day_vote(v, targets)
    assert p._opening(v) == reference._opening(v)


@pytest.mark.parametrize("mode", ["night", "combined"])
def test_night_preserves_low_daytime_targets_after_fixed_votes(mode):
    v = view()
    before = deepcopy(v)
    assert policy(mode)._simple_table(v) == "first"
    assert policy(mode)._kill_plan(v, [1, 2, 3, 4])[0] == 4
    assert v == before


def test_inference_ablation_preserves_night_behavior():
    v = view()
    assert policy("inference")._kill_plan(v, [1, 2, 3, 4]) == WerewolfPolicy()._kill_plan(v, [1, 2, 3, 4])


@pytest.mark.parametrize("text", ["I am the Seer. Player5 is a wolf.", "Player1 is a wolf.", "Player2 is clear."])
def test_public_evidence_disables_simple_model(text):
    v = view()
    v.messages = [{"sender": 3, "seq": 1, "content": text, "recipients": [], "day": 1}]
    p = policy("combined")
    assert p._simple_table(v) is None
    assert p._kill_plan(v, [1, 2, 3, 4]) == WerewolfPolicy()._kill_plan(v, [1, 2, 3, 4])


@pytest.mark.parametrize("deviant", [None, 2, 3, 4, 5, 6])
def test_one_unexplained_ballot_disables_fixed_model(deviant):
    v = view()
    v.votes[0][1][1] = deviant
    assert policy("combined")._simple_table(v) is None


def test_silence_alone_does_not_establish_model():
    assert policy("combined")._simple_table(replace(view(), votes=[])) is None


def test_abstention_is_not_the_fixed_vote_night_model():
    v = view()
    v.votes = [(1, {i: None for i in range(7)}, None)]
    p = policy("combined")
    assert p._simple_table(v) == "abstain"
    assert p._kill_plan(v, [1, 2, 3, 4]) == WerewolfPolicy()._kill_plan(v, [1, 2, 3, 4])


@pytest.mark.parametrize("mode", ["inference", "combined"])
def test_skipped_night_target_is_a_hypothesis_not_a_seer_result(mode):
    v = replace(view("villager"), sub_phase="day_vote", alive=[1, 2, 4, 5, 6],
                dead={0: "villager", 3: "seer"}, dead_cause={0: "vote", 3: "night_kill"})
    p = policy(mode)
    choice, reason = p._day_vote(v, [1, 2, 4, 5])
    assert choice == 1
    assert "not a verified role" in reason
    assert v.investigations == {}
    assert all("I am the Seer" not in m[1]["content"] for m in p._plan(v))


def test_seer_private_wolf_and_clear_override_inference():
    v = replace(view("seer"), sub_phase="day_vote", alive=[1, 2, 4, 5, 6],
                dead={0: "villager", 3: "villager"}, dead_cause={0: "vote", 3: "night_kill"},
                investigations={1: "villager", 5: "wolf"})
    p = policy("combined")
    assert p._day_vote(v, [1, 2, 4, 5])[0] == 5
    v.investigations = {1: "villager"}
    assert p._day_vote(v, [1, 2, 4, 5])[0] == 2


@pytest.mark.parametrize("targets", [[1], [2], [3], [4], [2, 3]])
def test_night_never_selects_outside_legal_targets(targets):
    assert policy("combined")._kill_plan(view(), targets)[0] in targets


def test_private_chat_and_display_names_cannot_establish_fixed_behavior():
    v = view()
    v.names = {i: f"opponent-random-{i}" for i in range(7)}
    v.messages = [{"sender": 5, "seq": 1, "content": "Player2 is a wolf.", "visibility": "direct", "recipients": [6]}]
    p = policy("combined")
    assert p._simple_table(v) == "first"
    assert p._kill_plan(v, [1, 2, 3, 4])[0] == 4


def test_fixed_solver_terminal_and_forced_low_seat_loss():
    from local.werewolf_simple import fixed_value
    assert fixed_value((0, 1), (0,), 0, False) == 1
    assert fixed_value((0, 1), (0,), 1, False) == 0
    assert fixed_value((0, 1, 2), (), 1, False) == 1
    assert fixed_value(tuple(range(7)), (0, 1), 0, False) == 0


def test_fixed_solver_preserves_a_needed_daytime_scapegoat():
    from local.werewolf_simple import fixed_actions
    values = fixed_actions((1, 2, 3, 4), (2,), 2, True, (1, 3, 4))
    assert values[1] == 0
    assert values[3] == values[4] == 1


@pytest.mark.parametrize("me", range(7))
def test_omniscient_upper_bound_matches_engine_for_every_wolf_pair(me):
    from itertools import combinations
    import random
    from local.werewolf_env import Game, OfficialBaseline
    from local.werewolf_simple import fixed_actions, fixed_value

    for wolves in combinations(range(7), 2):
        class Oracle(OfficialBaseline):
            def choose(self, raw):
                phase = raw["game_state"]["phase"]
                if phase == "night_seer" or raw["game_state"]["day"] == 1 and phase == "night_wolf":
                    return super().choose(raw)
                names = {p["name"]: p["position"] for p in raw["players"]}
                alive = tuple(sorted(names[n] for n in raw["game_state"]["alive"]))
                targets = tuple(int(a["action_id"]) for a in raw["legal_actions"]["actions"])
                stall = 0
                for vote in reversed(raw["game_state"]["vote_history"]):
                    if vote["eliminated"] is not None:
                        break
                    stall += 1
                values = fixed_actions(alive, wolves, me, phase == "night_wolf", targets, stall)
                return str(max(values, key=lambda s: (values[s], -s)))

        seats = [Oracle() if s == me else OfficialBaseline() for s in range(7)]
        game = Game(seats, random.Random(0))
        game.roles = ["wolf" if s in wolves else "villager" for s in range(7)]
        game.roles[next(s for s in range(7) if s not in wolves)] = "seer"
        outcomes = game.play()
        assert (outcomes[me] > 0) == bool(fixed_value(tuple(range(7)), wolves, me, False))
        assert all(not any(game.stats[k]) for k in ("crash", "illegal", "bad_chat", "timeout"))


def test_new_seed_registry_is_disjoint_from_consumed_confirmations():
    from local.werewolf_benchmark import SEED_REGISTRIES
    dev, heldout = SEED_REGISTRIES["simple"]
    old = {s for name, groups in SEED_REGISTRIES.items() if name != "simple" for group in groups for s in group}
    assert not (set(dev) | set(heldout)) & old
    assert not set(dev) & set(heldout)
