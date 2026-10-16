"""Contract/diagnostic tests only: these do not validate Tencent engine fidelity."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from local.hok_env import Baseline, candidate_factory, opponent_factory, play_game
from local.hok_sim import DiagnosticGame, PROFILES, SimConfig, profile_config
from scripts.hok_eval import evaluate, parser


def idle_all(game):
    for camp in (1, 2):
        for seat in game.seats(camp):
            if seat in game.pending:
                assert not game.submit(camp, f"s{seat}:button_0", game.step).get("error")


def test_five_seats_pending_and_standing():
    game = DiagnosticGame()
    raw = game.view(1)
    assert raw["seats"] == list(range(5))
    assert game.view(2)["seats"] == list(range(5, 10))
    assert not game.submit(1, "s0:macro_advance", 0).get("error")
    assert 0 not in game.view(1)["simultaneous"]["your_pending_seats"]
    assert all(not a["action_id"].startswith("s0:") for a in game.view(1)["legal_actions"]["actions"])
    x = game.heroes[0].x
    game.tick()
    assert game.heroes[0].x > x
    x = game.heroes[0].x
    game.tick()
    assert game.heroes[0].x > x  # no fresh order
    assert game.view(1)["simultaneous"]["standing_orders"]["0"] == "macro_advance"
    assert not game.submit(1, "s0:button_0", 0).get("error")  # old version OK in realtime
    x = game.heroes[0].x
    game.tick()
    assert game.heroes[0].x == x
    assert "0" not in game.view(1)["simultaneous"]["standing_orders"]


def test_raw_move_is_one_shot_and_requires_legal_values():
    game = DiagnosticGame()
    assert game.submit(1, "s0:button_2", 0)["error"] == "INVALID_ACTION"
    assert game.submit(1, {"seat": 0, "which_button": 2, "move_x": 14, "move_z": 8}, 0).get("error")
    assert not game.submit(1, {"seat": 0, "which_button": 2, "move_x": 15, "move_z": 8}, 0).get("error")
    game.tick()
    x = game.heroes[0].x
    game.tick()
    assert game.heroes[0].x == x


@pytest.mark.parametrize("action", ["s5:macro_advance", "s99:button_0", "s0:macro_skill3",
                                   {"seat": True, "which_button": 0},
                                   {"seat": 0, "macro": "macro_advance", "extra": 1},
                                   {"seat": 0, "which_button": False}, [], 17])
def test_illegal_actions_are_rejected_without_mutation(action):
    game = DiagnosticGame()
    before = game.state_hash()
    assert game.submit(1, action, 0)["error"] == "INVALID_ACTION"
    assert game.state_hash() == before


def test_lockstep_versions_all_ten_seats_and_explicit_timeout():
    game = DiagnosticGame(SimConfig(mode="lockstep"))
    with pytest.raises(RuntimeError):
        game.tick()
    with pytest.raises(RuntimeError):
        game.tick(timeout=True)  # timer cannot start before a team finishes
    assert game.submit(1, "s0:button_0", None)["error"] == "STALE_STATE"
    for seat in game.seats(1):
        assert not game.submit(1, f"s{seat}:macro_advance", 0).get("error")
    game.tick(timeout=True)
    assert game.last_fallback_players == list(range(5, 10))
    assert game.submit(1, "s0:button_0", 0)["error"] == "STALE_STATE"
    idle_all(game)
    game.tick()
    assert game.step == 2


def test_hidden_heroes_never_leak_state_or_target_labels():
    game = DiagnosticGame(SimConfig(vision_range=100))
    first = game.view(1)
    for h in game.heroes.values():
        if h.camp == 2:
            h.x += 123
            h.hp -= 42
            h.kills = 10
    assert first == game.view(1)
    for entry in first["observation"]["seats"].values():
        hidden = [h for h in entry["summary"]["other_heroes"] if not h["visible"]]
        assert len(hidden) == 5
        assert all(set(h) == {"runtime_id", "hero_id", "camp", "visible"} for h in hidden)
    assert all("enemy hero" not in a["label"] for a in first["legal_actions"]["actions"])
    # Returned payloads cannot mutate engine state.
    first["observation"]["seats"]["0"]["summary"]["my_hero"]["hp"] = -10
    assert game.heroes[0].hp > 0


def test_attack_reaim_cooldown_and_simultaneous_death_respawn():
    c = SimConfig(hero_hp=100, attack_damage=200, attack_cooldown_s=1.0, respawn_s=0.4,
                  tower_damage=0, base_heal_fraction_per_s=0, clear_orders_on_death=True)
    game = DiagnosticGame(c)
    for seat in (0, 5):
        game.heroes[seat].x = 0
        game.heroes[seat].z = 0
    assert not game.submit(1, "s0:macro_attack", 0).get("error")
    assert not game.submit(2, "s5:macro_attack", 0).get("error")
    game.tick()
    assert game.heroes[0].hp == game.heroes[5].hp == 0
    assert game.kills == {1: 1, 2: 1}
    assert not game.standing
    assert [a["action_id"] for a in game._entries(0)] == ["s0:button_0"]
    game.tick()
    game.tick()
    game.tick()
    assert game.heroes[0].hp == 100
    assert game.heroes[0].x == -c.half_length
    assert game.respawns == {1: 1, 2: 1}


def test_standing_attack_reaims_and_waits_for_cooldown():
    game = DiagnosticGame(SimConfig(tower_damage=0, attack_damage=100, attack_cooldown_s=0.4))
    for seat, x in ((0, 0), (5, 1000), (6, 2000)):
        game.heroes[seat].x, game.heroes[seat].z = x, 0
    game.submit(1, "s0:macro_attack", 0)
    game.tick()
    assert game.heroes[5].hp == 3900
    assert "s0:macro_attack" not in [a["action_id"] for a in game._entries(0)]
    game.heroes[5].x = 50000
    game.tick()
    assert game.heroes[6].hp == 4000
    game.tick()
    assert game.heroes[6].hp == 3900


def test_healing_capped_and_dead_heroes_do_not_heal():
    game = DiagnosticGame(SimConfig(base_heal_fraction_per_s=1.0))
    game.heroes[0].hp = 3999
    game.heroes[1].hp, game.heroes[1].revive_at = 0, 100
    game.tick()
    assert game.heroes[0].hp == 4000
    assert game.heroes[1].hp == 0
    assert game.healed[1] == 1


@pytest.mark.parametrize("damage,kills,winner", [({1: 5, 2: 4}, {1: 0, 2: 20}, 1),
                                                ({1: 4, 2: 4}, {1: 0, 2: 1}, 2),
                                                ({1: 4, 2: 4}, {1: 0, 2: 0}, None)])
def test_official_scoring_priority(damage, kills, winner):
    game = DiagnosticGame(SimConfig(max_steps=1))
    game.structure_damage, game.kills = damage, kills
    game.tick()
    assert game.winner_camp == winner
    assert game.result()["termination_reason"] == "step_limit"


def test_time_limit_and_crystal_victory():
    game = DiagnosticGame(SimConfig(time_limit_s=0.2))
    game.tick()
    assert game.termination_reason == "time_limit"
    game = DiagnosticGame()
    game.structures[201].hp = 0
    game.tick()
    assert game.winner_camp == 1
    assert game.result()["platform_scores"] == {"1": 1.0, "2": 0.0}


@pytest.mark.parametrize("profile", list(PROFILES))
@pytest.mark.parametrize("mode", ["realtime", "lockstep"])
def test_varied_configs_baseline_contract_and_candidate_smoke(profile, mode):
    config = profile_config(profile, mode=mode, max_steps=8)
    factory, _ = candidate_factory(Path(__file__).resolve().parents[2])
    opponent, _ = opponent_factory("random")
    report = play_game(factory, opponent, config=config, seed=7)
    assert report["protocol_failure"] is None
    assert report["stats"]["candidate"]["invalid"] == 0
    assert report["stats"]["opponent"]["invalid"] == 0
    assert set(report["stats"]["candidate"]["accepted_by_seat"]) == set("01234")


@pytest.mark.parametrize("name", ["random", "first-legal", "push", "idle"])
def test_builtin_baselines_are_legal_and_cover_every_seat(name):
    factory, _ = opponent_factory(name)
    report = play_game(factory, factory, config=SimConfig(mode="lockstep", max_steps=4))
    assert report["protocol_failure"] is None
    assert report["stats"]["candidate"]["accepted"] == 20
    assert report["stats"]["opponent"]["accepted"] == 20


def test_random_can_choose_macros_and_idle_but_never_unfilled_move():
    bot = Baseline("random", seed=2)
    raw = DiagnosticGame().view(1)
    actions = {bot.decide(raw)[0] for _ in range(100)}
    assert actions == {"s0:button_0", "s0:macro_advance", "s0:macro_retreat"}
    assert Baseline("first-legal").decide(raw)[0] == "s0:button_0"
    assert Baseline("push").decide(raw)[0] == "s0:macro_advance"


def test_reproducibility_and_no_raw_decisions_in_report():
    args = parser().parse_args(["--games", "1", "--max-steps", "3", "--opponent", "push"])
    one, two = evaluate(args), evaluate(args)
    assert one["reproducibility_sha256"] == two["reproducibility_sha256"]
    assert one["harness"]["ARENA_RECORD"] == "0"
    legs = one["cells"][0]["pairs"][0]["legs"]
    assert [g["candidate_camp"] for g in legs] == [1, 2]
    serialized = json.dumps(one)
    assert '"observation"' not in serialized and '"action_id"' not in serialized and '"tag"' not in serialized


def test_lockstep_wait_is_explicit_protocol_forfeit_not_game_strength():
    class Waiter:
        def decide(self, raw):
            return None, None
    push, _ = opponent_factory("push")
    report = play_game(Waiter, push, config=SimConfig(mode="lockstep", max_steps=2))
    assert report["protocol_failure"] == {"camp": 1, "kind": "lockstep_wait"}
    assert report["result"]["termination_reason"] == "evaluation_protocol_forfeit"
    assert report["stats"]["candidate"]["invalid"] == 1


@pytest.mark.parametrize("changes", [{"mode": "fake"}, {"max_steps": 0}, {"max_steps": True},
                                     {"hero_hp": float("nan")}, {"speed": -1},
                                     {"skill_ranges": [1, 2]}, {"raw_move": 1}, {"step_s": 0},
                                     {"speed": [1, 2, 3]}, {"skill_damage": 3},
                                     {"skill_cooldowns_s": "bad"}, {"skill_ranges": [1, True, 3]}])
def test_config_rejects_invalid_assumptions(changes):
    with pytest.raises(ValueError):
        replace(SimConfig(), **changes)


def test_sdk_parses_views_and_validates_ids_legalaction_and_raw_inputs():
    from altruagent import GameState
    from altruagent.runner import _validate_decision
    game = DiagnosticGame()
    state = GameState.from_mcp_state(game.view(2))
    assert state.raw["camp"] == 2
    advance = next(a for a in state.legal_actions if a.action_id == "s5:macro_advance")
    for action in (advance, advance.action_id, {"seat": 5, "macro": "macro_advance"}):
        parsed = _validate_decision(action, state.legal_actions)
        assert parsed.action_id == advance.action_id or parsed.action == {"seat": 5, "macro": "macro_advance"}
    assert not game.submit(2, advance, 0).get("error")


def test_malformed_and_foreign_seat_errors_notify_policy_without_recording_payload():
    class Refused:
        results = []
        def decide(self, raw):
            return "s9:macro_advance", "SECRET_TAG"
        def on_result(self, result):
            self.results.append(result)
    idle, _ = opponent_factory("idle")
    report = play_game(Refused, idle, config=SimConfig(max_steps=2))
    assert report["stats"]["candidate"]["invalid"] == 12
    assert report["protocol_failure"] is None  # realtime rejection is recoverable
    assert len(Refused.results) == 12
    assert all(r["error"] == "INVALID_ACTION" for r in Refused.results)
    assert "SECRET_TAG" not in json.dumps(report)
    assert "s9:macro_advance" not in json.dumps(report)


def test_stale_target_refused_against_current_offers_without_losing_standing_order():
    game = DiagnosticGame()
    game.heroes[0].x = game.heroes[5].x = 0
    game.heroes[0].z = game.heroes[5].z = 0
    assert any(a["action_id"] == "s0:macro_attack" for a in game.view(1)["legal_actions"]["actions"])
    game.heroes[5].x = game.config.half_length
    assert game.submit(1, "s0:macro_attack", 0)["error"] == "INVALID_ACTION"
    assert 0 in game.pending


def test_structure_damage_is_hp_capped_and_shielding_is_explicit_parameter():
    game = DiagnosticGame(SimConfig(attack_damage=100000, tower_damage=0, crystal_shielded=True))
    game.heroes[0].x, game.heroes[0].z = game.structures[200].x, 0
    assert game._shielded(game.structures[201])
    assert not game.submit(1, "s0:macro_attack", 0).get("error")
    game.tick()
    assert game.structures[200].hp == 0
    assert game.structure_damage[1] == game.config.tower_hp
    assert not game._shielded(game.structures[201])
    unshielded = DiagnosticGame(SimConfig(crystal_shielded=False))
    assert not unshielded._shielded(unshielded.structures[201])


def test_resignation_and_terminal_contract():
    game = DiagnosticGame()
    result = game.resign(1)
    assert result["termination_reason"] == "resignation"
    assert result["outcome"]["winner_camp"] == 2
    assert result["returns"] == {"1": -1, "2": 1}
    assert game.view(1)["legal_actions"]["actions"] == []
    assert game.submit(2, "s5:macro_advance", 0)["error"] == "INVALID_ACTION"
    with pytest.raises(RuntimeError):
        game.tick()


@pytest.mark.parametrize("profile", list(PROFILES))
def test_mirrored_same_deterministic_policy_has_no_first_camp_damage_advantage(profile):
    push, _ = opponent_factory("push")
    report = play_game(push, push, config=profile_config(profile, max_steps=150), seed=37)
    scores = report["result"]["outcome"]["score"]
    assert scores["1"] == scores["2"]
    assert report["candidate_score"] == 0.5


def test_importable_factory_and_separate_candidate_package(tmp_path):
    import os
    (tmp_path / "helper.py").write_text("ACTION = 'macro_advance'\n")
    (tmp_path / "policy.py").write_text(
        "from .helper import ACTION\n"
        "class HokPolicy:\n"
        "    def __init__(self, clock): self.clock = clock\n"
        "    def decide(self, raw):\n"
        "        seat = raw['simultaneous']['your_pending_seats'][0]\n"
        "        return {'seat': seat, 'macro': ACTION}, None\n")
    candidate, info = candidate_factory(tmp_path)
    idle, _ = opponent_factory("idle")
    report = play_game(candidate, idle, config=SimConfig(max_steps=3))
    assert report["stats"]["candidate"]["accepted"] == 15
    assert info["clock_parameter"] is True
    assert info["hash_scope"] == "hok package Python files"
    old_hash = info["source_sha256"]
    old_mtime = (tmp_path / "helper.py").stat().st_mtime_ns
    (tmp_path / "helper.py").write_text("ACTION = 'macro_retreat'\n")
    os.utime(tmp_path / "helper.py", ns=(old_mtime, old_mtime))
    updated, changed = candidate_factory(tmp_path)
    assert changed["source_sha256"] != old_hash
    next_report = play_game(updated, idle, config=SimConfig(max_steps=1))
    assert next_report["stats"]["candidate"]["action_kind_counts"] == {"macro_retreat": 5}
    assert not (tmp_path / "__pycache__").exists()


def test_policy_error_counts_do_not_include_sensitive_exception_messages():
    class Broken:
        def decide(self, raw):
            raise ValueError("DO_NOT_RECORD_RAW_DECISION")
    push, _ = opponent_factory("push")
    report = play_game(Broken, push, config=SimConfig(max_steps=2))
    assert report["stats"]["candidate"]["exceptions"] == {"ValueError": 1}
    assert report["protocol_failure"]["kind"] == "policy_exception"
    assert "DO_NOT_RECORD" not in json.dumps(report)


def test_evaluation_never_connects_to_network_or_enables_recording(monkeypatch):
    import os
    import socket
    def forbidden(*_args, **_kwargs):
        raise AssertionError("offline evaluation must never connect")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setenv("ARENA_RECORD", "1")
    candidate, _ = candidate_factory(Path(__file__).resolve().parents[2])
    push, _ = opponent_factory("push")
    report = play_game(candidate, push, config=SimConfig(max_steps=10))
    assert report["protocol_failure"] is None
    assert os.environ["ARENA_RECORD"] == "0"


def test_json_mode_override_is_honored_and_cli_override_wins(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"mode": "lockstep", "max_steps": 2}')
    args = parser().parse_args(["--games", "1", "--config", str(path)])
    assert evaluate(args)["cells"][0]["config"]["mode"] == "lockstep"
    args.mode = "realtime"
    assert evaluate(args)["cells"][0]["config"]["mode"] == "realtime"


def test_default_standing_order_survives_death_until_a_new_raw_order():
    game = DiagnosticGame(SimConfig(hero_hp=100, tower_damage=200, respawn_s=0.2))
    game.heroes[0].x, game.heroes[0].z = game.structures[200].x, 0
    assert not game.submit(1, "s0:macro_advance", 0).get("error")
    game.tick()
    assert game.heroes[0].hp == 0
    assert game.standing[0] == "macro_advance"
    game.tick()
    game.tick()
    assert game.heroes[0].hp == 100
    assert game.heroes[0].x > -game.config.half_length  # resumed without reissuing
    assert not game.submit(1, "s0:button_0", game.step).get("error")
    assert 0 not in game.standing


def test_independently_supplied_research_factory_interface(monkeypatch):
    import sys
    import types
    calls = []
    def make_bot(name, seed=0):
        calls.append((name, seed))
        return Baseline("push", seed=seed)
    monkeypatch.setitem(sys.modules, "local.hok_opponents_research", types.SimpleNamespace(make_bot=make_bot))
    factory, info = opponent_factory("research:external-bot")
    result = play_game(factory, factory, config=SimConfig(max_steps=3), seed=1016)
    assert calls == [("external-bot", 1016), ("external-bot", 1001019)]
    assert info["name"] == "external-bot"
    assert result["protocol_failure"] is None
