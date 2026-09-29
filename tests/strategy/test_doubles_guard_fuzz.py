"""agent.py's last line of defence for doubles turns must hold for ANY answer shape, not only for a typed pair.

Found by fuzzing the pair guard (09-29, after the f525d6ac hotfix): every recorded real doubles template, mutated
(faint one / both actives, fewer bench Pokemon, extra / first-listed pass, duplicated and display-name switch-ins,
Commander-like pass-only slots, locked moves, Struggle, odd target lists, swapped slots), answered by an adversarial
policy. Two answer shapes went past the guard to the runner:
  * the template's own action id ({"action_id": "doubles_turn"}, from the policy or its fallback): the runner sends
    the LegalAction as it is, i.e. the unfilled template;
  * an untyped pair ({"slot_0": pass, "slot_1": pass} without "type"): sent as it was, pair rule unchecked.
The real policy answers neither (it always sends a typed doubles_turn), so this is defence in depth. Also: a move
option without a move_id matched a move choice without one, and repair_doubles then failed on it (KeyError)."""
import copy
import itertools
import json
import random
import re
from pathlib import Path

import pytest

from agent import fallback as F
from agent.agent import Contestant, doubles_ok
from altruagent import DecisionContext, GameState
from altruagent.runner import _validate_decision

GAME = "pokemon_vgc_doubles_draft"
DATA = Path(__file__).parent / "data" / "f525d6ac_double_faint.json"
PASS = {"type": "pass"}


def SW(sp):
    return {"type": "switch", "species": sp}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ARENA_REASONING", "off")
    monkeypatch.setenv("ARENA_BATTLE", "classic")


def ctx():
    return DecisionContext(session_id="sid", tournament_id=None, game_type=GAME, agent_id="me")


def recorded():
    return json.loads(DATA.read_text())["raw"]


def tmpl_of(raw):
    return raw["legal_actions"]["actions"][0]["input"]["action"]


# ---------------------------------------------------------------- an oracle independent of agent/fallback.py
def _id(x) -> str:
    return re.sub(r"[^a-z0-9]", "", str(x).lower()) if x is not None else ""


def _int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _choice_ok(c, opts) -> bool:
    if not isinstance(c, dict):
        return False
    if c.get("type") == "move":
        return _int(c.get("target")) and any(
            o.get("type") == "move" and o.get("move_id") is not None and _id(o["move_id"]) == _id(c.get("move_id"))
            and c["target"] in (o.get("targets") or [0]) for o in opts)
    if c.get("type") == "switch":
        return isinstance(c.get("species"), str) and _id(c["species"]) in {
            _id(o.get("species")) for o in opts if o.get("type") == "switch" and o.get("species")}
    return c.get("type") == "pass" and any(o.get("type") == "pass" for o in opts)


def showdown_legal(tmpl, ans) -> bool:
    """Showdown (sim/side.ts): a typed dict, each slot one of its options, never the same switch-in twice, and in a
    switch request at most (#forced slots - #of them the offered bench can fill) of the forced slots pass."""
    if not isinstance(ans, dict) or ans.get("type") != "doubles_turn":
        return False
    slots = [(f"slot_{s['slot']}", s["options"], bool(s.get("force_switch"))) for s in tmpl["slots"]]
    if not all(_choice_ok(ans.get(k), opts) for k, opts, _ in slots):
        return False
    ins = [_id(ans[k]["species"]) for k, _, _ in slots if ans[k]["type"] == "switch"]
    if len(ins) != len(set(ins)):
        return False
    forced = [(k, {_id(o["species"]) for o in opts if o.get("type") == "switch"}) for k, opts, f in slots if f]
    fill = max((n for n in range(len(forced) + 1) for rows in itertools.combinations(forced, n)
                if any(all(sp in b for sp, (_, b) in zip(p, rows))
                       for p in itertools.permutations(set().union(*[b for _, b in rows]), n))), default=0)
    return sum(ans[k]["type"] == "pass" for k, _ in forced) <= len(forced) - fill


def _choices(opts):
    out = []
    for o in opts:
        if o.get("type") == "move" and o.get("move_id") is not None:
            out += [{"type": "move", "move_id": o["move_id"], "target": t} for t in (o.get("targets") or [0])]
        elif o.get("type") == "switch":
            out.append(SW(o["species"]))
        elif o.get("type") == "pass":
            out.append(dict(PASS))
    return out


def has_legal(tmpl) -> bool:
    return any(showdown_legal(tmpl, {"type": "doubles_turn", **{f"slot_{s['slot']}": c for s, c in zip(tmpl["slots"], p)}})
               for p in itertools.product(*[_choices(s["options"]) for s in tmpl["slots"]]))


def sent_legal(raw, out) -> bool:
    _validate_decision(out, GameState.from_mcp_state(raw).legal_actions)
    return showdown_legal(tmpl_of(raw), out) and doubles_ok(raw, out)


# ---------------------------------------------------------------- the reproducers
@pytest.mark.parametrize("where", ["policy", "fallback"])
def test_the_templates_own_action_id_is_never_sent_unfilled(where):
    raw = recorded()
    c = Contestant()
    if where == "policy":
        c.pk.act = lambda s: {"action_id": "doubles_turn"}
    else:
        c.pk.act = lambda s: 1 / 0
        c.pk.fallback = lambda s: {"action_id": "doubles_turn"}
    out = c.choose_action(GameState.from_mcp_state(copy.deepcopy(raw)), ctx())
    assert isinstance(out, dict) and sent_legal(raw, out), out
    assert SW("irontreads") in (out["slot_0"], out["slot_1"])
    assert c.joint_repairs == 1 and "filled" in c.last_decision["path"]


def test_the_templates_own_action_id_in_a_move_turn_keeps_both_actives_in():
    """The fill takes each slot's first option: in a move turn that is a move, never both actives switching out."""
    raw = recorded()
    for s in tmpl_of(raw)["slots"]:
        s["force_switch"] = False
        s["options"] = [{"type": "move", "move_id": "heatwave", "targets": [0]},
                        {"type": "move", "move_id": "darkpulse", "targets": [-2, 1, 2]}, SW("irontreads")]
    c = Contestant()
    c.pk.act = lambda s: {"action_id": "doubles_turn"}
    out = c.choose_action(GameState.from_mcp_state(copy.deepcopy(raw)), ctx())
    assert sent_legal(raw, out), out
    assert out["slot_0"]["type"] == out["slot_1"]["type"] == "move" and c.joint_repairs == 1


def test_an_untyped_pair_is_typed_and_checked_as_a_pair():
    raw = recorded()
    c = Contestant()
    c.pk.act = lambda s: {"action": {"slot_0": PASS, "slot_1": PASS}}               # the incident's pair, no type
    out = c.choose_action(GameState.from_mcp_state(copy.deepcopy(raw)), ctx())
    assert sent_legal(raw, out) and SW("irontreads") in (out["slot_0"], out["slot_1"])
    legal = {"slot_0": SW("irontreads"), "slot_1": PASS}
    c.pk.act = lambda s: {"action": {"type": "doubles", **legal}}                     # a wrong type, legal slots
    out = c.choose_action(GameState.from_mcp_state(copy.deepcopy(raw)), ctx())
    assert out == {"type": "doubles_turn", **legal}


def test_doubles_problems_checks_the_type_and_repair_changes_only_that():
    tmpl = tmpl_of(recorded())
    legal = {"slot_0": SW("irontreads"), "slot_1": PASS}
    assert F.doubles_problems(tmpl, {"type": "doubles_turn", **legal}) == []
    for t in (None, "doubles", "move"):
        act = {"type": t, **legal} if t else dict(legal)
        assert F.doubles_problems(tmpl, act)
        fixed, notes = F.repair_doubles(act, tmpl)
        assert fixed == {"type": "doubles_turn", **legal} and notes


def test_a_move_option_without_a_move_id_matches_no_move():
    tmpl = {"type": "doubles_turn", "slots": [
        {"slot": 0, "options": [{"type": "move"}, {"type": "move", "move_id": "heatwave", "targets": [0]}]},
        {"slot": 1, "options": [PASS]}]}
    act = {"type": "doubles_turn", "slot_0": {"type": "move", "move_id": None, "target": 0}, "slot_1": PASS}
    assert not F._slot_ok(act["slot_0"], tmpl["slots"][0]["options"])
    fixed, notes = F.repair_doubles(act, tmpl)
    assert not any("repair failed" in n for n in notes), notes
    assert fixed["slot_0"] == {"type": "move", "move_id": "heatwave", "target": 0} and not F.doubles_problems(tmpl, fixed)


# ---------------------------------------------------------------- a small fuzz over the recorded state
BENCH = ["irontreads", "wochien", "Iron Hands"]


def _mutate(raw, rng):
    """The recorded state with a random doubles template (the policy is adversarial: only the template matters)."""
    r = copy.deepcopy(raw)
    t = tmpl_of(r)
    moves = [{"type": "move", "move_id": m, "targets": tg} for m, tg in
             (("heatwave", [0]), ("darkpulse", [-2, 1, 2]), ("snarl", [0]), ("overheat", [-1, 1, 2]))]
    kind = rng.choice(["double faint", "single faint", "move", "commander", "locked", "struggle"])
    bench = rng.sample(BENCH, rng.randint(0, 3))
    if kind in ("double faint", "single faint"):
        forced = [0, 1] if kind == "double faint" else [rng.randint(0, 1)]
        for i, s in enumerate(t["slots"]):
            s["force_switch"] = i in forced
            o = [SW(sp) for sp in bench] if i in forced else [dict(PASS)]
            if i in forced and (len(bench) < len(forced) or rng.random() < 0.3):
                o.insert(rng.randint(0, len(o)), dict(PASS))
            s["options"] = o
    else:
        for i, s in enumerate(t["slots"]):
            s["force_switch"] = False
            o = copy.deepcopy(rng.sample(moves, rng.randint(1, 4))) + [SW(sp) for sp in bench]
            if kind == "commander" and i == 1:
                o = [dict(PASS)]
            if kind == "locked" and i == 0:
                o = [{"type": "move", "move_id": "overheat", **rng.choice([{}, {"targets": []}, {"targets": [1]}])}] \
                    + ([SW(sp) for sp in bench] if rng.random() < 0.5 else [])
            if kind == "struggle" and i == 1:
                o = [{"type": "move", "move_id": "struggle", "targets": rng.choice([[1, 2], [0], []])}]
            if rng.random() < 0.15:
                o.append(dict(PASS))
            s["options"] = o
    if rng.random() < 0.1:
        for s in t["slots"]:
            s["options"] += [copy.deepcopy(o) for o in s["options"] if o["type"] == "switch"]
    if rng.random() < 0.1:
        t["slots"].reverse()
    return r, kind


def _adversary(rng):
    def act(raw):
        t = tmpl_of(raw)
        pool = [c for s in t["slots"] for c in _choices(s["options"])] + [dict(PASS), SW("pikachu"), None, "pass"]
        pick = {f"slot_{s['slot']}": copy.deepcopy(rng.choice(pool)) for s in t["slots"]}
        r = rng.random()
        if r < 0.4:
            return {"action": {"type": "doubles_turn", **pick}}
        if r < 0.5:
            return {"action": {"type": "doubles_turn", "slot_0": dict(PASS), "slot_1": dict(PASS)}}
        if r < 0.6:
            return {"action": pick}                                                    # untyped
        if r < 0.7:
            return {"action_id": "doubles_turn"}                                        # the template's own id
        if r < 0.8:
            raise RuntimeError("adversarial crash")
        return rng.choice([None, {}, {"action": "junk"}, {"action": {"type": "doubles_turn"}}, {"action_id": "x"}])
    return act


def test_every_mutated_template_and_answer_shape_reaches_the_runner_legal(monkeypatch):
    monkeypatch.setenv("ARENA_TC_WATCHDOG", "0")                                        # inline: fast and deterministic
    rng = random.Random(20260929)
    raw0 = recorded()
    c = Contestant()
    c.pk.act = _adversary(rng)
    fb = c.pk.fallback
    c.pk.fallback = lambda s: fb(s) if rng.random() < 0.6 else rng.choice(
        [{"action_id": "doubles_turn"}, {"action": {"type": "doubles_turn", "slot_0": PASS, "slot_1": PASS}}, {}])
    n = 0
    for _ in range(600):
        raw, kind = _mutate(raw0, rng)
        if not has_legal(tmpl_of(raw)):
            continue
        out = c.choose_action(GameState.from_mcp_state(copy.deepcopy(raw)), ctx())
        assert sent_legal(raw, out), (kind, tmpl_of(raw)["slots"], out)
        n += 1
    assert n > 500
