"""Honor of Kings 5v5: the rule-based team (agent/hok/policy.py), its routing in the deployed Contestant
(agent/agent.py), and whole matches through the official runner. Payloads follow the official starter's contract
(GAMES.md "Honor of Kings") and the shapes of its tests (tests/test_hok.py upstream): seat-tagged legal actions
(`s3:macro_attack`), per-seat summaries, standing orders."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from altruagent import WAIT, DecisionContext, GameState, WithReasoning
from altruagent.runner import _validate_decision, run_game
from agent.agent import Contestant, _is_hok, _is_red_alert, hok_enabled
from agent.hok import policy as hok
from tests.test_runner import FakeMCPGameSession, result_dict, terminal_state

HOK = DecisionContext(session_id="h", tournament_id=None, game_type="honor_of_kings_5v5", agent_id="a")
SEATS = [0, 1, 2, 3, 4]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ARENA_RECORD", "0")
    monkeypatch.delenv("ARENA_HOK", raising=False)
    monkeypatch.delenv("ARENA_REASONING", raising=False)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _summary(seat, hp=3000, max_hp=3000, x=None, z=-30000, enemies=None, towers=None):
    return {
        "my_hero": {"hero_id": 100 + seat, "camp": 1, "hp": hp, "max_hp": max_hp, "level": 2,
                    "x": -30000 + 1000 * seat if x is None else x, "z": z, "kills": 0, "deaths": 0},
        "other_heroes": enemies if enemies is not None else [
            {"runtime_id": 7, "hero_id": 140, "camp": 2, "visible": True, "hp": 2000, "max_hp": 3000,
             "x": -20000, "z": -20000},
            {"runtime_id": 8, "hero_id": 131, "camp": 2, "visible": False}],
        "structures": towers if towers is not None else [
            {"camp": 2, "type": "ACTOR_TOWER", "hp": 6000, "max_hp": 6000, "x": 10000, "z": 10000},
            {"camp": 1, "type": "ACTOR_CRYSTAL", "hp": 8000, "max_hp": 8000, "x": -25000, "z": -25000}],
    }


def _entries(seat, macros=("macro_advance", "macro_retreat", "macro_attack"), near=12000, what="x"):
    out = [{"action_id": f"s{seat}:button_0", "label": f"[seat {seat}] None",
            "input": {"action": {"which_button": 0, "seat": seat}}},
           {"action_id": f"s{seat}:button_2", "label": f"[seat {seat}] Move",
            "input": {"action": {"which_button": 2, "legal_move_x": [4, 5], "legal_move_z": [7], "seat": seat}}}]
    for macro in macros:
        # The published platform guide offers basic attack only when a target is in range. Keep the original
        # far-target advance expectations with a physically consistent offer, rather than asserting against
        # an already-authorized attack because of a guessed distance threshold.
        if macro == "macro_attack" and near > 9000:
            continue
        out.append({"action_id": f"s{seat}:{macro}", "label": f"[seat {seat}] {macro} (nearest: {what}, {near} away)",
                    "input": {"action": {"macro": macro, "seat": seat}}})
    return out


def hok_payload(version=10, *, mode="realtime", pending=SEATS, standing=None, entries=None, summaries=None,
                game_type="honor_of_kings_5v5"):
    entries = entries if entries is not None else [e for s in pending for e in _entries(s)]
    return {
        "session_id": "h", "game_type": game_type, "status": "in_progress", "phase": "moving",
        "is_current_actor": bool(pending), "is_terminal": False, "state_version": version,
        "pacing": {"mode": mode, "step_interval_s": 0.2} if mode == "realtime" else {"mode": "lockstep"},
        "seats": SEATS, "camp": 1, "max_steps": 3000, "seconds_left": 1500,
        "observation": {"seats": {str(s): {"summary": (summaries or {}).get(s, _summary(s))} for s in SEATS}},
        "simultaneous": {"your_pending_seats": list(pending), "standing_orders": standing or {}},
        "legal_actions": {"actions": entries},
    }


def hok_state(*args, **kwargs) -> GameState:
    return GameState.from_mcp_state(hok_payload(*args, **kwargs))


def _unwrap(decision):
    return decision.action if isinstance(decision, WithReasoning) else decision


# -- the rules for one hero ----------------------------------------------------------------------------------------


def test_rules_retreat_use_offered_attacks_else_push():
    m = {"macro_advance": "a", "macro_retreat": "r", "macro_attack": "x (nearest: minion 3, 4000 away)"}
    assert hok.rule_macro(_summary(0, hp=500), m) == "macro_retreat"
    assert hok.rule_macro(_summary(0), m) == "macro_attack"
    assert hok.rule_macro(_summary(0), {**m, "macro_skill2": "s (nearest: minion 3, 3000 away)"}) == "macro_skill2"
    # The live guide makes attack availability authoritative; a display label is not a universal range rule.
    assert hok.rule_macro(_summary(0), {**m, "macro_attack": "x (nearest: y, 20000 away)"}) == "macro_attack"
    assert hok.rule_macro(_summary(0), {k: v for k, v in m.items() if k != "macro_attack"}) == "macro_advance"
    assert hok.rule_macro(_summary(0), {}) is None                     # a dead hero has no macros


def test_ultimate_only_at_an_enemy_hero():
    m = {"macro_advance": "a", "macro_attack": "x (nearest: minion 3, 4000 away)",
         "macro_skill3": "u (nearest: minion 3, 4000 away)"}
    assert hok.rule_macro(_summary(0), m) == "macro_attack"
    m["macro_skill3"] = "u (nearest: enemy hero 169, 4000 away)"
    assert hok.rule_macro(_summary(0), m) == "macro_skill3"


def test_retreat_holds_until_healed():
    m = {"macro_advance": "a", "macro_retreat": "r"}
    assert hok.rule_macro(_summary(0, hp=2000), m, retreating=True) == "macro_retreat"     # 67% < 85%
    assert hok.rule_macro(_summary(0, hp=2700), m, retreating=True) == "macro_advance"     # 90%: back out
    assert hok.rule_macro(_summary(0, hp=2000), m, retreating=False) == "macro_advance"


def test_numerical_overmatch_retreats_at_full_hp_but_tower_only_remains_hurt_gated():
    m = {"macro_advance": "a", "macro_retreat": "r", "macro_attack": "x (nearest: enemy hero 7, 5000 away)"}
    three = [{"runtime_id": i, "camp": 2, "visible": True, "hp": 3000, "x": 0, "z": 1000 * i} for i in range(3)]
    s = _summary(0, hp=1500, x=0, z=0, enemies=three, towers=[])
    raw = hok_payload(summaries={0: s, **{k: _summary(k, x=-60000, z=-60000) for k in SEATS[1:]}})
    team = hok.Team(raw, SEATS)
    assert team.outnumbered((0, 0))
    assert hok.rule_macro(s, m, team) == "macro_retreat"
    assert hok.rule_macro(_summary(0, hp=3000, x=0, z=0, enemies=three, towers=[]), m, team) == "macro_retreat"
    tower = [{"camp": 2, "type": "ACTOR_TOWER", "hp": 6000, "x": 3000, "z": 0}]
    s2 = _summary(0, hp=1500, x=0, z=0, enemies=[], towers=tower)
    team2 = hok.Team(hok_payload(summaries={k: s2 for k in SEATS}), SEATS)
    assert team2.under_tower((0, 0)) and hok.rule_macro(s2, m, team2) == "macro_retreat"
    assert hok.rule_macro(_summary(0, hp=3000, x=0, z=0, enemies=[], towers=tower), m, team2) == "macro_attack"


@pytest.mark.parametrize("scenario", ["one-extra", "allies-near", "hidden", "dead", "far"])
def test_full_hp_retreat_requires_actual_visible_nearby_numerical_overmatch(scenario):
    foes = [{"runtime_id": i, "camp": 2, "visible": True, "hp": 3000, "x": 0, "z": 1000 * i}
            for i in range(3)]
    if scenario == "one-extra":
        foes.pop()
    elif scenario == "hidden":
        foes[2]["visible"] = False
    elif scenario == "dead":
        foes[2]["hp"] = 0
    elif scenario == "far":
        foes[2]["x"] = hok.NEAR + 1
    me = _summary(0, hp=3000, x=0, z=0, enemies=foes, towers=[])
    allies = {k: _summary(k, x=-60000, z=-60000) for k in SEATS[1:]}
    if scenario == "allies-near":
        allies[1] = _summary(1, x=1000, z=0)
    team = hok.Team(hok_payload(summaries={0: me, **allies}), SEATS)
    macros = {"macro_advance": "a", "macro_retreat": "r", "macro_attack": "Attack"}
    assert not team.outnumbered((0, 0))
    assert hok.rule_macro(me, macros, team) == "macro_attack"


# -- real time --------------------------------------------------------------------------------------------------


def test_realtime_sends_each_changed_order_once_then_waits():
    p = hok.HokPolicy(clock=Clock())
    out, tag = p.decide(hok_payload())
    assert out == "s0:macro_advance" and tag
    standing = {"0": "macro_advance"}
    out, _ = p.decide(hok_payload(pending=[1, 2, 3, 4], standing=standing))
    assert out == "s1:macro_advance"
    done = {str(s): "macro_advance" for s in SEATS}
    assert p.decide(hok_payload(standing=done)) == (None, None)


def test_realtime_dead_hero_is_skipped_and_nothing_listed_is_nothing_to_send():
    p = hok.HokPolicy(clock=Clock())
    assert p.decide(hok_payload(pending=[], entries=[])) == (None, None)
    entries = _entries(0, macros=()) + _entries(1)
    assert p.decide(hok_payload(pending=[0, 1], entries=entries))[0] == "s1:macro_advance"


def test_a_fresh_order_is_kept_a_moment_but_retreat_is_immediate():
    clock = Clock()
    p = hok.HokPolicy(clock=clock)
    assert p.decide(hok_payload(pending=[0], entries=_entries(0)))[0] == "s0:macro_advance"
    near = _entries(0, near=4000)
    standing = {"0": "macro_advance"}
    clock.now = 0.2
    assert p.decide(hok_payload(pending=[0], entries=near, standing=standing)) == (None, None)
    clock.now = 1.0
    assert p.decide(hok_payload(pending=[0], entries=near, standing=standing))[0] == "s0:macro_attack"
    clock.now = 1.1                                                   # low HP: no hold
    low = {0: _summary(0, hp=300)}
    out, _ = p.decide(hok_payload(pending=[0], entries=near, standing={"0": "macro_attack"}, summaries=low))
    assert out == "s0:macro_retreat"


def test_an_order_just_sent_is_not_resent_while_the_standing_orders_lag():
    clock = Clock()
    p = hok.HokPolicy(clock=clock)
    assert p.decide(hok_payload(pending=[0], entries=_entries(0)))[0] == "s0:macro_advance"
    clock.now = 0.1                                                   # the next view still shows no standing order
    assert p.decide(hok_payload(pending=[0], entries=_entries(0))) == (None, None)
    clock.now = 1.0                                                   # a server that never lists them: ours count
    assert p.decide(hok_payload(pending=[0], entries=_entries(0))) == (None, None)
    clock.now = 1.1                                                   # one that lists another order: replace it
    out, _ = p.decide(hok_payload(pending=[0], entries=_entries(0), standing={"0": "macro_retreat"}))
    assert out == "s0:macro_advance"


def test_an_order_on_a_macro_that_went_away_is_replaced():
    p = hok.HokPolicy(clock=Clock())
    out, _ = p.decide(hok_payload(pending=[0], entries=_entries(0), standing={"0": "macro_skill1"}))
    assert out == "s0:macro_advance"


def test_a_refused_order_forgets_what_was_sent():
    p = hok.HokPolicy(clock=Clock())
    p.decide(hok_payload())
    p.on_result({"error": "INVALID_ACTION", "detail": "gone"})
    assert p.refused == 1 and p._sent == {}
    assert "orders=1" in p.summary_text()


# -- lockstep ---------------------------------------------------------------------------------------------------


def test_lockstep_gives_every_pending_hero_an_action_even_on_its_standing_order():
    p = hok.HokPolicy(clock=Clock())
    standing = {str(s): "macro_advance" for s in SEATS}
    assert p.decide(hok_payload(mode="lockstep", pending=[2, 3], standing=standing))[0] == "s2:macro_advance"


def test_lockstep_dead_hero_plays_the_do_nothing_button():
    p = hok.HokPolicy(clock=Clock())
    assert p.decide(hok_payload(mode="lockstep", pending=[0], entries=_entries(0, macros=())))[0] == "s0:button_0"
    raw_only = [{"action_id": "s0:button_2", "input": {"action": {"which_button": 2, "legal_move_x": [4, 5],
                                                                 "legal_move_z": [7], "seat": 0}}}]
    out, _ = p.decide(hok_payload(mode="lockstep", pending=[0], entries=raw_only))
    assert out == {"seat": 0, "which_button": 2, "move_x": 4, "move_z": 7}


# -- the deployed Contestant ------------------------------------------------------------------------------------


def test_the_switch():
    assert hok_enabled({}) and hok_enabled({"ARENA_HOK": "on"})
    for off in ("off", "0", "false", "no", " OFF ", "none", "disable", "disabled", "wait"):
        assert not hok_enabled({"ARENA_HOK": off})


def test_which_states_count_as_hok():
    ctx = lambda g: DecisionContext(session_id="x", tournament_id=None, game_type=g, agent_id="a")  # noqa: E731
    st = hok_state()
    for g in ("honor_of_kings_5v5", "Honor of Kings", "honor-of-kings", "hok", "HOK_5V5"):
        assert _is_hok(st, ctx(g))
    assert not _is_hok(hok_state(game_type=None), ctx(None))            # unknown games keep their original route
    assert not _is_red_alert(st, ctx("honor_of_kings_5v5"))
    for g in ("red_alert", "werewolf", "pokemon_vgc_doubles_draft"):
        assert not _is_hok(st, ctx(g))
    assert not _is_hok(SimpleNamespace(raw=None), ctx(None))


def test_contestant_plays_hok_with_listed_actions_and_waits_when_nothing_changes():
    c = Contestant()
    out = c.choose_action(hok_state(), HOK)
    assert isinstance(out, WithReasoning) and out.reasoning_summary
    assert _unwrap(out) == "s0:macro_advance"
    _validate_decision(out, hok_state().legal_actions)                  # what the runner accepts
    assert c.last_decision["path"] == "hok"
    done = {str(s): "macro_advance" for s in SEATS}
    assert c.choose_action(hok_state(standing=done), HOK) is WAIT
    assert "hok views=2 orders=1" in c.summary_line()


def test_contestant_lockstep_never_waits_even_when_the_policy_breaks():
    c = Contestant()
    st = hok_state(mode="lockstep", pending=[0], entries=_entries(0))
    assert _unwrap(c.choose_action(st, HOK)) == "s0:macro_advance"
    c._hok.decide = lambda raw: (_ for _ in ()).throw(RuntimeError("boom"))
    out = c.choose_action(st, HOK)
    assert out is not WAIT
    _validate_decision(out, st.legal_actions)
    rt = hok_state()
    assert c.choose_action(rt, HOK) is WAIT                           # real time: a broken policy just waits


def test_arena_hok_off_restores_the_old_behaviour(monkeypatch):
    monkeypatch.setenv("ARENA_HOK", "off")
    c = Contestant()
    assert c.choose_action(hok_state(), HOK) is WAIT and c._hok is None


def test_reasoning_off_sends_the_bare_action_id(monkeypatch):
    monkeypatch.setenv("ARENA_REASONING", "off")
    assert Contestant().choose_action(hok_state(), HOK) == "s0:macro_advance"


# -- through the official runner --------------------------------------------------------------------------------


class HokFakeGame(FakeMCPGameSession):
    def get_game_config(self, game_type) -> dict:
        return {}


def test_runner_plays_a_realtime_hok_match_one_hero_per_call(monkeypatch):
    monkeypatch.setenv("ARENA_REASONING", "off")                     # (this fake session takes no reasoning)
    accepted = {"session_id": "h", "status": "in_progress", "state_version": 10, "stepped": False}
    game = HokFakeGame("h")
    for done in range(6):
        standing = {str(s): "macro_advance" for s in range(done)}
        pending = list(range(done, 5))
        game.queue_state(hok_state(10, pending=pending, standing=standing, entries=None if pending else []))
    game.queue_state(terminal_state())
    game.queue_play_action(*[accepted] * 5).queue_result(result_dict())
    run_game(game, HOK, Contestant(), sleep=lambda s: None)
    assert [c["action_id"] for c in game.play_action_calls] == [f"s{s}:macro_advance" for s in SEATS]


def test_runner_plays_a_lockstep_hok_step_for_every_pending_hero(monkeypatch):
    monkeypatch.setenv("ARENA_REASONING", "off")
    accepted = {"session_id": "h", "status": "in_progress", "state_version": 10}
    game = HokFakeGame("h")
    standing = {str(s): "macro_advance" for s in SEATS}
    for s in SEATS:
        game.queue_state(hok_state(10, mode="lockstep", pending=SEATS[s:], standing=standing))
    game.queue_state(terminal_state())
    game.queue_play_action(*[accepted] * 5).queue_result(result_dict())
    run_game(game, HOK, Contestant(), sleep=lambda s: None)
    assert [c["action_id"] for c in game.play_action_calls] == [f"s{s}:macro_advance" for s in SEATS]
