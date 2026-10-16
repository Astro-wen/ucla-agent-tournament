"""Integration-specific boundary regressions; no external services or credentials."""
import pytest

from agent import agent as deployed
from agent.hok.policy import HokPolicy
from altruagent import GameState
from local.hok_fake_session import CONTEXT, HokFakeSession, entries, payload
from tests.hok.test_runtime_integration import play


@pytest.mark.parametrize("seats", [tuple(range(5)), tuple(range(5, 10))])
@pytest.mark.parametrize("offer", ["idle", "macro", "raw-template"])
def test_double_fault_fallback_uses_only_owned_pending_complete_offers(monkeypatch, seats, offer):
    def broken(*args, **kwargs):
        raise RuntimeError("independent tactical and primary fallback failure")

    monkeypatch.setenv("ARENA_REASONING", "off")
    monkeypatch.setattr(HokPolicy, "decide", broken)
    monkeypatch.setattr("agent.hok.safe_fallback_action", broken)
    pending = seats[-1]
    if offer == "idle":
        mine = entries(pending, ())
        command = "button_0"
    elif offer == "macro":
        mine = [a for a in entries(pending) if a["action_id"].endswith("macro_advance")]
        command = "macro_advance"
    else:
        mine = [{"action_id": f"s{pending}:button_2", "input": {"action": {
            "seat": pending, "which_button": 2, "legal_move_x": [4], "legal_move_z": [7]}}}]
        command = "button_2"
    foreign = 9 if seats[0] == 0 else 0
    raw = payload(mode="lockstep", seats=seats, pending=[pending],
                  actions=entries(foreign, ()) + entries(seats[0], ()) + mine)
    game = HokFakeSession([raw])
    play(game, monkeypatch)
    assert [(c["seat"], c["command"]) for c in game.accepted] == [(pending, command)]


def test_last_resort_never_invents_a_nonpending_action():
    raw = payload(mode="lockstep", pending=[], actions=entries(0, ()))
    assert deployed._hok_last_resort(raw) is None


def test_existing_match_reset_clears_refusal_recording_flag():
    # R5's suspected missing reset was already present in the parent. Test it,
    # do not duplicate it or touch another game's reset semantics.
    contestant = deployed.Contestant()
    contestant._hok_refused_record = True
    contestant._hok_record_faults = {"refused"}
    contestant._fresh()
    assert contestant._hok_refused_record is False
    assert contestant._hok_record_faults == set()


@pytest.mark.parametrize("mode", ["realtime", "lockstep"])
def test_recorder_fault_preserves_move_and_consumes_refusal_sample_flag(monkeypatch, mode):
    def broken_record(*args, **kwargs):
        raise OSError("unexpected outer recorder failure")

    monkeypatch.setenv("ARENA_REASONING", "off")
    monkeypatch.setattr(deployed, "record", broken_record)
    contestant = deployed.Contestant()
    state = GameState.from_mcp_state(payload(mode=mode, pending=[4]))
    contestant._bind(state, CONTEXT, "", "honor_of_kings_5v5")
    contestant._hok_refused_record = True
    out = contestant.choose_action(state, CONTEXT)
    assert out == "s4:macro_advance"
    assert contestant._hok_refused_record is False
