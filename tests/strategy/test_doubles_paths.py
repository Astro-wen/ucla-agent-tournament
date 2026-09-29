"""Every way agent.Contestant.choose_action can answer a doubles turn ends in a legal pair (the platform kills the match
process on an INVALID_ACTION): the paths test_doubles_pairs.py leaves out -- public reasoning on (the WithReasoning
wrapper), a busy worker (an earlier overrun still running), an answer that is not a doubles_turn dict at all (the
template's own id, the template itself, no `type`, a stale select_lineup from the preview, a singles action), and the
runner's separately fetched legal actions. Also: a repaired answer carries exactly `type` + one choice per slot, and
on every real recorded doubles template the two validity checks agree and the guard never touches a legal answer."""
import copy
import glob
import itertools
import json
import time
from pathlib import Path

import pytest

from agent import fallback as F
from agent.agent import Contestant, doubles_ok
from altruagent import DecisionContext, GameState, LegalAction, WithReasoning
from altruagent.runner import _validate_decision
from tests.strategy.test_doubles_pairs import DATA, forced_state, tmpl_of

GAME = "pokemon_vgc_doubles_draft"
PASS = {"type": "pass"}
SW = lambda sp: {"type": "switch", "species": sp}                      # noqa: E731
BAD = {"action": {"type": "doubles_turn", "slot_0": PASS, "slot_1": PASS}}
ROOT = Path(__file__).resolve().parents[2]


def ctx(game=GAME):
    return DecisionContext(session_id="sid", tournament_id=None, game_type=game, agent_id="me")


def pass_first(st):
    """The platform lists `pass` after the switch-in; listed first, a first-option fill passes."""
    st = copy.deepcopy(st)
    for s in tmpl_of(st)["slots"]:
        s["options"] = list(reversed(s["options"]))
    return st


def sent_ok(raw, out) -> bool:
    """What the runner accepts, and what the server accepts: the pair rule, and nothing but type + one choice per slot."""
    _validate_decision(out, GameState.from_mcp_state(raw).legal_actions)
    inner = out.action if isinstance(out, WithReasoning) else out
    keys = {"type"} | {f"slot_{s['slot']}" for s in tmpl_of(raw)["slots"]}
    return (isinstance(inner, dict) and set(inner) == keys and not F.doubles_problems(tmpl_of(raw), inner)
            and doubles_ok(raw, inner))


def _other_types(st):
    return {
        "template id": lambda s: {"action_id": "doubles_turn"},
        "template itself": lambda s: {"action": copy.deepcopy(tmpl_of(s))},
        "template itself + legal slots": lambda s: {"action": {**copy.deepcopy(tmpl_of(s)), "slot_0": SW("garchomp"),
                                                               "slot_1": PASS}},
        "no type": lambda s: {"action": {"slot_0": PASS, "slot_1": PASS}},
        "no type, legal slots": lambda s: {"action": {"slot_0": SW("garchomp"), "slot_1": PASS}},
        "stale select_lineup": lambda s: {"action": {"type": "select_lineup", "leads": ["a", "b"],
                                                     "bring": ["a", "b", "c", "d"]}},
        "singles switch": lambda s: {"action": {"type": "switch", "species": "garchomp", "slot": 0}},
        "action is a list": lambda s: {"action": [1, 2]},
        "no answer": lambda s: None,
    }


OTHER = list(_other_types(None))


# ---------------------------------------------------------------- answers that are not a doubles_turn dict
@pytest.mark.parametrize("reasoning", ["off", "safe", "detail"])
@pytest.mark.parametrize("what", OTHER)
def test_any_answer_shape_in_a_doubles_state_goes_out_as_a_clean_legal_pair(what, reasoning, monkeypatch):
    monkeypatch.setenv("ARENA_REASONING", reasoning)
    st = pass_first(forced_state(["garchomp"]))
    c = Contestant()
    monkeypatch.setattr(c.pk, "act", _other_types(st)[what])
    out = c.choose_action(GameState.from_mcp_state(copy.deepcopy(st)), ctx())
    assert sent_ok(st, out), (what, out)
    inner = out.action if isinstance(out, WithReasoning) else out
    assert SW("garchomp") in (inner["slot_0"], inner["slot_1"])
    assert isinstance(out, WithReasoning) == (reasoning != "off")


def test_the_recorded_payload_answered_with_its_own_template_sends_no_template_keys(monkeypatch):
    monkeypatch.setenv("ARENA_REASONING", "safe")
    raw = json.loads(DATA.read_text())["raw"]
    c = Contestant()
    monkeypatch.setattr(c.pk, "act", lambda s: {"action": copy.deepcopy(tmpl_of(s))})
    out = c.choose_action(GameState.from_mcp_state(copy.deepcopy(raw)), ctx())
    assert sent_ok(raw, out) and SW("irontreads") in (out.action["slot_0"], out.action["slot_1"])
    assert c.joint_repairs == 1


def test_a_second_legal_action_is_never_overridden(monkeypatch):
    """Only when the doubles template is the state's one legal action is every other answer rebuilt."""
    monkeypatch.setenv("ARENA_REASONING", "off")
    st = forced_state(["garchomp"])
    st["legal_actions"]["actions"].append({"action_id": "forfeit_later", "label": "x", "input": {}})
    c = Contestant()
    monkeypatch.setattr(c.pk, "act", lambda s: {"action_id": "forfeit_later"})
    out = c.choose_action(GameState.from_mcp_state(copy.deepcopy(st)), ctx())
    assert isinstance(out, LegalAction) and out.action_id == "forfeit_later" and c.joint_repairs == 0


# ---------------------------------------------------------------- reasoning, busy worker, fetched legal actions
@pytest.mark.parametrize("reasoning", ["safe", "detail"])
@pytest.mark.parametrize("bench", [[], ["garchomp"], ["garchomp", "amoonguss"]])
def test_public_reasoning_wraps_the_repaired_pair(bench, reasoning, monkeypatch):
    monkeypatch.setenv("ARENA_REASONING", reasoning)
    st = pass_first(forced_state(bench))
    c = Contestant()
    monkeypatch.setattr(c.pk, "act", lambda s: BAD)
    out = c.choose_action(GameState.from_mcp_state(copy.deepcopy(st)), ctx())
    assert sent_ok(st, out) and isinstance(out, WithReasoning)
    assert (out.action is BAD["action"]) == (not bench)            # nothing left: pass, pass is legal and goes as is
    assert c.joint_repairs == (1 if bench else 0)


def test_a_busy_worker_is_answered_by_a_legal_pair(monkeypatch):
    monkeypatch.setenv("ARENA_REASONING", "off")
    monkeypatch.setenv("ARENA_TC_BATTLE_S", "0.3")
    monkeypatch.setenv("ARENA_TC_MIN_S", "0.3")
    st = pass_first(forced_state(["garchomp"]))
    c = Contestant()
    monkeypatch.setattr(c.pk, "act", lambda s: time.sleep(1.5) or BAD)
    first = c.choose_action(GameState.from_mcp_state(copy.deepcopy(st)), ctx())
    assert c.last_decision["path"].startswith("overrun") and sent_ok(st, first)
    second = c.choose_action(GameState.from_mcp_state(copy.deepcopy(st)), ctx())
    assert c.last_decision["path"].startswith("busy") and sent_ok(st, second)
    time.sleep(1.6)                                                                  # let the hung call finish


def test_legal_actions_fetched_separately_by_the_runner(monkeypatch):
    """The runner fills state.legal_actions itself when the server left them out of the state (raw keeps None)."""
    monkeypatch.setenv("ARENA_REASONING", "off")
    st = pass_first(forced_state(["garchomp"]))
    gs = GameState.from_mcp_state(copy.deepcopy(st))
    gs.raw = {**gs.raw, "legal_actions": None}
    c = Contestant()
    monkeypatch.setattr(c.pk, "act", lambda s: BAD)
    out = c.choose_action(gs, ctx())
    assert sent_ok(st, out) and c.joint_repairs == 1


@pytest.mark.parametrize("game, actions", [
    ("werewolf", [{"action_id": "0", "label": "Vote to lynch Player0", "input": {"session_id": "s", "action_id": "0"}},
                  {"action_id": "7", "label": "Abstain", "input": {"session_id": "s", "action_id": "7"}}]),
    ("pokemon_gen9ou_teambuild", [{"action_id": "move:0", "label": "Earthquake",
                                   "input": {"action": {"type": "move", "slot": 0, "move_id": "earthquake"}}}]),
])
def test_the_guard_never_touches_werewolf_or_singles_answers(game, actions):
    st = {"session_id": "s", "game_type": game, "phase": "moving", "state_version": 1, "observation": {},
          "legal_actions": {"state_version": 1, "actions": actions}}
    gs = GameState.from_mcp_state(st)
    c = Contestant()
    for out in list(gs.legal_actions) + [a["action_id"] for a in actions] + [{"type": "move", "slot": 0}, None, 3]:
        assert c._joint_guard(gs, out) is out
    assert c.joint_repairs == 0


# ---------------------------------------------------------------- repair output shape
def test_a_repair_returns_exactly_type_and_one_choice_per_slot():
    tmpl = tmpl_of(forced_state(["garchomp"]))
    for junk in ({**tmpl, "slot_0": PASS, "slot_1": PASS}, {"type": "select_lineup", "bring": [1]}, {"x": 1}, None):
        fixed, notes = F.repair_doubles(junk, tmpl)
        assert notes and set(fixed) == {"type", "slot_0", "slot_1"} and not F.doubles_problems(tmpl, fixed)
    legal = {"type": "doubles_turn", "slot_0": SW("garchomp"), "slot_1": PASS}
    assert F.repair_doubles(legal, tmpl) == (legal, []) and F.repair_doubles(legal, tmpl)[0] is legal
    assert F.doubles_problems(tmpl, {"slot_0": SW("garchomp"), "slot_1": PASS})           # no type: not legal


# ---------------------------------------------------------------- every real recorded template
def _real_rows():
    for f in sorted(glob.glob(str(ROOT / "logs" / "*" / "*.jsonl"))):
        for line in open(f):
            rec = json.loads(line)
            if rec.get("kind") == "action":
                yield f, rec


def _choices(opts):
    return F._choices([o for o in opts if isinstance(o, dict)])


JUNK = [None, SW("pikachu"), {"type": "teleport"}]


@pytest.mark.skipif(not glob.glob(str(ROOT / "logs" / "*" / "*.jsonl")), reason="no real recordings")
def test_on_every_real_doubles_template_the_checks_agree_and_legal_answers_pass_untouched():
    seen, n_t, n_pairs, n_legal = set(), 0, 0, 0
    c = Contestant()
    for f, rec in _real_rows():
        raw = rec["raw"]
        obs = raw.get("observation") if isinstance(raw.get("observation"), dict) else {}
        gs = GameState.from_mcp_state(raw)
        if not (raw.get("phase") == "moving" and obs.get("is_doubles")):
            # not a doubles turn: the guard leaves every answer shape alone
            for out in [a for a in gs.legal_actions[:3]] + [rec.get("out")]:
                assert c._joint_guard(gs, out) is out, (f, out)
            continue
        tmpl = tmpl_of(raw)
        sig = json.dumps(tmpl["slots"], sort_keys=True)
        live = rec.get("out")
        if isinstance(live, dict) and not F.doubles_problems(tmpl, live):
            assert c._joint_guard(gs, live) is live
        if sig in seen:
            continue
        seen.add(sig)
        n_t += 1
        per = [_choices(s["options"]) for s in tmpl["slots"]]
        legal_exists = False
        for combo in itertools.product(*per):
            a = {"type": "doubles_turn", **{f"slot_{s['slot']}": ch for s, ch in zip(tmpl["slots"], combo)}}
            if not F.doubles_problems(tmpl, a):
                legal_exists = True
        assert legal_exists, f
        for combo in itertools.product(*[p + JUNK for p in per]):
            a = {"type": "doubles_turn", **{f"slot_{s['slot']}": ch for s, ch in zip(tmpl["slots"], combo)}}
            before = copy.deepcopy(a)
            n_pairs += 1
            ok = not F.doubles_problems(tmpl, a)
            assert doubles_ok(raw, a) == ok, (f, a)                  # the two checks agree
            fixed, notes = F.repair_doubles(a, tmpl)
            assert a == before                                      # never mutated
            if ok:
                n_legal += 1
                assert fixed is a and notes == []                   # a legal answer is never changed ...
                assert c._joint_guard(gs, a) is a                   # ... not even by the last line of defence
            else:
                assert notes and not F.doubles_problems(tmpl, fixed) and doubles_ok(raw, fixed), (f, a, fixed)
                assert set(fixed) == {"type"} | {f"slot_{s['slot']}" for s in tmpl["slots"]}
                if n_pairs % 7 == 0:                                # the guard (+ the policy's ranking): a sample
                    assert not F.doubles_problems(tmpl, c._joint_guard(gs, a))
    assert n_t > 50 and n_legal > 1000 and n_pairs > n_legal
