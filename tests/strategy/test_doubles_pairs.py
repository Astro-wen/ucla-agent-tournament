"""Doubles turns as a pair (Showdown's rule, enforced by the platform): never the same switch-in on both slots, and a
slot passes only when no switch-in is left for it after the other slot's choice.

Live incident 2026-09-29 (session f525d6ac, our Yoriko_Red = v4): both actives fainted on turn 2 with one Pokemon
left; v4 answered "pass, pass", the server rejected the pair and the runner killed the match three times. The engine
search produced it (its rebuilt battle lacked the last Pokemon: the platform's `team` lists all six drafted, so the
switch-in was unmappable and "pass, pass" was its only row). Fixed at three levels: the classic scorer (FORCED_PASS),
every validity check (fallback.joint_problems in agent.doubles_ok, policy._template_ok, search.js jointOk), and the
last line of defence in agent.py (fallback.repair_doubles on every answer, whatever path produced it)."""
import copy
import itertools
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from agent import fallback as F
from agent.agent import Contestant, doubles_ok
from agent.pokemon import engine
from agent.pokemon import policy as P
from agent.pokemon.policy import PokemonPolicy
from altruagent import DecisionContext, GameState
from altruagent.runner import _validate_decision
from tests.strategy import test_lookahead as T

GAME = "pokemon_vgc_doubles_draft"
DATA = Path(__file__).parent / "data" / "f525d6ac_double_faint.json"
REC = Path(__file__).resolve().parents[2] / "logs" / "yoriko" / "f525d6ac-e0a9-4b66-ba7c-a57c7ceb323d.jsonl"
HAVE_ENGINE = shutil.which("node") is not None and engine._paths() is not None
needs_engine = pytest.mark.skipif(not HAVE_ENGINE, reason="node / pokemon-showdown missing")

SW = lambda sp: {"type": "switch", "species": sp}                      # noqa: E731
PASS = {"type": "pass"}


@pytest.fixture(autouse=True)
def _no_public_reasoning(monkeypatch):
    monkeypatch.setenv("ARENA_REASONING", "off")


def ctx():
    return DecisionContext(session_id="sid", tournament_id=None, game_type=GAME, agent_id="me")


def tmpl_of(raw):
    return raw["legal_actions"]["actions"][0]["input"]["action"]


def sdk_legal(raw, out) -> bool:
    """What the runner validates, plus the server's pair rule."""
    _validate_decision(out, GameState.from_mcp_state(raw).legal_actions)
    return isinstance(out, dict) and not F.doubles_problems(tmpl_of(raw), out) and doubles_ok(raw, out)


def forced_state(bench, force=(True, True), fainted=(True, True), pass_too=True):
    """A forced-switch state: each forced slot offers the bench (+ pass when the bench cannot fill both slots, as
    the platform does), a slot that is not forced offers only pass."""
    need = sum(bool(f) for f in force)
    opts = []
    for i in range(2):
        if force[i]:
            o = [SW(sp) for sp in bench]
            if (len(bench) < need and pass_too) or not o:
                o.append(PASS)
        else:
            o = [PASS]
        opts.append(o)
    me = [T.view(T.CHIYU, 0.0, fainted=True) if fainted[0] else T.view(T.CHIYU),
          T.view(T.INC, 0.0, fainted=True) if fainted[1] else T.view(T.INC)]
    st = T.state(me, [T.view(T.RILL, foe=True), T.view(T.AMOO, foe=True)], opts=opts, force=force)
    st["observation"]["team"] = {f"p1: {sp}": T.view(T.GAR) if sp == "garchomp" else {"species": sp} for sp in bench}
    return st


# ---------------------------------------------------------------- the recorded failing payload (regression)
def _recorded():
    return json.loads(DATA.read_text())


def test_the_fixture_is_the_recorded_failure():
    doc = _recorded()
    raw = doc["raw"]
    assert raw["observation"]["force_switch"] == [True, True]
    assert doc["answered_live"]["slot_0"] == doc["answered_live"]["slot_1"] == PASS
    assert F.joint_problems(tmpl_of(raw), doc["answered_live"])            # the rule catches what the server rejected
    assert not doubles_ok(raw, doc["answered_live"])


@pytest.mark.parametrize("mode", ["search", "classic"])
def test_the_recorded_double_faint_brings_the_last_pokemon_in(mode, monkeypatch):
    monkeypatch.setenv("ARENA_BATTLE", mode)
    raw = _recorded()["raw"]
    c = Contestant()                                      # a fresh process, as each restarted worker was live
    out = c.choose_action(GameState.from_mcp_state(raw), ctx())
    assert sdk_legal(raw, out)
    assert sorted([out["slot_0"]["type"], out["slot_1"]["type"]]) == ["pass", "switch"]
    assert SW("irontreads") in (out["slot_0"], out["slot_1"])
    assert c.joint_repairs == 0                           # the policy itself answers legally now


@needs_engine
def test_the_search_no_longer_answers_pass_pass_on_the_recorded_payload(monkeypatch):
    monkeypatch.setenv("ARENA_BATTLE", "search")
    raw = _recorded()["raw"]
    engine.SearchEngine.get().warm()
    pol = PokemonPolicy()
    out = pol.act(raw)["action"]
    assert doubles_ok(raw, out) and SW("irontreads") in (out["slot_0"], out["slot_1"])
    # with only the four brought Pokemon rebuilt (battlestate._in_battle), the engine knows Iron Treads is the bench
    # Pokemon, so the search itself answers the double faint instead of falling back to the classic pick
    assert pol.search_stats["used"] == 1 and not pol.search_stats["reasons"]


@pytest.mark.skipif(not REC.exists(), reason="the f525d6ac recording is not in logs/yoriko")
def test_the_whole_recorded_match_replays_legally():
    c = Contestant()
    for line in REC.read_text().splitlines():
        rec = json.loads(line)
        raw = rec["raw"]
        if rec["kind"] != "action":
            continue
        out = c.choose_action(GameState.from_mcp_state(raw), ctx())
        if raw.get("phase") == "moving" and raw["observation"].get("is_doubles"):
            assert sdk_legal(raw, out), (raw.get("state_version"), out)


# ---------------------------------------------------------------- synthetic forced-switch templates
CASES = {
    "double faint, 0 bench": (forced_state([]), {("pass", "pass")}),
    "double faint, 1 bench": (forced_state(["garchomp"]), {("switch", "pass"), ("pass", "switch")}),
    "double faint, 2 bench": (forced_state(["garchomp", "amoonguss"]), {("switch", "switch")}),
    "single faint, 2 bench": (forced_state(["garchomp", "amoonguss"], force=(True, False), fainted=(True, False)),
                              {("switch", "pass")}),
    "single faint, 1 bench": (forced_state(["garchomp"], force=(False, True), fainted=(False, True)),
                              {("pass", "switch")}),
}


@pytest.mark.parametrize("mode", ["classic", "search"])
@pytest.mark.parametrize("name", list(CASES))
def test_forced_switch_templates_are_answered_as_a_legal_pair(name, mode, monkeypatch):
    monkeypatch.setenv("ARENA_BATTLE", mode)
    st, kinds = CASES[name]
    out = T.policy().act(copy.deepcopy(st))["action"]
    assert doubles_ok(st, out) and not F.doubles_problems(tmpl_of(st), out)
    assert (out["slot_0"]["type"], out["slot_1"]["type"]) in kinds
    c = Contestant()
    sent = c.choose_action(GameState.from_mcp_state(copy.deepcopy(st)), ctx())
    assert sdk_legal(st, sent) and c.joint_repairs == 0


@pytest.mark.parametrize("scorer", ["exact", "heuristic"])
def test_a_forced_pass_scores_below_any_switch_in_however_bad(scorer, monkeypatch):
    """The classic pick with every switch-in scored far below the old pass score (-100): still the switch-in."""
    monkeypatch.setenv("ARENA_BATTLE", "classic")
    monkeypatch.setattr(PokemonPolicy, "_switch_value", lambda self, opt, opps: -500.0)
    if scorer == "heuristic":
        monkeypatch.setattr(PokemonPolicy, "_exact_table", lambda *a, **k: None)
    st = forced_state(["garchomp"])
    out = T.policy().act(copy.deepcopy(st))["action"]
    assert doubles_ok(st, out) and SW("garchomp") in (out["slot_0"], out["slot_1"])
    monkeypatch.setattr(P, "FORCED_PASS", -100.0)                           # the old pass score: the bug comes back
    out = T.policy().act(copy.deepcopy(st))["action"]
    assert out["slot_0"] == PASS and not doubles_ok(st, out)
    c = Contestant()                                                         # ... and agent.py repairs it
    sent = c.choose_action(GameState.from_mcp_state(copy.deepcopy(st)), ctx())
    assert sdk_legal(st, sent) and c.joint_repairs == 1 and SW("garchomp") in (sent["slot_0"], sent["slot_1"])


def test_pass_only_and_commander_like_slots_keep_their_pass():
    both = forced_state([])                                                  # nothing left: pass, pass is legal
    assert F.joint_problems(tmpl_of(both), {"type": "doubles_turn", "slot_0": PASS, "slot_1": PASS}) == []
    st = T.state([T.view(T.CHIYU), T.view(T.INC)], [T.view(T.RILL, foe=True), T.view(T.AMOO, foe=True)])
    st["legal_actions"]["actions"][0]["input"]["action"]["slots"][1]["options"] = [PASS]   # Commander: only pass
    a = {"type": "doubles_turn", "slot_0": {"type": "move", "move_id": "heatwave", "target": 0}, "slot_1": PASS}
    assert doubles_ok(st, a) and F.repair_doubles(a, tmpl_of(st)) == (a, [])
    out = T.policy().act(st)["action"]
    assert doubles_ok(st, out) and out["slot_1"] == PASS and out["slot_0"]["type"] == "move"


# ---------------------------------------------------------------- the search's own answers
@pytest.mark.parametrize("bench, answer, reason", [
    (["garchomp"], {"slot_0": PASS, "slot_1": PASS}, "answer jointly illegal"),
    (["garchomp", "amoonguss"], {"slot_0": SW("garchomp"), "slot_1": SW("garchomp")}, "answer not in the template"),
])
def test_an_illegal_search_row_is_dropped_for_the_classic_pick(bench, answer, reason, monkeypatch):
    monkeypatch.setenv("ARENA_BATTLE", "search")
    st = forced_state(bench)

    class Fake:
        enabled = True
        last_error = None

        def search(self, *a, **k):
            return {"action": {"type": "doubles_turn", **answer}, "info": {"choice": "rigged"}}
    monkeypatch.setattr(engine.SearchEngine, "get", classmethod(lambda cls: Fake()))
    monkeypatch.setattr("agent.pokemon.battlestate.build", lambda *a, **k: {})
    pol = T.policy()
    out = pol.act(copy.deepcopy(st))["action"]
    assert pol.search_stats["used"] == 0 and pol.search_stats["reasons"] == {reason: 1}
    assert doubles_ok(st, out) and (out["slot_0"], out["slot_1"]) != (answer["slot_0"], answer["slot_1"])
    c = Contestant()
    c.pk = pol
    sent = c.choose_action(GameState.from_mcp_state(copy.deepcopy(st)), ctx())
    assert sdk_legal(st, sent) and c.joint_repairs == 0


def test_template_ok_applies_the_pair_rule():
    t = tmpl_of(forced_state(["garchomp"]))
    assert P._template_ok(t, {"slot_0": SW("garchomp"), "slot_1": PASS})
    assert P._template_ok(t, {"slot_0": PASS, "slot_1": SW("garchomp")})
    assert not P._template_ok(t, {"slot_0": PASS, "slot_1": PASS})
    assert P._template_ok(t, {"slot_0": PASS, "slot_1": PASS}, joint=False)
    assert not P._template_ok(t, {"slot_0": SW("garchomp"), "slot_1": SW("garchomp")}, joint=False)


@needs_engine
def test_search_js_joint_rows_follow_the_pair_rule():
    """search.js jointOptions with the platform template: no pass while the template offers a free switch-in, even one
    the rebuilt battle lacks; no row at all -> an error (the classic pick plays)."""
    js = Path(P.__file__).resolve().parents[1] / "js" / "search.js"
    code = f"""
const S = require({json.dumps(str(js))});
const sw = (j, sp) => ({{str: 'switch ' + j, kind: 'switch', j, species: sp}});
const pass = {{str: 'pass', kind: 'pass'}};
const T1 = [[{{type: 'switch', species: 'Iron Treads'}}, {{type: 'pass'}}], [{{type: 'switch', species: 'Iron Treads'}}, {{type: 'pass'}}]];
const known = S.jointOptions([[sw(3, 'irontreads'), pass], [sw(3, 'irontreads'), pass]], T1).map(r => r.str);
const missing = S.jointOptions([[pass], [pass]], T1).map(r => r.str);
const T0 = [[{{type: 'pass'}}], [{{type: 'pass'}}]];
const none = S.jointOptions([[pass], [pass]], T0).map(r => r.str);
const opp = S.jointOptions([[sw(3, 'x'), pass], [sw(3, 'x'), pass]]).map(r => r.str);
process.stdout.write(JSON.stringify({{known, missing, none, opp}}));
"""
    res = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=60, cwd=str(js.parent))
    assert res.returncode == 0, res.stderr
    got = json.loads(res.stdout)
    assert got["known"] == ["switch 3, pass", "pass, switch 3"]
    assert got["missing"] == []                              # search() then throws: no jointly legal row
    assert got["none"] == ["pass, pass"]                     # nothing left on both slots: legal
    assert got["opp"] == ["switch 3, pass", "pass, switch 3", "pass, pass"]   # the opponent's rows are untouched


# ---------------------------------------------------------------- repair_doubles: property test
MOVE = {"type": "move", "move_id": "heatwave", "targets": [0]}
ATTACK = {"type": "move", "move_id": "darkpulse", "targets": [-2, 1, 2]}
POOL = [MOVE, ATTACK, SW("garchomp"), SW("amoonguss"), PASS]


def _answers(opts):
    """Every per-slot legal choice of an option list."""
    out = []
    for o in opts:
        if o["type"] == "move":
            out += [{"type": "move", "move_id": o["move_id"], "target": t} for t in o["targets"]]
        elif o["type"] == "switch":
            out.append(SW(o["species"]))
        else:
            out.append(PASS)
    return out


JUNK = [None, {"type": "move", "move_id": "heatwave", "target": 1}, {"type": "teleport"}, SW("pikachu")]
SUBSETS = [list(c) for r in range(1, len(POOL) + 1) for c in itertools.combinations(POOL, r)]


def _kept(act, pair, o0, o1) -> int:
    """How many of `act`'s slot choices `pair` keeps: a per-slot legal choice as it is, an offered move at a bad
    target as the same move."""
    n = 0
    for k, opts in (("slot_0", o0), ("slot_1", o1)):
        a, p = act[k], pair[k]
        if F._slot_ok(a, opts):
            n += F._key(p) == F._key(a)
        elif isinstance(a, dict) and a.get("type") == "move" and p.get("type") == "move":
            n += F._tid(p.get("move_id")) == F._tid(a.get("move_id"))
    return n


def test_repair_is_legal_minimal_and_never_raises_over_every_pair_of_slots():
    """Every template made of 1-5 of {spread move, targeted move, switch A, switch B, pass} per slot (961 templates),
    every answer made of its legal choices and some junk per slot."""
    n = repaired = 0
    for o0, o1 in itertools.product(SUBSETS, SUBSETS):
        tmpl = {"type": "doubles_turn", "slots": [{"slot": 0, "options": o0}, {"slot": 1, "options": o1}]}
        legal = [{"slot_0": a, "slot_1": b} for a in _answers(o0) for b in _answers(o1)
                 if not F.doubles_problems(tmpl, {"type": "doubles_turn", "slot_0": a, "slot_1": b})]
        for a, b in itertools.product(_answers(o0) + JUNK, _answers(o1) + JUNK):
            act = {"type": "doubles_turn", "slot_0": a, "slot_1": b}
            before = copy.deepcopy(act)
            fixed, notes = F.repair_doubles(act, tmpl)
            n += 1
            assert act == before                                          # pure: the input is never mutated
            if not F.doubles_problems(tmpl, act):
                assert fixed is act and notes == []                       # a legal answer is sent as it is
                continue
            repaired += 1
            assert notes and fixed["type"] == "doubles_turn"
            if not legal:                                                 # nothing legal exists: best effort only
                continue
            assert not F.doubles_problems(tmpl, fixed), (tmpl, act, fixed, notes)
            assert F.repair_doubles(fixed, tmpl) == (fixed, [])           # idempotent
            # minimal: it keeps as many of the answer's legal slot choices as any legal pair does ...
            assert _kept(act, fixed, o0, o1) == max(_kept(act, p, o0, o1) for p in legal), (tmpl, act, fixed)
            for k, opts in (("slot_0", o0), ("slot_1", o1)):
                c = act[k]
                if isinstance(c, dict) and c.get("type") == "move" and F._slot_ok(c, opts):
                    assert fixed[k] == c                                  # ... always a legal move
                if c == PASS and not F._switch_ins(opts) and F._slot_ok(c, opts):
                    assert fixed[k] == PASS                               # ... and a pass with nothing to switch in
                if c == PASS and F._switch_ins(opts):
                    other = fixed["slot_1" if k == "slot_0" else "slot_0"]
                    free = [s for s in F._switch_ins(opts) if other.get("type") != "switch"
                            or F._tid(other.get("species")) != s]
                    assert not (fixed[k] == PASS and free)                # never a pass with a switch-in free
    assert n > 40000 and repaired > 10000


def test_repair_prefers_the_ranked_switch_in_and_survives_garbage():
    tmpl = {"type": "doubles_turn", "slots": [{"slot": 0, "options": [SW("garchomp"), SW("amoonguss"), PASS]},
                                              {"slot": 1, "options": [PASS]}]}
    bad = {"type": "doubles_turn", "slot_0": PASS, "slot_1": PASS}
    assert F.repair_doubles(bad, tmpl)[0]["slot_0"] == SW("garchomp")                 # no ranking: option order
    assert F.repair_doubles(bad, tmpl, prefer=["Amoonguss", "garchomp"])[0]["slot_0"] == SW("amoonguss")
    for junk in (None, "x", 3, [], {"slot_0": 5}):
        out, notes = F.repair_doubles(junk, tmpl)
        assert notes and not F.doubles_problems(tmpl, out)
    for t in (None, {}, {"slots": None}, {"slots": "x"}, {"slots": [None, 3]}):
        assert F.repair_doubles(bad, t) == (bad, [])                                  # nothing to check against
    assert F.doubles_problems(None, bad) and F.joint_problems(None, bad) == []


def test_template_fill_never_passes_next_to_a_free_switch_in():
    tmpl = {"type": "doubles_turn", "slots": [{"slot": 0, "options": [PASS, SW("garchomp")]},
                                              {"slot": 1, "options": [PASS, SW("garchomp")]}]}
    out = F.doubles_from_template(tmpl)
    assert not F.doubles_problems(tmpl, out) and SW("garchomp") in (out["slot_0"], out["slot_1"])
    st = forced_state(["garchomp"])
    for s in tmpl_of(st)["slots"]:
        s["options"] = list(reversed(s["options"]))                                    # pass listed first
    assert doubles_ok(st, PokemonPolicy().fallback(st)["action"])


# ---------------------------------------------------------------- the SDK-facing answer on every path
def _paths():
    return ["policy", "bad pair", "per-slot junk", "crash", "fallback crash", "fallback bad", "overrun"]


@pytest.mark.parametrize("path", _paths())
@pytest.mark.parametrize("bench", [[], ["garchomp"], ["garchomp", "amoonguss"]])
def test_the_answer_the_runner_gets_is_always_a_legal_pair(path, bench, monkeypatch):
    monkeypatch.setenv("ARENA_BATTLE", "classic")
    st = forced_state(bench)
    for s in tmpl_of(st)["slots"]:
        s["options"] = list(reversed(s["options"]))                                    # pass first when offered
    c = Contestant()
    bad = {"action": {"type": "doubles_turn", "slot_0": PASS, "slot_1": PASS}}
    if path == "bad pair":
        monkeypatch.setattr(c.pk, "act", lambda s: bad)
    elif path == "per-slot junk":
        monkeypatch.setattr(c.pk, "act", lambda s: {"action": {"type": "doubles_turn", "slot_0": SW("pikachu")}})
    elif path == "crash":
        monkeypatch.setattr(c.pk, "act", lambda s: 1 / 0)
    elif path == "fallback crash":
        monkeypatch.setattr(c.pk, "act", lambda s: 1 / 0)
        monkeypatch.setattr(c.pk, "fallback", lambda s: {}["boom"])
    elif path == "fallback bad":
        monkeypatch.setattr(c.pk, "act", lambda s: 1 / 0)
        monkeypatch.setattr(c.pk, "fallback", lambda s: bad)
    elif path == "overrun":
        monkeypatch.setenv("ARENA_TC_BATTLE_S", "0.3")
        monkeypatch.setenv("ARENA_TC_MIN_S", "0.3")
        monkeypatch.setattr(c.pk, "act", lambda s: time.sleep(1.0) or bad)
    out = c.choose_action(GameState.from_mcp_state(copy.deepcopy(st)), ctx())
    assert sdk_legal(st, out), (path, out)
    if bench:
        assert out["slot_0"]["type"] == "switch" or out["slot_1"]["type"] == "switch"
    if path == "overrun":
        time.sleep(1.0)                                                                # let the hung call finish


def test_a_repair_is_logged_and_counted(caplog):
    st = forced_state(["garchomp"])
    c = Contestant()
    c.pk.act = lambda s: {"action": {"type": "doubles_turn", "slot_0": PASS, "slot_1": PASS}}
    with caplog.at_level("WARNING", logger="agent"):
        out = c.choose_action(GameState.from_mcp_state(copy.deepcopy(st)), ctx())
    assert out == {"type": "doubles_turn", "slot_0": SW("garchomp"), "slot_1": PASS}
    assert c.joint_repairs == 1 and c.last_decision["path"] == "repaired"
    assert any("repaired" in r.getMessage() for r in caplog.records)


def test_switch_ranking_orders_the_offered_switch_ins():
    st = forced_state(["garchomp", "amoonguss"])
    got = P.switch_ranking(st)
    assert sorted(got) == ["amoonguss", "garchomp"]
    opps = st["observation"]["opponent_active_pokemon"]
    scores = [P._switch_score({"species": sp}, opps) for sp in got]
    assert scores == sorted(scores, reverse=True)
