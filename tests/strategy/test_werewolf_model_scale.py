from copy import deepcopy
import json

import httpx
import pytest

from agent.werewolf.policy import WerewolfPolicy
from scripts import werewolf_model_scale_eval as scale
from scripts import werewolf_local_model_eval as lm


@pytest.mark.parametrize("case", scale.validation_cases(), ids=lambda c: c["id"])
def test_new_validation_positions_have_legal_grounded_answers(case):
    raw = case["raw"]
    legal = {a["action_id"] for a in raw["legal_actions"]["actions"]}
    assert set(case["expected"]) <= legal
    assert str(case["seat"]) not in legal
    assert raw["new_messages"] == []
    assert "truth" not in raw and "expected" not in raw
    assert len(case["truth"]["roles"]) == 7
    assert case["truth"]["roles"].count("wolf") == 2
    assert case["truth"]["roles"].count("seer") == 1
    policy = WerewolfPolicy()
    if case["discussion"] is not None:
        policy.observe(case["discussion"])
    assert policy.act(raw)["action_id"] in case["expected"]


def test_option_order_is_an_information_preserving_paired_factor():
    cases = scale.validation_cases()
    assert len(cases) == 70 and len({c["cluster"] for c in cases}) == 35
    for first, second in zip(cases[::2], cases[1::2]):
        assert first["cluster"] == second["cluster"] and first["expected"] == second["expected"]
        a, b = deepcopy(first["raw"]), deepcopy(second["raw"])
        options = a.pop("legal_actions")["actions"]
        reversed_options = b.pop("legal_actions")["actions"]
        assert list(reversed(options)) == reversed_options
        assert a == b


@pytest.mark.parametrize("explicit", [False, True])
def test_rule_condition_adds_only_public_static_rules_not_gold_or_metadata(explicit):
    requests = []
    case = scale.validation_cases()[0]

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={"done": True, "done_reason": "stop", "model": lm.MODEL,
                            "message": {"role": "assistant", "content": json.dumps({"action_id": case["expected"][0], "reasoning_summary": "Use the private check."})}})

    provider = scale.RulesProvider(explicit_rules=explicit, transport=httpx.MockTransport(respond))
    result = scale.one_decision(provider, case)
    assert result["correct"] and result["legal"] and not result["fallback"]
    system = requests[0]["messages"][0]["content"]
    assert (scale.RULES in system) == explicit
    payload = json.loads(requests[0]["messages"][1]["content"])
    assert payload["your_seat"] == case["seat"]
    assert "expected" not in payload and "truth" not in payload and "family" not in payload
    assert case["cluster"] not in requests[0]["messages"][1]["content"]


@pytest.mark.parametrize("change", [{"pressure": 4}, {"swap_used_mib": 2025}, {"disk_free": 1024**3}])
def test_resource_stop_is_not_bypassed_for_bigger_models(change):
    base = {"pressure": 1, "swap_used_mib": 1000, "disk_free": 6 * 1024**3}
    assert scale.resource_safe(base, base)
    assert not scale.resource_safe(base | change, base)
    assert scale.resource_safe(base | {"pressure": 2}, base)


def test_scale_summary_counts_base_positions_not_option_permutations_as_clusters():
    cases = scale.validation_cases()
    rows = [{"id": c["id"], "cluster": c["cluster"], "family": c["family"], "seat": c["seat"], "order": c["order"],
             "model": model, "rules": "explicit", "correct": model == "qwen2.5:14b-x", "legal": True, "fallback": False}
            for c in cases for model in ("qwen2.5:7b-x", "qwen2.5:14b-x")]
    result = scale.summarize(rows, cases)
    contrast = next(iter(result["comparisons"].values()))
    assert contrast["n"] == 70 and contrast["clusters"] == 35 and contrast["mean"] == 1
