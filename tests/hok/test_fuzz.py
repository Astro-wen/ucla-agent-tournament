"""Seeded hostile-but-SDK-parseable HoK states, through the real runner.

The generic MCP envelope stays parseable: testing corrupt JSON or invalid
state_version in GameState.from_mcp_state would test the SDK, not this policy.
For lockstep an actionable frame always contains at least one current legal
pending-seat response. No test demands inventing an action from an empty list.
"""
from __future__ import annotations

from copy import deepcopy
import logging
import random

import pytest

from local.hok_fake_session import SEATS, HokFakeSession, entries, payload
from tests.hok.test_runtime_integration import offline, play  # shared offline fixture, not production oracles


BAD_VALUES = [None, True, False, 0, -1, "", "not a number", [], {}, float("nan"), float("inf"), -float("inf")]
MACROS = ("macro_advance", "macro_retreat", "macro_attack", "macro_skill1", "macro_skill2", "macro_skill3")


def corrupt_observation(raw, seat, rng, case):
    """One reproducible field mutation; never corrupt the SDK's MCP envelope."""
    bad = deepcopy(rng.choice(BAD_VALUES))
    summary = raw["observation"]["seats"][str(seat)]["summary"]
    if case == 0:
        raw.pop("observation")
    elif case == 1:
        raw["observation"] = bad
    elif case == 2:
        raw["observation"]["seats"] = bad
    elif case == 3:
        raw["observation"]["seats"][str(seat)] = bad
    elif case == 4:
        raw["observation"]["seats"][str(seat)]["summary"] = bad
    elif case == 5:
        summary["my_hero"] = bad
    elif case == 6:
        summary["my_hero"][rng.choice(("hp", "max_hp", "x", "z", "level", "revive_time"))] = bad
    elif case == 7:
        summary[rng.choice(("other_heroes", "structures"))] = bad
    elif case == 8:
        summary["other_heroes"][0][rng.choice(("runtime_id", "camp", "hp", "x", "z", "visible"))] = bad
    elif case == 9:
        summary["structures"][0][rng.choice(("type", "camp", "hp", "x", "z"))] = bad
    elif case == 10:
        raw["simultaneous"]["standing_orders"] = bad
    elif case == 11:
        raw["simultaneous"] = bad
    elif case == 12:
        raw["simultaneous"]["your_pending_seats"] = [None, {}, [], True, str(seat), seat, seat]
    elif case == 13:
        raw["seats"] = [None, False, "unknown", *SEATS]
    elif case == 14:
        raw["camp"] = bad
    elif case == 15:
        raw["pacing"]["step_interval_s"] = bad
        raw["seconds_left"] = bad
        raw["max_steps"] = bad
    elif case == 16:
        for action in raw["legal_actions"]["actions"]:
            action["label"] = bad
    elif case == 17:
        raw["simultaneous"]["standing_orders"] = {
            "-1": "macro_advance", "NaN": None, "9999": [], str(seat): bad,
        }
    elif case == 18:
        # Finite Python integer but too large for float conversion: malformed
        # telemetry must still not escape choose_action into the runner.
        summary["my_hero"]["hp"] = 10 ** 1000
    elif case == 19:
        summary["other_heroes"].extend([None, [], True, "fog"])


@pytest.mark.parametrize("mode", ["realtime", "lockstep"])
@pytest.mark.parametrize("seed", [1016, 2471, 8191, 65537])
def test_seeded_malformed_views_never_escape_or_send_unoffered_actions(monkeypatch, caplog, mode, seed):
    caplog.set_level(logging.CRITICAL, logger="agent")  # fallback logging is not the legality oracle
    rng = random.Random(seed)
    count = 320

    def frames():
        for version in range(count):
            seat = rng.choice(SEATS)
            macros = tuple(rng.sample(MACROS, rng.randint(0, len(MACROS))))
            raw = payload(version, mode=mode, pending=[seat],
                          actions=entries(seat, macros, distance=rng.choice([0, 4000, 12000, 10**100])))
            corrupt_observation(raw, seat, rng, version % 20)
            yield raw

    game = HokFakeSession(frames())
    play(game, monkeypatch)
    assert game.views == count
    if mode == "lockstep":
        assert len(game.accepted) == count, f"seed={seed}: every pending hero must get a legal answer, never WAIT"
    assert {c["seat"] for c in game.accepted} == set(SEATS), f"seed={seed}: some hero never received any order"


@pytest.mark.parametrize("mode", ["realtime", "lockstep"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")], ids=["nan", "inf", "negative-inf"])
def test_nonfinite_telemetry_cannot_enter_outgoing_actions(monkeypatch, mode, value):
    def frames():
        for seat in SEATS:
            raw = payload(seat, pending=[seat], mode=mode, actions=entries(seat, MACROS, distance=3000))
            summary = raw["observation"]["seats"][str(seat)]["summary"]
            for actor in [summary["my_hero"], *summary["other_heroes"], *summary["structures"]]:
                actor.update(hp=value, max_hp=value, x=value, z=value)
            raw["seconds_left"] = value
            raw["pacing"]["step_interval_s"] = value
            yield raw

    game = HokFakeSession(frames())
    play(game, monkeypatch)
    # The fake MCP checks outgoing structured actions with allow_nan=False.
    assert len(game.accepted) == 5


@pytest.mark.parametrize("seed", [20261016, 681409])
@pytest.mark.parametrize("mode", ["realtime", "lockstep"])
def test_stateful_offer_churn_and_result_races_stay_legal(monkeypatch, seed, mode):
    rng = random.Random(seed)

    def frames():
        for version in range(600):
            pending = rng.sample(list(SEATS), rng.randint(1, 5))
            actions = []
            for seat in pending:
                macros = tuple(rng.sample(MACROS, rng.randint(0, 6)))
                actions.extend(entries(seat, macros, distance=rng.choice([3000, 7000, 15000])))
            rng.shuffle(actions)
            yield payload(version, pending=pending, mode=mode, actions=actions, standing=game.standing)

    def reject(call):
        if mode == "realtime" and call["view"] % 17 == 0:
            return "INVALID_ACTION" if call["view"] % 2 else "STALE_STATE"
        return None

    game = HokFakeSession(frames(), reject=reject)
    play(game, monkeypatch)
    assert game.views == 600
    assert {c["seat"] for c in game.accepted} == set(SEATS)
    if mode == "lockstep":
        assert len(game.accepted) == 600 and game.waits == 0


def test_empty_realtime_offers_wait_instead_of_inventing_an_order(monkeypatch):
    raw = payload(actions=[])
    game = HokFakeSession([raw] * 20)
    play(game, monkeypatch)
    assert not game.calls and game.waits == 20


def test_inactive_lockstep_empty_offers_are_waited_by_the_runner(monkeypatch):
    raw = payload(mode="lockstep", pending=[], actions=[])
    game = HokFakeSession([raw] * 20)
    play(game, monkeypatch)
    assert not game.calls and game.waits == 20


def test_lockstep_exception_fallback_must_not_answer_an_already_satisfied_seat(monkeypatch):
    # A stale extra legal entry must not outrank the authoritative pending list,
    # including when malformed telemetry sends the policy into its safe path.
    raw = payload(mode="lockstep", pending=[4], actions=entries(0, ()) + entries(4))
    raw["observation"]["seats"]["4"]["summary"]["other_heroes"] = 7
    game = HokFakeSession([raw])
    play(game, monkeypatch)
    assert [c["seat"] for c in game.accepted] == [4]


@pytest.mark.parametrize("mode", ["realtime", "lockstep"])
def test_unowned_seat_in_malformed_pending_and_offers_is_never_controlled(monkeypatch, mode):
    raw = payload(mode=mode, pending=[9, 4], actions=entries(9) + entries(4))
    game = HokFakeSession([raw])
    play(game, monkeypatch)
    assert all(c["seat"] in SEATS for c in game.accepted)
    if mode == "lockstep":
        assert [c["seat"] for c in game.accepted] == [4]


@pytest.mark.parametrize("mode", ["realtime", "lockstep"])
@pytest.mark.parametrize("representation", ["owned", "pending", "action-seat", "mixed"])
def test_numeric_string_seat_metadata_preserves_playable_owned_hero(monkeypatch, mode, representation):
    raw = payload(mode=mode, pending=[4], actions=entries(4))
    if representation in ("owned", "mixed"):
        raw["seats"] = ["0", 1, "2", 3, "4"]
    if representation in ("pending", "mixed"):
        raw["simultaneous"]["your_pending_seats"] = ["4"]
    if representation in ("action-seat", "mixed"):
        for action in raw["legal_actions"]["actions"]:
            action["input"]["action"]["seat"] = "4"
    game = HokFakeSession([raw])
    play(game, monkeypatch)
    assert [(c["seat"], c["command"]) for c in game.accepted] == [(4, "macro_advance")]


@pytest.mark.parametrize("representation", ["own-camp", "enemy-camp", "mixed-camps"])
def test_numeric_string_camp_metadata_does_not_change_visible_threat_response(monkeypatch, representation):
    raw = payload(pending=[4], actions=entries(4, ("macro_advance", "macro_retreat")))
    summary = raw["observation"]["seats"]["4"]["summary"]
    summary["my_hero"].update(hp=1500, x=0, z=0)
    summary["other_heroes"] = []
    summary["structures"] = [{"camp": 2, "type": "ACTOR_TOWER", "hp": 6000, "x": 100, "z": 0}]
    canonical = HokFakeSession([deepcopy(raw)])
    play(canonical, monkeypatch)
    assert len(canonical.accepted) == 1
    if representation in ("own-camp", "mixed-camps"):
        raw["camp"] = "1"
        for entry in raw["observation"]["seats"].values():
            entry["summary"]["my_hero"]["camp"] = "1"
    if representation in ("enemy-camp", "mixed-camps"):
        summary["structures"][0]["camp"] = "2"
    encoded = HokFakeSession([raw])
    play(encoded, monkeypatch)
    # This is a representation-invariance test, not a copied tactical cutoff.
    assert [(c["seat"], c["command"]) for c in encoded.accepted] == [
        (c["seat"], c["command"]) for c in canonical.accepted
    ], "integer and numeric-string camp IDs describe exactly the same visible threat"


@pytest.mark.parametrize("mode", ["realtime", "lockstep"])
def test_stringified_current_ownership_never_permits_an_extra_offered_enemy_seat(monkeypatch, mode):
    raw = payload(mode=mode, pending=["9", "4"], actions=entries(9) + entries(4))
    raw["seats"] = ["0", "1", "2", "3", "4"]
    game = HokFakeSession([raw])
    play(game, monkeypatch)
    assert [(c["seat"], c["command"]) for c in game.accepted] == [(4, "macro_advance")]
