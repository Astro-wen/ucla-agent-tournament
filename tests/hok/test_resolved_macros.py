"""Regression for the resolved macro inputs recorded in two real morning games."""
from copy import deepcopy

import pytest

from agent.hok import policy as hok
from tests.hok.test_policy import HOK, _unwrap, hok_payload
from agent.agent import Contestant
from altruagent import GameState
from altruagent.runner import _validate_decision


def recorded_movement(seat, macro, move):
    # ca125797 line 1, seat 5, state_version 0 (04:04:28 PDT):
    # the platform includes a filled raw expansion AND its domain in a macro offer.
    return {"action_id": f"s{seat}:{macro}", "label": "Move toward the nearest enemy tower (72845 away)",
            "input": {"action": {"which_button": 2, "move": move,
                                  "legal_move": list(range(1, 25)), "seat": seat}}}


@pytest.mark.parametrize("camp", [1, 2])
@pytest.mark.parametrize("mode", ["realtime", "lockstep"])
def test_resolved_platform_macros_dispatch_all_five_heroes(camp, mode, monkeypatch):
    monkeypatch.setenv("ARENA_RECORD", "0")
    monkeypatch.delenv("ARENA_HOK", raising=False)
    seats = list(range(0 if camp == 1 else 5, 5 if camp == 1 else 10))
    entries = [recorded_movement(s, m, v) for s in seats
               for m, v in (("macro_advance", 10), ("macro_retreat", 21))]
    raw = hok_payload(mode=mode, pending=seats, entries=entries)
    raw.update(seats=seats, camp=camp)
    # No danger in the recorded opening: full HP, enemies hidden, no orders.
    raw["observation"] = {"seats": {str(s): {"summary": {
        "my_hero": {"hp": 3382, "max_hp": 3382, "camp": camp},
        "other_heroes": [], "structures": []}} for s in seats}}
    before = deepcopy(raw)
    contestant = Contestant()
    actions = []
    for _ in seats:
        state = GameState.from_mcp_state(raw)
        action = _unwrap(contestant.choose_action(state, HOK))
        sent = _validate_decision(action, state.legal_actions)
        assert sent.action_id == action and sent.action is None
        actions.append(action)
        if mode == "lockstep":
            raw["simultaneous"]["your_pending_seats"].remove(int(action[1]))
    assert set(actions) == {f"s{s}:macro_advance" for s in seats}
    assert raw["legal_actions"] == before["legal_actions"]


@pytest.mark.parametrize("changes", [
    {"move": None}, {"move": 25}, {"move": True}, {"move": [10]},
    {"legal_move": []}, {"legal_move": [True]}, {"legal_move": list(range(300))},
    {"legal_target": [1]}, {"legal_move": "10"},
    {"legal_seat": [0]}, {"legal_macro": ["macro_advance"]},
])
def test_unresolved_or_invalid_macro_domains_stay_rejected(changes):
    entry = recorded_movement(0, "macro_advance", 10)
    entry["input"]["action"].update(changes)
    assert not hok.offers_by_seat([entry])


def test_resolved_macro_compatibility_can_be_disabled(monkeypatch):
    monkeypatch.setattr(hok, "RESOLVED_MACRO_INPUTS", False)
    assert not hok.offers_by_seat([recorded_movement(0, "macro_advance", 10)])
