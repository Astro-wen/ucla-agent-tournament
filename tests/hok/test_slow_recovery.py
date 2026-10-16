"""Timed recovery regressions; synthetic views, no live gameplay or strength claims."""
from copy import deepcopy

import pytest

from agent.hok import policy as hok
from tests.hok.test_strategy import Clock, entries, payload, summary


@pytest.fixture(autouse=True)
def recording_off(monkeypatch):
    monkeypatch.setenv("ARENA_RECORD", "0")


def recovery_view(hp=2100, *, seat=0, standing="macro_retreat", radius=2000,
                  fountain="own", enemies=(), deaths=0):
    camp = 1 if seat < 5 else 2
    base = -45000 if camp == 1 else 45000
    spring = {"camp": camp, "type": "ACTOR_TOWER_SPRING", "hp": 6000,
              "max_hp": 6000, "x": base, "z": base}
    if fountain == "foe":
        spring["camp"] = 3 - camp
    elif fountain == "dead":
        spring["hp"] = 0
    elif fountain == "crystal":
        spring["type"] = "ACTOR_CRYSTAL"
    structures = [] if fountain == "missing" else [spring]
    s = summary(seat, hp=hp, structures=structures, enemies=enemies)
    s["my_hero"].update(x=base + radius, z=base, deaths=deaths)
    orders = {} if standing is None else {str(seat): standing}
    return payload(seats=(seat,), standing=orders, summaries={seat: s})


def begin_recovery(*, seat=0, radius=2000, fountain="own"):
    clock = Clock()
    policy = hok.HokPolicy(clock=clock)
    raw = recovery_view(200, seat=seat, standing=None, radius=radius, fountain=fountain)
    assert policy.decide(raw)[0] == f"s{seat}:macro_retreat"
    policy.on_result({"status": "in_progress"})
    raw["simultaneous"]["standing_orders"] = {str(seat): "macro_retreat"}
    assert policy.decide(raw) == (None, None)
    return policy, clock


@pytest.mark.parametrize("seat", [0, 5])
def test_thirty_seconds_confirmed_at_own_fountain_allows_seventy_percent(seat):
    policy, clock = begin_recovery(seat=seat)
    assert policy._healing_since[seat] == 0.0
    clock.now = 29.999
    raw = recovery_view(seat=seat)
    assert policy.decide(raw) == (None, None)
    clock.now = 30.0
    assert policy.decide(raw)[0] == f"s{seat}:macro_advance"
    assert seat not in policy._retreating
    assert seat not in policy._healing_since
    assert hok.HEALED == .85 and hok.LOW_HP == .30


def test_long_wait_does_not_release_below_seventy_percent():
    policy, clock = begin_recovery()
    clock.now = 30
    assert policy.decide(recovery_view(2097)) == (None, None)  # 69.9%
    clock.now = 31
    assert policy.decide(recovery_view(2100))[0] == "s0:macro_advance"


def test_disabled_switch_preserves_original_eighty_five_percent(monkeypatch):
    monkeypatch.setattr(hok, "SLOW_RECOVERY", False)
    policy, clock = begin_recovery()
    clock.now = 120
    assert policy.decide(recovery_view(2100)) == (None, None)
    assert policy.decide(recovery_view(2550))[0] == "s0:macro_advance"


@pytest.mark.parametrize("fountain", ["missing", "foe", "dead", "crystal"])
def test_only_live_own_fountain_can_relax_recovery(fountain):
    policy, clock = begin_recovery(fountain=fountain)
    clock.now = 60
    assert policy.decide(recovery_view(2100, fountain=fountain)) == (None, None)
    assert 0 not in policy._healing_since


@pytest.mark.parametrize("radius,eligible", [(4000, True), (4000.01, False)])
def test_base_range_boundary(radius, eligible):
    policy, clock = begin_recovery(radius=radius)
    clock.now = 30
    result = policy.decide(recovery_view(radius=radius))[0]
    assert result == ("s0:macro_advance" if eligible else None)


def test_travel_time_is_not_counted_as_fountain_recovery():
    policy, clock = begin_recovery(radius=20000)
    clock.now = 60
    assert policy.decide(recovery_view()) == (None, None)
    assert policy._healing_since[0] == 60
    clock.now = 89.999
    assert policy.decide(recovery_view()) == (None, None)
    clock.now = 90
    assert policy.decide(recovery_view())[0] == "s0:macro_advance"


def test_leaving_fountain_resets_continuous_recovery_timer():
    policy, clock = begin_recovery()
    clock.now = 20
    assert policy.decide(recovery_view(radius=5000)) == (None, None)
    assert 0 not in policy._healing_since
    clock.now = 30
    assert policy.decide(recovery_view()) == (None, None)
    clock.now = 59.999
    assert policy.decide(recovery_view()) == (None, None)
    clock.now = 60
    assert policy.decide(recovery_view())[0] == "s0:macro_advance"


@pytest.mark.parametrize("standing", [None, "macro_advance", ""])
def test_unconfirmed_retreat_does_not_keep_recovery_clock(standing):
    policy, clock = begin_recovery()
    clock.now = 20
    assert policy.decide(recovery_view(standing=standing))[0] != "s0:macro_advance"
    assert 0 not in policy._healing_since
    clock.now = 60
    assert policy.decide(recovery_view()) == (None, None)
    assert policy._healing_since[0] == 60


def test_missing_health_resets_timer_and_does_not_imply_recovery():
    policy, clock = begin_recovery()
    clock.now = 20
    raw = recovery_view()
    del raw["observation"]["seats"]["0"]["summary"]["my_hero"]["hp"]
    assert policy.decide(raw) == (None, None)
    assert 0 not in policy._healing_since
    clock.now = 60
    assert policy.decide(recovery_view()) == (None, None)
    assert policy._healing_since[0] == 60


def test_fast_healing_still_exits_at_eighty_five_without_waiting():
    policy, clock = begin_recovery()
    clock.now = 10
    assert policy.decide(recovery_view(2549)) == (None, None)
    assert policy.decide(recovery_view(2550))[0] == "s0:macro_advance"
    assert not policy._retreating and not policy._healing_since


def test_healthy_retreat_for_numbers_does_not_start_wounded_episode():
    clock = Clock()
    policy = hok.HokPolicy(clock=clock)
    enemies = [{"runtime_id": i, "camp": 2, "visible": True, "hp": 3000,
                "x": -43000, "z": -45000 + i * 100} for i in range(3)]
    assert policy.decide(recovery_view(3000, standing=None, enemies=enemies))[0] == "s0:macro_retreat"
    clock.now = 60
    assert policy.decide(recovery_view(2100, enemies=enemies)) == (None, None)
    assert 0 not in policy._retreating and 0 not in policy._healing_since


def test_timed_recovery_does_not_override_visible_numerical_danger():
    policy, clock = begin_recovery()
    clock.now = 30
    enemies = [{"runtime_id": i, "camp": 2, "visible": True, "hp": 3000,
                "x": -43000, "z": -45000 + i * 100} for i in range(3)]
    assert policy.decide(recovery_view(enemies=enemies)) == (None, None)
    clock.now = 31
    assert policy.decide(recovery_view())[0] == "s0:macro_advance"


def test_low_health_and_wounded_tower_retreat_still_apply_after_timeout():
    policy, clock = begin_recovery()
    clock.now = 60
    raw = recovery_view(800)
    assert policy.decide(raw) == (None, None)
    raw = recovery_view(1500)
    raw["observation"]["seats"]["0"]["summary"]["structures"].append(
        {"camp": 2, "type": "ACTOR_TOWER", "hp": 4000, "x": -43000, "z": -44000})
    assert policy.decide(raw) == (None, None)


def test_heroes_have_independent_fountain_clocks():
    policy, clock = begin_recovery()
    clock.now = 20
    raw = recovery_view(200, seat=1, standing=None)
    assert policy.decide(raw)[0] == "s1:macro_retreat"
    policy.on_result({"status": "in_progress"})
    raw["simultaneous"]["standing_orders"] = {"1": "macro_retreat"}
    assert policy.decide(raw) == (None, None)
    assert policy._healing_since == {0: 0.0, 1: 20}
    clock.now = 30
    assert policy.decide(recovery_view())[0] == "s0:macro_advance"
    assert policy.decide(recovery_view(seat=1)) == (None, None)
    clock.now = 50
    assert policy.decide(recovery_view(seat=1))[0] == "s1:macro_advance"


@pytest.mark.parametrize("missed_dead_frame", [False, True])
def test_death_clears_timer_even_without_legal_offers(missed_dead_frame):
    policy, clock = begin_recovery()
    clock.now = 20
    dead = recovery_view(2100 if missed_dead_frame else 0, deaths=1)
    dead["legal_actions"]["actions"] = []
    assert policy.decide(dead) == (None, None)
    assert 0 not in policy._healing_since and 0 not in policy._retreating
    clock.now = 40
    raw = recovery_view(200, standing=None, deaths=1)
    assert policy.decide(raw)[0] == "s0:macro_retreat"
    raw["simultaneous"]["standing_orders"] = {"0": "macro_retreat"}
    policy.on_result({"status": "in_progress"})
    assert policy.decide(raw) == (None, None)
    clock.now = 60
    assert policy.decide(recovery_view(deaths=1)) == (None, None)
    clock.now = 70
    assert policy.decide(recovery_view(deaths=1))[0] == "s0:macro_advance"


def test_early_recovery_does_not_lend_timer_to_next_wounded_episode():
    policy, clock = begin_recovery()
    clock.now = 30
    assert policy.decide(recovery_view())[0] == "s0:macro_advance"
    clock.now = 31
    raw = recovery_view(200, standing="macro_advance")
    assert policy.decide(raw)[0] == "s0:macro_retreat"
    policy.on_result({"status": "in_progress"})
    raw["simultaneous"]["standing_orders"] = {"0": "macro_retreat"}
    assert policy.decide(raw) == (None, None)
    clock.now = 60
    assert policy.decide(recovery_view()) == (None, None)
    clock.now = 61
    assert policy.decide(recovery_view())[0] == "s0:macro_advance"


def test_rejected_retreat_invalidates_its_recovery_clock_only():
    policy, clock = begin_recovery()
    # A refused sent retreat invalidates confirmation even if an earlier view
    # showed this order. The callback targets one inflight seat only.
    policy._healing_since[1] = 7.0
    policy._inflight = (0, "macro_retreat", 10)
    policy.on_result({"error": "INVALID_ACTION"})
    assert 0 not in policy._healing_since
    assert policy._healing_since[1] == 7.0
    clock.now = 30
    raw = recovery_view()
    raw["state_version"] = 11
    assert policy.decide(raw) == (None, None)
    assert policy._healing_since[0] == 30


def test_eligible_recovery_uses_same_threshold_when_refused_attack_is_reconsidered():
    policy, clock = begin_recovery()
    clock.now = 30
    raw = recovery_view()
    raw["legal_actions"]["actions"] = entries(
        0, ("macro_advance", "macro_retreat", "macro_attack"), distance=1000)
    policy._rejected[(0, "macro_attack")] = (raw["state_version"], 0.0)
    before = deepcopy(raw)
    assert policy.decide(raw)[0] == "s0:macro_advance"
    assert raw == before
