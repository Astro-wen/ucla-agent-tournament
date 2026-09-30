"""A doubles turn whose observation lacks the battle view (platform 2026-09-29, session 4b85cc32 row 16, the
Apocalypse_Red = v3.2 seat): the server sent the doubles_turn template next to an observation of only
{"message": "No pending decision is currently available.", "waiting_for_action": false, "finished": false} and still
took the answer (turn 8: Amoonguss used Spore). Without `is_doubles` the policy routed the state to the singles scorer,
which answered the unfilled template itself; agent.py then repaired it (`doubles action failed local validation`),
the one "joint repair" scripts/replay_real.py reported on 2349 real decisions. Now the policy recognises the doubles
turn by its template and answers the template's own legal pair, without touching its per-match memory; the public
line no longer claims a knockout count the observation does not carry."""
import copy
import json
from pathlib import Path

import pytest

from agent import fallback as F
from agent import reasoning as R
from agent.agent import Contestant, _doubles_template, doubles_ok
from agent.pokemon import policy as P
from agent.pokemon.policy import PokemonPolicy
from altruagent import GameState
from tests.strategy import test_lookahead as T
from tests.strategy.test_doubles_pairs import PASS, ctx, forced_state, sdk_legal, tmpl_of

DATA = Path(__file__).parent / "data" / "4b85cc32_blind_doubles.json"
REC = Path(__file__).resolve().parents[2] / "logs" / "apocalypse" / "4b85cc32-cbb0-4440-aa8d-2cfe28f92987.jsonl"
BLIND_OBS = {"message": "No pending decision is currently available.", "waiting_for_action": False, "finished": False}
SPORE_PASS = {"type": "doubles_turn", "slot_0": {"type": "move", "move_id": "spore", "target": 1}, "slot_1": PASS}


@pytest.fixture(autouse=True)
def _no_public_reasoning(monkeypatch):
    monkeypatch.setenv("ARENA_REASONING", "off")
    monkeypatch.setenv("ARENA_RECORD", "0")             # replaying a real payload must not record it again


def _recorded():
    return json.loads(DATA.read_text())


def test_the_fixture_is_the_recorded_blind_state():
    doc = _recorded()
    raw = doc["raw"]
    assert raw["phase"] == "moving" and raw["observation"] == BLIND_OBS
    assert tmpl_of(raw)["type"] == "doubles_turn" and len(raw["legal_actions"]["actions"]) == 1
    assert P._battle_kind(raw, raw["observation"]) == "doubles"
    assert doc["answered_live"] == SPORE_PASS and doubles_ok(raw, SPORE_PASS)    # what went out live (and was taken)


@pytest.mark.parametrize("mode", ["search", "classic"])
def test_the_policy_answers_the_blind_state_with_a_legal_pair(mode, monkeypatch):
    monkeypatch.setenv("ARENA_BATTLE", mode)
    raw = _recorded()["raw"]
    pol = PokemonPolicy()
    out = pol.act(copy.deepcopy(raw))["action"]
    assert "slots" not in out and not F.doubles_problems(tmpl_of(raw), out) and doubles_ok(raw, out)
    assert out == SPORE_PASS                     # the template's first options, a foe as target: the same move as live
    assert pol.search_stats["decisions"] == 0    # no field to rebuild: no engine search


@pytest.mark.parametrize("mode", ["search", "classic"])
def test_the_contestant_sends_it_without_a_repair(mode, monkeypatch):
    monkeypatch.setenv("ARENA_BATTLE", mode)
    raw = _recorded()["raw"]
    c = Contestant()
    out = c.choose_action(GameState.from_mcp_state(copy.deepcopy(raw)), ctx())
    assert sdk_legal(raw, out) and out == SPORE_PASS
    assert c.joint_repairs == 0 and c.fallbacks == 0 and c.last_decision["path"] == "policy"


def test_the_blind_state_leaves_the_match_memory_alone():
    """Fake Out freshness, foes on the field, speed conditions and Protect chains stay as the last real turn left them
    (the doubles scorer on an empty observation would, e.g., reset prev_active to [None, None])."""
    raw = _recorded()["raw"]
    pol = T.policy()
    pol.cond_set = {"opp_tailwind": 6}
    pol.last_move = {0: ("amoonguss", "sludgebomb")}
    pol.last_protect = {"amoonguss": 5}
    pol.seen_foes = ["rillaboom", "fluttermane"]

    def memory():
        return copy.deepcopy((pol.prev_active, pol.prev_opp, pol.opp_prev_active, pol.cond_set, pol.last_move,
                              pol.last_protect, pol.seen_foes))
    before = memory()
    pol.act(copy.deepcopy(raw))
    assert memory() == before


def test_the_public_line_claims_no_knockout_count_it_cannot_see():
    raw = _recorded()["raw"]
    # it used to read "Turn: choices locked in. Knockouts so far: 0 for us, 0 for them." (2 and 3 by then)
    assert R.attach(SPORE_PASS, raw, raw["game_type"], R.SAFE)[1] == "Turn: choices locked in."
    assert R.attach(SPORE_PASS, raw, raw["game_type"], R.DETAIL)[1] == "Turn: Slot 1 uses Spore."


def test_the_policy_fallback_fills_a_blind_doubles_template():
    raw = _recorded()["raw"]
    out = PokemonPolicy().fallback(copy.deepcopy(raw))["action"]
    assert "slots" not in out and doubles_ok(raw, out)            # it used to hand the unfilled template back


@pytest.mark.parametrize("bench, kinds", [
    ([], {("pass", "pass")}),
    (["garchomp"], {("switch", "pass"), ("pass", "switch")}),
    (["garchomp", "amoonguss"], {("switch", "switch")}),
])
def test_a_blind_forced_switch_is_answered_as_a_legal_pair(bench, kinds):
    st = forced_state(bench)
    for s in tmpl_of(st)["slots"]:
        s["options"] = list(reversed(s["options"]))            # pass listed first: a plain first-option fill passes
    st["observation"] = dict(BLIND_OBS)
    out = PokemonPolicy().act(copy.deepcopy(st))["action"]
    assert doubles_ok(st, out) and not F.doubles_problems(tmpl_of(st), out)
    assert (out["slot_0"]["type"], out["slot_1"]["type"]) in kinds
    c = Contestant()
    sent = c.choose_action(GameState.from_mcp_state(copy.deepcopy(st)), ctx())
    assert sdk_legal(st, sent) and c.joint_repairs == 0


def test_without_a_doubles_template_the_singles_scorer_still_answers(monkeypatch):
    called = []
    monkeypatch.setattr(PokemonPolicy, "_singles", lambda self, st, obs: called.append(1) or {"action_id": "move:0"})
    st = {"phase": "moving", "observation": dict(BLIND_OBS), "legal_actions": {"actions": [
        {"action_id": "move:0", "input": {"action": {"type": "move", "slot": 0}}}]}}
    assert PokemonPolicy().act(st) == {"action_id": "move:0"} and called == [1]


@pytest.mark.skipif(not REC.exists(), reason="the 4b85cc32 recording is not in logs/apocalypse")
def test_the_whole_recorded_match_replays_without_a_repair(monkeypatch):
    monkeypatch.setenv("ARENA_BATTLE", "classic")       # the classic scorer: fast; the blind row does not search anyway
    c = Contestant()
    n = 0
    for line in REC.read_text().splitlines():
        rec = json.loads(line)
        if rec["kind"] != "action":
            continue
        raw = rec["raw"]
        out = c.choose_action(GameState.from_mcp_state(raw), ctx())
        if raw.get("phase") == "moving" and _doubles_template(raw) is not None:
            n += 1
            assert sdk_legal(raw, out), (raw.get("state_version"), out)
    assert n >= 10 and c.joint_repairs == 0 and c.fallbacks == 0
