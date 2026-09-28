"""Time control and crash-proofing (agent/timecontrol.py, agent/fallback.py, DamageCalc restarts, the Contestant's
watchdog): per-decision budgets from the platform's timer, a slow / hung / killed / crashing calculator, exceptions in
policy code paths, unknown games / phases / payload shapes, and every bounded step degrading at deadline expiry."""
import json
import sys
import time

import pytest

from agent import timecontrol as tc
from agent.agent import Contestant, doubles_ok, lineup_ok
from agent.fallback import safe_action
from agent.pokemon import calc as calc_mod
from agent.pokemon import drafter as drafter_mod
from agent.pokemon import lookahead as L
from agent.pokemon import policy as P
from agent.pokemon import preview as preview_mod
from agent.pokemon.calc import DamageCalc
from agent.pokemon.policy import PokemonPolicy
from altruagent import TERMINATE_MESSAGING, DecisionContext, GameState, LegalAction, SendMessage
from tests.strategy import test_lookahead as T

GAME = "pokemon_vgc_doubles_draft"


@pytest.fixture(autouse=True)
def _no_public_reasoning(monkeypatch):
    """These tests check the move itself; with the public line on, moves come back wrapped in WithReasoning (see
    test_crash_paths_stay_legal_with_public_reasoning_on below and tests/strategy/test_reasoning.py)."""
    monkeypatch.setenv("ARENA_REASONING", "off")


def ctx(game=GAME):
    return DecisionContext(session_id="sid", tournament_id=None, game_type=game, agent_id="me")


def gs(raw):
    return GameState.from_mcp_state(raw)


# ================================================================ budgets from the platform's clocks
def timer_state(*lines, phase="moving"):
    return {"phase": phase, "game_type": GAME, "observation": {"protocol_log": ["|turn|3", *lines]}}


def test_timer_line_parsing_takes_the_newest_line():
    raw = timer_state("|inactive|Time left: 90 sec this turn | 420 sec total | 90 sec grace", "|move|p1a: X|Y",
                      "|inactive|Time left: 55 sec this turn | 385 sec total")
    assert tc.parse_timer(raw) == {"turn": 55, "total": 385, "grace": 0}
    assert tc.parse_timer(timer_state("|inactive|Battle timer is ON: inactive players will lose")) is None
    for bad in (None, {}, {"observation": "text"}, {"observation": {"protocol_log": "x"}},
                {"observation": {"protocol_log": [None, 3, {"a": 1}]}}):
        assert tc.parse_timer(bad) is None


@pytest.mark.parametrize("line,want", [
    (None, 10.0),                                                                   # no timer visible: the default
    ("|inactive|Time left: 90 sec this turn | 420 sec total | 90 sec grace", 10.0),  # first turn: the cap
    ("|inactive|Time left: 55 sec this turn | 420 sec total", 10.0),
    ("|inactive|Time left: 55 sec this turn | 200 sec total", 8.5),                  # (200 - 30) / 20
    ("|inactive|Time left: 55 sec this turn | 100 sec total", 3.5),
    ("|inactive|Time left: 55 sec this turn | 40 sec total", 0.5),                   # floor
    ("|inactive|Time left: 5 sec this turn | 420 sec total", 1.0),                   # (5 - 3) x 0.5
    ("|inactive|Time left: 300 sec this turn | 300 sec total | 5 sec grace", 10.0),  # gen9 singles
])
def test_battle_budget_follows_the_turn_clock_and_the_bank(line, want):
    raw = timer_state(*([line] if line else []))
    d = tc.budget_for(raw, GAME)
    assert d.kind == "battle" and d.seconds == pytest.approx(want)
    assert d.source == ("timer" if line else "default")
    assert d.hard - d.end == pytest.approx(min(tc.HARD_GRACE_S, 0.25 * want))


def test_budgets_per_decision_type_and_overrides(monkeypatch):
    assert tc.budget_for({"phase": "draft"}, GAME).seconds == tc.DRAFT_S == 20.0
    assert tc.budget_for({"phase": "team_preview"}, GAME).seconds == tc.PREVIEW_S == 30.0
    assert tc.budget_for({"phase": "teambuild"}, "pokemon_gen9ou_teambuild").seconds == 20.0
    assert tc.budget_for({"phase": "moving"}, "werewolf").seconds == tc.OTHER_S
    assert tc.budget_for({"phase": "messaging"}, "werewolf").kind == "message"
    odd = tc.budget_for({"phase": "mystery"}, GAME)
    assert odd.kind == "pokemon:mystery" and odd.seconds == tc.BATTLE_S
    for junk in (None, "x", 3, {"phase": object()}):
        assert tc.budget_for(junk, GAME).seconds > 0                                   # never raises
    monkeypatch.setenv("ARENA_TC_BATTLE_S", "2.5")
    monkeypatch.setenv("ARENA_TC_DRAFT_S", "nonsense")
    assert tc.budget_for({"phase": "moving"}, GAME).seconds == 2.5
    assert tc.budget_for({"phase": "draft"}, GAME).seconds == 20.0


def test_deadline_cap_and_context():
    assert tc.current() is None and tc.cap(2.5) == 2.5                               # no decision: unbounded
    with tc.decision(tc.Deadline(10.0)) as d:
        assert tc.current() is d and tc.cap(2.5) == 2.5                              # far deadline: unchanged
        assert tc.cap(20.0) == pytest.approx(9.0, abs=0.05)                          # 10 s - 1 s reserve
        assert tc.cap(20.0, 0.5) == pytest.approx(4.0, abs=0.05)
    with tc.decision(tc.Deadline(0.0)) as d:
        assert d.expired() and tc.cap(2.5) == 0.0 and tc.left() == 0.0
    assert tc.current() is None


# ================================================================ DamageCalc: slow / hung / killed / crashing node
FAKE_NODE = r'''
import json, os, sys, time
opts = dict(a.split("=", 1) for a in sys.argv[1:])
time.sleep(float(opts.get("start", 0)))
n = 0
for line in sys.stdin:
    msg = json.loads(line)
    n += 1
    if "die_after" in opts and n > int(opts["die_after"]):
        sys.exit(3)
    flag = opts.get("hang_once")
    if flag and n > 1 and not os.path.exists(flag):
        open(flag, "w").close()
        time.sleep(600)
    if "garbage_after" in opts and n > int(opts["garbage_after"]):
        print("this is not json", flush=True)
        continue
    time.sleep(float(opts.get("each", 0)))
    print(json.dumps({"id": msg["id"], "results": [
        {"rolls": [len(c["move"])], "defender_hp": 100, "move": c["move"]} for c in msg["calcs"]]}), flush=True)
'''


@pytest.fixture
def fake_node(tmp_path, monkeypatch):
    """make(**opts) -> a DamageCalc whose 'node' is the scripted fake above (real subprocess, real pipes)."""
    script = tmp_path / "fake_node.py"
    script.write_text(FAKE_NODE)
    made = []
    monkeypatch.setattr(calc_mod, "_js_paths", lambda: (tmp_path, tmp_path))

    def make(**opts):
        args = [f"{k}={v}" for k, v in opts.items()]
        dc = DamageCalc()
        dc.enabled = True
        dc._command = lambda _script: [sys.executable, str(script), *args]
        made.append(dc)
        return dc
    yield make
    for dc in made:
        dc._kill()


def calc(move):
    return {"attacker": {"species": "Garchomp"}, "defender": {"species": "Archaludon"}, "move": move}


def test_killed_calc_is_restarted_not_disabled(fake_node):
    dc = fake_node()
    assert dc.warm() and dc.starts == 1 and dc.startup_s is not None
    assert dc.batch([calc("Earthquake")])[0]["move"] == "Earthquake"
    dc.proc.kill()
    dc.proc.wait()
    out = dc.batch([calc("Rock Slide")])                   # the old code disabled the calculator for the match here
    assert out[0]["move"] == "Rock Slide"
    assert dc.enabled and dc.starts == 2 and dc.errors == 1 and dc.failures == 0


def test_hung_calc_never_blocks_past_the_deadline_and_is_replaced(fake_node, tmp_path, monkeypatch):
    monkeypatch.setattr(calc_mod, "CALC_BASE_S", 0.25)       # allowance 0.25 s whatever the batch size ...
    monkeypatch.setattr(calc_mod, "CALC_PER_CALC_S", 0.0)
    monkeypatch.setattr(calc_mod, "STALL_FACTOR", 4.0)       # ... hung after 1 s
    dc = fake_node(hang_once=tmp_path / "hung")
    assert dc.warm()
    with tc.decision(tc.Deadline(5.0)):
        t = time.monotonic()
        assert dc.batch([calc("Earthquake")]) is None       # node never answers: back after its allowance
        assert 0.2 < time.monotonic() - t < 0.4
    assert dc.timeouts == 1 and dc.starts == 1 and dc._pending is not None and dc.enabled
    with tc.decision(tc.Deadline(0.2)):
        assert dc.batch([calc("Earthquake")]) is None       # still owed, not yet stalled: keep the process
    assert dc.starts == 1 and dc.errors == 0
    time.sleep(0.5)
    assert dc.batch([calc("Earthquake")]) is None           # stalled: killed and restarted at once
    assert dc.errors == 1 and dc.starts == 2
    out = dc.batch([calc("Iron Head")])                     # the new process answers
    assert out[0]["move"] == "Iron Head" and dc.enabled


def test_round_trips_are_bounded_by_their_allowance_and_the_deadline(fake_node):
    assert DamageCalc.allowance(50) == pytest.approx(calc_mod.CALC_BASE_S + 50 * calc_mod.CALC_PER_CALC_S)
    assert DamageCalc.allowance(100000) == calc_mod.CALC_TIMEOUT_S
    dc = fake_node(each=1.0)                                 # a healthy but slow node: 1 s per answer
    assert dc.warm()
    with tc.decision(tc.Deadline(0.5)):
        t = time.monotonic()
        assert dc.batch([calc("Earthquake")]) is None       # the deadline binds before the 2 s allowance
        assert time.monotonic() - t < 0.5
    assert dc.timeouts == 1 and dc.errors == 0
    out = dc.batch([calc("Rock Slide")])                     # collects the owed answer, then its own
    assert out[0]["move"] == "Rock Slide" and dc.starts == 1 and dc.errors == 0


def test_slow_start_is_not_killed_by_a_short_deadline(fake_node):
    dc = fake_node(start=1.0)
    with tc.decision(tc.Deadline(0.3)):
        t = time.monotonic()
        assert dc.warm() is False                            # still starting when the decision's time is up
        assert time.monotonic() - t < 0.35
    assert dc.starts == 1 and dc.errors == 0 and dc._pending is not None
    out = dc.batch([calc("Earthquake")])                     # no deadline: waits for the start-up, then answers
    assert out[0]["move"] == "Earthquake" and dc.starts == 1 and dc.ready and dc.startup_s >= 0.9


def test_malformed_answer_restarts_the_process(fake_node):
    dc = fake_node(garbage_after=1)
    assert dc.warm()
    assert dc.batch([calc("Earthquake")]) is None
    assert dc.errors == 1 and dc.enabled and dc.starts == 2            # restarted immediately (first failure)
    assert "malformed" in dc.last_error


def test_only_a_real_answer_ends_a_failure_run(fake_node):
    dc = fake_node()
    dc.failures = 2                                        # (as after two failed processes)
    assert dc.warm() and dc.ready and dc.failures == 2     # a new process answering its warm-up proves nothing yet
    assert dc.batch([calc("Earthquake")])[0]["move"] == "Earthquake"
    assert dc.failures == 0                                # an answer to a real request ends the run


def test_crashing_calc_backs_off_and_is_capped(fake_node, monkeypatch):
    """Every process loads fine (answers its warm-up) and then dies on its first real request: the restarts must back
    off (0 / 0.3 s here) instead of burning all MAX_STARTS processes within a few calls (review: the warm-up answer
    used to reset the failure run, so every restart was immediate)."""
    monkeypatch.setattr(calc_mod, "RESTART_BACKOFF_S", (0.0, 0.3, 0.6))
    monkeypatch.setattr(calc_mod, "MAX_STARTS", 3)
    dc = fake_node(die_after=1)
    t0 = time.monotonic()
    tries = []              # per call: (start, end) since t0, answer, processes started, failures, backoff left
    while dc.enabled and time.monotonic() - t0 < 10:
        before, start = dc.starts, time.monotonic()
        r = dc.batch([calc("Earthquake")])
        now = time.monotonic()
        tries.append((start - t0, now - t0, r, dc.starts - before, dc.failures, dc.next_start - now))
        time.sleep(0.05)
    assert all(x[2] is None for x in tries)
    assert not dc.enabled and dc.starts == 3 and dc.errors == 3 and dc.proc is None
    first, second, *waiting, last = tries
    assert first[3] == 2 and first[4] == 1 and first[5] <= 0      # 1st failure: restarted at once
    assert second[3] == 0 and second[4] == 2 and second[5] > 0.2  # 2nd in a row: the next start moves 0.3 s ahead
    assert len(waiting) >= 2 and all(x[3] == 0 and x[4] == 2 and x[5] > 0 for x in waiting)   # nothing started
    assert last[3] == 1 and last[4] == 3                          # the 3rd process, only after the backoff ...
    assert last[0] - second[1] >= 0.25                            # ... fails too: the cap switches the calc off
    assert time.monotonic() - t0 < 5
    n = dc.starts
    assert dc.batch([calc("Earthquake")]) is None and dc.starts == n     # off for good: no more processes
    assert dc.warm() is False


def test_expired_deadline_sends_nothing(fake_node):
    dc = fake_node()
    assert dc.warm()
    sent = dc._id
    with tc.decision(tc.Deadline(0.0)):
        assert dc.batch([calc("Earthquake")]) is None
    assert dc._id == sent and dc.errors == 0 and dc.timeouts == 0
    assert dc.batch([calc("Earthquake")])[0]["move"] == "Earthquake"


def test_broken_calc_mid_battle_plays_the_heuristic_then_recovers(fake_node, monkeypatch):
    """Policy level: a calculator that dies mid-battle costs heuristic decisions, not the match."""
    monkeypatch.setattr(calc_mod, "RESTART_BACKOFF_S", (0.0,))
    dc = fake_node()
    good = dc._command
    monkeypatch.setattr(DamageCalc, "_inst", dc)
    def st(hp):                          # a new board each turn (the same board would be answered from the cache)
        return T.state([T.view(T.CHIYU), T.view(T.INC)], [T.view(T.RILL, hp, foe=True), T.view(T.AMOO, foe=True)])
    pol = T.policy()
    assert doubles_ok(st(1.0), pol.act(st(1.0))["action"]) and pol.last_exact
    dc.proc.kill()
    dc.proc.wait()
    dc._command = lambda _s: [sys.executable, "-c", "import sys; sys.exit(1)"]   # the restart dies at once too
    assert doubles_ok(st(0.8), pol.act(st(0.8))["action"]) and not pol.last_exact  # heuristic this turn
    assert dc.enabled and dc.errors == 2 and dc.starts == 3          # (the 3rd process was started right away)
    dc._command = good                                                           # node works again
    time.sleep(0.5)                                                              # (the 3rd process has died)
    assert doubles_ok(st(0.6), pol.act(st(0.6))["action"]) and pol.last_exact
    assert dc.enabled and dc.errors == 3 and dc.starts == 4 and dc.starts <= calc_mod.MAX_STARTS


# ================================================================ the watchdog and the fallbacks
def draft_state(n=4):
    cards = [{"card_id": f"c{k}", "species": sp, "item": f"item{k}", "ability": "x", "moves": ["Tackle"],
              "evs": {}, "ivs": {}, "nature": "Hardy", "level": 50}
             for k, sp in enumerate(["Incineroar", "Rillaboom", "Garchomp", "Amoonguss"][:n])]
    return {"phase": "draft", "game_type": GAME, "session_id": "sid", "current_actor": {"agent_id": "me"},
            "observation": {"phase": "draft", "current_seat": "me", "available_cards": cards,
                            "rosters": {"me": [], "o": []}, "pick_number": 1},
            "legal_actions": {"actions": [{"action_id": f"draft_pick:{c['card_id']}",
                                           "input": {"action": {"type": "draft_pick", "card_id": c["card_id"]}}}
                                          for c in cards]}}


def preview_state():
    mine = [T.view(c) for c in (T.CHIYU, T.INC, T.GAR, T.RILL, T.AMOO)]
    roster = [m["species"] for m in mine]
    return {"phase": "team_preview", "game_type": GAME, "session_id": "sid",
            "observation": {"battle_format": "gen9vgc2025regi", "your_roster": mine,
                            "opponent_roster": [{"species": s} for s in ("rillaboom", "amoonguss", "garchomp", "incineroar")]},
            "legal_actions": {"actions": [{"action_id": "select_lineup", "input": {"action": {
                "type": "select_lineup", "roster": roster, "bring_count": 4, "lead_count": 2,
                "instructions": "bring 4, lead 2"}}}]}}


def battle_state():
    return T.state([T.view(T.CHIYU), T.view(T.INC)], [T.view(T.RILL, foe=True), T.view(T.AMOO, foe=True)])


def singles_state():
    mon = T.view(T.CHIYU)
    return {"phase": "moving", "game_type": "pokemon_gen9same", "session_id": "sid",
            "observation": {"is_doubles": False, "active_pokemon": mon, "opponent_active_pokemon": T.view(T.RILL, foe=True),
                            "turn": 2},
            "legal_actions": {"actions": [
                {"action_id": "move:0", "input": {"action": {"type": "move", "slot": 0, "move_id": "heatwave"}}},
                {"action_id": "move:1", "input": {"action": {"type": "move", "slot": 1, "move_id": "darkpulse"}}},
                {"action_id": "switch:0", "input": {"action": {"type": "switch", "slot": 0, "species": "incineroar"}}}]}}


def legal(raw, out) -> bool:
    """Would the platform take `out` for this state?"""
    phase = raw["phase"]
    ids = [a["action_id"] for a in raw["legal_actions"]["actions"]]
    if isinstance(out, LegalAction):
        return out.action_id in ids and not (out.input.get("action") or {}).get("slots") \
            and "roster" not in (out.input.get("action") or {})
    if not isinstance(out, dict):
        return False
    if phase == "team_preview":
        return lineup_ok(raw, out)
    if out.get("type") == "doubles_turn":
        return doubles_ok(raw, out)
    if out.get("type") == "draft_pick":
        return f"draft_pick:{out.get('card_id')}" in ids
    return False


STATES = {"draft": draft_state, "preview": preview_state, "battle": battle_state, "singles": singles_state}


@pytest.mark.parametrize("name", list(STATES))
def test_policy_crash_in_every_phase_answers_legally(name, monkeypatch):
    raw = STATES[name]()
    c = Contestant()
    monkeypatch.setattr(c.pk, "act", lambda st: 1 / 0)
    out = c.choose_action(gs(raw), ctx(raw["game_type"]))
    assert legal(raw, out) and c.last_decision["path"] == "crash" and c.fallbacks == 1
    monkeypatch.setattr(c.pk, "fallback", lambda st: {}["boom"])        # the policy's own fallback crashes too
    out = c.choose_action(gs(raw), ctx(raw["game_type"]))
    assert legal(raw, out)


@pytest.mark.parametrize("name", list(STATES))
def test_crash_paths_stay_legal_with_public_reasoning_on(name, monkeypatch):
    from agent.reasoning import unwrap
    monkeypatch.setenv("ARENA_REASONING", "safe")
    raw = STATES[name]()
    c = Contestant()
    monkeypatch.setattr(c.pk, "act", lambda st: 1 / 0)
    out = c.choose_action(gs(raw), ctx(raw["game_type"]))
    assert legal(raw, unwrap(out)) and c.last_decision["path"] == "crash"
    monkeypatch.setattr("agent.reasoning.attach", lambda *a, **k: 1 / 0)   # the public line itself crashes
    out = c.choose_action(gs(raw), ctx(raw["game_type"]))
    assert legal(raw, out) and c.last_reasoning is None


@pytest.mark.parametrize("method", ["_exact_table", "_exact_context", "_doubles_heuristic", "_speed_model",
                                    "_track_conditions"])
def test_exceptions_inside_battle_code_paths_answer_legally(method, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError(f"bug in {method}")
    monkeypatch.setattr(PokemonPolicy, method, boom)
    raw = battle_state()
    c = Contestant()
    out = c.choose_action(gs(raw), ctx())
    assert legal(raw, out)


@pytest.mark.parametrize("method", ["_drafter_pick", "_track_draft"])
def test_exceptions_inside_draft_code_paths_answer_legally(method, monkeypatch):
    monkeypatch.setattr(PokemonPolicy, method, lambda *a, **k: 1 / 0)
    raw = draft_state()
    assert legal(raw, Contestant().choose_action(gs(raw), ctx()))


def test_watchdog_answers_in_time_when_the_policy_hangs(monkeypatch):
    monkeypatch.setenv("ARENA_TC_BATTLE_S", "0.3")
    monkeypatch.setenv("ARENA_TC_MIN_S", "0.3")                  # (below the 0.5 s floor, to keep the test short)
    c = Contestant()
    orig = c.pk.act
    calls = []

    def slow(st):
        calls.append(1)
        if len(calls) == 1:
            time.sleep(1.5)                                  # a pathological decision
        return orig(st)
    monkeypatch.setattr(c.pk, "act", slow)
    raw = battle_state()
    t = time.monotonic()
    out = c.choose_action(gs(raw), ctx())
    took = time.monotonic() - t
    assert legal(raw, out) and took < 0.3 + 0.075 + 0.2      # budget + hard grace (25%) + slack
    assert c.overruns == 1 and c.last_decision["path"] == "overrun"
    t = time.monotonic()
    out = c.choose_action(gs(raw), ctx())                    # the overrun call is still running: no second one
    assert legal(raw, out) and c.last_decision["path"] == "busy" and len(calls) == 1
    assert time.monotonic() - t < 0.5
    time.sleep(1.5)
    out = c.choose_action(gs(raw), ctx())                    # finished: the policy plays again
    assert legal(raw, out) and c.last_decision["path"] == "policy" and len(calls) == 2


def test_watchdog_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("ARENA_TC_WATCHDOG", "0")
    c = Contestant()
    assert not c.watchdog
    monkeypatch.setattr(c.pk, "act", lambda st: 1 / 0)
    raw = battle_state()
    assert legal(raw, c.choose_action(gs(raw), ctx()))


def test_decisions_carry_their_budget(monkeypatch):
    c = Contestant()
    seen = []
    monkeypatch.setattr(c.pk, "act", lambda st: seen.append(tc.current()) or c.pk.fallback(st))
    for raw, kind, secs in ((draft_state(), "draft", 20.0), (preview_state(), "preview", 30.0),
                            (battle_state(), "battle", 10.0)):
        c.choose_action(gs(raw), ctx())
        assert seen[-1].kind == kind and seen[-1].seconds == secs
        assert c.last_decision["kind"] == kind and c.last_decision["budget"] == secs
    assert tc.current() is None                              # nothing leaks out of a decision


# ================================================================ unknown games, phases, payload shapes
def discrete_state(ids, game="tic_tac_toe", labels=None):
    return {"phase": "moving", "game_type": game, "session_id": "sid", "observation": {},
            "legal_actions": {"actions": [{"action_id": i, "label": (labels or {}).get(i)} for i in ids]}}


def test_unknown_discrete_game_plays_a_deterministic_legal_action():
    raw = discrete_state(["resign", "4", "0"], labels={"4": "Resign? no: cell 4"})
    c = Contestant()
    outs = {c.choose_action(gs(raw), ctx("tic_tac_toe")).action_id for _ in range(3)}
    assert outs == {"0"} and c.last_decision["path"] == "unknown-game"     # never gives up, same answer every time
    raw = discrete_state(["2", "5"], game="avalon")
    assert c.choose_action(gs(raw), ctx("avalon")).action_id == "2"


def test_unknown_game_with_structured_templates_fills_them_in():
    slots = [{"slot": 0, "options": [{"type": "switch", "species": "a"}, {"type": "move", "move_id": "x", "targets": [1]}]},
             {"slot": 1, "options": [{"type": "switch", "species": "a"}, {"type": "switch", "species": "b"}]}]
    raw = {"phase": "moving", "game_type": "pokemon_newformat", "observation": {},
           "legal_actions": {"actions": [{"action_id": "doubles_turn", "input": {"action": {"type": "doubles_turn",
                                                                                            "slots": slots}}}]}}
    out = safe_action(gs(raw))
    assert out == {"type": "doubles_turn", "slot_0": {"type": "switch", "species": "a"},
                   "slot_1": {"type": "switch", "species": "b"}}
    raw = {"phase": "team_preview", "game_type": "mystery", "observation": {},
           "legal_actions": {"actions": [{"action_id": "select_lineup", "input": {"action": {
               "type": "select_lineup", "roster": ["a", "b", "c", "d", "e", "f"], "bring_count": 3, "lead_count": 1}}}]}}
    assert Contestant().choose_action(gs(raw), ctx("mystery")) == {"type": "select_lineup", "bring": ["a", "b", "c"],
                                                                     "leads": ["a"]}
    raw = {"phase": "teambuild", "game_type": "x", "legal_actions": {"actions": [
        {"action_id": "submit_team", "input": {"action": {"type": "submit_team", "instructions": "6 sets"}}}]}}
    out = safe_action(gs(raw))
    assert out["type"] == "submit_team" and len(out["team"]) == 6


def test_unknown_pokemon_phase_answers_legally_and_battle_shaped_ones_are_played(monkeypatch):
    raw = battle_state()
    raw["phase"] = "battle_turn"                           # a renamed battle phase: still played by the policy
    c = Contestant()
    out = c.choose_action(gs(raw), ctx())
    assert legal(raw, out) and c.last_decision["path"] == "policy"
    raw = preview_state()
    raw["phase"] = "lineup_select"                         # unknown and not a battle: the safe fallback
    out = c.choose_action(gs(raw), ctx())
    assert lineup_ok(raw, out)
    raw = discrete_state(["wait:0", "wait:1"], game=GAME)
    raw["phase"] = "waiting_room"
    out = c.choose_action(gs(raw), ctx())
    assert isinstance(out, (LegalAction, dict))


def test_legal_actions_missing_from_the_raw_state_are_filled_in():
    """The runner fetches legal actions separately when the server omits them: state.legal_actions has them,
    state.raw["legal_actions"] stays None. The policy must still play (it read the raw payload)."""
    raw = battle_state()
    state = gs(raw)
    state.raw = dict(raw, legal_actions=None)
    c = Contestant()
    out = c.choose_action(state, ctx())
    assert legal(raw, out) and c.last_decision["path"] == "policy"
    assert state.raw["legal_actions"] is None               # the recorded payload is not mutated


@pytest.mark.parametrize("mutate", [
    lambda r: r.__setitem__("observation", "a string"),
    lambda r: r.__setitem__("observation", None),
    lambda r: r["legal_actions"]["actions"][0].__setitem__("input", None),
    lambda r: r["legal_actions"]["actions"][0]["input"]["action"]["slots"][0].__setitem__("options", None),
    lambda r: r["legal_actions"]["actions"][0]["input"]["action"]["slots"].append("junk"),
    lambda r: r["observation"].__setitem__("active_pokemon", "x"),
    lambda r: r["observation"].__setitem__("turn", "three"),
])
def test_unexpected_payload_shapes_never_raise(mutate):
    raw = battle_state()
    mutate(raw)
    out = Contestant().choose_action(gs(raw), ctx())
    assert isinstance(out, (dict, LegalAction))


def test_no_legal_actions_at_all_never_raises():
    raw = {"phase": "moving", "game_type": GAME, "observation": {}, "legal_actions": {"actions": []}}
    assert Contestant().choose_action(gs(raw), ctx()) == {"type": "pass"}
    assert Contestant().choose_action(gs({}), DecisionContext("s", None, None, "me")) == {"type": "pass"}


def test_werewolf_messages_survive_odd_payloads(monkeypatch):
    c = Contestant()
    raw = {"phase": "messaging", "game_type": "werewolf", "game_state": "not a dict",
           "new_messages": ["junk", 3, {"seq": 1, "content": "hi"}]}
    state = gs({"phase": "messaging", "game_type": "werewolf"})
    state.raw = raw                                   # (the SDK's own parser would already choke on these messages)
    out = c.choose_message(state, ctx("werewolf"))
    assert out is TERMINATE_MESSAGING or isinstance(out, SendMessage)
    monkeypatch.setattr(c.ww, "observe", lambda st: 1 / 0)
    assert c.choose_message(state, ctx("werewolf")) is TERMINATE_MESSAGING
    c = Contestant()
    c.ww.chat = lambda st: ["not a dict", {"content": "ok", "recipients": ["x"]}]
    out = c.choose_message(gs({"phase": "messaging", "game_state": {"day": 1}}), ctx("werewolf"))
    assert isinstance(out, SendMessage) and out.content == "ok" and not out.recipients


def test_invalid_policy_answers_are_replaced(monkeypatch):
    c = Contestant()
    raw = preview_state()
    monkeypatch.setattr(c.pk, "act", lambda st: {"action": {"type": "select_lineup", "bring": ["chiyu"] * 4,
                                                            "leads": ["chiyu", "chiyu"]}})
    assert lineup_ok(raw, c.choose_action(gs(raw), ctx()))
    raw = draft_state()
    monkeypatch.setattr(c.pk, "act", lambda st: {"action": {"type": "draft_pick", "card_id": "not-offered"}})
    assert legal(raw, c.choose_action(gs(raw), ctx()))
    monkeypatch.setattr(c.pk, "act", lambda st: {"action": "a string"})
    assert legal(raw, c.choose_action(gs(raw), ctx()))


# ================================================================ bounded steps degrade at deadline expiry
def test_expired_deadline_still_answers_a_battle_turn():
    raw = battle_state()
    pol = T.policy()
    with tc.decision(tc.Deadline(0.0)):
        t = time.monotonic()
        out = pol.act(raw)["action"]
    assert doubles_ok(raw, out) and pol.last_lookahead is None and time.monotonic() - t < 1.0


@T.needs_calc
def test_no_time_for_the_lookahead_keeps_the_exact_pick(monkeypatch):
    raw = battle_state()
    exact = T.policy()
    monkeypatch.setattr(P, "joint_lookahead", lambda *a, **k: None)
    want = exact.act(raw)["action"]
    monkeypatch.undo()
    monkeypatch.setattr(P, "MIN_LOOKAHEAD_S", 100.0)          # the lookahead's cap is below its minimum
    pol = T.policy()
    with tc.decision(tc.Deadline(10.0)):
        out = pol.act(raw)["action"]
    assert out == want and pol.last_exact and pol.last_lookahead is None and pol.lookahead_skipped == 1
    got = {}
    monkeypatch.setattr(P, "MIN_LOOKAHEAD_S", 0.05)
    monkeypatch.setattr(P, "joint_lookahead", lambda *a, **k: got.update(k) or None)
    with tc.decision(tc.Deadline(1.0)):                       # a tight deadline: the lookahead gets a smaller cap
        T.policy().act(raw)
    assert 0 < got["time_cap"] < 0.95
    got.clear()
    with tc.decision(tc.Deadline(10.0)):                      # a far one: TIME_CAP as always
        T.policy().act(raw)
    assert "time_cap" not in got


def test_lookahead_time_cap_parameter_binds():
    s = L._Search.__new__(L._Search)
    s.time_cap = 0.3
    assert s._cap() == 0.3
    s.time_cap = None
    assert s._cap() == L.TIME_CAP
    s.time_cap = 99.0
    assert s._cap() == L.TIME_CAP


def test_expired_deadline_keeps_the_card_score_pick(monkeypatch):
    monkeypatch.setattr(drafter_mod.Drafter, "pick", lambda *a, **k: pytest.fail("drafter ran with no time left"))
    pol = PokemonPolicy()
    with tc.decision(tc.Deadline(0.0)):
        out = pol.act(draft_state())
    assert out["action"]["type"] == "draft_pick" and "draft:card_score" in pol.degraded


def test_drafter_search_wall_follows_the_deadline(monkeypatch):
    from agent.pokemon import dex
    pool = list(dex.card_catalog().values())[:18]
    monkeypatch.setattr(drafter_mod, "threat_table", lambda: type("TT", (), {"ensure": lambda s, c: None,
                                                                             "beats": lambda s, a, b: 0.5})())
    d = drafter_mod.Drafter(prior_score=lambda c: 0.0, use_matchups=False)
    legal_ids = [c["card_id"] for c in pool]
    with tc.decision(tc.Deadline(0.0)):
        cid = d.pick(pool, [], [], legal_ids, n_taken=0)
    assert cid in legal_ids and d.info["depth"] == 1 and d.info["budget_hit"]   # greedy roll-outs only
    d2 = drafter_mod.Drafter(prior_score=lambda c: 0.0, use_matchups=False)
    assert d2.pick(pool, [], [], legal_ids, n_taken=0) in legal_ids and d2.info["depth"] > 1


def test_expired_deadline_uses_the_heuristic_preview(monkeypatch):
    monkeypatch.setattr(preview_mod, "choose", lambda *a, **k: pytest.fail("matrix game ran with no time left"))
    raw = preview_state()
    pol = T.policy()
    with tc.decision(tc.Deadline(0.1)):
        out = pol.act(raw)["action"]
    assert lineup_ok(raw, out) and "preview:heuristic" in pol.degraded


def test_preview_wall_cap_is_bounded_by_the_deadline(monkeypatch):
    got = {}

    def spy(*a, **k):
        got.update(k.get("knobs") or {})
        return None
    monkeypatch.setattr(preview_mod, "choose", spy)
    monkeypatch.setattr(DamageCalc, "warm", lambda self: True)        # no node start-up inside the 3 s
    pol = T.policy()
    with tc.decision(tc.Deadline(3.0)):
        pol.act(preview_state())
    assert 1.5 < got["wall_cap_s"] < 2.2                      # 0.8 x 3 s - 0.3 s reserve, not the 6 s default
    got.clear()
    with tc.decision(tc.Deadline(30.0)):
        pol.act(preview_state())
    assert got == {}                                          # a far deadline changes nothing
