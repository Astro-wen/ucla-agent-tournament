"""Independent evidence checks: sample units, pairing, masked failures and env isolation."""
import copy
import json
import math
import os
import random

import pytest

from agent.agent import create_agent
from local import werewolf_benchmark as wb
from local.werewolf_env import Game, OfficialBaseline


def record(scenario, variant, wins, roles=None):
    roles = roles or ["villager"] * scenario.k
    return {"id": scenario.key, "scenario": wb.asdict(scenario), "variant": variant,
            "deal": {"seed": scenario.game, "seats": list(range(scenario.k)), "roles": roles,
                     "background": {str(i): "first" for i in range(scenario.k, 7)}},
            "wins": wins, "failures": {"illegal": 0}, "background_failures": {"illegal": 0},
            "max_ms": 1, "latency_histogram": {"1": 3}}


def test_exact_mcnemar_matches_enumerated_binomial():
    for improved in range(15):
        for worsened in range(15):
            n = improved + worsened
            expected = min(1, 2 * sum(math.comb(n, k) for k in range(min(improved, worsened) + 1)) / 2 ** n)
            assert wb.exact_mcnemar(improved, worsened) == pytest.approx(expected)
    assert wb.exact_mcnemar(1000, 0) > 0
    assert wb.exact_mcnemar(1000, 1000) == pytest.approx(1)
    assert wb.exact_mcnemar(20, 0) < 0.00001


def test_unchanged_pairs_have_uncertainty_and_cannot_prove_gain():
    stats = wb.paired_stats([(1, 1)] * 10)
    assert stats["delta"] == 0 and stats["mcnemar_p"] == 1
    assert stats["conservative_ci95"][0] < 0 < stats["conservative_ci95"][1]
    assert wb.paired_stats([])["candidate_rate"] is None
    # Multi-seat means are bounded outcomes, not Bernoulli trials.
    assert wb.paired_stats([(2 / 3, 1 / 3)] * 10)["mcnemar_p"] is None


def test_paired_improvement_and_regression_are_symmetric():
    pairs = [(1, 0)] * 60 + [(0, 1)] * 10 + [(1, 1)] * 130
    a, b = wb.paired_stats(pairs), wb.paired_stats([(y, x) for x, y in pairs])
    assert a["delta"] == pytest.approx(0.25)
    assert a["mcnemar_p"] < 0.001
    assert a["conservative_ci95"][0] > 0
    assert a["conservative_ci95"][0] == pytest.approx(-b["conservative_ci95"][1])
    assert a["mcnemar_p"] == pytest.approx(b["mcnemar_p"])


def test_multi_seat_roles_are_side_isolated_and_count_table_once():
    s = wb.Scenario(151, 0, "first", 3)
    roles = ["wolf", "villager", "seer"]
    rows = [record(s, "reference", [False, True, True], roles),
            record(s, "candidate", [True, False, False], roles),
            record(s, "wolf_only", [True, False, False], roles),
            record(s, "village_only", [False, True, True], roles)]
    report = wb.summarize(rows, [s])
    metrics = report["metrics"]["all"]
    assert metrics["wolf"]["delta"] == 1
    assert metrics["villager"]["delta"] == metrics["seer"]["delta"] == 0
    assert metrics["seat"]["delta"] == pytest.approx(-1 / 3)
    assert all(m["n"] == 1 for m in metrics.values())
    assert not report["local_evidence_pass"]  # No realistic k=1 evidence.


def test_missing_duplicate_mismatched_and_failed_pairs_block_verdict():
    s = wb.Scenario(151, 0, "first", 1)
    rows = [record(s, "reference", [False]), record(s, "candidate", [True])]
    assert not wb.summarize(rows, [s])["integrity_errors"]
    for invalid in (rows[:1], rows + rows[:1]):
        assert wb.summarize(invalid, [s])["integrity_errors"]
    mismatch = copy.deepcopy(rows)
    mismatch[1]["deal"]["roles"] = ["wolf"]
    assert wb.summarize(mismatch, [s])["integrity_errors"]
    failed = copy.deepcopy(rows)
    failed[1]["failures"]["fallbacks"] = 1
    assert not wb.summarize(failed, [s])["gates"]["zero_failures"]
    errors = [{**r, "harness_error": "construction failed"} for r in rows]
    assert wb.summarize(errors, [s])["integrity_errors"]


def test_favorable_pooled_result_does_not_hide_opponent_regression():
    scenarios, rows = [], []
    for bg in ("first", "heuristic"):
        for g in range(100):
            s = wb.Scenario(151, g, bg, 1)
            scenarios.append(s)
            candidate = (g < 90) if bg == "first" else (g < 20)
            reference = (g < 10) if bg == "first" else (g < 60)
            rows += [record(s, "reference", [reference]), record(s, "candidate", [candidate])]
    report = wb.summarize(rows, scenarios)
    assert report["primary"]["delta"] > 0
    assert report["regressions"]
    assert not report["gates"]["no_significant_opponent_regression"]


def test_candidate_env_is_scoped_during_creation_and_each_decision(monkeypatch):
    seen = []

    class Probe:
        def __init__(self):
            seen.append(os.environ.get("ARENA_WW_PROBE"))

        def choose_action(self, state, context):
            seen.append(os.environ.get("ARENA_WW_PROBE"))
            return state.legal_actions[0]

    monkeypatch.setenv("ARENA_WW_PROBE", "reference")
    candidate = wb.MeasuredSeat(Probe, {"ARENA_WW_PROBE": "candidate"})
    reference = wb.MeasuredSeat(Probe, {})
    game = Game([candidate, reference] + [OfficialBaseline() for _ in range(5)], random.Random(1))
    candidate.choose(game.state(0, "moving", "day_vote", [1, 7]))
    reference.choose(game.state(1, "moving", "day_vote", [0, 7]))
    assert seen == ["candidate", "reference", "candidate", "reference"]
    assert os.environ["ARENA_WW_PROBE"] == "reference"
    with pytest.raises(RuntimeError), wb.environment({"ARENA_WW_PROBE": "temp"}):
        raise RuntimeError("test restoration")
    assert os.environ["ARENA_WW_PROBE"] == "reference"


def test_swallowed_policy_exception_is_counted_as_fallback(monkeypatch):
    def broken_factory():
        agent = create_agent()

        def boom(raw):
            raise ValueError("synthetic strategy fault")

        agent.ww.act = boom
        return agent

    monkeypatch.setattr(wb.ev, "factory", lambda name: broken_factory)
    s = wb.Scenario(151, 0, "first", 1)
    rec = wb.play_variant(s, "candidate", "bad", "bad", {})
    assert rec["failures"]["crash"] == rec["failures"]["illegal"] == 0
    assert rec["failures"]["fallbacks"] > 0
    assert rec["failures"]["fallback_paths"] > 0
    assert rec["failures"]["logged_errors"] > 0


def test_mirror_pair_is_deterministic_and_logging_is_bounded(monkeypatch):
    monkeypatch.setenv("ARENA_RECORD", "1")
    monkeypatch.setenv("ARENA_STATE", "1")
    scenario = wb.Scenario(151, 0, "llmq", 1)
    args = (scenario, "champion", "champion", {}, {"ARENA_WW_TIMING": "rt"}, True)
    runs = wb.play_pair(args)
    assert len(runs) == 2
    for key in ("deal", "winner", "wins", "days", "failures", "synthetic_trace"):
        assert runs[0][key] == runs[1][key]
    assert not any(runs[0]["failures"].values())
    assert len(runs[0]["synthetic_trace"]["messages"]) <= 100
    assert all(len(v) <= 80 for v in runs[0]["synthetic_trace"]["decisions"].values())
    # Scoped to synthetic simulation, not caller's deployment environment.
    assert os.environ["ARENA_RECORD"] == "1"
    assert os.environ["ARENA_STATE"] == "1"


def test_scenarios_reject_duplicate_or_paid_evidence():
    with pytest.raises(ValueError, match="duplicate"):
        wb.build_scenarios([151, 151], ["first"], [1], 10)
    with pytest.raises(ValueError, match="paid"):
        wb.build_scenarios([151], ["llm_real"], [1], 10)
    with pytest.raises(ValueError):
        wb.build_scenarios([151], ["first"], [0], 10)
    planned = wb.build_scenarios([151], ["first", "random"], [1, 7], 2)
    assert len(planned) == 6  # k=7 has no opponent; do not count duplicates.
    for env in ("ARENA_RECORD=1", "ARENA_WW_TOKEN=secret", "BAD_FORMAT"):
        with pytest.raises(ValueError):
            wb.parse_candidate_env([env])


def test_cli_preserves_records_and_rejects_seed_misuse(tmp_path):
    out = tmp_path / "smoke"
    report = wb.main(["--games", "1", "--backgrounds", "first", "--ks", "1", "--out", str(out),
                      "--candidate", "champion"])
    assert not report["local_evidence_pass"]
    assert report["gates"]["source_unchanged_during_run"]
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["source"]["files"] and manifest["planned_games"] == 2
    assert len((out / "games.jsonl").read_text().splitlines()) == 2
    for args in (["--out", str(out)], ["--out", str(tmp_path / "bad"), "--seeds", "28101"],
                 ["--out", str(tmp_path / "partial"), "--split", "heldout", "--seeds", "28101"]):
        with pytest.raises(SystemExit):
            wb.main(args)


def test_missing_latency_and_untracked_factories_cannot_look_verified(tmp_path):
    assert wb.histogram_percentile({}, 0.99) is None
    with pytest.raises(SystemExit):
        wb.main(["--out", str(tmp_path / "bad-factory"), "--candidate", "untracked_package:factory"])
    manifest = wb.source_manifest()
    assert "pyproject.toml" in manifest["files"]
    assert "altruagent/runner.py" in manifest["files"]
