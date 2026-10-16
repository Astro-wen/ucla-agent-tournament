"""Functional adversarial fixtures, NOT evidence of competitive strength.

The final tests import the actual local.hok_env when present. In an isolated
opponent worktree, set HOK_EVALUATOR_ROOT to a checkout containing that module.
Its local package is appended read-only; this checkout's opponent implementation
remains the one under test. Without an evaluator, only those tests are skipped.
"""

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
def test_lockstep_pending_and_vanishing_offers_cannot_produce_stale_actions(name):
    bot = opponents.make_bot(name, seed=4)
    raw = view(mode="lockstep", pending=[4, "2", 2, True, "0.0", 99])
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
    assert opponents.make_bot("objective_rusher").decide(raw)[0] == "s0:macro_attack"
    raw = view((0,), target="enemy hero 42", distance=50000)
    assert opponents.make_bot("opportunistic_combat").decide(raw)[0] == "s0:macro_attack"
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
    # No attack is offered: this guard fixture has no attack-ready target.
    raw["legal_actions"]["actions"] = [action for action in raw["legal_actions"]["actions"]
                                       if action["action_id"] != "s0:macro_attack"]
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


@pytest.mark.parametrize("name", opponents.BOT_NAMES)
@pytest.mark.parametrize("mode", ["realtime", "lockstep"])
def test_many_sparse_changing_offers_select_current_eligible_seat(name, mode):
    """A synthetic input/legality check, not a simulator or win-rate sample."""
    generator = random.Random(256)
    bot = opponents.make_bot(name, seed=51)
    for _ in range(160):
        raw = view(mode=mode)
        actions = []
        for seat in range(5):
            menu = [*MACROS, "button_0", "macro_future"]
            generator.shuffle(menu)
            actions.extend(offer(seat, base) for base in menu[:generator.randrange(1, len(menu) + 1)])
        generator.shuffle(actions)
        raw["legal_actions"]["actions"] = actions
        pending = generator.sample(range(5), generator.randrange(6))
        raw["simultaneous"] = {"your_pending_seats": [str(seat) for seat in pending],
                               "standing_orders": {str(seat): "macro_skill3" for seat in range(5)}}
        action, _ = bot.decide(raw)
        if mode == "lockstep" and not pending:
            assert action is None
        else:
            assert action in {item["action_id"] for item in actions}
            assert int(action.split(":")[0][1:]) in (pending if mode == "lockstep" else range(5))
        bot.on_result({"error": "synthetic refusal"} if generator.random() < 0.1 else {})


@pytest.mark.parametrize("name", opponents.BOT_NAMES)
def test_unusual_observation_values_and_unknown_labels_do_not_crash(name):
    raw = view()
    for entry in raw["observation"]["seats"].values():
        entry["summary"] = {
            "my_hero": {"hp": float("nan"), "max_hp": 10**1000, "x": True, "z": float("inf"), "camp": []},
            "other_heroes": [None, 3, {"visible": True, "x": 1, "z": 2, "camp": [1], "runtime_id": []}],
            "structures": [None, {"camp": [], "hp": 100, "x": 1, "z": 2, "type": {}}],
        }
    for action in raw["legal_actions"]["actions"]:
        action["label"] = {"unexpected": "object"}
    assert opponents.make_bot(name, seed=2).decide(raw)[0] in {
        action["action_id"] for action in raw["legal_actions"]["actions"]}


@pytest.mark.parametrize("name", opponents.BOT_NAMES)
@pytest.mark.parametrize("pending", [[], ["1", "3"], [4, 2, True, "bad"]])
@pytest.mark.parametrize("start", [0, 5])
def test_realtime_pending_is_not_permission_and_owned_five_seats_still_rotate(name, pending, start):
    seats = tuple(range(start, start + 5))
    raw = view(seats, pending=pending)
    raw["seats"] = [str(seat) for seat in reversed(seats)]
    raw["camp"] = str(raw["camp"])
    raw["simultaneous"]["standing_orders"] = {str(seat): "macro_advance" for seat in seats}
    # Even if an extraneous offer is present, it is not an owned hero.
    raw["legal_actions"]["actions"].append(offer((start + 5) % 10, "macro_advance"))
    raw["legal_actions"]["actions"].reverse()
    original = copy.deepcopy(raw)
    bot = opponents.make_bot(name, seed=27)
    actual = [bot.decide(raw)[0] for _ in range(15)]
    assert [int(action.split(":")[0][1:]) for action in actual] == list(seats) * 3
    assert raw == original


@pytest.mark.parametrize("name", opponents.BOT_NAMES)
@pytest.mark.parametrize("mode", ["realtime", "lockstep"])
def test_mixed_pending_standing_orders_and_raw_buttons_never_reuse_disappeared_choices(name, mode):
    raw = view((2,), mode=mode, pending=["2"])
    raw["seats"] = ["2"]
    raw["simultaneous"]["standing_orders"] = {"2": "macro_skill3"}
    raw["legal_actions"]["actions"] = [
        offer(2, "button_2", template={"which_button": 2, "seat": "2",
                                      "legal_move_x": [7, 8], "legal_move_z": [9, 10]})]
    bot = opponents.make_bot(name, seed=2)
    assert bot.decide(raw)[0] == {"which_button": 2, "seat": 2, "move_x": 7, "move_z": 9}
    raw["simultaneous"]["your_pending_seats"] = []
    if mode == "lockstep":
        assert bot.decide(raw) == (None, None)
        raw["simultaneous"]["your_pending_seats"] = [2]
    else:
        assert bot.decide(raw)[0] == {"which_button": 2, "seat": 2, "move_x": 7, "move_z": 9}
    # Same button, different current legal values: no stale completed payload.
    raw["legal_actions"]["actions"][0]["input"]["action"]["legal_move_x"] = [15]
    assert bot.decide(raw)[0] == {"which_button": 2, "seat": 2, "move_x": 15, "move_z": 9}
    # Now neither the standing macro nor the old raw button is available.
    raw["legal_actions"]["actions"] = [offer(2, "button_0", template={"which_button": 0, "seat": "2"})]
    assert bot.decide(raw)[0] == "s2:button_0"
    raw["legal_actions"]["actions"] = [offer(2, "macro_retreat")]
    assert bot.decide(raw)[0] == "s2:macro_retreat"


def test_idle_explicitly_clears_all_five_already_fresh_standing_orders():
    raw = view(pending=[])
    raw["simultaneous"]["standing_orders"] = {str(seat): "macro_attack" for seat in range(5)}
    bot = opponents.make_bot("idle")
    assert [bot.decide(raw)[0] for _ in range(5)] == [f"s{seat}:button_0" for seat in range(5)]


@pytest.mark.parametrize("mode", ["realtime", "lockstep"])
@pytest.mark.parametrize("bad", [True, False, 0.0, 1.5, "1.0", "1e0", "-1", "99", "", "１", "1" * 5000, None, {}])
def test_bad_owned_seat_values_are_not_coerced_to_another_players_hero(mode, bad):
    raw = view(mode=mode, pending=list(range(5)))
    raw["seats"] = [bad]
    assert opponents.make_bot("first_legal").decide(raw) == (None, None)


def test_ownership_falls_back_to_public_camp_mapping_only_when_roster_absent():
    raw = view(tuple(range(10)), pending=[])
    del raw["seats"]
    raw["camp"] = "2"
    bot = opponents.make_bot("idle")
    assert [bot.decide(raw)[0] for _ in range(5)] == [f"s{seat}:button_0" for seat in range(5, 10)]
    raw["seats"] = []
    assert bot.decide(raw) == (None, None)
    raw["seats"] = None
    assert bot.decide(raw) == (None, None)


def test_camp_and_summary_seat_strings_are_normalized_without_inventing_enemies():
    raw = view((0,))
    raw["camp"] = "1"
    summary = my_summary(raw)
    summary["my_hero"]["camp"] = 1
    summary["other_heroes"] = [
        {"runtime_id": index, "camp": camp, "visible": True, "hp": 100, "x": 1, "z": 1}
        for index, camp in enumerate((1, "1", "2"))
    ]
    # Non-canonical decimal spelling is still the same owned seat.
    raw["observation"]["seats"] = {" 00 ": {"summary": summary}}
    raw["seats"] = [" 00 "]
    bot = opponents.make_bot("cautious_defender")
    assert bot.decide(raw)[0] == "s0:macro_skill3"  # Two allies plus self vs one enemy.
    summary["other_heroes"] = [
        {"runtime_id": index, "camp": camp, "visible": True, "hp": 100, "x": 1, "z": 1}
        for index, camp in enumerate((2, "2"))
    ]
    assert bot.decide(raw)[0] == "s0:macro_retreat"


@pytest.mark.parametrize("camp", [True, False, 2.0, "2.0", "2e0", "-2", "99", "", [], {}])
def test_invalid_other_hero_camps_are_unknown_not_hostile(camp):
    raw = view((0,))
    my_summary(raw)["other_heroes"] = [
        {"runtime_id": index, "camp": camp, "visible": True, "hp": 100, "x": 1, "z": 1}
        for index in range(3)]
    assert opponents.make_bot("cautious_defender").decide(raw)[0] == "s0:macro_skill3"


def test_defender_guards_allied_structure_with_mixed_integer_string_camps():
    raw = view((0,), distance=20000)
    raw["legal_actions"]["actions"] = [action for action in raw["legal_actions"]["actions"]
                                       if action["action_id"] != "s0:macro_attack"]
    raw["camp"] = "1"
    del my_summary(raw)["my_hero"]["camp"]  # Public guide does not require this field.
    my_summary(raw)["structures"] = [{"type": "CRYSTAL", "camp": 1, "hp": 100, "x": 100, "z": 0}]
    my_summary(raw)["other_heroes"] = [
        {"runtime_id": 42, "camp": "2", "hp": 100, "x": 1000, "z": 0, "visible": True}]
    assert opponents.make_bot("cautious_defender").decide(raw)[0] == "s0:button_0"


@pytest.mark.parametrize("name", ["opportunistic_combat", "cautious_defender"])
@pytest.mark.parametrize("label", [None, "Basic attack", "Basic attack (nearest: enemy hero 42, 50000 away)"])
def test_offered_attack_readiness_is_not_overruled_by_missing_or_large_label_distance(name, label):
    raw = view((0,), pending=[], distance=50000)
    for action in raw["legal_actions"]["actions"]:
        if action["action_id"] == "s0:macro_attack":
            action["label"] = label
    bot = opponents.make_bot(name, seed=3)
    assert bot.decide(raw)[0] == "s0:macro_attack"
    raw["legal_actions"]["actions"] = [action for action in raw["legal_actions"]["actions"]
                                       if action["action_id"] != "s0:macro_attack"]
    assert bot.decide(raw)[0] == "s0:macro_advance"


def test_objective_rusher_uses_offered_structure_attack_without_guessed_attack_radius():
    raw = view((0,), target="enemy tower 99", distance=50000)
    assert opponents.make_bot("objective_rusher").decide(raw)[0] == "s0:macro_attack"


def test_realtime_defender_reevaluates_already_ordered_hurt_hero_within_one_rotation():
    raw = view(pending=["4"])
    raw["simultaneous"]["standing_orders"] = {str(seat): "macro_advance" for seat in range(5)}
    bot = opponents.make_bot("cautious_defender")
    assert bot.decide(raw)[0] == "s0:macro_skill3"
    raw["simultaneous"]["standing_orders"]["0"] = "macro_skill3"
    my_summary(raw, 0)["my_hero"]["hp"] = 20
    actions = [bot.decide(raw)[0] for _ in range(5)]
    assert [int(action.split(":")[0][1:]) for action in actions] == [1, 2, 3, 4, 0]
    assert actions[-1] == "s0:macro_retreat"


@pytest.mark.parametrize("name", opponents.BOT_NAMES)
def test_ready_macro_input_action_does_not_require_raw_button_completion(name):
    raw = view((3,), pending=[])
    raw["seats"] = ["3"]
    raw["legal_actions"]["actions"] = [
        offer(3, "macro_retreat", template={"macro": "macro_retreat", "seat": "3"})]
    assert opponents.make_bot(name).decide(raw)[0] == "s3:macro_retreat"


@pytest.mark.parametrize("name", opponents.BOT_NAMES)
def test_optional_clock_injection_preserves_seeded_opportunity_based_cadence(name):
    def unnecessary_clock_read():
        raise AssertionError("Opportunity-based opponents should not read any clock")

    raw = view(pending=[])
    original = opponents.make_bot(name, seed=41)
    injected = opponents.make_bot(name, seed=41, clock=unnecessary_clock_read)
    assert injected._clock is unnecessary_clock_read
    assert [original.decide(raw) for _ in range(50)] == [injected.decide(raw) for _ in range(50)]


@pytest.mark.parametrize("name", opponents.BOT_NAMES)
def test_bounded_runtime_calls_have_stable_rotation_without_quiescence_or_surrender(name):
    raw = view(pending=[])
    raw["simultaneous"]["standing_orders"] = {str(seat): "macro_advance" for seat in range(5)}
    game_time = [0.0]
    bot = opponents.make_bot(name, seed=83, clock=lambda: game_time[0])
    actions = []
    # The caller, not the policy, owns this finite equal-opportunity budget.
    for _ in range(4):
        for _ in range(7):
            action, _ = bot.decide(raw)
            assert action in {item["action_id"] for item in raw["legal_actions"]["actions"]}
            actions.append(action)
            bot.on_result({"ok": True})
        game_time[0] += 0.2
    assert [int(action.split(":")[0][1:]) for action in actions] == [index % 5 for index in range(28)]
    if name == "uniform_random":
        assert len({action.split(":")[1] for action in actions}) > 1


@pytest.fixture
def research_evaluator(monkeypatch):
    """Use the real evaluator API, never a replacement simulator/test adapter."""
    import importlib
    import os
    import sys
    from pathlib import Path

    import local

    root = os.environ.get("HOK_EVALUATOR_ROOT")
    if root:
        local_path = Path(root).resolve() / "local"
        if not (local_path / "hok_env.py").is_file():
            pytest.fail("HOK_EVALUATOR_ROOT must contain local/hok_env.py")
        monkeypatch.setattr(sys, "dont_write_bytecode", True)
        monkeypatch.setattr(local, "__path__", [*local.__path__, str(local_path)])
    monkeypatch.setenv("ARENA_RECORD", "0")
    try:
        return importlib.import_module("local.hok_env")
    except ModuleNotFoundError as error:
        if error.name != "local.hok_env":
            raise  # A missing dependency is an incompatibility, not an absent evaluator.
        pytest.skip("local.hok_env unavailable; set HOK_EVALUATOR_ROOT for integration coverage")


@pytest.mark.parametrize("name", opponents.ALL_BOT_NAMES)
@pytest.mark.parametrize("mode", ["realtime", "lockstep"])
@pytest.mark.parametrize("candidate_camp", [1, 2])
def test_actual_evaluator_factory_short_camp_swapped_matrix(research_evaluator, name, mode, candidate_camp):
    """Synthetic interface exercise only; do NOT interpret the scores as strength."""
    env = research_evaluator
    # Prefix is required: bare idle/random may select the evaluator's own bots.
    candidate, metadata = env.opponent_factory(f"research:{name}")
    other, _ = env.opponent_factory("research:uniform_random")
    clock = lambda: 0.0
    probe = candidate(seed=19, clock=clock)
    assert isinstance(probe, opponents.ResearchBot)
    assert probe.name == name
    assert probe._clock is clock
    assert metadata["clock_parameter"]
    config = env.SimConfig(mode=mode, max_steps=4)
    kwargs = dict(config=config, seed=1016, candidate_camp=candidate_camp, calls_per_step=7)
    report = env.play_game(candidate, other, **kwargs)
    assert report["protocol_failure"] is None
    assert report["steps"] == 4
    assert report["elapsed_game_s"] > 0
    for stats in report["stats"].values():
        assert stats["invalid"] == stats["local_validation_errors"] == stats["resignations"] == 0
        assert not stats["exceptions"]
        assert not stats["callback_exceptions"]
    for role, camp in (("candidate", candidate_camp), ("opponent", 3 - candidate_camp)):
        stats = report["stats"][role]
        # These bots issue an order on every offered realtime call. Exhausting
        # a finite budget is normal, and both teams must then advance game time.
        expected = 4 * (7 if mode == "realtime" else 5)
        assert stats["calls"] == expected, (
            "Realtime pending-empty must not end the runtime budget or hide owned offers", role, stats["calls"])
        assert stats["accepted"] == stats["calls"]
        counts = stats["accepted_by_seat"]
        start = 0 if camp == 1 else 5
        assert set(counts) == {str(seat) for seat in range(start, start + 5)}
        assert max(counts.values()) - min(counts.values()) <= 1
    if name == "uniform_random":
        repeated = env.play_game(candidate, other, **kwargs)
        assert repeated["trajectory_sha256"] == report["trajectory_sha256"]
        assert repeated["elapsed_game_s"] == report["elapsed_game_s"]


@pytest.mark.parametrize("camp", [1, 2])
def test_actual_evaluator_realtime_keeps_owned_offers_after_all_heroes_are_fresh(research_evaluator, camp):
    env = research_evaluator
    game = env.DiagnosticGame(env.SimConfig(mode="realtime", max_steps=2), seed=1016)
    factory, _ = env.opponent_factory("research:uniform_random")
    bot = factory(seed=21, clock=lambda: 0.0)
    for _ in range(5):
        raw = game.view(camp)
        action, _ = bot.decide(raw)
        result = game.submit(camp, action, raw["state_version"])
        assert not result.get("error")
        bot.on_result(result)
    raw = game.view(camp)
    assert raw["simultaneous"]["your_pending_seats"] == []
    offered = {int(action["action_id"].split(":")[0][1:]) for action in raw["legal_actions"]["actions"]}
    assert offered == set(raw["seats"]), "Realtime fresh heroes must remain offered and updateable"
    action, _ = bot.decide(raw)
    assert action is not None
    result = game.submit(camp, action, raw["state_version"])
    assert not result.get("error")
    bot.on_result(result)


def test_development_catalog_stays_frozen_and_heldout_definitions_are_separate():
    assert opponents.BOT_NAMES == (
        "idle", "first_legal", "uniform_random", "objective_rusher",
        "opportunistic_combat", "cautious_defender", "pulse_pressure", "split_pressure")
    assert opponents.HELDOUT_NAMES == ("relay_pressure", "counterpunch_interceptor")
    assert not set(opponents.BOT_NAMES) & set(opponents.HELDOUT_NAMES)
    assert opponents.ALL_BOT_NAMES == opponents.BOT_NAMES + opponents.HELDOUT_NAMES
    assert opponents.HELDOUT_PARAMETERS == {
        "relay_pressure": {
            "emergency_hp": 0.20, "request_hp": 0.60, "release_hp": 0.85,
            "minimum_net_hp_gain": 0.01, "stagnant_owner_opportunities": 12,
            "routine_cooldown_opportunities": 10, "routine_permits": 1,
        },
        "counterpunch_interceptor": {
            "emergency_hp": 0.25, "vulnerable_hp": 0.45, "observed_hp_loss": 0.20,
            "approach_fraction": 0.10, "hold_opportunities_per_visible_target": 3,
        },
    }
    assert set(opponents.WEAK_CONTROL_SCOPE) == {"idle", "first_legal", "uniform_random"}
    assert all("human" in description or "strategic" in description
               for description in opponents.WEAK_CONTROL_SCOPE.values())


def visible_foe(*, x=4000, hp=100, maximum=100, visible=True, identity=42):
    return {"runtime_id": identity, "camp": "2", "x": x, "z": 0,
            "hp": hp, "max_hp": maximum, "visible": visible}


def heldout_view(seats=(0, 1, 2, 3, 4), *, mode="realtime", hp=50):
    raw = view(seats, mode=mode, hp=hp, pending=[] if mode == "realtime" else list(seats))
    for entry in raw["observation"]["seats"].values():
        foe = visible_foe(hp=35)
        foe["camp"] = str(3 - raw["camp"])
        entry["summary"]["other_heroes"] = [foe]
    return raw


@pytest.mark.parametrize("name", opponents.HELDOUT_NAMES)
@pytest.mark.parametrize("mode", ["realtime", "lockstep"])
@pytest.mark.parametrize("seats", [tuple(range(5)), tuple(range(5, 10))])
def test_heldout_shared_legality_five_seat_rotation_and_disappearing_actions(name, mode, seats):
    raw = heldout_view(seats, mode=mode)
    raw["seats"] = [str(seat) for seat in seats]
    raw["simultaneous"]["standing_orders"] = {str(seat): "macro_skill3" for seat in seats}
    original = copy.deepcopy(raw)
    bot = opponents.make_bot(name, seed=1707)
    actions = [bot.decide(raw)[0] for _ in range(25)]
    assert [int(action.split(":")[0][1:]) for action in actions] == list(seats) * 5
    assert set(actions) <= {action["action_id"] for action in raw["legal_actions"]["actions"]}
    assert raw == original
    seat = seats[-1]
    raw["simultaneous"]["your_pending_seats"] = [str(seat)]
    raw["legal_actions"]["actions"] = [
        offer(seat, "button_2", template={"which_button": 2, "legal_move_x": [3, 4], "legal_move_z": [5]})]
    assert bot.decide(raw)[0] == {"seat": seat, "which_button": 2, "move_x": 3, "move_z": 5}
    raw["legal_actions"]["actions"] = [offer(seat, "button_0")]
    assert bot.decide(raw)[0] == f"s{seat}:button_0"
    raw["legal_actions"]["actions"] = []
    assert bot.decide(raw) == (None, None)


@pytest.mark.parametrize("name", opponents.HELDOUT_NAMES)
def test_heldout_seed_reproduction_camp_symmetry_and_fair_clock(name):
    def irrelevant_clock():
        raise AssertionError("Held-out policies count opportunities, not time or hidden state")

    sequences = []
    for seed in (17, 17, 18, 19, 20, 21):
        bot = opponents.make_bot(name, seed=seed, clock=irrelevant_clock)
        raw = heldout_view()
        sequences.append([bot.decide(raw)[0] for _ in range(60)])
    assert sequences[0] == sequences[1]
    assert any(sequence != sequences[0] for sequence in sequences[2:])
    mirror = opponents.make_bot(name, seed=17, clock=lambda: 10**6)
    raw = heldout_view(tuple(range(5, 10)))
    mirrored = [mirror.decide(raw)[0].split(":")[1] for _ in range(60)]
    assert mirrored == [action.split(":")[1] for action in sequences[0]]


@pytest.mark.parametrize("name", opponents.HELDOUT_NAMES)
def test_heldout_fog_and_unowned_summaries_cannot_change_decisions(name):
    raw = heldout_view()
    poisoned = copy.deepcopy(raw)
    poisoned.update({"hidden_state": {"enemy_hp": 0}, "candidate_policy_name": "never_read",
                     "future_actions": ["macro_attack"]})
    poisoned["observation"]["seats"]["5"] = {"summary": {"my_hero": {"hp": 1, "max_hp": 100}}}
    for entry in poisoned["observation"]["seats"].values():
        entry["summary"].setdefault("other_heroes", []).extend([
            visible_foe(x=0, hp=1, visible=False, identity=100),
            visible_foe(x=1, hp=0, visible=True, identity=101),
        ])
    a, b = opponents.make_bot(name, seed=1707), opponents.make_bot(name, seed=1707)
    assert [a.decide(raw) for _ in range(60)] == [b.decide(poisoned) for _ in range(60)]


def test_relay_grants_only_one_routine_permit_but_allows_emergency_retreats():
    raw = heldout_view()
    bot = opponents.make_bot("relay_pressure", seed=22)
    first = [bot.decide(raw)[0].split(":")[1] for _ in range(5)]
    assert Counter(first) == {"macro_retreat": 1, "macro_attack": 4}
    for seat in (0, 1):
        my_summary(raw, seat)["my_hero"]["hp"] = 15
    second = [bot.decide(raw)[0].split(":")[1] for _ in range(5)]
    assert second[:2] == ["macro_retreat", "macro_retreat"]
    assert second[2:].count("macro_retreat") == 1


def test_relay_releases_stalled_recovery_and_applies_owner_opportunity_cooldown():
    raw = heldout_view((0,), hp=50)
    bot = opponents.make_bot("relay_pressure", seed=1)
    assert [bot.decide(raw)[0] for _ in range(11)] == ["s0:macro_retreat"] * 11
    assert bot.decide(raw)[0] == "s0:macro_attack"
    assert [bot.decide(raw)[0] for _ in range(10)] == ["s0:macro_attack"] * 10
    assert bot.decide(raw)[0] == "s0:macro_retreat"


def test_relay_observed_healing_extends_recovery_and_death_or_recovery_releases_it():
    raw = heldout_view((0,), hp=50)
    bot = opponents.make_bot("relay_pressure", seed=1)
    for _ in range(11):
        assert bot.decide(raw)[0] == "s0:macro_retreat"
    my_summary(raw)["my_hero"]["hp"] = 52
    assert bot.decide(raw)[0] == "s0:macro_retreat"
    my_summary(raw)["my_hero"]["hp"] = 85
    assert bot.decide(raw)[0] == "s0:macro_attack"
    my_summary(raw)["my_hero"]["hp"] = 0
    assert bot.decide(raw)[0] == "s0:button_0"
    my_summary(raw)["my_hero"]["hp"] = 100
    assert bot.decide(raw)[0] == "s0:macro_attack"


def test_counterpunch_conserves_skills_until_visible_target_opportunity():
    raw = heldout_view((0,), hp=100)
    foe = my_summary(raw)["other_heroes"][0]
    foe["hp"] = 100
    bot = opponents.make_bot("counterpunch_interceptor", seed=5)
    assert bot.decide(raw)[0] == "s0:macro_attack"
    foe["hp"] = 70  # Publicly observed damage, not simulator HP access.
    assert ":macro_skill" in bot.decide(raw)[0]
    foe["visible"] = False
    assert bot.decide(raw)[0] == "s0:macro_attack"
    foe.update(visible=True, hp=100)
    assert bot.decide(raw)[0] == "s0:macro_attack"  # No cross-fog damage inference.


def test_counterpunch_never_redirects_nearest_target_or_infers_unknown_max_hp():
    raw = heldout_view((0,), hp=100)
    foe = my_summary(raw)["other_heroes"][0]
    foe.update(hp=20, max_hp=None)
    bot = opponents.make_bot("counterpunch_interceptor", seed=5)
    assert bot.decide(raw)[0] == "s0:macro_attack"  # 20 is not assumed to mean 20%.
    foe["hp"] = 10
    assert ":macro_skill" in bot.decide(raw)[0]  # But a visible 50% HP drop is observable.
    for action in raw["legal_actions"]["actions"]:
        if ":macro_skill" in action["action_id"]:
            action["label"] = "Skill (nearest: enemy hero 99, 100 away)"
    foe["max_hp"] = 100
    assert bot.decide(raw)[0] == "s0:macro_attack"  # No selectable/fabricated focus-fire target.


def test_counterpunch_waiting_is_bounded_and_own_motion_is_not_enemy_approach():
    raw = heldout_view((0,), hp=100)
    raw["legal_actions"]["actions"] = [offer(0, base) for base in ("macro_advance", "macro_retreat", "button_0")]
    foe = my_summary(raw)["other_heroes"][0]
    foe.update(x=10000, hp=100)
    bot = opponents.make_bot("counterpunch_interceptor", seed=5)
    assert bot.decide(raw)[0] == "s0:macro_advance"
    my_summary(raw)["my_hero"]["x"] = 2000
    assert bot.decide(raw)[0] == "s0:macro_advance"  # Only we moved.
    for x in (8000, 6000, 4000):
        foe["x"] = x
        assert bot.decide(raw)[0] == "s0:button_0"
    foe["x"] = 3000
    assert bot.decide(raw)[0] == "s0:macro_advance"  # Three waits exhausted.
    foe["visible"] = False
    assert bot.decide(raw)[0] == "s0:macro_advance"
    foe.update(visible=True, x=2100)
    assert bot.decide(raw)[0] == "s0:macro_advance"  # Fog erased the old approach.


def test_counterpunch_fog_on_another_heros_turn_also_breaks_consecutive_tracking():
    raw = heldout_view(hp=100)
    raw["legal_actions"]["actions"] = [action for action in raw["legal_actions"]["actions"]
                                       if action["action_id"].split(":")[1]
                                       in ("macro_advance", "macro_retreat", "button_0")]
    foe = my_summary(raw)["other_heroes"][0]
    foe.update(x=10000, hp=100)
    bot = opponents.make_bot("counterpunch_interceptor", seed=5)
    assert bot.decide(raw)[0] == "s0:macro_advance"
    foe.update(visible=False, x=1, hp=1)
    assert bot.decide(raw)[0].startswith("s1:")
    foe.update(visible=True, x=5000, hp=100)
    for seat in (2, 3, 4):
        assert bot.decide(raw)[0].startswith(f"s{seat}:")
    assert bot.decide(raw)[0] == "s0:macro_advance"  # Not a continuously observed approach.


@pytest.mark.parametrize("name", opponents.HELDOUT_NAMES)
def test_heldout_helpers_ignore_malformed_or_hidden_entity_fields(name):
    class Hidden(dict):
        def get(self, key, default=None):
            if key != "visible":
                raise AssertionError("A hidden hero's other fields must not be read")
            return False

    raw = heldout_view((0,))
    my_summary(raw)["other_heroes"] = [Hidden(), None, {"visible": True, "camp": [], "runtime_id": {}}]
    bot = opponents.make_bot(name, seed=17)
    assert bot.decide(raw)[0] in {action["action_id"] for action in raw["legal_actions"]["actions"]}
    bot.on_result({"error": "synthetic refusal", "hidden_state": Hidden()})
    assert bot.refused == 1
    assert bot.decide(raw)[0] in {action["action_id"] for action in raw["legal_actions"]["actions"]}


def test_heldout_actual_evaluator_contract_model_is_v3(research_evaluator):
    from local.hok_sim import MODEL_VERSION

    assert MODEL_VERSION == "hok-contract-diagnostic-v3"
