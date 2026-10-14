from collections import Counter
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import random

import httpx
import pytest

from agent.werewolf.state import parse
from local.werewolf_env import OfficialBaseline, deal_roles
from local.werewolf_research import ResearchGame
from scripts import werewolf_minimal_model_eval as pilot
from scripts import werewolf_local_model_eval as lm
from scripts import werewolf_model_scale_eval as scale


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    original = httpx.AsyncClient.__init__
    def checked(self, *args, **kwargs):
        assert isinstance(kwargs.get("transport"), httpx.MockTransport)
        original(self, *args, **kwargs)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", checked)
    for name in ("ARENA_RECORD", "ARENA_STATE", "ARENA_SUMMARY", "ARENA_TC_WATCHDOG"):
        monkeypatch.setenv(name, "0")


def response(model, answer):
    return httpx.Response(200, json={"model": model, "done": True, "done_reason": "stop",
                         "message": {"role": "assistant", "content": json.dumps(answer)},
                         "prompt_eval_count": 900, "eval_count": 20})


def reply(action):
    return {"action_id": action, "reasoning_summary": "Use the offered action."}


def row(scenario, profile, model, clean=True, win=False):
    roles = deal_roles(random.Random(scenario["deal_seed"]))
    return {"id": scenario["id"], "profile": profile, "model": model, "scenario": deepcopy(scenario),
            "seat": scenario["ours"], "role": roles[scenario["ours"]], "roles": roles,
            "complete": True, "win": win, "clean": clean, "failures": {}, "background_failures": {},
            "llm_stats": {}, "provider_stats": {}, "llm_seats": [], "synthetic_trace": {"messages": [], "actions": []}}


def ready_guard(path):
    path.write_text(json.dumps({"status": "complete", "models": {
        name: {"unloaded_after_run": True, "model_digest": digest} for name, digest in pilot.MODELS}}))


def test_plan_has_eighteen_unique_deals_exact_role_and_background_quotas():
    a = pilot.build_plan()
    assert a == pilot.build_plan()
    scenarios = a["scenarios"]
    assert len(scenarios) == len({s["deal_seed"] for s in scenarios}) == 18
    assert Counter(s["our_role"] for s in scenarios) == {"villager": 6, "seer": 6, "wolf": 6}
    assert Counter((s["background"], s["our_role"]) for s in scenarios) == {
        (background, role): 2 for background in pilot.BACKGROUNDS for role in pilot.ROLES}
    assert a["skipped"] and all(not item["accepted"] for item in a["skipped"])
    for bg in pilot.BACKGROUNDS:
        scanned = [s["game"] for s in a["selection"] if s["background"] == bg]
        assert scanned == list(range(max(scanned) + 1))
    for scenario in scenarios:
        assert scenario["seed"] == 214 and scenario["llm_count"] == 2
        seed, ours, types = lm.ev.deal(214, scenario["game"], scenario["background"], 1)
        assert (seed, ours[0], types) == (scenario["deal_seed"], scenario["ours"], scenario["types"])
        assert scenario["our_role"] == deal_roles(random.Random(seed))[ours[0]]
        assert scenario["llm_seats"] == pilot.llm_placements(scenario["background"], scenario["game"], ours[0])
        assert len(set(scenario["llm_seats"])) == 2 and ours[0] not in scenario["llm_seats"]


def test_grouping_never_quietly_overwrites_duplicate_or_missing_profiles():
    plan = pilot.build_plan()["scenarios"][:1]
    records = [row(plan[0], label, model) for model, _ in pilot.MODELS for label, _ in pilot.PROFILES]
    full = pilot.summarize(records, plan)
    assert full["complete_groups"] == 2 and full["common_clean_scenarios"] == 1
    missing = pilot.summarize(records[:-1], plan)
    assert missing["complete_groups"] == 1 and missing["common_clean_scenarios"] == 0
    assert missing["missing_groups"] and missing["partial"]
    with pytest.raises(ValueError, match="duplicate"):
        pilot.summarize(records + records[:1], plan)
    corrupted = deepcopy(records)
    corrupted[0]["role"] = "wrong"
    with pytest.raises(ValueError, match="pair|identity"):
        pilot.summarize(corrupted, plan)
    partial = deepcopy(records)
    partial[0]["complete"] = False
    assert pilot.summarize(partial, plan)["complete_groups"] == 1


def test_clean_subset_is_common_to_both_models_and_all_profiles_not_selected_pairs():
    plan = pilot.build_plan()["scenarios"][:2]
    records = [row(s, label, model, win=label != "agent") for model, _ in pilot.MODELS
               for s in plan for label, _ in pilot.PROFILES]
    records[-1]["clean"] = False
    result = pilot.summarize(records, plan)
    for model, _ in pilot.MODELS:
        stats = result["metrics"][model]
        for name, _ in pilot.PROFILES[1:]:
            assert stats["all_attempts"][name]["all"]["n"] == 2
            assert stats["common_clean"][name]["all"]["n"] == 1
    unchanged = pilot.summarize([dict(r, win=False) for r in records], plan)
    cell = unchanged["metrics"][pilot.MODELS[0][0]]["all_attempts"][pilot.PROFILES[1][0]]["all"]
    assert cell["ci95"][0] < 0 < cell["ci95"][1] and cell["small_sample_warning"]
    assert "role_balanced" in unchanged["sampling"]


@pytest.mark.parametrize("patch", [{"status": "running"}, {"status": "blocked_partial"}, {"models": {}},
                                  {"models": {pilot.MODELS[0][0]: {"unloaded_after_run": False}}}])
def test_guard_gate_blocks_without_any_network(tmp_path, patch):
    path = tmp_path / "summary.json"
    ready_guard(path)
    content = json.loads(path.read_text()) | patch
    path.write_text(json.dumps(content))
    with pytest.raises(ValueError, match="guard"):
        pilot.check_guard(path)
    path.unlink()
    with pytest.raises(ValueError, match="guard"):
        pilot.check_guard(path)


def test_source_manifest_explicitly_includes_own_script_and_test(tmp_path, monkeypatch):
    for relative in pilot.EXTRA_SOURCES:
        file = tmp_path / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("synthetic source\n")
    monkeypatch.setattr(pilot, "ROOT", tmp_path)
    monkeypatch.setattr(lm, "source_manifest", lambda: {"files": {}, "loaded_python_files": {}})
    manifest = pilot.source_manifest()
    assert set(pilot.EXTRA_SOURCES) <= set(manifest["files"])
    expected = hashlib.sha256(b"synthetic source\n").hexdigest()
    assert manifest["files"]["scripts/werewolf_minimal_model_eval.py"] == expected
    lock = pilot.SourceLock(manifest, tmp_path)
    lock.check()
    (tmp_path / pilot.EXTRA_SOURCES[0]).write_text("changed source\n")
    with pytest.raises(pilot.PilotStop, match="source"):
        lock.check()


def test_mock_actual_game_never_exposes_plan_or_full_truth_to_seats(monkeypatch):
    scenario = pilot.build_plan()["scenarios"][0]
    seen = []
    original = lm.ObservingResearchGame.state
    def inspect(self, seat, phase, sub, actions=None):
        raw = original(self, seat, phase, sub, actions)
        assert set(raw) <= lm.RAW_KEYS
        assert not {"roles", "secretRole", "our_role", "types", "llm_seats", "deal_seed", "seed", "profile"} & set(raw)
        assert not {"roles", "secretRole", "types", "seed"} & set(raw["game_state"])
        assert all(not {"role", "secretRole", "types"} & set(p) for p in raw["players"])
        for message in raw["new_messages"]:
            assert not message.get("recipients") or seat == message["sender"] or seat in message["recipients"]
        seen.append((seat, parse(raw).role))
        return raw
    monkeypatch.setattr(lm.ObservingResearchGame, "state", inspect)
    wires = []
    def handle(request):
        body = json.loads(request.content)
        wires.append(body)
        assert scale.RULES in body["messages"][0]["content"]
        payload = json.loads(body["messages"][1]["content"])
        assert not {"roles", "types", "seed", "deal_seed", "our_role", "profile"} & set(payload)
        assert isinstance(payload["state"], dict)
        schema = body["format"]["properties"]
        answer = reply(schema["action_id"]["enum"][0]) if "action_id" in schema else {
            "decision": "end", "message": "", "recipient": None}
        return response(body["model"], answer)
    provider = scale.RulesProvider(explicit_rules=True, num_ctx=16384, encoding="label_object", transport=httpx.MockTransport(handle))
    result = lm.play_profile(scenario, "agent", "agent", provider)
    assert result["role"] == scenario["our_role"] and seen and wires and result["clean"]
    assert sum(s["stats"].get("post_termination_updates", 0) for s in result["llm_seats"]) > 0
    assert pilot.verify_cache_keys(provider.events, provider.model, provider.expected_digest)


def test_schema_valid_self_dm_retries_but_legal_wrong_wolf_vote_is_not_rewritten():
    g = ResearchGame([OfficialBaseline() for _ in range(7)], random.Random(214))
    g.roles = ["seer", "villager", "wolf", "villager", "wolf", "villager", "villager"]
    g.invest = {2: "wolf"}
    answers = iter([{"decision": "send", "message": "Vote Player2.", "recipient": 0},
                    {"decision": "end", "message": "", "recipient": None},
                    reply("Vote to eliminate Player1")])
    def handle(request):
        return response(lm.MODEL, next(answers))
    provider = scale.RulesProvider(explicit_rules=True, num_ctx=16384, encoding="label_object", transport=httpx.MockTransport(handle))
    seat = lm.LocalLLMSeat(provider)
    assert seat.message(g.state(0, "messaging", "day_vote")) is None
    assert seat.stats["invalid_answers"] == seat.stats["retries"] == 1
    assert provider.stats["errors"] == 0 and seat.stats["fallbacks"] == 0
    assert seat.choose(g.state(0, "moving", "day_vote", [1, 2, 3, 4, 5, 6, 7])) == "1"
    assert seat.stats["retries"] == 1
    trace = {"llm_seats": [{"seat": 0, "role": "seer", "win": False}], "roles": g.roles,
             "synthetic_trace": {"messages": [], "actions": [{"seat": 0, "phase": "day_vote", "legal": [1, 2, 7],
                          "investigations_before": {2: "wolf"}, "action": 1, "day": 1}]}}
    assert pilot.behavior_metrics(trace)["known_checked_wolf_vote"] == {"numerator": 0, "denominator": 1}


def test_digest_cache_keys_are_distinct_between_models_and_checked():
    g = ResearchGame([OfficialBaseline() for _ in range(7)], random.Random(214))
    providers = []
    for model, pin in pilot.MODELS:
        def handle(request, model=model):
            return response(model, {"decision": "end", "message": "", "recipient": None})
        provider = scale.RulesProvider(model=model, expected_digest=pin, explicit_rules=True, num_ctx=16384,
                                      encoding="label_object", transport=httpx.MockTransport(handle), cache={})
        lm.LocalLLMSeat(provider).message(g.state(0, "messaging", "day_vote"))
        lm.LocalLLMSeat(provider).message(g.state(0, "messaging", "day_vote"))
        assert provider.stats["cache_hits"] == 1
        assert pilot.verify_cache_keys(provider.events, model, pin)
        providers.append(provider)
    assert providers[0].cache is not providers[1].cache
    assert providers[0].events[0]["key"] != providers[1].events[0]["key"]
    providers[0].events[0]["key"] = "poison"
    with pytest.raises(ValueError, match="cache"):
        pilot.verify_cache_keys(providers[0].events, *pilot.MODELS[0])


def test_deadline_must_be_explicit_timezone_aware_and_never_after_1630():
    for bad in (None, "2026-10-13T16:30:00", "2026-10-13T16:30:01-07:00"):
        with pytest.raises(ValueError, match="deadline"):
            pilot.validate_deadline(bad, now=0)
    assert pilot.validate_deadline("2026-10-13T16:30:00-07:00", now=0).tzinfo is not None


def mock_run(tmp_path, monkeypatch, *, after_game=None, resource_after=None, interrupt=False, loaded=False):
    output = tmp_path / "out"
    output.mkdir()
    source_file = tmp_path / "frozen.py"
    source_file.write_text("frozen\n")
    monkeypatch.setattr(pilot, "ROOT", tmp_path)
    source = {"digest": "synthetic", "files": {"frozen.py": hashlib.sha256(source_file.read_bytes()).hexdigest()}}
    guard_file = tmp_path / "guard.json"
    ready_guard(guard_file)
    guard = pilot.check_guard(guard_file)
    plan = pilot.build_plan()
    plan["scenarios"] = plan["scenarios"][:1]
    calls, games, providers = [], [], []
    active = [pilot.MODELS[1][0] if loaded else None]
    now = [datetime.now(lm.PDT).timestamp()]
    stop = datetime.fromtimestamp(now[0] + 300, lm.PDT)
    baseline = {"pressure": 1, "swap_used_mib": 1000, "disk_free": 5 * 1024**3}
    def handle(request):
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "mock"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m, "digest": p} for m, p in pilot.MODELS]})
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [] if active[0] is None else [
                {"name": active[0], "digest": dict(pilot.MODELS)[active[0]], "context_length": 16384}]})
        body = json.loads(request.content)
        calls.append(body)
        assert "format" not in body
        active[0] = None if body.get("keep_alive") == 0 else body["model"]
        return response(body["model"], "OK")
    def factory(**kwargs):
        provider = pilot.PilotProvider(**kwargs, transport=httpx.MockTransport(handle))
        providers.append(provider)
        return provider
    def play(scenario, profile, factory, provider):
        games.append((provider.model, profile, scenario["id"]))
        if interrupt:
            raise pilot.PilotStop("deadline_partial")
        if after_game:
            after_game(source_file, now, stop)
        return row(scenario, profile, provider.model)
    def resources():
        return baseline | (resource_after if games and resource_after else {})
    result = pilot.run_pilot(plan, output, stop, source, guard, provider_factory=factory,
                            game_runner=play, resource_reader=resources, clock=lambda: now[0])
    return result, games, calls, providers, active


def test_mock_whole_model_blocks_preload_not_scored_cache_fresh_and_verified_unload(tmp_path, monkeypatch):
    result, games, calls, providers, active = mock_run(tmp_path, monkeypatch)
    assert result["status"] == "complete" and result["games_completed"] == 6
    assert [g[0] for g in games] == [pilot.MODELS[0][0]] * 3 + [pilot.MODELS[1][0]] * 3
    assert [g[1] for g in games] == [p for p, _ in pilot.PROFILES] * 2
    assert len({g[2] for g in games}) == 1 and active == [None]
    assert [c["keep_alive"] for c in calls] == [-1, 0, -1, 0]
    assert all(c["options"]["num_predict"] == 8 for c in calls if c["keep_alive"] == -1)
    assert all(not p.events and not p.stats["attempts"] for p in providers)
    assert providers[0].cache is not providers[1].cache
    assert len(result["resources"]) == len(games) + len(pilot.MODELS) + 1
    assert all(value["unloaded_after_run"] and value["cache_keys_verified"] for value in result["models"].values())


@pytest.mark.parametrize("resource", [{"pressure": 4}, {"swap_used_mib": 2025}, {"disk_free": 1024**3}])
def test_resource_stop_between_every_game_keeps_partial_and_unloads(tmp_path, monkeypatch, resource):
    result, games, calls, _, active = mock_run(tmp_path, monkeypatch, resource_after=resource)
    assert result["status"] == "blocked_partial" and result["games_completed"] == len(games) == 1
    assert result["partial"] and result["complete_groups"] == 0
    assert [c["keep_alive"] for c in calls] == [-1, 0] and active == [None]


def test_deadline_does_not_start_a_new_game_and_interrupted_match_is_not_complete(tmp_path, monkeypatch):
    def expire(source, now, stop):
        now[0] = stop.timestamp()
    result, games, _, _, active = mock_run(tmp_path, monkeypatch, after_game=expire)
    assert len(games) == result["games_completed"] == 1
    assert result["status"] == "deadline_partial" and active == [None]
    other = tmp_path / "interrupted"
    other.mkdir()
    result, games, _, _, active = mock_run(other, monkeypatch, interrupt=True)
    assert len(games) == result["games_attempted"] == 1 and result["games_completed"] == 0
    assert result["complete_groups"] == 0 and result["status"] == "deadline_partial" and active == [None]


def test_source_edit_stops_before_next_game_but_does_not_block_unload(tmp_path, monkeypatch):
    def change(source, now, stop):
        source.write_text("modified\n")
    result, games, calls, _, active = mock_run(tmp_path, monkeypatch, after_game=change)
    assert result["status"] == "blocked_partial" and "source_changed" in result["error"]
    assert len(games) == 1 and calls[-1]["keep_alive"] == 0 and active == [None]


def test_existing_loaded_model_blocks_without_autofix_or_preload(tmp_path, monkeypatch):
    result, games, calls, _, active = mock_run(tmp_path, monkeypatch, loaded=True)
    assert result["status"] == "blocked_partial" and "loaded model" in result["error"]
    assert not games and not calls and active == [pilot.MODELS[1][0]]


def test_source_check_runs_even_before_exact_cached_requests(tmp_path):
    file = tmp_path / "source.py"
    file.write_text("before\n")
    source = {"files": {"source.py": hashlib.sha256(file.read_bytes()).hexdigest()}}
    lock = pilot.SourceLock(source, tmp_path)
    requests = []
    def handle(request):
        requests.append(request)
        return response(lm.MODEL, {"decision": "end", "message": "", "recipient": None})
    provider = pilot.PilotProvider(check_run=lock.check, transport=httpx.MockTransport(handle))
    game = ResearchGame([OfficialBaseline() for _ in range(7)], random.Random(214))
    raw = game.state(0, "messaging", "day_vote")
    assert lm.LocalLLMSeat(provider).message(raw) is None
    file.write_text("after source changed\n")
    with pytest.raises(pilot.PilotStop, match="source_changed"):
        lm.LocalLLMSeat(provider).message(raw)
    assert len(requests) == 1 and provider.stats["cache_hits"] == 0


@pytest.mark.parametrize("filename,contents,valid", [("000000.json", '{"index":0}', True),
                                                     ("000100.json", '{"index":100}', False),
                                                     ("000000.json", '{"index":', False)])
def test_request_archive_indexes_hash_bytes_and_detect_missing_or_partial_logs(tmp_path, filename, contents, valid):
    model = pilot.MODELS[0][0]
    directory = tmp_path / model.replace(":", "_") / "requests"
    directory.mkdir(parents=True)
    file = directory / filename
    file.write_text(contents)
    report = pilot.index_requests(tmp_path, {model: {"stats": {"attempts": 1}}})
    assert report["complete"] is valid
    index = json.loads((directory.parent / "request-index.json").read_text())
    assert index["files"][0]["sha256"] == hashlib.sha256(contents.encode()).hexdigest()
