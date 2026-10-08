"""The deployed Contestant (agent/agent.py) and the Red Alert bot: routing by game type, the ARENA_RA switch,
reasoning tags, on_action_result, windows mode, sparse recording, the match summary, and whole matches through
the official runner (fake GameAPI and the local simulator)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from altruagent import WAIT, DecisionContext, GameState, WithReasoning
from altruagent.runner import _validate_decision, run_game
from agent import agent as agent_mod
from agent.agent import Contestant, _is_red_alert, create_agent, ra_enabled
from local.ra_bots import make_bot
from local.ra_platform import make_factory, play_match
from tests.test_realtime import CONFIG, RED_ALERT, RealtimeFakeGame, accepted, ra_payload, ra_state
from tests.test_runner import result_dict, terminal_state


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ARENA_RECORD", "0")
    monkeypatch.delenv("ARENA_RA", raising=False)
    monkeypatch.delenv("ARENA_REASONING", raising=False)


def _orders(out):
    act = out.action if isinstance(out, WithReasoning) else out
    assert isinstance(act, dict) and act.get("type") == "orders", out
    return act["orders"]


def test_the_switch():
    assert ra_enabled({}) and ra_enabled({"ARENA_RA": "on"}) and ra_enabled({"ARENA_RA": "1"})
    for off in ("off", "0", "false", "no", "wait", " OFF "):
        assert not ra_enabled({"ARENA_RA": off})


def test_red_alert_views_go_to_the_bot_with_a_phase_tag():
    c = create_agent()
    out = c.choose_action(ra_state(), RED_ALERT)
    assert isinstance(out, WithReasoning) and out.reasoning_summary in ("opening", "economy", "gathering")
    orders = _orders(out)
    assert orders and all(o["cmd"] in ("build", "train", "deploy", "move", "attack_move", "rally") for o in orders)
    _validate_decision(out, ra_state().legal_actions)              # what the runner accepts
    assert c._ra is not None and c.decisions == 1 and c.last_decision["path"] == "red-alert"


def test_reasoning_off_sends_the_bare_batch(monkeypatch):
    monkeypatch.setenv("ARENA_REASONING", "off")
    out = Contestant().choose_action(ra_state(), RED_ALERT)
    assert isinstance(out, dict) and out["type"] == "orders"


def test_arena_ra_off_restores_wait(monkeypatch):
    monkeypatch.setenv("ARENA_RA", "off")
    c = Contestant()
    assert c.choose_action(ra_state(), RED_ALERT) is WAIT
    assert c._ra is None and c.waits == 1


def test_which_states_count_as_red_alert():
    rt = ra_state()
    ctx = lambda g: DecisionContext(session_id="x", tournament_id=None, game_type=g, agent_id="a")  # noqa: E731
    for g in ("red_alert", "Red Alert", "red-alert", "redalert", "RED_ALERT"):
        assert _is_red_alert(rt, ctx(g))
    assert _is_red_alert(rt, ctx(None))                            # no game type: the state's shape says so
    assert not _is_red_alert(rt, ctx("honor_of_kings"))            # another real-time game keeps the WAIT path
    ww = GameState.from_mcp_state({"session_id": "w", "game_type": "werewolf", "phase": "night", "state_version": 1,
                                   "is_current_actor": True, "legal_actions": {"actions": []}})
    assert not _is_red_alert(ww, ctx("werewolf"))
    pk = GameState.from_mcp_state({"session_id": "p", "game_type": "pokemon_vgc_doubles_draft", "phase": "moving",
                                   "state_version": 1, "is_current_actor": True, "legal_actions": {"actions": []}})
    assert not _is_red_alert(pk, ctx("pokemon_vgc_doubles_draft"))
    assert not _is_red_alert(SimpleNamespace(raw=None), ctx(None))


def test_another_real_time_game_still_waits():
    other = DecisionContext(session_id="x", tournament_id=None, game_type="honor_of_kings", agent_id="a")
    assert Contestant().choose_action(ra_state(), other) is WAIT


def test_on_action_result_never_raises_and_does_nothing_for_other_games():
    c = Contestant()
    c.on_action_result({"accepted": True}, RED_ALERT)              # no Red Alert match yet: nothing
    c.choose_action(ra_state(), RED_ALERT)
    c._ra.on_result = None                                          # broken: not callable
    c.on_action_result({"error": "INVALID_ACTION", "detail": "[0] build: not_available (x)"}, RED_ALERT)
    c2 = Contestant()
    c2._game = "werewolf"
    c2.on_action_result({"accepted": True}, RED_ALERT)


def test_windows_mode_never_answers_wait():
    c = Contestant()
    raw = ra_payload(pacing={"mode": "windows", "window_seconds": 5.0})
    raw["observation"]["production"] = []
    for tick in (1, 2, 3):
        raw["state_version"] = tick
        out = c.choose_action(GameState.from_mcp_state(raw), RED_ALERT)
        assert out is not WAIT and _orders(out)
    # even a broken bot answers a valid batch there
    c._ra.decide = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    out = c.choose_action(GameState.from_mcp_state(raw), RED_ALERT)
    assert out is not WAIT and _orders(out)[0]["cmd"] in ("set_stance", "power_toggle")


def test_states_are_recorded_sparsely(monkeypatch, tmp_path):
    monkeypatch.setenv("ARENA_RECORD", "1")
    monkeypatch.setattr(agent_mod, "RECORD_DIR", str(tmp_path))
    c = Contestant()
    for i in range(60):                                             # 12 game seconds of views
        c.choose_action(ra_state(100 + 5 * i), RED_ALERT)
    rows = [json.loads(line) for line in (tmp_path / "ra.jsonl").read_text().splitlines()]
    assert 1 <= len(rows) <= 3                                      # the first view, then about one per 10 s
    assert rows[0]["game_type"] == "red_alert" and rows[0]["decision"]["path"] == "red-alert"


def test_the_match_summary_line_has_the_bot_metrics():
    c = Contestant()
    for i in range(5):
        c.choose_action(ra_state(100 + 5 * i), RED_ALERT)
    line = c.summary_line()
    assert line.startswith("[match-summary] session=ra game=red_alert decisions=5 ")
    assert "batches=" in line and "invalid_action=0" in line and "ms_p99=" in line


def test_a_red_alert_match_through_the_runner_sends_valid_batches():
    views = [ra_state(100 + 5 * i) for i in range(30)]
    game = RealtimeFakeGame(CONFIG).queue_state(*views, terminal_state())
    game.queue_play_action(*[accepted(("x", "pending", "pending")) for _ in range(30)])
    game.queue_result(result_dict())
    c = create_agent()
    final = run_game(game, RED_ALERT, c, sleep=lambda s: None)
    assert final.is_terminal
    assert game.play_action_calls, "the bot sent nothing"
    for call in game.play_action_calls:
        assert call["action"]["type"] == "orders" and 1 <= len(call["action"]["orders"]) <= 20
        assert call["reasoning_summary"] in (None, "opening", "economy", "gathering", "defending", "attacking")
    assert game.config_calls == ["red_alert"]
    assert c._ra.batches == len(game.play_action_calls)


# ---- the replay scripts take recorded Red Alert states with the bot on ---------------------------------------------
def _script(name: str):
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(f"{name}_under_test_ra", root / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _recorded(tmp_path):
    f = tmp_path / "logs" / "deploy" / "ra.jsonl"
    f.parent.mkdir(parents=True)
    rows = [{"t": 0, "kind": "action", "game_type": "red_alert", "raw": ra_payload(100 + 5 * i), "out": "WAIT"}
            for i in range(3)]
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return f


def test_replay_real_and_selfcheck_take_recorded_red_alert_states(monkeypatch, tmp_path, capsys):
    rr = _script("replay_real")
    counts = {"n": 0, "fails": 0, "with_reason": 0}
    rr._replay_file(str(_recorded(tmp_path)), Contestant(), False, [], [], counts)
    assert counts["n"] == 3 and counts["fails"] == 0 and counts["with_reason"] >= 1
    sc = _script("selfcheck")
    monkeypatch.setattr(sc, "ROOT", tmp_path)
    sc.replay(50, with_calc=False)
    assert sc.FAILS == []
    assert "replayed 3 recorded decisions (calculator disabled; {'playing': 3}): 0 rejected" in capsys.readouterr().out


# ---- whole matches on the simulator, through the official runner ---------------------------------------------------
def test_our_agent_beats_the_idle_bot_on_the_simulator():
    row = play_match(make_factory("ours")(1), make_bot("idle", 2), seed=31, map_id="trapped", max_minutes=8)
    a = row["a"]
    assert a["error"] is None and not a["hung"]
    assert row["result_a"] == "win"
    assert a["invalid_actions"] == 0 and a["rejected"] <= 0.02 * max(1, a["orders"])
    assert a["low_power_s"] <= 5 and a["place_delay_max_s"] <= 1.0 and a["p99_ms"] < 30


def test_our_agent_rides_out_a_flaky_connection_on_the_simulator():
    # SDK PR #7: temporary failures (HTTP 502 before / after a batch was applied, 429 rate_limited after) are
    # retried by the runner, which never resends; the bot keeps playing and never queues what may have landed twice.
    row = play_match(make_factory("ours")(1), make_bot("rusher", 2), seed=33, map_id="trapped", max_minutes=4,
                     fault_rate=0.05)
    a = row["a"]
    assert a["error"] is None and not a["hung"] and row["b"]["error"] is None
    assert sum(a["faults"].values()) >= 10 and a["faults"]["after_coded"] >= 1
    assert a["invalid_actions"] == 0 and a["rejected"] <= 0.05 * max(1, a["orders"])


def test_our_agent_survives_windows_mode_on_the_simulator():
    row = play_match(make_factory("ours")(1), make_bot("rusher", 2), seed=32, map_id="sidestep", max_minutes=3,
                     pacing="windows")
    a = row["a"]
    assert a["error"] is None and a["invalid_actions"] == 0 and a["batches"] >= 30   # one batch every 5 s window
