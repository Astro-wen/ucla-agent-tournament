import asyncio
from copy import deepcopy
import json
import random

import httpx
import pytest

from altruagent import DecisionContext, GameState
from examples.llm.base import InvalidChoice, object_schema
from examples.llm.providers import ProviderError
from examples.llm_agent import LLMAgent
from local.werewolf_env import ContestantSeat, OfficialBaseline
from local.werewolf_research import ResearchGame
from scripts import werewolf_local_model_eval as lm


def response(answer, **extra):
    content = answer if isinstance(answer, str) else json.dumps(answer)
    return httpx.Response(200, json={"done": True, "done_reason": "stop", "model": lm.MODEL,
                                     "message": {"role": "assistant", "content": content},
                                     "prompt_eval_count": 900, "eval_count": 24, **extra})


def provider_for(answers, **kwargs):
    seen = []
    answers = iter(answers)

    def handle(request):
        seen.append(request)
        assert request.url.host == "127.0.0.1" and request.url.port == 11437
        assert "authorization" not in request.headers
        body = json.loads(request.content)
        assert "tools" not in body and "functions" not in body and body["stream"] is False
        answer = next(answers)
        return answer if isinstance(answer, httpx.Response) else response(answer)

    return lm.LocalProvider(transport=httpx.MockTransport(handle), **kwargs), seen


def test_model_scale_changes_request_identity_without_changing_default_model():
    cache = {}
    first, first_requests = provider_for([valid()], cache=cache)
    second, second_requests = provider_for([response(valid(), model="qwen2.5:14b-instruct-q4_K_M")], cache=cache,
                                          model="qwen2.5:14b-instruct-q4_K_M", expected_digest="a" * 64)
    raw = lm.guard_cases()[0][1]
    assert lm.LocalLLMSeat(first).choose(raw) == "2"
    assert lm.LocalLLMSeat(second).choose(raw) == "2"
    assert len(first_requests) == len(second_requests) == 1
    assert first.events[0]["key"] != second.events[0]["key"]
    assert json.loads(second_requests[0].content)["model"] == second.model
    assert first.model == lm.MODEL and first.expected_digest == lm.DIGEST
    assert lm.provider_summary(second)["model_digest"] == "a" * 64


def test_scale_reply_cannot_be_misattributed_to_the_requested_model():
    model = "qwen2.5:14b-instruct-q4_K_M"
    provider, requests = provider_for([response(valid()), response(valid(), model=model)],
                                      model=model, expected_digest="a" * 64)
    seat = lm.LocalLLMSeat(provider)
    assert seat.choose(lm.guard_cases()[0][1]) == "2"
    assert len(requests) == 2 and seat.stats["invalid_answers"] == 1
    assert "model" in provider.events[0]["error"]


@pytest.mark.parametrize("model,digest", [("remote-cloud", "a" * 64), (lm.MODEL, "invalid"), (lm.MODEL, "../hash")])
def test_scale_provider_rejects_unapproved_models_and_missing_pins(model, digest):
    with pytest.raises(ValueError):
        lm.LocalProvider(model=model, expected_digest=digest)


def valid(action="2"):
    return {"action_id": action, "reasoning_summary": "Vote for the named suspect."}


@pytest.mark.parametrize("case", lm.guard_cases(), ids=lambda c: c[0])
def test_known_wolf_clear_private_partner_and_three_alive(case):
    name, raw, expected = case
    provider, requests = provider_for([valid(expected)])
    seat = lm.LocalLLMSeat(provider)
    assert seat.choose(raw) == expected
    assert not isinstance(seat, ContestantSeat)
    assert type(seat.agent) is LLMAgent
    assert not seat.stats["fallbacks"]
    prompt = json.loads(json.loads(requests[0].content)["messages"][1]["content"])
    assert prompt["your_seat"] == raw["your_position"] == 0
    if name == "private_wolf_partner_night":
        assert "Your fellow wolves: Player4" in prompt["state"]
        assert "Player2 claimed Seer" in str(prompt["recent_messages"])
        assert "4" not in [x["action_id"] for x in prompt["legal_actions"]]


@pytest.mark.parametrize("bad", ["not JSON", '{"action_id":"2","action_id":"1","reasoning_summary":"x"}',
                                  {"action_id": "999", "reasoning_summary": "x"},
                                  {"action_id": 2, "reasoning_summary": "x"},
                                  {"action_id": "2", "reasoning_summary": "x", "extra": True},
                                  {"action_id": "2"}, "[]", "NaN"])
def test_invalid_json_wrong_id_types_and_extra_fields_retry(bad):
    provider, seen = provider_for([bad, valid()])
    seat = lm.LocalLLMSeat(provider)
    assert seat.choose(lm.guard_cases()[0][1]) == "2"
    assert len(seen) == 2
    assert seat.stats["invalid_answers"] == seat.stats["retries"] == 1
    assert seat.stats["fallbacks"] == 0
    assert "That answer was invalid" in json.loads(seen[1].content)["messages"][-1]["content"]
    assert provider.stats["errors"] == 1


def test_official_agent_independently_rejects_wrong_id():
    class Unchecked:
        def complete_structured(self, messages, schema_name, schema, **kwargs):
            return valid("999")

    raw = lm.guard_cases()[0][1]
    official = LLMAgent(Unchecked(), log=lambda _: None)
    choice = official._action_choice(GameState.from_mcp_state(raw))
    with pytest.raises(InvalidChoice):
        choice.build(valid("999"))
    result = official.choose_action(GameState.from_mcp_state(raw), DecisionContext("synthetic", None, "werewolf", "test", 0))
    assert result.action.action_id == "1"


def test_failed_calls_are_explicit_fallback_not_clean_llm():
    provider, seen = provider_for([valid("999"), "bad"])
    seat = lm.LocalLLMSeat(provider)
    assert seat.choose(lm.guard_cases()[0][1]) == "1"
    assert len(seen) == 2 and seat.stats["fallbacks"] == 1
    assert seat.stats["invalid_answers"] == 2 and provider.stats["errors"] == 2
    assert not provider.cache


def test_quota_and_fresh_per_game_official_memory():
    provider, seen = provider_for([{"decision": "send", "message": "I will compare votes.", "recipient": None}] * 2)
    seat = lm.LocalLLMSeat(provider)
    game = ResearchGame([OfficialBaseline() for _ in range(7)], random.Random(lm.SEED))
    raw = game.state(0, "messaging", "day_vote")
    assert seat.message(raw) is not None
    assert seat.message(raw) is not None
    assert seat.message(raw) is None
    assert seat.message(raw) is None
    assert len(provider.events) == 2 and seat.agent._round_requests == 2
    assert len(seen) == 2 and provider.stats["cache_hits"] == 0
    old = seat.agent
    seat.reset()
    assert seat.agent is not old and not seat.agent._transcript and not seat.agent._seen_messages
    assert seat.agent._round is None and not seat.agent._round_requests
    assert seat.message(raw) is not None


def test_long_message_uses_official_word_and_character_limits():
    provider, _ = provider_for([{"decision": "send", "message": "word " * 100, "recipient": None}])
    seat = lm.LocalLLMSeat(provider)
    game = ResearchGame([OfficialBaseline() for _ in range(7)], random.Random(lm.SEED))
    message = seat.message(game.state(0, "messaging", "day_vote"))
    assert len(message["content"].split()) == 40
    assert len(message["content"]) <= 280


def test_own_observation_boundary_and_private_message_isolation():
    game = ResearchGame([OfficialBaseline() for _ in range(7)], random.Random(lm.SEED))
    game.roles = ["villager", "wolf", "seer", "villager", "wolf", "villager", "villager"]
    game.invest = {1: "wolf"}
    game.messages = [
        {"seq": 0, "sender": 1, "recipients": [4], "visibility": "direct", "content": "unseen secret", "type": "chat"},
        {"seq": 1, "sender": 2, "recipients": [], "visibility": "broadcast", "content": "public text", "type": "chat"},
    ]
    raw = game.state(0, "messaging", "day_vote")
    provider, seen = provider_for([{"decision": "end", "message": "", "recipient": None}])
    seat = lm.LocalLLMSeat(provider)
    assert seat.message(raw) is None
    text = seen[0].content.decode()
    assert "unseen secret" not in text and "Your investigations" not in text
    assert "Your fellow wolves" not in text and "public text" in text
    raw["full_roles"] = game.roles
    with pytest.raises(ValueError, match="boundary"):
        seat.message(raw)
    del raw["full_roles"]
    raw["new_messages"] = [game.messages[0]]
    with pytest.raises(ValueError, match="private message"):
        seat.message(raw)
    assert len(seen) == 1


def test_exact_cache_includes_schema_order_options_and_messages():
    provider, seen = provider_for([valid()] * 4, order="original")
    messages = [{"role": "user", "content": "synthetic"}]
    a = object_schema({"action_id": {"type": "string", "enum": ["1", "2"]}})
    b = deepcopy(a)
    b["properties"] = dict(reversed(list(b["properties"].items())))
    assert provider.complete_structured(messages, "choose_action", a) == valid()
    assert provider.complete_structured(messages, "choose_action", a) == valid()
    provider.complete_structured(messages, "choose_action", b)
    provider.options["seed"] = 206
    provider.complete_structured(messages, "choose_action", b)
    provider.complete_structured([{"role": "user", "content": "different synthetic text"}], "choose_action", b)
    assert len(seen) == 4 and provider.stats["cache_hits"] == 1
    assert len({e["key"] for e in provider.events}) == 4


def test_schema_adaptation_preserves_semantics_and_is_not_cache_alias():
    provider, seen = provider_for([valid(), valid()])
    schema = object_schema({"action_id": {"type": "string", "enum": ["1", "2"]}})
    copy = deepcopy(schema)
    changed = provider.adapted_schema(schema)
    assert changed == schema == copy
    assert list(changed["properties"]) == ["reasoning_summary", "action_id"]
    assert list(schema["properties"]) == ["action_id", "reasoning_summary"]
    provider.complete_structured([{"role": "user", "content": "x"}], "action", schema)
    provider.order = "original"
    provider.complete_structured([{"role": "user", "content": "x"}], "action", schema)
    assert len(seen) == 2 and provider.events[0]["key"] != provider.events[1]["key"]


@pytest.mark.parametrize("endpoint", ["http://localhost:11437", "http://127.0.0.1:11434", "https://127.0.0.1:11437",
                                      "http://example.com:11437", "http://127.1:11437", "http://127.0.0.1:11437@evil.com",
                                      "http://user:pass@127.0.0.1:11437", "http://127.0.0.1:11437/evil",
                                      "http://[::1]:11437", "http://127.0.0.1:11437?x=1"])
def test_remote_endpoint_denial(endpoint):
    with pytest.raises(ValueError):
        lm.LocalProvider(endpoint=endpoint)


def test_redirect_not_followed_and_environment_authorization_not_read(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-unused")
    monkeypatch.setenv("HTTP_PROXY", "http://example.invalid")
    provider, seen = provider_for([httpx.Response(302, headers={"Location": "https://example.invalid"})])
    with pytest.raises(ProviderError, match="302"):
        provider.complete_structured([{"role": "user", "content": "x"}], "action", object_schema({"action_id": {"type": "string"}}))
    assert len(seen) == 1 and "authorization" not in seen[0].headers


def test_total_timeout_is_bounded_not_only_http_socket_timeout():
    async def slow(request):
        await asyncio.sleep(.1)
        return response(valid())

    provider = lm.LocalProvider(transport=httpx.MockTransport(slow), timeout=.01)
    with pytest.raises(ProviderError, match="TimeoutError"):
        provider.complete_structured([{"role": "user", "content": "x"}], "action", object_schema({"action_id": {"type": "string"}}))
    assert provider.stats["timeouts"] == 1
    assert provider.events[0]["latency_s"] < .1
    with pytest.raises(ValueError):
        lm.LocalProvider(timeout=51)


@pytest.mark.parametrize("text", ["界" * 2400, "x" * 7000, "...(truncated)"])
def test_context_refusal_counts_utf8_bytes_and_never_truncates(text):
    provider, seen = provider_for([])
    with pytest.raises(ProviderError, match="context refused"):
        provider.complete_structured([{"role": "user", "content": text}], "action", object_schema({"action_id": {"type": "string"}}))
    assert not seen and provider.stats["context_rejects"] == 1
    assert provider.events[0]["original_messages"][0]["content"] == text
    assert provider.input_byte_cap < 8192 - provider.options["num_predict"]


def test_output_exhaustion_refused():
    provider, _ = provider_for([response(valid(), done_reason="length")])
    with pytest.raises(ProviderError, match="token limit"):
        provider.complete_structured([{"role": "user", "content": "x"}], "action", object_schema({"action_id": {"type": "string"}}))
    assert provider.stats["output_truncations"] == 1


def test_deals_swaps_fixed_independent_of_profile_and_roles():
    plan = lm.scenario_plan(40)
    assert len(plan) == 40 and all(s["seed"] == 205 for s in plan)
    assert [s["llm_count"] for s in plan] == [1] * 12 + [2] * 12 + [1] * 8 + [2] * 8
    for one, two in zip(plan[:12], plan[12:24]):
        assert one["deal_seed"] == two["deal_seed"] and one["ours"] == two["ours"] and one["types"] == two["types"]
        assert set(one["llm_seats"]) <= set(two["llm_seats"])
        assert one["ours"] not in two["llm_seats"]
        expected = lm.ev.deal(205, one["game"], one["background"], 1)
        assert expected == (one["deal_seed"], [one["ours"]], one["types"])
    assert lm.scenario_plan(12) == plan[:12]
    assert "llm_real" not in str(plan)


def test_real_guard_runner_uses_both_orders_without_picking_winners():
    provider, seen = provider_for([valid()] * 10)
    quality = lm.run_quality(provider)
    assert len(quality) == 10 and all(q["legal"] and q["correct"] for q in quality)
    assert {q["order"] for q in quality} == {"original", "reason_first"}
    assert len(seen) == 10 and provider.order == "reason_first"


def test_summary_includes_fallback_attempts_but_excludes_dirty_pairs():
    plan = lm.scenario_plan(12)
    rows = []
    for profile, _ in lm.PROFILES:
        rows.append({"id": plan[0]["id"], "profile": profile, "scenario": plan[0], "seat": plan[0]["ours"],
                     "role": "seer", "roles": ["seer"] * 7, "win": profile != "agent", "clean": profile != "f08",
                     "failures": {}, "background_failures": {}, "llm_stats": {"fallbacks": int(profile == "f08")}})
    summary = lm.summarize(rows, plan)
    assert summary["completed_scenarios"] == 1 and summary["partial"]
    assert summary["metrics"]["llm1"]["all_attempts"]["f48"]["seer"]["n"] == 1
    assert summary["metrics"]["llm1"]["clean_paired_subset"]["f48"]["seer"]["n"] == 0
    assert summary["failures"]["f08"]["llm_fallbacks"] == 1


def official_request(raw):
    provider, _ = provider_for([valid(raw["legal_actions"]["actions"][0]["action_id"])])
    lm.LocalLLMSeat(provider).choose(deepcopy(raw))
    event = provider.events[0]
    return event["original_messages"], event["input_schema"]


@pytest.mark.parametrize("mode", lm.ENCODING_MODES)
def test_interface_encoding_is_reversible_and_raw_system_retry_unchanged(mode):
    raw = lm.guard_cases()[0][1]
    saved = deepcopy(raw)
    messages, schema = official_request(raw)
    messages.append({"role": "user", "content": "That answer was invalid: retry using the offered options."})
    originals = deepcopy((messages, schema))
    action = "Vote to eliminate Player2" if mode.startswith("label") else "2"
    provider, seen = provider_for([valid(action)], encoding=mode)
    answer = provider.complete_structured(messages, "choose_action", schema)
    assert answer["action_id"] == "2"
    assert raw == saved and (messages, schema) == originals
    wire = json.loads(seen[0].content)
    assert wire["messages"][0] == messages[0]
    assert wire["messages"][2:] == messages[2:]
    original = json.loads(messages[1]["content"])
    payload = json.loads(wire["messages"][1]["content"])
    if mode.endswith("object"):
        assert payload["state"] == json.loads(original["state"])
        payload["state"] = original["state"]
    if mode.startswith("label"):
        mapping = provider.events[0]["aliases"]
        for item in payload["legal_actions"]:
            assert item["action_id"] == item["label"]
            item["action_id"] = mapping[item["action_id"]]
        assert wire["format"]["properties"]["action_id"]["enum"] == list(mapping)
    assert payload == original
    assert list(wire["format"]["properties"])[0] == "reasoning_summary"
    assert wire["format"]["required"] == schema["required"]
    assert provider.events[0]["input_bytes"] == len(lm.dump(wire).encode())
    lm.validate_schema(answer, schema)


@pytest.mark.parametrize("bad_label", [None, "", " ", " bad ", 4, ["bad"], "Vote to eliminate Player2"])
def test_malformed_or_ambiguous_action_label_is_refused_without_call(bad_label):
    raw = deepcopy(lm.guard_cases()[0][1])
    raw["legal_actions"]["actions"][0]["label"] = bad_label
    messages, schema = official_request(raw)
    provider, seen = provider_for([], encoding="label_string")
    with pytest.raises(ProviderError, match="malformed or ambiguous"):
        provider.complete_structured(messages, "choose_action", schema)
    assert not seen and not provider.cache and provider.stats["errors"] == 1


def test_alias_decoding_never_uses_summary_or_accepts_unoffered_numeric_id():
    provider, seen = provider_for([valid("2"), {"action_id": "Vote to eliminate Player1", "reasoning_summary": "Vote Player2."}], encoding="label_object")
    seat = lm.LocalLLMSeat(provider)
    assert seat.choose(lm.guard_cases()[0][1]) == "1"
    assert len(seen) == 2 and seat.stats["invalid_answers"] == 1
    assert provider.events[1]["model_answer"]["action_id"] == "Vote to eliminate Player1"
    assert provider.events[1]["answer"]["action_id"] == "1"


@pytest.mark.parametrize("mode", ("label_string", "label_object"))
def test_label_aliases_do_not_change_message_decision_or_recipients(mode):
    provider, seen = provider_for([{"decision": "send", "message": "Vote Player2", "recipient": 1}], encoding=mode)
    game = ResearchGame([OfficialBaseline() for _ in range(7)], random.Random(lm.SEED))
    seat = lm.LocalLLMSeat(provider)
    answer = seat.message(game.state(0, "messaging", "day_vote"))
    assert answer == {"content": "Vote Player2", "recipients": [1]}
    event = provider.events[0]
    assert not event["aliases"] and event["input_schema"] == json.loads(seen[0].content)["format"]


def test_encoding_is_explicit_cache_identity_even_if_wire_request_unchanged():
    cache = {}
    first, a = provider_for([{"decision": "end", "message": "", "recipient": None}], encoding="numeric_string", cache=cache)
    second, b = provider_for([{"decision": "end", "message": "", "recipient": None}], encoding="label_string", cache=cache)
    game = ResearchGame([OfficialBaseline() for _ in range(7)], random.Random(lm.SEED))
    raw = game.state(0, "messaging", "day_vote")
    assert lm.LocalLLMSeat(first).message(raw) is None
    assert lm.LocalLLMSeat(second).message(raw) is None
    assert len(a) == len(b) == 1
    assert first.events[0]["key"] != second.events[0]["key"]
    assert first.events[0]["request"] == second.events[0]["request"]


@pytest.mark.parametrize("mode", lm.ENCODING_MODES)
def test_encoded_context_bytes_are_enforced_without_truncation(mode):
    raw = deepcopy(lm.guard_cases()[0][1])
    messages, schema = official_request(raw)
    payload = json.loads(messages[1]["content"])
    payload["state"] = json.dumps({"observation": "界" * 2500})
    messages[1]["content"] = json.dumps(payload, ensure_ascii=False)
    provider, seen = provider_for([], encoding=mode)
    with pytest.raises(ProviderError, match="context refused"):
        provider.complete_structured(messages, "choose_action", schema)
    assert not seen and provider.stats["context_rejects"] == 1
    state = json.loads(provider.events[0]["request"]["messages"][1]["content"])["state"]
    assert (state if isinstance(state, dict) else json.loads(state))["observation"] == "界" * 2500


def test_guard_rotations_are_predeclared_consistent_and_not_expected_truth_in_prompt():
    cases = lm.interface_guard_cases()
    assert len(cases) == 29 and sum(case["known_wolf"] for case in cases) == 8
    assert len({case["name"] for case in cases}) == 29
    assert lm.GUARD_ROTATIONS == (1, 2, 3, 4, 5, 6)
    assert len([case for case in cases if case["source"] == "prior_diagnostic"]) == 5
    for case in cases:
        raw = case["raw"]
        assert raw["your_position"] == case["rotation"]
        assert "expected" not in raw and "correct" not in raw
        assert {p["position"]: p["name"] for p in raw["players"]} == {i: f"agent{i}" for i in range(7)}
        actions = raw["legal_actions"]["actions"]
        assert case["expected"] in {a["action_id"] for a in actions}
        for action in actions:
            assert action["input"]["action_id"] == action["action_id"]
            assert action["label"] == "Abstain" if action["action_id"] == "7" else action["label"].endswith("Player" + action["action_id"])
        if case["known_wolf"]:
            assert f"Player{case['expected']} is wolf" in raw["observation"]


def test_selection_requires_all_known_wolves_and_80_percent_not_game_wins():
    rows = []
    for mode in lm.ENCODING_MODES:
        for case in lm.interface_guard_cases():
            rows.append({"case": case["name"], "encoding": mode, "known_wolf": case["known_wolf"], "correct": True})
    result = lm.select_encoding(rows)
    assert result["selected"] == "numeric_string"
    rows[0]["correct"] = False
    assert lm.select_encoding(rows)["selected"] == "label_string"
    for row in rows:
        if row["known_wolf"]:
            row["correct"] = False
    assert lm.select_encoding(rows)["selected"] is None
    assert lm.select_encoding(rows[:10])["selected"] is None
    cases = lm.interface_guard_cases()
    only_known = [{"case": c["name"], "encoding": "numeric_string", "known_wolf": c["known_wolf"],
                   "correct": c["known_wolf"]} for c in cases]
    assert lm.select_encoding(only_known)["selected"] is None


def test_terminated_observer_remembers_late_seer_at_strict_vote_without_calls_or_private_leak():
    class Speaker(OfficialBaseline):
        def __init__(self, content, recipients):
            self.content, self.recipients = content, recipients

        def message(self, raw):
            return {"content": self.content, "recipients": self.recipients}

    provider, seen = provider_for([{"decision": "end", "message": "", "recipient": None}, valid("2")])
    listener = lm.LocalLLMSeat(provider)
    dormant = lm.LocalLLMSeat(provider)
    seats = [listener, Speaker("I am the Seer. Player2 is wolf.", []), dormant,
             Speaker("Other seats' private secret", [4]), OfficialBaseline(), OfficialBaseline(), OfficialBaseline()]
    game = lm.ObservingResearchGame(seats, random.Random(lm.SEED), timing="rt", strict_messaging=True)
    game.roles = ["villager", "seer", "wolf", "villager", "villager", "villager", "wolf"]
    game.clock = 0
    sent = dict.fromkeys(range(7), 0)
    assert game._ask_message(0, sent) == "term"
    before = (len(provider.events), listener.agent._round_requests, listener.agent.message_calls, listener.stats["retries"])
    assert game._ask_message(1, sent) == "msg"
    assert game._ask_message(3, sent) == "msg"
    assert game._ask_message(4, sent) == "term"
    after = (len(provider.events), listener.agent._round_requests, listener.agent.message_calls, listener.stats["retries"])
    assert before == after == (1, 1, 1, 0)
    assert len(seen) == 1 and len(game.messages) == 2
    assert listener.stats["post_termination_updates"] == 4
    assert dormant.agent.message_calls == 0 and not dormant.agent._transcript
    assert "Player2 is wolf" in str(listener.agent._transcript)
    assert "private secret" not in str(listener.agent._transcript)
    raw = game.state(0, "moving", "day_vote", [1, 2, 3, 4, 5, 6, 7])
    assert raw["new_messages"] == []
    assert listener.choose(raw) == "2"
    prompt = json.loads(json.loads(seen[1].content)["messages"][1]["content"])
    assert "Player2 is wolf" in str(prompt["recent_messages"])
    assert "private secret" not in str(prompt)
    assert len(seen) == 2 and listener.stats["retries"] == 0


def test_observer_rejects_inconsistent_termination_before_any_extra_call():
    provider, seen = provider_for([])
    listener = lm.LocalLLMSeat(provider)
    game = lm.ObservingResearchGame([listener] + [OfficialBaseline() for _ in range(6)], random.Random(lm.SEED))
    game.terminated = [0]
    with pytest.raises(RuntimeError, match="inconsistent official round"):
        game._ask_message(1, dict.fromkeys(range(7), 0))
    assert not seen and not provider.events


def test_rt_discussion_delivers_late_claim_only_after_scheduled_slow_seat_speaks():
    from local.werewolf_env import SIM_HOLD

    class LateSeer(OfficialBaseline):
        def reset(self):
            self.calls = 0

        def message(self, raw):
            self.calls += 1
            if self.calls == 1:
                return SIM_HOLD
            if self.calls == 2:
                return {"content": "I am the Seer. Player2 is wolf.", "recipients": []}
            return None

    provider, seen = provider_for([{"decision": "end", "message": "", "recipient": None}, valid("2")])
    listener = lm.LocalLLMSeat(provider)
    late = LateSeer()
    game = lm.ObservingResearchGame([listener, late] + [OfficialBaseline() for _ in range(5)],
                                    random.Random(lm.SEED), timing="rt", strict_messaging=True)
    game.discussion()
    assert late.calls == 3 and len(game.messages) == 1 and game.messages[0]["sender"] == 1
    assert listener.agent.message_calls == listener.agent._round_requests == len(seen) == 1
    assert listener.stats["post_termination_updates"] >= 1 and listener.stats["retries"] == 0
    assert "Player2 is wolf" in str(listener.agent._transcript)
    assert listener.choose(game.state(0, "moving", "day_vote", [1, 2, 3, 4, 5, 6, 7])) == "2"
    assert "Player2 is wolf" in str(json.loads(json.loads(seen[1].content)["messages"][1]["content"])["recent_messages"])


def test_four_mode_sweep_covers_all_predeclared_cases_before_selection():
    answers = []
    for mode in lm.ENCODING_MODES:
        for case in lm.interface_guard_cases():
            ident = case["expected"]
            if mode.startswith("label"):
                ident = next(a["label"] for a in case["raw"]["legal_actions"]["actions"] if a["action_id"] == ident)
            answers.append(valid(ident))
    provider, seen = provider_for(answers)
    rows = lm.run_interface_quality(provider)
    assert len(rows) == len(seen) == 116 and all(row["correct"] for row in rows)
    assert all(row["order"] == "reason_first" for row in rows)
    assert lm.select_encoding(rows)["selected"] == "numeric_string"
    assert lm.select_encoding(rows[:87])["selected"] is None


@pytest.mark.parametrize("num_ctx", lm.CONTEXT_TOKENS)
@pytest.mark.parametrize("output_tokens", (32, 192, 256))
def test_requested_context_budget_scales_with_output_reserve(num_ctx, output_tokens):
    provider = lm.LocalProvider(num_ctx=num_ctx, output_tokens=output_tokens)
    assert provider.options["num_ctx"] == num_ctx
    assert provider.input_byte_cap == num_ctx - output_tokens - 1024
    with pytest.raises(ValueError, match="context"):
        lm.LocalProvider(num_ctx=32768)


@pytest.mark.parametrize("mode", lm.ENCODING_MODES)
def test_larger_context_accepts_full_utf8_payload_but_still_refuses_overflow(mode):
    messages, schema = official_request(lm.guard_cases()[0][1])
    payload = json.loads(messages[1]["content"])
    payload["state"] = json.dumps({"observation": "界" * 2500}, ensure_ascii=False)
    messages[1]["content"] = json.dumps(payload, ensure_ascii=False)
    originals = deepcopy(messages)
    small, no_calls = provider_for([], encoding=mode, num_ctx=8192)
    with pytest.raises(ProviderError, match="context refused"):
        small.complete_structured(messages, "choose_action", schema)
    action = "Vote to eliminate Player2" if mode.startswith("label") else "2"
    large, calls = provider_for([valid(action)], encoding=mode, num_ctx=16384)
    assert large.complete_structured(messages, "choose_action", schema) == valid()
    assert not no_calls and len(calls) == 1 and messages == originals
    wire = json.loads(calls[0].content)
    assert wire["options"]["num_ctx"] == 16384
    assert 6976 < large.events[0]["input_bytes"] <= 15168
    state = json.loads(wire["messages"][1]["content"])["state"]
    assert (state if isinstance(state, dict) else json.loads(state))["observation"] == "界" * 2500
    payload["state"] = json.dumps({"observation": "界" * 5500}, ensure_ascii=False)
    messages[1]["content"] = json.dumps(payload, ensure_ascii=False)
    with pytest.raises(ProviderError, match="context refused"):
        large.complete_structured(messages, "choose_action", schema)
    assert len(calls) == 1 and large.stats["context_rejects"] == 1


@pytest.mark.parametrize("mode", lm.ENCODING_MODES)
def test_all_encodings_cache_context_and_output_options_separately(mode):
    messages, schema = official_request(lm.guard_cases()[0][1])
    action = "Vote to eliminate Player2" if mode.startswith("label") else "2"
    cache, keys = {}, []
    for num_ctx, output_tokens in ((8192, 192), (16384, 192), (16384, 256)):
        provider, seen = provider_for([valid(action)], encoding=mode, num_ctx=num_ctx,
                                      output_tokens=output_tokens, cache=cache)
        assert provider.complete_structured(messages, "choose_action", schema) == valid()
        assert provider.complete_structured(messages, "choose_action", schema) == valid()
        assert len(seen) == 1 and provider.stats["cache_hits"] == 1
        keys.append(provider.events[0]["key"])
    assert len(set(keys)) == len(cache) == 3


@pytest.mark.parametrize("requested", lm.CONTEXT_TOKENS)
@pytest.mark.parametrize("actual", (4096, 8192, 16384, 32768))
def test_ps_verification_uses_requested_context_and_frozen_model(requested, actual):
    provider = lm.LocalProvider(num_ctx=requested)
    model = {"name": lm.MODEL, "digest": lm.DIGEST, "context_length": actual}
    health = {"/api/ps": {"models": [model]}}
    assert provider.context_verified(health) is (actual == requested)
    model["digest"] = "wrong-digest"
    assert not provider.context_verified(health)
    model["digest"], model["name"] = lm.DIGEST, "another-model"
    assert not provider.context_verified(health)


@pytest.mark.parametrize("flag,value", (("--context-tokens", "4096"), ("--context-tokens", "32768"),
                                       ("--encoding", "truth_forced")))
def test_cli_rejects_undeclared_context_or_encoding(flag, value):
    with pytest.raises(SystemExit) as exc:
        lm.main(["--out", "unused", flag, value])
    assert exc.value.code == 2


def test_cli_deadline_cannot_extend_past_0700_pdt(monkeypatch):
    monkeypatch.setattr(lm.time, "time", lambda: 0)
    with pytest.raises(SystemExit) as exc:
        lm.main(["--out", "unused", "--stop-at", "2026-10-13T07:00:01-07:00"])
    assert exc.value.code == 2


def test_fixed_followup_guards_cannot_reselect_or_claim_independent_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(lm, "ROOT", tmp_path)
    prior = tmp_path / lm.PRIOR_SELECTION
    prior.parent.mkdir(parents=True)
    prior.write_text(json.dumps({"selected": "label_object", "metrics": [{"correct": 25, "n": 29}]}))
    monkeypatch.setattr(lm.ev, "factory", lambda _: object())
    monkeypatch.setattr(lm, "source_manifest", lambda: {"digest": "frozen", "loaded_python_files": {}})
    monkeypatch.setattr(lm.time, "time", lambda: 0)
    monkeypatch.setattr(lm, "select_encoding", lambda _: pytest.fail("fixed follow-up must never reselect"))
    calls = []

    def handle(request):
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "test"})
        if request.url.path in ("/api/tags", "/api/ps"):
            return httpx.Response(200, json={"models": [{"name": lm.MODEL, "digest": lm.DIGEST,
                                                         "context_length": 16384}]})
        body = json.loads(request.content)
        calls.append(body)
        return response(valid(body["format"]["properties"]["action_id"]["enum"][0]))

    original = lm.LocalProvider
    monkeypatch.setattr(lm, "LocalProvider", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    out = tmp_path / "logs/werewolf_benchmarks/overnight-local-llm-test"
    assert lm.main(["--out", str(out), "--guards-only", "--encoding", "label_object",
                    "--context-tokens", "16384"]) == 0
    manifest = json.loads((out / "manifest.json").read_text())
    summary = json.loads((out / "summary.json").read_text())
    selection = summary["selection"]
    assert selection["selected"] == "label_object" and selection["mode"] == "fixed_cli_no_reselection"
    assert selection["provenance"]["matches_prior_selected"]
    assert selection["provenance"]["sha256"] == lm.hashlib.sha256(prior.read_bytes()).hexdigest()
    assert manifest["predeclared_selection"] == selection and manifest["source_frozen_before_requests"]
    assert not summary["independent_validation"] and summary["context_verified_end"]
    assert summary["status"] == "guards_complete" and summary["partial"]
    assert len(calls) == 30 and all(c["options"]["num_ctx"] == 16384 for c in calls)
    assert len(summary["quality"]) == 29 and summary["quality_failures"]
    assert all(q["source"] == "reused_v3_selection_diagnostic" and not q["independent_validation"]
               for q in summary["quality"])
    assert summary["provider"]["request_scope"]["network_calls"] == 30
    assert summary["game_request_scope"]["attempts"] == 0


def test_request_scope_distinguishes_underlying_requests_from_cache_and_refusals():
    provider, _ = provider_for([valid()])
    schema = object_schema({"action_id": {"type": "string", "enum": ["2"]}})
    for _ in range(3):
        provider.complete_structured([{"role": "user", "content": "x"}], "action", schema)
    with pytest.raises(ProviderError):
        provider.complete_structured([{"role": "user", "content": "x" * 8000}], "action", schema)
    scope = lm.request_scope(provider.events)
    assert scope["attempts"] == 4 and scope["network_calls"] == scope["unique_network_keys"] == 1
    assert scope["cache_hits"] == scope["unique_attempt_keys"] == 2
    assert sum(scope["errors"].values()) == 1
