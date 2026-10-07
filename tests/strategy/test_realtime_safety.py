"""A real-time game we have no policy for (Red Alert, `pacing.mode == "realtime"`) assigned to our deployed agent.

Upstream's real-time runner (altruagent/runner.py, PR #3) lets choose_action answer altruagent.WAIT and no longer stops on
a refused batch in real time. Before this, our Contestant answered a Red Alert view with the generic safe action, which
is no order batch at all ({"type": "pass"}: the state lists no legal actions), at every view (~5 a second), recording
every state. Now it answers WAIT for the whole match: nothing is sent, the runner waits for the next view, the match
ends normally, one log line, one recorded state. The fixtures are upstream's (tests/test_realtime.py)."""
from __future__ import annotations

import importlib.util
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from altruagent import WAIT, DecisionContext, GameState
from altruagent.runner import run_game
from agent import agent as agent_mod
from agent.agent import Contestant, create_agent
from tests.test_realtime import CONFIG, RED_ALERT, RealtimeFakeGame, ra_payload, ra_state
from tests.test_runner import result_dict, terminal_state

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _no_recording(monkeypatch):
    monkeypatch.setenv("ARENA_RECORD", "0")             # (the deployed agent's default is on: logs/<session>.jsonl)


def _match(views: int) -> RealtimeFakeGame:
    game = RealtimeFakeGame(CONFIG).queue_state(*[ra_state(100 + 5 * i) for i in range(views)], terminal_state())
    game.queue_result(result_dict())
    return game


def test_a_whole_red_alert_match_through_the_runner_sends_nothing_and_ends(caplog):
    game = _match(40)
    c = create_agent()
    with caplog.at_level(logging.INFO, logger="agent"):
        final = run_game(game, RED_ALERT, c, sleep=lambda s: None)
    assert final.is_terminal
    assert game.play_action_calls == []                  # no guessed batch, ever
    assert len(game.wait_calls) == 40                    # every view: WAIT, then the runner waits for the next one
    assert [w["since_version"] for w in game.wait_calls] == [100 + 5 * i for i in range(40)]
    assert game.config_calls == ["red_alert"]            # (the runner's own one fetch)
    assert c.waits == 40 and c.decisions == 0 and c.fallbacks == 0
    errors = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(errors) == 1 and "real-time game 'red_alert'" in errors[0].getMessage()


def test_the_policies_and_the_safe_action_are_never_asked(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a turn-based path ran on a real-time state")

    monkeypatch.setattr(agent_mod, "safe_action", boom)
    monkeypatch.setattr(agent_mod.timecontrol, "budget_for", boom)
    monkeypatch.setattr(agent_mod.reasoning, "attach", boom)
    c = Contestant()
    monkeypatch.setattr(c.pk, "act", boom)
    monkeypatch.setattr(c.ww, "act", boom)
    for tick in (100, 105, 110):
        assert c.choose_action(ra_state(tick), RED_ALERT) is WAIT


def test_the_first_view_is_recorded_once(monkeypatch, tmp_path):
    monkeypatch.setenv("ARENA_RECORD", "1")
    monkeypatch.setattr(agent_mod, "RECORD_DIR", str(tmp_path))
    c = Contestant()
    for tick in (100, 105, 110, 115):
        assert c.choose_action(ra_state(tick), RED_ALERT) is WAIT
    rows = [json.loads(line) for line in (tmp_path / "ra.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["out"] == "WAIT" and rows[0]["game_type"] == "red_alert"
    assert rows[0]["raw"]["pacing"]["mode"] == "realtime" and rows[0]["decision"]["path"] == "realtime-wait"


def test_the_match_summary_counts_the_waits():
    c = Contestant()
    for tick in (100, 105, 110):
        c.choose_action(ra_state(tick), RED_ALERT)
    line = c.summary_line()
    assert line.startswith("[match-summary] session=ra game=red_alert decisions=0 ")
    assert line.endswith(" resumed=no waits=3")


def test_a_red_alert_view_without_a_game_type_waits_too():
    ctx = DecisionContext(session_id="ra", tournament_id=None, game_type=None, agent_id="a")
    assert Contestant().choose_action(ra_state(), ctx) is WAIT


def test_turn_based_states_never_take_the_real_time_path():
    """Our games' states carry no `pacing`: the real-time check is False and nothing about them changes."""
    ww = GameState.from_mcp_state({"session_id": "w", "game_type": "werewolf", "phase": "night", "is_current_actor": True,
                                   "state_version": 3, "your_position": 2, "game_state": {"day": 1},
                                   "legal_actions": {"actions": [{"action_id": "4", "label": "4", "input": {}}]}})
    assert not agent_mod._realtime(ww)
    assert not agent_mod._realtime(SimpleNamespace(raw={"pacing": {"mode": "turn"}}))
    assert not agent_mod._realtime(SimpleNamespace(raw=None))
    assert agent_mod._realtime(ra_state())


def test_a_game_we_play_still_gets_its_policy_even_in_real_time():
    """Only a game none of our policies plays is answered with WAIT: a (hypothetical) real-time Werewolf state still goes
    through the Werewolf policy."""
    raw = {"session_id": "w", "game_type": "werewolf", "phase": "night", "is_current_actor": True, "state_version": 3,
           "your_position": 2, "pacing": {"mode": "realtime"}, "game_state": {"day": 1},
           "legal_actions": {"actions": [{"action_id": str(i), "label": str(i), "input": {}} for i in (0, 1, 4)]}}
    out = Contestant().choose_action(GameState.from_mcp_state(raw), DecisionContext(
        session_id="w", tournament_id=None, game_type="werewolf", agent_id="a"))
    assert out is not WAIT


def test_the_official_worker_plays_a_red_alert_seat_to_the_end():
    """The official tournament runtime's worker (altruagent/worker.py run_tournament_worker: create_agent() from the
    agent spec, the seat's grant and lease, then run_game) on a Red Alert seat: the match ends, the worker exits 0."""
    from altruagent.models import SeatGrant
    from altruagent.worker import EXIT_SUCCESS, TournamentWorkerInput, run_tournament_worker

    grant = SeatGrant.from_dict({"access_token": "seat-jwt-FAKE", "agent_id": "a", "seat_id": "seat-ra",
                                 "gameapi_server_url": "https://gameapi.example.test", "game_session_id": "ra",
                                 "match_id": "match-ra", "seat_position": 0, "seat_count": 2,
                                 "game_type": "red_alert", "match_status": "in_progress"})

    class FakeOfficial:
        control_url = "https://control.example.test"

        def grant(self, seat_id, execution_id):
            return grant

        def renew_lease(self, seat_id, execution_id):
            return {"seat_id": seat_id, "execution_lease_expires_at": "x"}

        def close(self):
            pass

    game = _match(25)
    played = []

    def run(_lease_guarded_game, context, contestant):           # the fake GameAPI in place of the real session
        played.append((context.game_type, type(contestant).__name__))
        return run_game(game, context, contestant, sleep=lambda s: None)

    code = run_tournament_worker(TournamentWorkerInput("seat-ra", "match-ra", "red_alert", "agent.agent:create_agent",
                                                       "exec-" + "r" * 40),
                                 official_factory=FakeOfficial, run_game_fn=run)
    assert code == EXIT_SUCCESS
    assert played == [("red_alert", "Contestant")]
    assert game.play_action_calls == [] and len(game.wait_calls) == 25


def test_a_red_alert_seat_on_the_fake_platform_through_the_official_worker(tmp_path, monkeypatch):
    """End to end over HTTP / MCP: local/fake_platform.py --official --red-alert (a scripted real-time match), the SDK's
    official tournament worker with the real OfficialAgentClient and MCPGameSession, our create_agent(). The match
    plays to its end and the worker exits 0; no batch is sent, the runner fetches the game's reference once."""
    import signal
    import socket
    import subprocess
    import sys
    import time

    from altruagent.official import OfficialAgentClient
    from altruagent.worker import TournamentWorkerInput, run_tournament_worker
    from local.fake_platform import DEFAULT_OFFICIAL_KEY

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    stats = tmp_path / "platform.json"
    proc = subprocess.Popen([sys.executable, "-m", "local.fake_platform", "--port", str(port), "--official",
                             "--concurrent", "0", "--red-alert", "1", "--ra-seconds", "4", "--stats", str(stats),
                             "--stats-every", "0.5"], cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT)),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        official = OfficialAgentClient(url, DEFAULT_OFFICIAL_KEY, load_env_file=False)
        rows, deadline = [], time.monotonic() + 30
        while not rows and time.monotonic() < deadline:
            try:
                official.authenticate()
                rows = official.assignments()
            except Exception:  # noqa: BLE001 - the platform is still starting
                time.sleep(0.3)
        assert [(r.game_type, r.seat_count) for r in rows] == [("red_alert", 2)]
        monkeypatch.setenv("ALTRUAGENT_CONTROL_URL", url)
        monkeypatch.setenv("ALTRUAGENT_OFFICIAL_AGENT_KEY", DEFAULT_OFFICIAL_KEY)
        code = run_tournament_worker(TournamentWorkerInput(seat_id=rows[0].seat_id, match_id=rows[0].match_id,
                                                           game_type="red_alert", agent_spec="agent.agent:create_agent",
                                                           execution_id="exec-R-" + "r" * 40))
        assert code == 0
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            st = json.loads(stats.read_text()) if stats.exists() else {}
            if (st.get("totals") or {}).get("matches_done"):
                break
            time.sleep(0.3)
        ra = next(m for m in st["matches"] if m["game_type"] == "red_alert")
        assert ra["terminal"] and ra["reason"] == "time_limit" and not ra["error"]
        assert ra["batches"] == 0 and ra["invalid_actions"] == 0 and ra["race_errors"] == 0
        assert ra["views"] >= 10                                   # a view every 0.2 s, each one answered with WAIT
        assert st["totals"]["game_config_calls"] == 1 and st["totals"]["red_alert_matches"] == 1
        assert st["official"]["grants"] == 1 and st["official"]["moves_by_non_holder"] == 0
        assert official.assignments() == []                        # an ended match leaves the listing
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---- a recorded Red Alert state in logs/ (the deployment records next to its log): the replays take it ----------------
def _script(monkeypatch, name: str):
    monkeypatch.setenv("ARENA_RECORD", os.environ.get("ARENA_RECORD", "0"))    # (the scripts set it on import)
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _recorded(tmp_path: Path) -> Path:
    f = tmp_path / "logs" / "deploy" / "ra.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"t": 0, "kind": "action", "game_type": "red_alert", "raw": ra_payload(), "out": "WAIT"})
                 + "\n")
    return f


def test_selfcheck_replays_a_recorded_red_alert_state(monkeypatch, tmp_path, capsys):
    sc = _script(monkeypatch, "selfcheck")
    _recorded(tmp_path)
    monkeypatch.setattr(sc, "ROOT", tmp_path)
    sc.replay(50, with_calc=False)
    assert sc.FAILS == []
    assert "replayed 1 recorded decisions (calculator disabled; {'playing': 1}): 0 rejected" in capsys.readouterr().out


def test_replay_real_replays_a_recorded_red_alert_state(monkeypatch, tmp_path):
    rr = _script(monkeypatch, "replay_real")
    counts = {"n": 0, "fails": 0, "with_reason": 0}
    rr._replay_file(str(_recorded(tmp_path)), Contestant(), False, [], [], counts)
    assert counts == {"n": 1, "fails": 0, "with_reason": 0}
