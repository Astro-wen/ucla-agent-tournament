"""Functional adversarial fixtures, NOT games or evidence of opponent strength."""

import ast
import copy
import inspect
import random
from collections import Counter

import pytest

from local import hok_opponents_research as opponents


MACROS = ("macro_advance", "macro_retreat", "macro_attack",
          "macro_skill1", "macro_skill2", "macro_skill3")


def offer(seat, base, label=None, template=None):
    result = {"action_id": f"s{seat}:{base}", "label": label or base}
    if template is not None:
        result["input"] = {"action": template}
    return result


def view(seats=(0, 1, 2, 3, 4), *, pending=None, mode="realtime", hp=100, target="enemy hero 42", distance=4000):
    result = {
        "camp": 1 if min(seats) < 5 else 2,
        "seats": list(seats),
        "pacing": {"mode": mode},
        "legal_actions": {"actions": []},
        "observation": {"seats": {}},
    }
    if pending is not None:
        result["simultaneous"] = {"your_pending_seats": pending}
    for seat in seats:
        result["legal_actions"]["actions"].extend(
            offer(seat, base, f"{base} (nearest: {target}, {distance} away)") for base in MACROS)
        result["legal_actions"]["actions"].append(offer(seat, "button_0", template={"which_button": 0}))
        result["observation"]["seats"][str(seat)] = {
            "summary": {"my_hero": {"hp": hp, "max_hp": 100, "camp": result["camp"], "x": 0, "z": 0},
                        "other_heroes": [], "structures": []}}
    return result


def my_summary(raw, seat=0):
    return raw["observation"]["seats"][str(seat)]["summary"]


@pytest.mark.parametrize("name", opponents.BOT_NAMES)
@pytest.mark.parametrize("mode", ["realtime", "lockstep"])
@pytest.mark.parametrize("seats", [tuple(range(5)), tuple(range(5, 10))])
def test_all_bots_same_five_seat_opportunity_and_only_current_offers(name, mode, seats):
    raw = view(seats, mode=mode)
    original = copy.deepcopy(raw)
    bot = opponents.make_bot(name, seed=17)
    actions = [bot.decide(raw)[0] for _ in range(15)]
    assert [int(action.split(":")[0][1:]) for action in actions] == list(seats) * 3
    assert set(actions) <= {item["action_id"] for item in raw["legal_actions"]["actions"]}
    assert raw == original
    assert bot.orders == 15


@pytest.mark.parametrize("name", opponents.BOT_NAMES)
def test_pending_and_vanishing_offers_cannot_produce_stale_actions(name):
    bot = opponents.make_bot(name, seed=4)
    raw = view(pending=[4, 2, 2, True, "0", 99])
    assert [bot.decide(raw)[0].split(":")[0] for _ in range(4)] == ["s2", "s4"] * 2
    raw["simultaneous"]["your_pending_seats"] = []
    assert bot.decide(raw) == (None, None)
    raw["simultaneous"]["your_pending_seats"] = [1]
    raw["legal_actions"]["actions"] = [offer(1, "button_0")]
    assert bot.decide(raw)[0] == "s1:button_0"
    raw["legal_actions"]["actions"] = [offer(1, "macro_retreat")]
    assert bot.decide(raw)[0] == "s1:macro_retreat"
    raw["legal_actions"]["actions"] = []
    assert bot.decide(raw) == (None, None)


@pytest.mark.parametrize("name", opponents.BOT_NAMES)
def test_raw_button_legal_completion_is_shared_and_does_not_mutate_input(name):
    raw = view((0,))
    raw["legal_actions"]["actions"] = [offer(
        0, "button_7", template={"which_button": 7, "seat": 999,
                                 "legal_move_angle": [90, 180], "legal_target": [42, 43]})]
    original = copy.deepcopy(raw)
    assert opponents.make_bot(name).decide(raw)[0] == {
        "which_button": 7, "seat": 0, "move_angle": 90, "target": 42}
    assert raw == original
    raw["legal_actions"]["actions"][0]["input"]["action"]["legal_target"] = []
    assert opponents.make_bot(name).decide(raw) == (None, None)


@pytest.mark.parametrize("name", opponents.BOT_NAMES)
@pytest.mark.parametrize("raw", [None, {}, {"legal_actions": None}, {"legal_actions": {"actions": "bad"}},
                               {"legal_actions": {"actions": [None, 3, {}, {"action_id": "oops"}]}}])
def test_absent_or_malformed_offers_do_not_invent_actions(name, raw):
    assert opponents.make_bot(name).decide(raw) == (None, None)


def test_idle_is_explicit_cancellation_and_first_legal_respects_input_order():
    raw = view((0,))
    raw["simultaneous"] = {"standing_orders": {"0": "macro_advance"}}
    assert opponents.make_bot("idle").decide(raw)[0] == "s0:button_0"
    assert opponents.make_bot("first_legal").decide(raw)[0] == "s0:macro_advance"
    raw["legal_actions"]["actions"].reverse()
    assert opponents.make_bot("first_legal").decide(raw)[0] == "s0:button_0"


def test_uniform_random_uses_current_unique_macros_plus_exactly_one_idle():
    raw = view((0,))
    actions = [offer(0, "macro_advance"), offer(0, "macro_retreat"), offer(0, "macro_future"),
               offer(0, "button_0")]
    raw["legal_actions"]["actions"] = actions + [copy.deepcopy(actions[0]), offer(0, "button_9")]
    bot = opponents.make_bot("uniform_random", seed=146)
    reference = random.Random(146)
    expected_menu = [action["action_id"] for action in actions]
    draws = [bot.decide(raw)[0] for _ in range(4000)]
    assert draws == [reference.choice(expected_menu) for _ in draws]
    assert set(draws) == set(expected_menu)
    assert all(850 < count < 1150 for count in Counter(draws).values())
    raw["legal_actions"]["actions"] = [offer(0, "macro_skill1"), offer(0, "button_0")]
    assert {bot.decide(raw)[0] for _ in range(100)} == {"s0:macro_skill1", "s0:button_0"}


@pytest.mark.parametrize("name", ["uniform_random", *opponents.STRESS_NAMES])
def test_seeds_reproduce_are_meaningful_and_do_not_touch_global_rng(name):
    raw = view(target="enemy hero 42", distance=4000, hp=30)
    global_state = random.getstate()
    sequences = []
    for seed in (11, 11, 12, 13, 14, 15):
        bot = opponents.make_bot(name, seed=seed)
        sequences.append([bot.decide(raw)[0] for _ in range(60)])
    assert sequences[0] == sequences[1]
    assert any(sequence != sequences[0] for sequence in sequences[2:])
    assert random.getstate() == global_state


def test_objective_and_combat_are_distinct_and_use_labelled_current_targets():
    raw = view((0,), target="enemy hero 42")
    assert opponents.make_bot("objective_rusher").decide(raw)[0] == "s0:macro_advance"
    assert opponents.make_bot("opportunistic_combat").decide(raw)[0] == "s0:macro_skill3"
    raw = view((0,), target="enemy crystal 99")
    assert opponents.make_bot("objective_rusher").decide(raw)[0] == "s0:macro_skill3"
    raw = view((0,), target="enemy hero 42", distance=50000)
    assert opponents.make_bot("opportunistic_combat").decide(raw)[0] == "s0:macro_advance"
    raw = view((0,), target="friendly hero 42")
    assert opponents.make_bot("opportunistic_combat").decide(raw)[0] == "s0:macro_advance"


def test_cautious_defender_recovers_without_threshold_flapping_and_resets_on_death():
    bot = opponents.make_bot("cautious_defender")
    raw = view((0,), hp=40)
    assert bot.decide(raw)[0] == "s0:macro_retreat"
    my_summary(raw)["my_hero"]["hp"] = 70
    assert bot.decide(raw)[0] == "s0:macro_retreat"
    my_summary(raw)["my_hero"]["hp"] = 95
    assert bot.decide(raw)[0] == "s0:macro_skill3"
    my_summary(raw)["my_hero"]["hp"] = 0
    assert bot.decide(raw)[0] == "s0:button_0"
    my_summary(raw)["my_hero"]["hp"] = 100
    assert bot.decide(raw)[0] == "s0:macro_skill3"


def test_cautious_defender_only_counts_visible_live_heroes_and_deduplicates_ids():
    raw = view((0,))
    heroes = [{"runtime_id": index, "camp": 2, "x": 100, "z": 100, "hp": 100, "visible": False}
              for index in (1, 2)]
    my_summary(raw)["other_heroes"] = heroes
    bot = opponents.make_bot("cautious_defender")
    assert bot.decide(raw)[0] == "s0:macro_skill3"
    for hero in heroes:
        hero["visible"] = True
    assert bot.decide(raw)[0] == "s0:macro_retreat"
    heroes[1]["hp"] = 0
    assert bot.decide(raw)[0] == "s0:macro_skill3"
    heroes[1].update(heroes[0])
    assert bot.decide(raw)[0] == "s0:macro_skill3"


def test_cautious_defender_guards_visibly_threatened_allied_structure():
    raw = view((0,), distance=20000)
    my_summary(raw)["structures"] = [{"type": "TOWER", "camp": 1, "hp": 100, "x": 100, "z": 0}]
    enemy = {"runtime_id": 42, "camp": 2, "hp": 100, "x": 1000, "z": 0, "visible": True}
    my_summary(raw)["other_heroes"] = [enemy]
    assert opponents.make_bot("cautious_defender").decide(raw)[0] == "s0:button_0"
    enemy["visible"] = False
    assert opponents.make_bot("cautious_defender").decide(raw)[0] == "s0:macro_advance"


def test_pulse_is_per_hero_not_per_global_call_and_split_roles_are_camp_symmetric():
    raw = view()
    bot = opponents.make_bot("pulse_pressure", seed=7)
    per_round = [[bot.decide(raw)[0].split(":")[1] for _ in range(5)] for _ in range(6)]
    assert all(len(set(actions)) == 1 for actions in per_round)
    assert Counter(actions[0] for actions in per_round) == {"macro_retreat": 2, "macro_skill3": 4}
    a, b = opponents.make_bot("split_pressure", seed=9), opponents.make_bot("split_pressure", seed=9)
    left, right = view(hp=30), view(tuple(range(5, 10)), hp=30)
    a_actions = [a.decide(left)[0].split(":")[1] for _ in range(5)]
    b_actions = [b.decide(right)[0].split(":")[1] for _ in range(5)]
    assert a_actions == b_actions
    assert Counter(a_actions) == {"macro_advance": 2, "macro_skill3": 2, "macro_retreat": 1}


@pytest.mark.parametrize("name", opponents.BOT_NAMES)
def test_unrelated_hidden_metadata_future_actions_and_candidate_name_are_ignored(name):
    plain = view()
    extra = copy.deepcopy(plain)
    extra.update({"hidden_state": {"all_enemy_hp": 1}, "candidate_policy_name": "some_candidate",
                  "future_actions": ["retreat"], "simulator": {"true_positions": [99, 100]}})
    for entry in extra["observation"]["seats"].values():
        entry["summary"]["unobserved_enemies"] = [{"hp": 1, "x": 0, "z": 0}]
    a, b = opponents.make_bot(name, seed=9), opponents.make_bot(name, seed=9)
    assert [a.decide(plain) for _ in range(30)] == [b.decide(extra) for _ in range(30)]


@pytest.mark.parametrize("name", opponents.BOT_NAMES)
def test_result_errors_do_not_reset_scheduler_or_leak_future_information(name):
    bot = opponents.make_bot(name, seed=10)
    raw = view()
    assert bot.decide(raw)[0].startswith("s0:")
    bot.on_result({"error": "refused", "hidden_state": {"next_action": "macro_retreat"}})
    bot.on_result(None)
    bot.on_result({"ok": True})
    assert bot.refused == 1
    assert bot.decide(raw)[0].startswith("s1:")
    assert "refused=1" in bot.summary_text()


def test_factory_validation_aliases_and_import_independence():
    assert opponents.make_bot("first-legal").name == "first_legal"
    assert opponents.make_bot("random").name == "uniform_random"
    with pytest.raises(ValueError, match="Unknown HoK"):
        opponents.make_bot("candidate_policy")
    with pytest.raises(ValueError, match="must be a string"):
        opponents.make_bot(None)
    tree = ast.parse(inspect.getsource(opponents))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module)
    assert not any(module.split(".")[0] in {"agent", "champion", "altruagent", "local"} for module in imported)
