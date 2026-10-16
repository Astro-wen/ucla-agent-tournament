"""Our Honor of Kings 5v5 team (agent/hok) and the Contestant's routing to it. Payloads follow the official SDK's
tests/test_hok.py (upstream 686e756, mirroring GameAPI's HoK adapter): seat-tagged legal actions, per-seat summaries,
standing orders, real time or lockstep."""
from __future__ import annotations

import pytest

from altruagent import WAIT, DecisionContext, LegalAction, WithReasoning
from altruagent.models import GameState
from altruagent.runner import run_game
from agent import agent as agent_mod
from agent.agent import Contestant, _is_hok
from agent.hok import HokPolicy, offers_by_seat, target_of
from tests.test_runner import FakeMCPGameSession, result_dict, terminal_state

HOK = DecisionContext(session_id="h", tournament_id=None, game_type="honor_of_kings_5v5", agent_id="a")
SEATS = [0, 1, 2, 3, 4]


def me(seat, hp=3000, max_hp=3000, x=None, z=-30000, revive=0):
    return {"hero_id": 100 + seat, "camp": 1, "hp": hp, "max_hp": max_hp, "level": 2,
            "x": -30000 + 1000 * seat if x is None else x, "z": z, "kills": 0, "deaths": 0, "revive_time": revive}


def summary(seat, enemies=(), towers=(), **kw):
    others = [{"runtime_id": 50 + i, "hero_id": 140 + i, "camp": 2, "visible": True, "hp": 2000, "max_hp": 3000,
               "x": x, "z": z} for i, (x, z) in enumerate(enemies)]
    others.append({"runtime_id": 99, "hero_id": 131, "camp": 2, "visible": False})
    structures = [{"camp": 2, "type": "ACTOR_TOWER", "hp": 6000, "max_hp": 6000, "x": x, "z": z} for x, z in towers]
    structures.append({"camp": 1, "type": "ACTOR_CRYSTAL", "hp": 8000, "max_hp": 8000, "x": -50000, "z": -50000})
    return {"my_hero": me(seat, **kw), "other_heroes": others, "structures": structures}


def entries(seat, macros=("macro_advance", "macro_retreat", "macro_attack"), near=12000, kind="enemy minion 7"):
    out = [{"action_id": f"s{seat}:button_0", "label": f"[seat {seat}] None",
            "input": {"action": {"which_button": 0, "seat": seat}}}]
    for m in macros:
        out.append({"action_id": f"s{seat}:{m}", "label": f"[seat {seat}] {m} (nearest: {kind}, {near} away)",
                    "input": {"action": {"which_button": 3, "seat": seat}}})
    return out


def payload(*, mode="realtime", pending=SEATS, standing=None, acts=None, summaries=None, version=10, game_type=True):
    acts = acts if acts is not None else [e for s in pending for e in entries(s)]
    out = {"session_id": "h", "status": "in_progress", "phase": "moving",
           "is_current_actor": bool(pending), "is_terminal": False, "state_version": version,
           "pacing": {"mode": mode, "step_interval_s": 0.2} if mode == "realtime" else {"mode": "lockstep"},
           "seats": SEATS, "camp": 1, "max_steps": 3000, "seconds_left": 1500,
           "observation": {"seats": {str(s): {"summary": (summaries or {}).get(s, summary(s))} for s in SEATS}},
           "simultaneous": {"your_pending_seats": list(pending), "standing_orders": standing or {}},
           "legal_actions": {"actions": acts}}
    if game_type:
        out["game_type"] = "honor_of_kings_5v5"
    return out


def state(**kw) -> GameState:
    return GameState.from_mcp_state(payload(**kw))


def decide(policy, st: GameState):
    return policy.decide(st.raw, st.legal_actions)[0]


def unwrap(d):
    return d.action if isinstance(d, WithReasoning) else d


ALL_ADVANCE = {str(s): "macro_advance" for s in SEATS}


# ---- parsing -------------------------------------------------------------------------------------------------------
def test_labels_give_the_target_kind_and_distance():
    assert target_of("Basic attack (nearest: enemy hero 169, 4123 away)") == ("enemy hero 169", 4123.0)
    assert target_of("x (nearest: y, 20000 away)") == ("y", 20000.0)
    assert target_of("Advance")[1] == float("inf")


def test_offers_split_macros_from_buttons_per_seat():
    st = state(acts=entries(3) + [{"action_id": "junk", "label": "?"}])
    offers = offers_by_seat(st.legal_actions)
    assert set(offers) == {3}
    assert set(offers[3]["macros"]) == {"macro_advance", "macro_retreat", "macro_attack"}
    assert [a.action_id for a in offers[3]["raw"]] == ["s3:button_0"]


# ---- what a hero wants ---------------------------------------------------------------------------------------------
def test_nothing_in_range_pushes_and_a_target_in_range_is_fought():
    p = HokPolicy()
    assert decide(p, state()) == {"macro": "macro_advance", "seat": 0}
    acts = [e for s in SEATS for e in entries(s, near=4000)]
    assert decide(HokPolicy(), state(acts=acts)) == {"macro": "macro_attack", "seat": 0}


def test_skills_before_the_basic_attack_and_the_ultimate_only_on_heroes():
    mac = ("macro_advance", "macro_retreat", "macro_attack", "macro_skill1", "macro_skill3")
    minion = [e for s in SEATS for e in entries(s, macros=mac, near=3000)]
    assert decide(HokPolicy(), state(acts=minion)) == {"macro": "macro_skill1", "seat": 0}
    hero = [e for s in SEATS for e in entries(s, macros=mac, near=3000, kind="enemy hero 169")]
    assert decide(HokPolicy(), state(acts=hero)) == {"macro": "macro_skill3", "seat": 0}


def test_low_hp_retreats_and_keeps_retreating_until_healed():
    p = HokPolicy()
    low = {0: summary(0, hp=600)}
    assert decide(p, state(summaries=low)) == {"macro": "macro_retreat", "seat": 0}
    standing = {**ALL_ADVANCE, "0": "macro_retreat"}
    half = {0: summary(0, hp=1800)}                       # 60%: still on the way home
    assert decide(p, state(summaries=half, standing=standing, version=30)) is None
    healed = {0: summary(0, hp=2900)}
    assert decide(p, state(summaries=healed, standing=standing, version=60)) == {"macro": "macro_advance", "seat": 0}
    assert p.retreats == 1


def test_outnumbered_heroes_back_off_and_a_hurt_hero_leaves_tower_range():
    three = [(-30000, -25000), (-29000, -25000), (-31000, -25000)]
    alone = {0: summary(0, enemies=three, x=-30000)}
    for s in SEATS[1:]:
        alone[s] = summary(s, x=20000, z=20000)            # the others far away
    assert decide(HokPolicy(), state(summaries=alone)) == {"macro": "macro_retreat", "seat": 0}
    hurt = {0: summary(0, hp=1200, towers=[(-30000, -24000)], x=-30000)}
    assert decide(HokPolicy(), state(summaries=hurt)) == {"macro": "macro_retreat", "seat": 0}
    fine = {0: summary(0, hp=2500, towers=[(-30000, -24000)], x=-30000)}
    assert decide(HokPolicy(), state(summaries=fine)) == {"macro": "macro_advance", "seat": 0}


# ---- real time -----------------------------------------------------------------------------------------------------
def test_realtime_sends_only_changes_and_waits_otherwise():
    p = HokPolicy()
    assert decide(p, state(standing=ALL_ADVANCE)) is None
    acts = entries(0, near=3000) + [e for s in SEATS[1:] for e in entries(s)]
    assert decide(p, state(acts=acts, standing=ALL_ADVANCE)) == {"macro": "macro_attack", "seat": 0}
    # Not shown as standing yet: not sent again right away; seat 0 is skipped, nothing else changes.
    assert decide(p, state(acts=acts, standing=ALL_ADVANCE, version=11)) is None


def test_realtime_breaking_off_a_fight_waits_for_the_hold_but_retreats_do_not():
    p = HokPolicy()
    st = {**ALL_ADVANCE, "0": "macro_attack"}
    assert decide(p, state(standing=st, version=10)) is None   # attack just started: held
    assert decide(p, state(standing=st, version=20)) == {"macro": "macro_advance", "seat": 0}
    p = HokPolicy()
    decide(p, state(standing=st, version=10))
    assert decide(p, state(standing=st, version=11, summaries={0: summary(0, hp=300)})) == \
        {"macro": "macro_retreat", "seat": 0}


def test_realtime_dead_hero_is_skipped_and_nothing_listed_waits():
    p = HokPolicy()
    acts = entries(0, macros=()) + [e for s in SEATS[1:] for e in entries(s)]
    assert decide(p, state(acts=acts)) == {"macro": "macro_advance", "seat": 1}
    assert decide(HokPolicy(), state(pending=[], acts=[])) is None


def test_a_refused_order_is_not_repeated_at_once():
    p = HokPolicy()
    acts = entries(0, near=3000) + [e for s in SEATS[1:] for e in entries(s)]
    assert decide(p, state(acts=acts, standing=ALL_ADVANCE)) == {"macro": "macro_attack", "seat": 0}
    p.on_result({"error": "INVALID_ACTION", "detail": "gone"})
    assert decide(p, state(acts=acts, standing=ALL_ADVANCE, version=12)) is None
    assert decide(p, state(acts=acts, standing=ALL_ADVANCE, version=20)) == {"macro": "macro_attack", "seat": 0}
    assert p.refusals == 1


# ---- lockstep ------------------------------------------------------------------------------------------------------
def test_lockstep_every_pending_hero_gets_a_listed_action():
    p = HokPolicy()
    st = state(mode="lockstep", pending=[2, 3], standing=ALL_ADVANCE)
    assert decide(p, st) == {"macro": "macro_advance", "seat": 2}
    dead = state(mode="lockstep", pending=[0], acts=entries(0, macros=()))
    out = decide(HokPolicy(), dead)
    assert isinstance(out, LegalAction) and out.action_id == "s0:button_0"


def test_lockstep_crash_answers_a_listed_button(monkeypatch):
    p = HokPolicy()
    monkeypatch.setattr(p, "_decide", lambda *a: 1 / 0)
    out = decide(p, state(mode="lockstep", pending=[3, 4]))
    assert isinstance(out, LegalAction) and out.action_id == "s3:button_0"
    assert decide(p, state()) is None and p.errors == 2


@pytest.mark.parametrize("raw", [None, {}, {"observation": "x", "simultaneous": 5, "pacing": {"mode": "realtime"}}])
def test_malformed_views_never_raise(raw):
    p = HokPolicy()
    p.decide(raw, [LegalAction.from_dict({"action_id": "s0:macro_advance", "label": "a"})])
    p.decide(raw, [])


# ---- the Contestant -----------------------------------------------------------------------------------------------
@pytest.fixture
def contestant(monkeypatch):
    monkeypatch.setenv("ARENA_RECORD", "0")
    monkeypatch.setenv("ARENA_MATCHSTATE", "0")
    return Contestant()


def test_hok_is_recognised_by_name_or_by_shape():
    assert _is_hok(state(), HOK)
    bare = DecisionContext(session_id="h", tournament_id=None, game_type=None, agent_id="a")
    assert _is_hok(state(game_type=False), bare)
    ra = DecisionContext(session_id="h", tournament_id=None, game_type="red_alert", agent_id="a")
    assert not _is_hok(state(), ra)


def test_contestant_plays_hok_with_a_public_line(contestant):
    out = contestant.choose_action(state(), HOK)
    assert isinstance(out, WithReasoning) and out.action == {"macro": "macro_advance", "seat": 0}
    assert out.reasoning_summary == "Hero 0 pushes."
    assert contestant.choose_action(state(standing=ALL_ADVANCE, version=11), HOK) is WAIT
    contestant.on_action_result({"error": "INVALID_ACTION"}, HOK)
    assert "hok views=2 orders=1" in contestant.summary_line()


def test_contestant_lockstep_never_waits(contestant, monkeypatch):
    from agent import hok
    monkeypatch.setattr(hok.HokPolicy, "decide", lambda self, raw, la: (None, None))
    out = contestant.choose_action(state(mode="lockstep", pending=[1]), HOK)
    assert out is not WAIT and out is not None


def test_arena_hok_off_waits_in_real_time(monkeypatch):
    monkeypatch.setenv("ARENA_RECORD", "0")
    monkeypatch.setenv("ARENA_HOK", "off")
    assert not agent_mod.hok_enabled()
    assert Contestant().choose_action(state(), HOK) is WAIT


class HokFakeGame(FakeMCPGameSession):
    def get_game_config(self, game_type) -> dict:
        return {}


def test_runner_plays_a_realtime_match_one_hero_per_call(monkeypatch):
    monkeypatch.setenv("ARENA_RECORD", "0")
    monkeypatch.setenv("ARENA_REASONING", "off")          # (the fake session's play_action takes no reasoning)
    monkeypatch.setenv("ARENA_MATCHSTATE", "0")
    accepted = {"session_id": "h", "status": "in_progress", "state_version": 10, "stepped": False}
    game = HokFakeGame("h")
    for done in range(6):
        standing = {str(s): "macro_advance" for s in range(done)}
        pending = list(range(done, 5))
        game.queue_state(state(pending=pending, standing=standing, acts=None if pending else []))
    game.queue_state(terminal_state())
    game.queue_play_action(*[accepted] * 5).queue_result(result_dict())
    run_game(game, HOK, Contestant(), sleep=lambda s: None)
    assert [c["action"] for c in game.play_action_calls] == [{"macro": "macro_advance", "seat": s} for s in SEATS]
    assert all(c["action_id"] is None for c in game.play_action_calls)
