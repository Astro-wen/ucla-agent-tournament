"""Independent contract tests: real Contestant -> official run_game -> fake MCP.

No tactical thresholds or policy-private counters are used as correctness
oracles. The peer checks current legal offers and pending/owned seats.
"""
from __future__ import annotations

import json
import socket
import time
from types import SimpleNamespace

import pytest

from agent import agent as deployed
from agent.hok.policy import HokPolicy
from altruagent import DecisionContext
from altruagent.runner import run_game
from local.hok_fake_session import (
    CONTEXT, SEATS, HokFakeSession, assert_legal_submission, entries, payload,
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("ARENA_RECORD", "0")
    monkeypatch.setenv("ARENA_HOK", "on")
    monkeypatch.setenv("ARENA_REASONING", "off")
    monkeypatch.setenv("ARENA_TC_WATCHDOG", "0")

    def forbidden(*args, **kwargs):
        raise AssertionError("contract tests must not connect to any platform/model")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def play(game, monkeypatch, *, context=CONTEXT, agent=None):
    # Only time is controlled: the real policy, routing, result hook, recorder,
    # SDK validation, WAIT handling, and exception recovery all run unchanged.
    monkeypatch.setattr("agent.hok.HokPolicy", lambda: HokPolicy(clock=game.clock))
    monkeypatch.setattr(deployed, "time", SimpleNamespace(monotonic=game.clock, time=time.time))
    agent = agent or deployed.Contestant()
    result = run_game(game, context, agent, sleep=lambda _: None, now=game.clock, log=lambda _: None)
    assert result.is_terminal and result.termination_reason == "completed"
    return agent


@pytest.mark.parametrize("seats", [tuple(range(5)), tuple(range(5, 10))], ids=["camp1", "camp2"])
@pytest.mark.parametrize("reasoning", ["off", "safe"])
def test_realtime_initializes_all_five_then_only_sends_changed_orders(monkeypatch, seats, reasoning):
    monkeypatch.setenv("ARENA_REASONING", reasoning)

    def frames():
        for version in range(35):
            actions = [a for s in seats for a in entries(
                s, ("macro_attack",) if version >= 15 and s == seats[-1] else ("macro_advance",))]
            yield payload(version, seats=seats, standing=game.standing, actions=actions)

    game = HokFakeSession(frames())
    play(game, monkeypatch)
    assert {c["seat"] for c in game.accepted[:5]} == set(seats)
    assert len(game.accepted) == 6, "unchanged standing orders should not be spammed"
    assert game.accepted[-1]["seat"] == seats[-1]
    assert game.accepted[-1]["command"] == "macro_attack"
    assert game.waits >= 25


@pytest.mark.parametrize("seats", [tuple(range(5)), tuple(range(5, 10))], ids=["camp1", "camp2"])
def test_lockstep_completes_every_pending_hero_each_step_including_dead_heroes(monkeypatch, seats):
    def frames():
        for version in range(12):
            for _ in seats:
                answered = {c["seat"] for c in game.accepted if c["version"] == version}
                pending = [s for s in reversed(seats) if s not in answered]
                actions = [a for s in pending for a in entries(s, () if s == seats[2] else ("macro_advance",))]
                yield payload(version, seats=seats, pending=pending, mode="lockstep",
                              standing=game.standing, actions=actions)

    game = HokFakeSession(frames())
    play(game, monkeypatch)
    assert len(game.accepted) == 60 and game.waits == 0
    for version in range(12):
        assert {c["seat"] for c in game.accepted if c["version"] == version} == set(seats)
    assert all(c["command"] == "button_0" for c in game.accepted if c["seat"] == seats[2])


def test_lockstep_fills_raw_button_template_from_current_legal_values(monkeypatch):
    frames = []
    for seat in SEATS:
        action = {"action_id": f"s{seat}:button_2", "input": {"action": {
            "seat": seat, "which_button": 2, "legal_move_x": [4, 9], "legal_move_z": [7]}}}
        frames.append(payload(seat, mode="lockstep", pending=[seat], actions=[action]))
    game = HokFakeSession(frames)
    play(game, monkeypatch)
    assert len(game.accepted) == 5 and game.waits == 0
    assert all(c["command"] == "button_2" for c in game.accepted)


def test_realtime_dead_hero_does_not_block_living_heroes_and_revives(monkeypatch):
    def frames():
        for version in range(25):
            actions = [a for s in SEATS for a in entries(
                s, () if s == 0 and version < 12 else ("macro_advance",))]
            yield payload(version, standing=game.standing, actions=actions)

    game = HokFakeSession(frames())
    play(game, monkeypatch)
    assert {c["seat"] for c in game.accepted[:4]} == {1, 2, 3, 4}
    assert game.accepted[-1]["seat"] == 0 and game.accepted[-1]["version"] >= 12
    assert len(game.accepted) == 5


def test_respawn_reinstalls_order_that_the_server_cleared_on_death(monkeypatch):
    def frames():
        for version in range(35):
            dead = 10 <= version < 18
            if dead:
                game.standing.pop("0", None)
            actions = [a for s in SEATS for a in entries(s, () if s == 0 and dead else ("macro_advance",))]
            raw = payload(version, standing=game.standing, actions=actions)
            raw["observation"]["seats"]["0"]["summary"]["my_hero"]["hp"] = 0 if dead else 3000
            yield raw

    game = HokFakeSession(frames())
    play(game, monkeypatch)
    revived_orders = [c for c in game.accepted if c["seat"] == 0 and c["version"] >= 18]
    assert revived_orders, "the pre-death local send cache suppressed every order after respawn"
    assert {c["seat"] for c in game.accepted if c["version"] < 10} == set(SEATS)


def test_lagging_standing_order_acknowledgements_do_not_duplicate_or_block_team(monkeypatch):
    def frames():
        for version in range(25):
            # For a few views the server accepts orders but its snapshot still
            # shows no acknowledgements. Eventually the snapshot catches up.
            yield payload(version, standing={} if version < 8 else game.standing)

    game = HokFakeSession(frames())
    play(game, monkeypatch)
    assert {c["seat"] for c in game.accepted} == set(SEATS)
    assert len(game.accepted) == 5, "brief acknowledgement lag must not cause resends"


@pytest.mark.parametrize("error", ["INVALID_ACTION", "STALE_STATE", "NOT_YOUR_TURN"])
def test_runner_recovers_refusal_and_revalidates_changed_offers(monkeypatch, error):
    def frames():
        yield payload(0, pending=[0], actions=entries(0, ("macro_attack",)))
        for version in range(1, 18):
            # The refused target has vanished: no old attack is legal now.
            yield payload(version, standing=game.standing)

    game = HokFakeSession(frames(), reject=lambda call: error if call["view"] == 1 else None)
    play(game, monkeypatch)
    assert game.calls[0]["command"] == "macro_attack"
    assert {c["seat"] for c in game.accepted} == set(SEATS)
    assert all(c["command"] == "macro_advance" for c in game.accepted)
    assert len(game.accepted) == 5


def test_stale_standing_order_is_replaced_without_starving_other_heroes(monkeypatch):
    def frames():
        for version in range(20):
            standing = {"0": "macro_skill3", **game.standing}
            yield payload(version, standing=standing)

    game = HokFakeSession(frames())
    play(game, monkeypatch)
    assert {c["seat"] for c in game.accepted} == set(SEATS)
    assert len(game.accepted) == 5


def test_repeated_race_on_one_hero_does_not_starve_the_other_four(monkeypatch):
    def frames():
        for version in range(30):
            yield payload(version, standing=game.standing)

    # Seat 0's target disappears between read and send every time. Other seats'
    # offered moves remain valid. A whole team must not idle behind one race.
    game = HokFakeSession(frames(), reject=lambda call: "INVALID_ACTION" if call["seat"] == 0 else None)
    play(game, monkeypatch)
    served = {c["seat"] for c in game.accepted}
    assert served >= {1, 2, 3, 4}, f"one rejected hero starved healthy teammates: served={served}, calls={game.calls}"


def test_changing_early_seat_does_not_starve_later_seats(monkeypatch):
    def frames():
        for version in range(30):
            # Exactly one macro is currently available to seat 0, and it
            # alternates; preserving its now-illegal old macro cannot help.
            macro = "macro_attack" if version % 2 else "macro_advance"
            actions = entries(0, (macro,)) + [a for s in SEATS[1:] for a in entries(s)]
            yield payload(version, standing=game.standing, actions=actions)

    game = HokFakeSession(frames())
    play(game, monkeypatch)
    served = {c["seat"] for c in game.accepted}
    assert served == set(SEATS), f"a frequently changing seat monopolized all decisions: served={served}"


@pytest.mark.parametrize("other", ["werewolf", "pokemon_vgc_doubles_draft", "red_alert", "unrecognized_realtime"])
def test_authoritative_other_game_context_never_enters_hok(monkeypatch, other):
    raw = payload()
    context = DecisionContext(session_id=CONTEXT.session_id, tournament_id=None, game_type=other,
                              agent_id=CONTEXT.agent_id)
    calls = []

    def unexpected(*args, **kwargs):
        calls.append(True)
        raise AssertionError("another game's authoritative context entered HoK")

    monkeypatch.setattr(deployed.Contestant, "_honor_of_kings", unexpected)
    # Actual non-HoK policies remain responsible for their own legal actions.
    # Capture any send here without interpreting the conflicting HoK-shaped
    # raw data as that game's real server contract.
    game = HokFakeSession([raw])
    def capture(**kwargs):
        game.calls.append(kwargs)
        return {"accepted": True}

    game.play_action = capture
    agent = play(game, monkeypatch, context=context)
    assert not calls and agent._hok is None
    assert all(not str(c.get("action_id") or "").split(":")[-1].startswith("macro_") for c in game.calls)
    assert all("macro" not in (c.get("action") or {}) for c in game.calls)


def test_hok_disabled_sends_no_realtime_orders(monkeypatch):
    monkeypatch.setenv("ARENA_HOK", "off")
    game = HokFakeSession(payload(version) for version in range(8))
    agent = play(game, monkeypatch)
    assert not game.calls and game.waits == 8 and agent._hok is None


@pytest.mark.parametrize("wall_seconds_per_view", [0.2, 1.0], ids=["normal-cadence", "slow-server"])
def test_3000_steps_record_at_most_about_one_row_per_ten_game_seconds(
        monkeypatch, tmp_path, wall_seconds_per_view):
    monkeypatch.setenv("ARENA_RECORD", "1")
    monkeypatch.setattr(deployed, "RECORD_DIR", str(tmp_path))

    def frames():
        for version in range(3000):
            yield payload(version, standing=game.standing)

    game = HokFakeSession(frames(), wall_seconds_per_view=wall_seconds_per_view)
    play(game, monkeypatch)
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1, "recording test must exercise the real recorder"
    rows = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert rows and rows[0]["raw"]["state_version"] == 0
    # 3000 * 0.2 = 600 game seconds. Allow initial/final boundary slack;
    # a slow wall clock or many pending seats must not multiply recordings.
    assert len(rows) <= 62, f"3000 steps wrote {len(rows)} rows (budget 62)"
    assert len(game.accepted) == 5


def test_contract_peer_rejects_nonpending_seat_and_unfilled_templates():
    raw = payload(pending=[4], actions=entries(0) + entries(4))
    with pytest.raises(AssertionError, match="non-pending"):
        assert_legal_submission(raw, action_id="s0:button_0")
    raw = payload(pending=[4], actions=[{
        "action_id": "s4:button_2",
        "input": {"action": {"seat": 4, "which_button": 2, "legal_move_x": [9]}}}])
    with pytest.raises(AssertionError, match="unfilled"):
        assert_legal_submission(raw, action_id="s4:button_2")
    with pytest.raises(AssertionError, match="not offered"):
        assert_legal_submission(raw, action={"seat": 4, "which_button": 2, "move_x": 99})
