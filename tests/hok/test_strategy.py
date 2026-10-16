"""Synthetic contract/strategy regressions; no live matches, private payloads, or strength claims."""

from copy import deepcopy
import math
import random

import pytest

from agent.hok import policy as hok


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def summary(seat=0, hp=3000, *, enemies=(), structures=()):
    return {"my_hero": {"camp": 1 if seat < 5 else 2, "hp": hp, "max_hp": 3000,
                        "x": seat * 1000, "z": 0},
            "other_heroes": list(enemies), "structures": list(structures)}


def entries(seat, macros=("macro_advance", "macro_retreat", "macro_attack"), *, target="minion 7", distance=12000):
    return [{"action_id": f"s{seat}:button_0", "input": {"action": {"seat": seat, "which_button": 0}}}] + [
        {"action_id": f"s{seat}:{macro}", "label": f"{macro} (nearest: {target}, {distance} away)",
         "input": {"action": {"seat": seat, "macro": macro}}} for macro in macros]


def payload(*, seats=(0, 1, 2, 3, 4), pending=None, mode="realtime", standing=None, version=10,
            actions=None, summaries=None):
    pending = list(seats) if pending is None else pending
    return {"seats": list(seats), "camp": 1 if seats[0] < 5 else 2,
            "state_version": version, "pacing": {"mode": mode},
            "simultaneous": {"your_pending_seats": pending, "standing_orders": standing or {}},
            "legal_actions": {"actions": actions if actions is not None else
                              [a for seat in seats for a in entries(seat)]},
            "observation": {"seats": {str(seat): {"summary": (summaries or {}).get(seat, summary(seat))}
                                      for seat in seats}}}


def test_high_seat_emergency_precedes_routine_low_seat_push():
    policy = hok.HokPolicy(clock=Clock())
    raw = payload(summaries={4: summary(4, hp=500)})
    assert policy.decide(raw)[0] == "s4:macro_retreat"


def test_equal_priority_updates_round_robin_not_fixed_seat_order():
    clock = Clock()
    policy = hok.HokPolicy(clock=clock)
    choices = []
    for step in range(10):
        clock.now = step
        raw = payload(version=step, standing={str(seat): "macro_retreat" for seat in range(5)})
        choices.append(policy.decide(raw)[0])
    assert choices == [f"s{seat}:macro_advance" for seat in range(5)] * 2


def test_continuously_reappearing_emergency_cannot_starve_other_four():
    clock = Clock()
    policy = hok.HokPolicy(clock=clock)
    sent = []
    for step in range(8):
        clock.now = step
        raw = payload(version=step, standing={str(seat): "macro_attack" for seat in range(5)},
                      summaries={0: summary(hp=100)})
        sent.append(int(policy.decide(raw)[0][1]))
    assert sent[0] == 0
    assert set(sent) == set(range(5))


@pytest.mark.parametrize("mode", ["realtime", "lockstep"])
def test_orders_never_escape_ownership_or_explicit_pending_list(mode):
    policy = hok.HokPolicy(clock=Clock())
    raw = payload(mode=mode, seats=(5, 6, 7, 8, 9), pending=[True, 0, 4, 9, 9],
                  actions=entries(0) + entries(4) + entries(9))
    assert policy.decide(raw)[0] == "s9:macro_advance"
    raw["simultaneous"]["your_pending_seats"] = []
    assert policy.decide(raw) == (None, None)


def test_rejection_invalidates_only_last_hero_and_preserves_teammate_orders():
    clock = Clock()
    policy = hok.HokPolicy(clock=clock)
    assert policy.decide(payload())[0] == "s0:macro_advance"
    policy.on_result({"status": "in_progress"})
    assert policy.decide(payload())[0] == "s1:macro_advance"
    policy.on_result({"error": "INVALID_ACTION"})
    assert policy.refused == 1
    assert set(policy._sent) == {0}
    assert policy.decide(payload())[0] == "s2:macro_advance"


def test_rejected_macro_requires_fresh_version_and_backoff_before_retry():
    clock = Clock()
    policy = hok.HokPolicy(clock=clock)
    raw = payload(seats=(0,), actions=entries(0, ("macro_advance", "macro_retreat")))
    assert policy.decide(raw)[0] == "s0:macro_advance"
    policy.on_result({"error": "INVALID_ACTION"})
    assert policy.decide(raw) == (None, None)
    clock.now = 10
    assert policy.decide(raw) == (None, None)  # even much later, the exact rejected snapshot is not new evidence
    raw["state_version"] += 1
    assert policy.decide(raw)[0] == "s0:macro_advance"
    policy.on_result({"error": "INVALID_ACTION"})
    raw["state_version"] += 1
    assert policy.decide(raw) == (None, None)  # fresh snapshot alone does not bypass the short cooldown
    clock.now += hok.RETRY_S + 0.01
    assert policy.decide(raw)[0] == "s0:macro_advance"


@pytest.mark.parametrize("mode", ["realtime", "lockstep"])
def test_rejected_emergency_never_becomes_an_attack(mode):
    policy = hok.HokPolicy(clock=Clock())
    raw = payload(mode=mode, seats=(0,), actions=entries(0, distance=1000),
                  summaries={0: summary(hp=200)})
    assert policy.decide(raw)[0] == "s0:macro_retreat"
    policy.on_result({"error": "INVALID_ACTION"})
    assert policy.decide(raw)[0] == (None if mode == "realtime" else "s0:button_0")


def test_rejected_skill_can_fall_back_to_a_different_currently_listed_attack():
    policy = hok.HokPolicy(clock=Clock())
    raw = payload(seats=(0,), actions=entries(0, hok.MACROS, target="enemy hero 91", distance=3000))
    assert policy.decide(raw)[0] == "s0:macro_skill3"
    policy.on_result({"error": "INVALID_ACTION"})
    assert policy.decide(raw)[0] == "s0:macro_skill2"


def test_error_without_inflight_order_does_not_forget_other_heroes():
    policy = hok.HokPolicy(clock=Clock())
    policy.decide(payload())
    policy.on_result({"status": "in_progress"})
    policy.on_result({"error": "STALE_STATE"})
    assert set(policy._sent) == {0}


def test_dead_hero_with_stale_macro_offer_is_skipped_then_recommanded_on_respawn():
    policy = hok.HokPolicy(clock=Clock())
    assert policy.decide(payload(seats=(0,)))[0] == "s0:macro_advance"
    assert policy.decide(payload(seats=(0,), summaries={0: summary(hp=0)})) == (None, None)
    assert policy.decide(payload(seats=(0,)))[0] == "s0:macro_advance"


@pytest.mark.parametrize("target", ["enemy tower 4", "enemy crystal 8", "enemy structure 12"])
def test_structure_basic_attack_precedes_unverified_skill_damage(target):
    raw = payload(seats=(0,), actions=entries(0, hok.MACROS, target=target, distance=3000))
    assert hok.HokPolicy(clock=Clock()).decide(raw)[0] == "s0:macro_attack"


def test_hero_skill_and_objective_opportunities_can_interrupt_new_advance():
    for target, expected in (("enemy hero 77", "macro_skill3"), ("enemy tower 6", "macro_attack")):
        clock = Clock()
        policy = hok.HokPolicy(clock=clock)
        policy.decide(payload(seats=(0,)))
        clock.now = 0.1
        raw = payload(seats=(0,), standing={"0": "macro_advance"},
                      actions=entries(0, hok.MACROS, target=target, distance=3000))
        assert policy.decide(raw)[0] == f"s0:{expected}"


def test_missing_optional_distance_does_not_disable_legal_attack():
    actions = entries(0)
    for action in actions:
        action.pop("label", None)
    assert hok.HokPolicy(clock=Clock()).decide(payload(seats=(0,), actions=actions))[0] == "s0:macro_attack"


def test_unknown_hero_ids_never_change_tactics():
    raw = payload(seats=(0,), actions=entries(0, hok.MACROS, target="enemy hero 7821", distance=3000))
    raw["observation"]["seats"]["0"]["summary"]["my_hero"]["hero_id"] = -8765432
    assert hok.HokPolicy(clock=Clock()).decide(raw)[0] == "s0:macro_skill3"


def test_shared_vision_without_runtime_id_is_not_counted_five_times():
    enemy = {"camp": 2, "visible": True, "hp": 3000, "x": 0, "z": 1000}
    raw = payload(summaries={s: summary(s, enemies=[enemy]) for s in range(5)})
    team = hok.Team(raw, list(range(5)))
    assert len(team.enemies) == 1
    assert not team.outnumbered((0, 0))
    enemy["runtime_id"] = ["malformed", "unhashable"]
    assert len(hok.Team(raw, list(range(5))).enemies) == 1


@pytest.mark.parametrize("visible", [False, "false", "true", 1, None, [], {}])
def test_only_explicit_visible_true_enemy_positions_affect_tactics(visible):
    enemy = {"camp": 2, "visible": visible, "hp": 3000, "x": 0, "z": 1000}
    raw = payload(summaries={0: summary(enemies=[enemy])})
    assert hok.Team(raw, list(range(5))).enemies == {}


def test_team_merges_all_structure_views_and_ignores_explicitly_hidden_towers():
    tower = {"camp": 2, "type": "ACTOR_TOWER", "hp": 6000, "x": 0, "z": 1000}
    far = {**tower, "x": 50000}
    hidden = {**tower, "x": -50000, "visible": False}
    raw = payload(summaries={0: summary(structures=[far, hidden]), 4: summary(4, structures=[tower, far])})
    assert hok.Team(raw, list(range(5))).towers == [(0.0, 1000.0), (50000.0, 1000.0)]


def test_unknown_camp_does_not_reclassify_unknown_heroes_as_enemies():
    enemy = {"camp": 2, "visible": True, "x": 0, "z": 0}
    raw = payload(seats=(0,), summaries={0: summary(enemies=[enemy])})
    raw.pop("camp")
    raw["observation"]["seats"]["0"]["summary"]["my_hero"].pop("camp")
    assert hok.Team(raw, [0]).enemies == {}


@pytest.mark.parametrize("bad", [None, False, 123, "x", {}, [None, False, 123, "x", {}]])
def test_malformed_action_containers_fail_closed(bad):
    assert hok.offers_by_seat(bad) == {}
    assert not hok.is_hok_shape(bad)


@pytest.mark.parametrize("action_id", ["s10:macro_attack", "s-1:macro_attack", "s0:unknown", "s0:macro_attack\n",
                                      "s00:macro_advance", "s0:macro_invented", "s0:button_2:extra"])
def test_unknown_and_malformed_action_ids_are_not_sent(action_id):
    assert hok.offers_by_seat([{"action_id": action_id}]) == {}


@pytest.mark.parametrize("bad_seat", [True, "0", 1, [], {}])
def test_conflicting_payload_seat_is_not_sent(bad_seat):
    actions = [{"action_id": "s0:macro_advance", "input": {"action": {"seat": bad_seat}}}]
    assert hok.offers_by_seat(actions) == {}


def test_raw_fallback_uses_only_fully_populated_listed_domains():
    broken = {"action_id": "s0:button_2", "input": {"action": {
        "seat": 0, "which_button": 2, "legal_move_x": [], "legal_move_z": [8]}}}
    valid = deepcopy(broken)
    valid["input"]["action"]["legal_move_x"] = [4, 5]
    assert hok.idle_action([broken], 0) is None
    assert hok.idle_action([broken, valid], 0) == {"seat": 0, "which_button": 2, "move_x": 4, "move_z": 8}
    valid["input"]["action"]["move_x"] = 6
    assert hok.idle_action([valid], 0) is None
    assert hok.idle_action([], 0) is None


@pytest.mark.parametrize("number", [True, float("nan"), float("inf"), float("-inf"), 10 ** 1000, "3000", [], {}])
def test_nonfinite_and_malformed_hp_or_coordinates_do_not_raise(number):
    raw = payload(seats=(0,))
    me = raw["observation"]["seats"]["0"]["summary"]["my_hero"]
    me.update(hp=number, x=number, z=number)
    result = hok.HokPolicy(clock=Clock()).decide(raw)[0]
    assert result in {a["action_id"] for a in raw["legal_actions"]["actions"]}
    assert hok._num(number) is None


def test_randomized_valid_contract_states_only_emit_current_pending_legal_actions():
    rng = random.Random(1016)
    for mode in ("realtime", "lockstep"):
        clock = Clock()
        policy = hok.HokPolicy(clock=clock)
        for step in range(300):
            clock.now = step * 0.2
            seats = tuple(range(5, 10))
            pending = rng.sample(list(seats), rng.randint(1, 5))
            actions = [a for seat in pending for a in entries(
                seat, rng.sample(list(hok.MACROS), rng.randint(0, len(hok.MACROS))),
                target=rng.choice(["enemy hero 73", "minion 4", "enemy tower 7"]),
                distance=rng.randrange(20000))]
            raw = payload(seats=seats, pending=pending, mode=mode, actions=actions, version=step,
                          summaries={s: summary(s, hp=rng.randrange(3001)) for s in seats})
            action, _ = policy.decide(raw)
            if action is not None:
                assert action in {a["action_id"] for a in actions}
                assert int(action[1]) in pending
                policy.on_result({"error": "INVALID_ACTION"} if rng.random() < 0.1 else {})
            else:
                assert mode == "realtime"


def test_malformed_nested_state_samples_never_raise():
    values = [None, False, 17, "x", [], {}, {"invalid": []}, [None, 1, "x"]]
    rng = random.Random(716)
    for _ in range(250):
        raw = payload()
        for path in [("legal_actions", "actions"), ("simultaneous", "standing_orders"),
                     ("simultaneous", "your_pending_seats"), ("observation", "seats")]:
            if rng.random() < 0.5:
                raw[path[0]][path[1]] = deepcopy(rng.choice(values))
        for seat in raw["observation"]["seats"].values() if isinstance(raw["observation"]["seats"], dict) else []:
            if isinstance(seat, dict):
                seat["summary"] = deepcopy(rng.choice(values))
        action, _ = hok.HokPolicy(clock=Clock()).decide(raw)
        assert action is None or isinstance(action, (str, dict))
    assert hok.HokPolicy().decide(None) == (None, None)
    assert math.isinf(hok.distance_of(None))
