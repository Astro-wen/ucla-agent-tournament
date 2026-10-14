from collections import defaultdict
from copy import deepcopy
from dataclasses import replace
import math
import time

import pytest

from agent.werewolf.policy import WerewolfPolicy
from agent.werewolf.state import View, read_chat
from local.werewolf_belief_research import DEALS, BeliefPolicy, constraints, inquisitive, seer_audit, tempered


FACTORIES = (constraints, tempered, inquisitive, seer_audit)


def view(role="villager", **changes):
    v = View(me=6, role=role, day=2, sub_phase="day_vote", alive=list(range(7)),
             dead={}, dead_cause={}, allies=[0] if role == "wolf" else [])
    return replace(v, **changes)


def messages(*lines):
    return [{"sender": sender, "content": text, "seq": i, "day": 2, "recipients": [], "type": "chat"}
            for i, (sender, text) in enumerate(lines)]


def raw(v):
    observation = f"Your role: {v.role.upper()}"
    if v.role == "wolf":
        observation += "\nYour fellow wolves: " + ", ".join(f"Player{i}" for i in v.allies)
    if v.investigations:
        observation += "\nYour investigations: " + ", ".join(
            f"Player{i} is {role}" for i, role in v.investigations.items())
    label = {"night_seer": "Investigate", "night_wolf": "Kill"}.get(v.sub_phase, "Vote to eliminate")
    targets = [s for s in v.others_alive if v.sub_phase != "night_wolf" or s not in v.allies]
    return {"game_type": "werewolf", "your_position": v.me, "phase": "moving", "observation": observation,
            "players": [{"position": s, "name": f"Player{s}"} for s in range(7)],
            "game_state": {"day": v.day, "phase": v.sub_phase, "alive": [f"Player{s}" for s in v.alive],
                           "dead": [{"player": f"Player{s}", "role": role, "cause": v.dead_cause.get(s, "vote")}
                                    for s, role in v.dead.items()],
                           "vote_history": [{"day": day,
                                             "votes": {f"Player{s}": f"Player{t}" if t is not None else "abstain"
                                                       for s, t in votes.items()},
                                             "eliminated": f"Player{elim}" if elim is not None else None}
                                            for day, votes, elim in v.votes]},
            "legal_actions": {"actions": [{"action_id": str(s), "label": f"{label} Player{s}"} for s in targets]
                              + ([{"action_id": "7", "label": "Abstain"}] if v.sub_phase == "day_vote" else [])},
            "new_messages": deepcopy(v.messages)}


def beliefs(v, factory=tempered):
    return factory().ww.beliefs(v)


def assert_distribution(b):
    assert b.consistent
    assert len(b.deals) == len(b.probabilities) <= 105
    assert all(math.isfinite(p) and p > 0 for p in b.probabilities)
    assert sum(b.probabilities) == pytest.approx(1)
    assert sum(b.wolf) == pytest.approx(2)
    assert sum(b.seer) == pytest.approx(1)
    assert all(0 <= p <= 1 for p in (*b.wolf, *b.seer))


def test_exact_deals_have_twenty_one_pairs_and_one_hundred_five_role_assignments():
    assert len(DEALS) == len(set(DEALS)) == 105
    assert len({deal.wolves for deal in DEALS}) == 21
    assert all(len(deal.wolves) == 2 and deal.seer not in deal.wolves for deal in DEALS)


@pytest.mark.parametrize("factory", FACTORIES)
@pytest.mark.parametrize("role,count", [("villager", 60), ("seer", 15), ("wolf", 5)])
def test_uniform_prior_is_normalized_and_respects_own_role(factory, role, count):
    b = beliefs(view(role), factory)
    assert_distribution(b)
    assert len(b.deals) == count
    assert b.probabilities == pytest.approx([1 / count] * count)
    assert b.wolf[6] == (1 if role == "wolf" else 0)
    if role == "wolf":
        assert b.wolf[0] == 1
        assert b.seer[1:6] == pytest.approx([0.2] * 5)
    else:
        assert b.wolf[:6] == pytest.approx([1 / 3] * 6)
        assert b.seer[6] == (1 if role == "seer" else 0)


def test_hard_only_posteriors_match_exhaustive_conditional_frequencies():
    groups = defaultdict(list)
    for deal in DEALS:
        role = "wolf" if 6 in deal.wolves else "seer" if deal.seer == 6 else "villager"
        dead_role = "wolf" if 0 in deal.wolves else "seer" if deal.seer == 0 else "villager"
        ally = tuple(s for s in deal.wolves if s != 6) if role == "wolf" else ()
        check = ("wolf" if 5 in deal.wolves else "villager") if role == "seer" else None
        groups[(role, dead_role, ally, check)].append(deal)
    for (role, dead_role, ally, check), deals in groups.items():
        v = view(role, alive=list(range(1, 7)), dead={0: dead_role}, allies=list(ally),
                 investigations={5: check} if check else {})
        b = beliefs(v, constraints)
        assert_distribution(b)
        assert set(b.deals) == set(deals)
        for s in range(7):
            assert b.wolf[s] == pytest.approx(sum(s in d.wolves for d in deals) / len(deals))
            assert b.seer[s] == pytest.approx(sum(s == d.seer for d in deals) / len(deals))


@pytest.mark.parametrize("factory", FACTORIES)
def test_hard_private_truth_overrides_claims_and_unbounded_pressure(factory):
    v = view("seer", investigations={1: "villager", 4: "wolf"},
             messages=messages(*([(1, "I am the Seer. Player4 is clear. Player2 is a wolf.")] * 30
                                 + [(3, "Player1 is a wolf. Vote Player1.")] * 30)))
    pol = factory().ww
    b = pol.beliefs(v)
    assert_distribution(b)
    assert b.wolf[1] == 0 and b.wolf[4] == 1
    assert pol._day_vote(v, [0, 1, 2, 3, 4, 5])[0] == 4
    assert pol.act(raw(v))["action_id"] == "4"
    assert pol.seer_knowledge(v) == ([4], [1])


@pytest.mark.parametrize("factory", FACTORIES)
def test_public_reveals_are_constraints_not_chat(factory):
    v = view(alive=[1, 2, 3, 5, 6], dead={0: "seer", 4: "wolf"},
             messages=messages((2, "I am the Seer. Player4 is clear. Player0 is a wolf.")))
    b = beliefs(v, factory)
    assert_distribution(b)
    assert b.seer[0] == 1 and b.seer[2] == 0
    assert b.wolf[0] == 0 and b.wolf[4] == 1
    assert b.wolf[2] < 1


@pytest.mark.parametrize("factory", FACTORIES)
def test_constraint_deduction_finds_last_wolf_without_promoting_a_liar(factory):
    v = view("seer", alive=[1, 2, 3, 4, 6], dead={0: "wolf", 5: "villager"},
             investigations={1: "villager", 2: "villager", 3: "villager"},
             messages=messages((1, "I am the Seer. Player2 is a wolf.")))
    pol = factory().ww
    b = pol.beliefs(v)
    assert b.hard_wolves == frozenset({0, 4})
    assert pol._day_vote(v, [1, 2, 3, 4])[0] == 4


def test_claims_alone_never_remove_feasible_deals_or_make_verified_wolves():
    empty = beliefs(view())
    v = view(messages=messages(*[(s, "I am the Seer. Player6 is a wolf.") for s in range(6)]))
    b = beliefs(v)
    assert_distribution(b)
    assert b.deals == empty.deals
    assert b.hard_wolves == frozenset()
    assert all(0 < b.wolf[s] < 1 for s in range(6))


def test_simple_contradictory_fake_claim_is_not_followed_or_amplified():
    lines = [(1, "I am the Seer. Player4 is a wolf. Player4 is clear.")]
    p = tempered().ww
    v = view(messages=messages(*lines))
    b = p.beliefs(v)
    assert b.wolf[1] > b.wolf[4]
    assert b.wolf[1] < 0.8
    assert p._day_vote(v, v.others_alive)[0] != 4
    assert not any("Following the Seer" in msg["content"] for msg in p.chat(raw(v)))
    assert beliefs(replace(v, messages=messages(*(lines * 50)))).probabilities == pytest.approx(b.probabilities)


@pytest.mark.parametrize("factory", FACTORIES)
def test_seer_never_announces_unchecked_liar_as_verified_wolf(factory):
    v = view("seer", investigations={2: "villager"},
             messages=messages((1, "I am the Seer. Player4 is a wolf.")))
    pol = factory().ww
    sent = pol.chat(raw(v))
    assert sent and any("I am the Seer" in m["content"] for m in sent)
    assert all(not read_chat(m["content"], 6).wolves for m in sent)
    assert all(len(m["content"].split()) <= 50 and len(m["content"].encode()) <= 1024 for m in sent)


@pytest.mark.parametrize("factory", FACTORIES)
def test_checked_villager_bluffer_is_never_a_day_target(factory):
    v = view("seer", investigations={1: "villager"},
             messages=messages((1, "I am the Seer. Player4 is a wolf.")))
    assert factory().ww._day_vote(v, v.others_alive)[0] != 1


def test_plausible_claim_is_evidence_but_not_a_verified_result():
    b = beliefs(view(messages=messages((1, "I am the Seer. Player4 is a wolf."))))
    assert b.wolf[4] > b.wolf[3]
    assert 0 < b.wolf[1] < 1 and b.wolf[4] < 0.9
    assert b.hard_wolves == frozenset()


def test_bus_vote_does_not_prove_innocence_and_duplicate_ballots_do_not_amplify():
    v = view(alive=list(range(1, 7)), dead={0: "wolf"}, votes=[(1, {1: 0, 2: 3}, 0)])
    b = beliefs(v)
    assert_distribution(b)
    assert 0 < b.wolf[1] < 1
    assert any(1 in d.wolves for d in b.deals)
    assert b.probabilities == pytest.approx(beliefs(replace(v, votes=v.votes * 100)).probabilities)


def test_each_voters_total_soft_weight_is_bounded():
    v = view(votes=[(d, {s: (s + d) % 7 for s in range(6)}, None) for d in range(1, 50)])
    b = beliefs(v)
    assert_distribution(b)
    assert max(b.probabilities) / min(b.probabilities) < 500


def test_own_actions_and_repeated_accusations_are_not_independent_evidence():
    v = view(messages=messages((3, "Player4 is a wolf.")))
    augmented = replace(v, votes=[(1, {6: 4}, None)],
                        messages=v.messages + messages(*[(6, "I vote Player4.")] * 20))
    assert beliefs(v).probabilities == beliefs(augmented).probabilities
    assert beliefs(view()).probabilities == beliefs(v).probabilities


@pytest.mark.parametrize("factory", FACTORIES)
def test_unrelated_private_fields_never_supply_village_truth(factory):
    v = view(investigations={1: "wolf", 2: "villager"}, allies=[3])
    assert beliefs(v, factory).probabilities == beliefs(view(), factory).probabilities


class Forbidden:
    def __getattribute__(self, name):
        raise AssertionError(f"forbidden private object was inspected: {name}")

    def __str__(self):
        raise AssertionError("forbidden private object was rendered")


@pytest.mark.parametrize("factory", FACTORIES)
def test_raw_private_information_boundary_and_input_immutability(factory):
    normal = raw(view(messages=messages((1, "I am the Seer. Player4 is a wolf."))))
    before = deepcopy(normal)
    changed = deepcopy(normal)
    for key in ("roles", "seed", "opponent_type", "other_observations", "simulator"):
        changed[key] = Forbidden()
        changed["game_state"][key] = Forbidden()
    for player in changed["players"]:
        player["role"] = Forbidden()
        player["opponent_type"] = Forbidden()
    p, q = factory().ww, factory().ww
    assert p.beliefs(p._view(normal)) == q.beliefs(q._view(changed))
    assert p.act(normal) == q.act(changed)
    assert p.chat(normal) == q.chat(changed)
    assert normal == before


@pytest.mark.parametrize("visibility,recipients", [("direct", []), ("broadcast", [2])])
def test_direct_messages_are_not_public_belief_evidence(visibility, recipients):
    v = view(messages=messages((1, "I am the Seer. Player4 is a wolf.")))
    v.messages[0].update(visibility=visibility, recipients=recipients)
    assert beliefs(v) == beliefs(view())


@pytest.mark.parametrize("factory", FACTORIES)
def test_restart_keeps_evidence_and_chat_deduplication_but_fresh_match_clears_it(factory):
    a, b = factory(), factory()
    first = raw(view("seer", investigations={2: "villager"},
                     messages=messages((1, "I am the Seer. Player4 is a wolf."))))
    a.ww.chat(first)
    b.ww.import_memory(deepcopy(a.ww.export_memory()))
    later = raw(view("seer", investigations={2: "villager"}, messages=[]))
    assert a.ww.beliefs(a.ww._view(later)) == b.ww.beliefs(b.ww._view(later))
    assert a.ww.act(later) == b.ww.act(later)
    assert a.ww.chat(later) == b.ww.chat(later) == []
    profile = b.ww.profile
    b._fresh()
    assert isinstance(b.ww, BeliefPolicy) and b.ww.profile == profile
    assert not b.ww.transcript and not b.ww._topics
    assert b.ww.beliefs(view()) == factory().ww.beliefs(view())


@pytest.mark.parametrize("factory", FACTORIES)
def test_fresh_instances_do_not_share_match_memory(factory):
    a, b = factory(), factory()
    a.ww.observe(raw(view(messages=messages((1, "I am the Seer. Player4 is a wolf.")))))
    assert not b.ww.transcript
    assert not factory().ww.transcript


@pytest.mark.parametrize("factory", FACTORIES)
@pytest.mark.parametrize("phase", ["day_vote", "night_wolf"])
@pytest.mark.parametrize("tactic", ["off", "public_vote", "no_skip"])
def test_wolf_actions_and_messages_remain_the_reference(factory, phase, tactic, monkeypatch):
    monkeypatch.setenv("ARENA_WW_TACTICS", tactic)
    v = view("wolf", sub_phase=phase, messages=messages((1, "I am the Seer. Player3 is a wolf.")))
    st = raw(v)
    a, b = factory().ww, WerewolfPolicy()
    assert a.act(st) == b.act(st)
    assert a.chat(st) == b.chat(st)


@pytest.mark.parametrize("factory", FACTORIES)
def test_inconsistent_hard_input_is_detected_and_still_returns_a_legal_action(factory):
    v = view("seer", dead={0: "seer"}, alive=list(range(1, 7)), investigations={4: "wolf"})
    p = factory().ww
    assert not p.beliefs(v).consistent
    assert not p.beliefs(v).deals
    assert p.act(raw(v))["action_id"] == "4"


@pytest.mark.parametrize("factory", FACTORIES)
def test_legal_subset_is_respected_even_if_beliefs_prefer_an_unavailable_wolf(factory):
    st = raw(view("seer", investigations={4: "wolf", 1: "villager"}))
    st["legal_actions"]["actions"] = [{"action_id": "1"}, {"action_id": "3"}]
    assert factory().ww.act(st)["action_id"] == "3"
    st["legal_actions"]["actions"] = [{"action_id": "7"}]
    assert factory().ww.act(st)["action_id"] == "7"


def test_hard_constraint_profile_and_soft_profile_are_distinct():
    v = view(messages=messages((1, "I am the Seer. Player4 is a wolf.")))
    assert constraints().ww._day_vote(v, v.others_alive)[0] == 0
    assert tempered().ww._day_vote(v, v.others_alive)[0] == 4


def test_information_profile_prefers_a_split_over_a_more_suspicious_claimant():
    v = view("seer", sub_phase="night_seer", investigations={1: "villager", 2: "villager"},
             messages=messages((3, "I am the Seer. Player4 is a wolf.")))
    a, b = tempered().ww, inquisitive().ww
    assert a.profile != b.profile
    belief = b.beliefs(v)
    targets = [3, 4, 5]
    entropy = lambda t: -sum(p * math.log(p) for p in (belief.wolf[t], 1 - belief.wolf[t]) if p)
    chosen = b._investigate(v, targets)[0]
    assert entropy(chosen) == pytest.approx(max(map(entropy, targets)))
    assert a._investigate(v, targets)[0] != chosen


@pytest.mark.parametrize("factory", FACTORIES)
def test_investigations_do_not_skip_unverified_liars_or_repeat_known_results(factory):
    v = view("seer", sub_phase="night_seer", investigations={1: "villager"},
             messages=messages((3, "I am the Seer. Player4 is a wolf.")))
    pol = factory().ww
    assert pol._investigate(v, [1, 3])[0] == 3
    assert pol._investigate(v, [1])[0] == 1
    assert pol._investigate(v, [])[0] is None


@pytest.mark.parametrize("factory", FACTORIES)
def test_unclaimed_wolf_public_vote_uses_unchanged_public_persona(factory, monkeypatch):
    monkeypatch.setenv("ARENA_WW_TACTICS", "public_vote")
    v = view("wolf", day=1, messages=messages((3, "Player4 is a wolf.")))
    st = raw(v)
    a, b = factory().ww, WerewolfPolicy()
    assert a.act(st) == b.act(st)
    assert a.chat(st) == b.chat(st)


def test_claim_rephrasing_and_cross_day_repetition_leave_weights_unchanged():
    v = view(messages=messages((1, "I am the Seer. Player4 is a wolf.")))
    other = replace(v, messages=messages((1, "I am the Seer. Player4 is a wolf."),
                                         (1, "I am the Seer. Player4 is definitely a werewolf."),
                                         (1, "Player4 is a wolf. Vote Player4.")))
    other.messages[0]["day"] = 1
    assert beliefs(v).probabilities == pytest.approx(beliefs(other).probabilities)


def test_revealed_seers_unverified_statements_are_still_soft():
    v = view(alive=[0, 2, 3, 4, 5, 6], dead={1: "seer"},
             messages=messages((1, "I am the Seer. Player4 is a wolf.")))
    b = beliefs(v)
    assert b.seer[1] == 1 and 0 < b.wolf[4] < 1
    assert b.hard_wolves == frozenset()


def test_soft_inference_is_equivariant_to_seat_permutations():
    v = view(alive=[0, 1, 2, 3, 4, 6], dead={5: "villager"},
             messages=messages((1, "I am the Seer. Player4 is a wolf.")),
             votes=[(1, {0: 5, 1: 3, 2: 5, 3: 2, 4: 1, 5: 0, 6: 5}, 5)])
    expected = beliefs(v)
    for shift in range(7):
        rename = lambda s: (s + shift) % 7
        changed = replace(v, me=rename(6), alive=[rename(s) for s in v.alive], dead={rename(5): "villager"},
                          votes=[(1, {rename(s): rename(t) for s, t in v.votes[0][1].items()}, rename(5))],
                          messages=messages((rename(1), f"I am the Seer. Player{rename(4)} is a wolf.")))
        actual = beliefs(changed)
        for seat in range(7):
            assert actual.wolf[rename(seat)] == pytest.approx(expected.wolf[seat])
            assert actual.seer[rename(seat)] == pytest.approx(expected.seer[seat])


@pytest.mark.parametrize("lines", [[], [(1, "I am the Seer. Player4 is a wolf.")],
                                   [(1, "I am the Seer. Player6 is a wolf.")],
                                   [(1, "I am the Seer. Player4 is a wolf. Player4 is clear.")]])
def test_seer_audit_changes_no_villager_decision_or_message(lines):
    v = view(messages=messages(*lines), dead={5: "villager"}, alive=[0, 1, 2, 3, 4, 6],
             votes=[(1, {s: 5 if s != 5 else 0 for s in range(7)}, 5)])
    a, b = seer_audit().ww, WerewolfPolicy()
    assert a.act(raw(v)) == b.act(raw(v))
    assert a.chat(raw(v)) == b.chat(raw(v))


def test_seer_audit_matches_tempered_seer_without_its_villager_changes():
    v = view("seer", messages=messages((1, "I am the Seer. Player4 is a wolf.")),
             investigations={2: "villager"})
    a, b = seer_audit().ww, tempered().ww
    assert a.act(raw(v)) == b.act(raw(v))
    assert a.chat(raw(v)) == b.chat(raw(v))
    assert a._investigate(v, v.others_alive) == b._investigate(v, v.others_alive)


def test_bounded_inference_runtime_and_no_mutation():
    v = view(messages=messages(*[(s, f"I am the Seer. Player{(s + 1) % 6} is a wolf.")
                                for _ in range(40) for s in range(6)]),
             votes=[(1, {s: (s + 1) % 7 for s in range(7)}, None)] * 30)
    before = deepcopy(v)
    pol = tempered().ww
    started = time.perf_counter()
    for _ in range(50):
        assert_distribution(pol.beliefs(v))
    assert time.perf_counter() - started < 5
    assert v == before
