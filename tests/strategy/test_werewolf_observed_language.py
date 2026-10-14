import json
from pathlib import Path

import pytest

from agent.werewolf.state import read_chat


ROOT = Path(__file__).resolve().parents[2]
AUDIT = json.loads((ROOT / "data/werewolf_local_chat_audit.json").read_text())
CASES = [case for case in AUDIT["guard_proposals"] if case["kind"] == "natural_missed_positive_with_literal_rewrite"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["sample_id"])
def test_literal_positive_rewrites_preserve_weak_evidence_without_fabricating_checks(case):
    facts = read_chat(case["literal_rewrite"], case["sender"])
    assert facts.sus == set(case["required_natural_sus"])
    assert not facts.claim and not facts.wolves and not facts.clears
    assert not facts.injection


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["sample_id"])
def test_quoting_a_supported_vote_does_not_make_it_the_speakers_own_vote(case):
    text = f'Example: "{case["literal_rewrite"]}" This is a quotation, not my vote.'
    facts = read_chat(text, case["sender"])
    assert not facts.claim and not facts.wolves and not facts.clears and not facts.sus


@pytest.mark.parametrize("seat", range(7))
def test_a_self_vote_in_language_is_not_an_eligible_other_target(seat):
    facts = read_chat(f"I vote Player{seat}.", seat)
    assert seat not in facts.sus
    assert not facts.wolves and not facts.clears


def test_literal_rewrite_guards_keep_their_manual_provenance():
    assert len(CASES) == 6
    assert all(case["rewrite_is_new_human_authored_not_model_observation"] for case in CASES)
