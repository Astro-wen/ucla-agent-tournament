"""Label regressions from our own fea41303 decisions, not strength claims."""
import math

import pytest

from agent.agent import Contestant
from agent.hok import policy as hok
from altruagent import GameState
from altruagent.runner import _validate_decision
from tests.hok.test_policy import HOK, _entries, _summary, _unwrap, hok_payload


@pytest.fixture(autouse=True)
def recording_off(monkeypatch):
    monkeypatch.setenv("ARENA_RECORD", "0")
    monkeypatch.delenv("ARENA_HOK", raising=False)
    monkeypatch.delenv("ARENA_REASONING", raising=False)


@pytest.mark.parametrize("step,macro,label,distance", [
    (201, "macro_skill2", "[seat 3] Cast skill 2 toward enemy tower (73449 away)", 73449),
    (251, "macro_skill1", "[seat 3] Cast skill 1 toward enemy tower (41945 away)", 41945),
])
def test_recorded_far_skills_preserve_advance(step, macro, label, distance):
    # Exact labels in our fea41303 recording; the sparse samples do not prove
    # whether a cast dealt damage or provided useful movement.
    assert hok.distance_of(label) == distance
    assert hok.target_kind(label) == "structure"
    assert not hok._near_enough(label, hok.SKILL_RANGE)
    entries = _entries(3, macros=("macro_advance", "macro_retreat"))
    entries.append({"action_id": f"s3:{macro}", "label": label,
                    "input": {"action": {"macro": macro, "seat": 3}}})
    raw = hok_payload(version=step, pending=[3], entries=entries)
    state = GameState.from_mcp_state(raw)
    action = _unwrap(Contestant().choose_action(state, HOK))
    assert action == "s3:macro_advance"
    assert _validate_decision(action, state.legal_actions).action_id == action


@pytest.mark.parametrize("distance,expected", [
    (7999, "macro_skill2"), (8000, "macro_skill2"), (8001, "macro_advance"),
])
def test_real_distance_format_obeys_existing_skill_cutoff(distance, expected):
    assert hok.SKILL_RANGE == 8000
    macros = {"macro_advance": "Advance",
              "macro_skill2": f"[seat 3] Cast skill 2 toward enemy hero 169 ({distance} away)"}
    assert hok.rule_macro(_summary(3), macros) == expected


def test_toward_hero_label_allows_near_ultimate():
    # Contract-shaped ultimate label: early recorded heroes had no ultimate offer.
    label = "[seat 3] Cast skill 3 toward enemy hero 169 (6256 away)"
    assert hok.target_kind(label) == "hero"
    assert hok.target_is_hero(label)
    macros = {"macro_advance": "Advance", "macro_skill3": label}
    assert hok.rule_macro(_summary(3), macros) == "macro_skill3"


def test_toward_tower_label_keeps_ultimate_disallowed():
    label = "[seat 3] Cast skill 3 toward enemy tower (6256 away)"
    assert hok.target_kind(label) == "structure"
    assert not hok.target_is_hero(label)
    assert hok.rule_macro(_summary(3), {"macro_advance": "Advance",
                                        "macro_skill3": label}) == "macro_advance"


@pytest.mark.parametrize("target,kind", [
    ("enemy hero 169", "hero"), ("enemy tower", "structure"), ("enemy minion 3", None),
])
def test_legacy_nearest_comma_labels_unchanged(target, kind, monkeypatch):
    label = f"[seat 3] macro_skill2 (nearest: {target}, 6256 away)"
    for enabled in (True, False):
        monkeypatch.setattr(hok, "REAL_MACRO_LABELS", enabled)
        assert hok.distance_of(label) == 6256
        assert hok.target_kind(label) == kind
        assert hok.rule_macro(_summary(3), {"macro_advance": "Advance",
                                            "macro_skill2": label}) == "macro_skill2"


def test_real_label_switch_restores_previous_parsing(monkeypatch):
    monkeypatch.setattr(hok, "REAL_MACRO_LABELS", False)
    far = "[seat 3] Cast skill 2 toward enemy tower (73449 away)"
    assert math.isinf(hok.distance_of(far))
    assert hok.target_kind(far) is None
    assert hok._near_enough(far, hok.SKILL_RANGE)
    assert hok.rule_macro(_summary(3), {"macro_advance": "Advance",
                                        "macro_skill2": far}) == "macro_skill2"
    near_hero = "[seat 3] Cast skill 3 toward enemy hero 169 (6256 away)"
    assert not hok.target_is_hero(near_hero)
    assert hok.rule_macro(_summary(3), {"macro_advance": "Advance",
                                        "macro_skill3": near_hero}) == "macro_advance"
