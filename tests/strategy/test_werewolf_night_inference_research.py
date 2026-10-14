from collections import Counter
from copy import deepcopy
from dataclasses import replace
from itertools import product
import math

import pytest

from agent.werewolf.policy import WerewolfPolicy
from agent.werewolf.state import RoleEvidence, View, read_chat
from local.werewolf_night_inference_research import (
    MODELS, NOISE, POSTERIOR_GAP, WORLDS, Night, NightInferencePolicy,
    feasible, mixed_first, night_probability, posterior, random_min,
    random_min_probability, reconstruct_nights,
)


FACTORIES = (random_min, mixed_first)


def ballots(alive, target=None):
    return {s: target if s != target else min(t for t in alive if t != s) for s in alive}


def view(**changes):
    return replace(View(me=6, role="villager", day=1, sub_phase="day_vote",
                        alive=list(range(7)), dead={}, dead_cause={}), **changes)


def day_two():
    return view(day=2, alive=[1, 3, 4, 5, 6], dead={0: "villager", 2: "villager"},
                dead_cause={0: "vote", 2: "night_kill"},
                votes=[(1, {0: 3, 1: None, 2: None, 3: 0, 4: 0, 5: None, 6: None}, 0)])


def day_three():
    return view(day=3, alive=[1, 4, 6], dead={0: "villager", 2: "villager", 3: "wolf", 5: "seer"},
                dead_cause={0: "vote", 2: "night_kill", 3: "vote", 5: "night_kill"},
                votes=day_two().votes + [(2, ballots([1, 3, 4, 5, 6], 3), 3)])


def messages(*lines):
    return [{"seq": i, "sender": sender, "content": text, "day": 2,
             "visibility": "broadcast", "recipients": [], "type": "chat"}
            for i, (sender, text) in enumerate(lines)]


def raw(v):
    obs = f"Your role: {v.role.upper()}"
    if v.role == "wolf":
        obs += "\nYour fellow wolves: " + ", ".join(f"Player{s}" for s in v.allies)
    if v.role == "seer":
        obs += "\nYour investigations: " + ", ".join(f"Player{s} is {r}" for s, r in v.investigations.items())
    targets = [s for s in v.others_alive if v.sub_phase != "night_wolf" or s not in v.allies]
    label = {"night_wolf": "Kill", "night_seer": "Investigate"}.get(v.sub_phase, "Vote to eliminate")
    return {"game_type": "werewolf", "phase": "moving", "your_position": v.me, "observation": obs,
            "players": [{"position": s, "name": f"Player{s}"} for s in range(7)],
            "game_state": {"day": v.day, "phase": v.sub_phase, "alive": [f"Player{s}" for s in v.alive],
                           "dead": [{"player": f"Player{s}", "role": r, "cause": v.dead_cause[s]}
                                    for s, r in v.dead.items()],
                           "vote_history": [{"day": d, "votes": {f"Player{s}": f"Player{t}" if t is not None else "abstain"
                                                                 for s, t in votes.items()},
                                             "eliminated": f"Player{e}" if e is not None else None}
                                            for d, votes, e in v.votes]},
            "legal_actions": {"actions": [{"action_id": str(s), "label": f"{label} Player{s}"} for s in targets]
                              + ([{"action_id": "7", "label": "Abstain"}] if v.sub_phase == "day_vote" else [])},
            "new_messages": deepcopy(v.messages)}


def assert_distribution(b):
    assert b.worlds and len(b.worlds) == len(b.probabilities) <= 105
    assert all(math.isfinite(p) and p > 0 for p in b.probabilities)
    assert sum(b.probabilities) == pytest.approx(1)
    assert sum(b.wolf) == pytest.approx(2)
    assert sum(b.seer) == pytest.approx(1)
    assert all(0 <= p <= 1 for p in (*b.wolf, *b.seer))


def test_probability_normalization_and_all_105_joint_worlds():
    assert len(WORLDS) == len(set(WORLDS)) == 105
    assert len({w.wolves for w in WORLDS}) == 21
    for model in MODELS:
        for v in (view(), day_two(), day_three()):
            assert_distribution(posterior(v, model))


@pytest.mark.parametrize("w", [1, 2])
@pytest.mark.parametrize("targets", [[0], [1, 4], [0, 2, 3, 5, 6], [8, 12, 19]])
def test_w1_uniform_and_w2_minimum_law_by_enumeration(w, targets):
    outcomes = Counter(min(choices) for choices in product(targets, repeat=w))
    for target in targets:
        p = random_min_probability(target, targets, w)
        assert p == pytest.approx(outcomes[target] / len(targets) ** w)
        if w == 1:
            assert p == pytest.approx(1 / len(targets))
    assert sum(random_min_probability(t, targets, w) for t in targets) == pytest.approx(1)


@pytest.mark.parametrize("model", MODELS)
def test_noisy_night_likelihood_normalizes_for_every_world_and_living_wolf_count(model):
    for world in WORLDS:
        for alive in (frozenset(range(7)), frozenset(range(7)) - {world.wolves[0]}):
            targets = alive - set(world.wolves)
            values = [night_probability(Night(2, alive, t), world, model) for t in targets]
            assert sum(values) == pytest.approx(1)
            assert min(values) >= NOISE / len(targets)


@pytest.mark.parametrize("factory", FACTORIES)
def test_peaceful_first_night_and_unobserved_history_preserve_reference(factory):
    for v in (view(), view(day=2), replace(day_two(), votes=[])):
        p = factory().ww
        assert p._day_vote(v, v.others_alive) == WerewolfPolicy()._day_vote(v, v.others_alive)
        if v.day == 1:
            b = p.beliefs(v)
            assert b.nights == () and len(b.worlds) == 60
            assert b.probabilities == pytest.approx([1 / 60] * 60)
        else:
            assert p.beliefs(v).nights is None


@pytest.mark.parametrize("model", MODELS)
def test_private_and_public_role_facts_are_hard_but_night_preference_is_not(model):
    v = view(role="seer", investigations={1: "wolf", 2: "villager"},
             alive=[1, 2, 3, 4, 5, 6], dead={0: "wolf"}, dead_cause={0: "vote"})
    b = posterior(v, model)
    assert_distribution(b)
    assert b.hard_wolves == frozenset({0, 1})
    assert b.wolf[2] == b.wolf[6] == 0 and b.seer[6] == 1
    b = posterior(day_three(), model)
    assert b.hard_wolves == frozenset({3})
    assert b.wolf[1] < 1 and b.wolf[4] > 0
    assert b.seer[5] == 1
    assert len(b.worlds) == 2


def test_public_night_victim_is_hard_nonwolf_even_when_role_not_revealed():
    v = replace(day_two(), dead={0: "villager", 2: "?"})
    b = posterior(v)
    assert 2 in b.hard_clears and b.wolf[2] == 0
    assert all(2 not in w.wolves for w in b.worlds)
    contradictory = replace(v, dead={0: "villager", 2: "wolf"})
    assert not posterior(contradictory).worlds


def test_private_role_fields_are_used_only_for_their_owner_role():
    v = replace(day_two(), investigations={1: "wolf"}, allies=[4])
    assert posterior(v) == posterior(day_two())
    wolf = view(role="wolf", allies=[2])
    assert all(set(w.wolves) == {2, 6} for w in posterior(wolf).worlds)


@pytest.mark.parametrize("model", MODELS)
def test_abstaining_and_day_vote_targets_have_no_role_likelihood(model):
    v = day_two()
    changed = replace(v, votes=[(1, {s: None for s in range(7)}, 0)])
    # Explicit public elimination and voter set are authoritative. Even changing
    # every ballot to abstain cannot turn those targets into identity evidence.
    assert posterior(v, model) == posterior(changed, model)
    no_elim = view(day=2, alive=[0, 1, 3, 4, 5, 6], dead={2: "villager"},
                   dead_cause={2: "night_kill"}, votes=[(1, ballots(range(7)), None)])
    assert reconstruct_nights(no_elim) == (Night(2, frozenset(range(7)), 2),)


def test_history_reconstruction_uses_voter_sets_not_death_order_or_dates():
    v = day_three()
    expected = (Night(2, frozenset({1, 2, 3, 4, 5, 6}), 2), Night(3, frozenset({1, 4, 5, 6}), 5))
    assert reconstruct_nights(v) == expected
    unordered = replace(v, dead=dict(reversed(list(v.dead.items()))), votes=list(reversed(v.votes)),
                        dead_cause=dict(reversed(list(v.dead_cause.items()))))
    assert reconstruct_nights(unordered) == expected
    parsed = NightInferencePolicy()._view(raw(unordered))
    assert reconstruct_nights(parsed) == expected
    assert not hasattr(parsed, "death_dates")


@pytest.mark.parametrize("phase", ["night_wolf", "night_seer"])
def test_history_at_night_does_not_hallucinate_current_kill(phase):
    v = day_three()
    before_dawn = replace(v, sub_phase=phase, alive=[1, 4, 5, 6],
                          dead={s: r for s, r in v.dead.items() if s != 5},
                          dead_cause={s: r for s, r in v.dead_cause.items() if s != 5})
    assert reconstruct_nights(before_dawn) == (Night(2, frozenset({1, 2, 3, 4, 5, 6}), 2),)
    night_two = replace(day_two(), sub_phase=phase, alive=[1, 2, 3, 4, 5, 6],
                        dead={0: "villager"}, dead_cause={0: "vote"})
    assert reconstruct_nights(night_two) == ()
    assert reconstruct_nights(replace(v, sub_phase=phase)) is None


@pytest.mark.parametrize("case", ["missing_day", "duplicate_day", "missing_voter", "resign", "two_losses",
                                 "resurrection", "unknown_cause", "missing_current_day", "unknown_phase"])
def test_ambiguous_or_resigned_history_declines_inference(case):
    v = day_three()
    if case == "missing_day":
        v.votes = v.votes[1:]
    elif case == "duplicate_day":
        v.votes += v.votes[:1]
    elif case == "missing_voter":
        v.votes[0][1].pop(4)
    elif case == "resign":
        v.dead_cause[5] = "resign"
    elif case == "two_losses":
        v.votes[1][1].pop(5)
    elif case == "resurrection":
        v.votes[1][1][0] = None
    elif case == "unknown_cause":
        v.dead_cause.pop(5)
    elif case == "missing_current_day":
        v.day = None
    else:
        v.sub_phase = None
    assert reconstruct_nights(v) is None
    for factory in FACTORIES:
        assert factory().ww._day_vote(v, v.others_alive) == WerewolfPolicy()._day_vote(v, v.others_alive)


@pytest.mark.parametrize("model", MODELS)
def test_high_night_misspecification_has_positive_true_world_and_low_gap_fallback(model):
    v = view(day=2, alive=[1, 2, 3, 4, 6], dead={0: "villager", 5: "seer"},
             dead_cause={0: "vote", 5: "night_kill"}, votes=[(1, ballots(range(7), 0), 0)])
    # A high-target killer is neither hypothesized model. All legal worlds still
    # receive positive mass, and surviving seats remain tied rather than proven.
    b = posterior(v, model)
    assert_distribution(b)
    assert b.hard_wolves == frozenset()
    assert b.wolf[1:5] == pytest.approx([0.5] * 4)
    p = NightInferencePolicy()
    p.model = model
    assert p._day_vote(v, v.others_alive) == WerewolfPolicy()._day_vote(v, v.others_alive)


@pytest.mark.parametrize("model", MODELS)
def test_switching_models_never_zero_a_hard_feasible_world(model):
    v = day_three()
    b = posterior(v, model)
    assert_distribution(b)
    assert set(b.worlds) == {w for w in WORLDS if feasible(w, v)}
    assert all(night_probability(n, w, model) > 0 for n in b.nights for w in b.worlds)


@pytest.mark.parametrize("factory", FACTORIES)
def test_margins_are_fixed_and_weak_or_tied_posteriors_keep_base(factory):
    assert POSTERIOR_GAP == 0.10 and NOISE == 0.15
    v = day_two()
    p = factory().ww
    if p.model == "random_min":
        b = p.beliefs(v)
        assert 0 < b.wolf[1] - b.wolf[3] < POSTERIOR_GAP
        assert p._day_vote(v, v.others_alive) == WerewolfPolicy()._day_vote(v, v.others_alive)
    tied = replace(v, dead={0: "wolf", 2: "villager"})
    assert p._day_vote(tied, tied.others_alive) == WerewolfPolicy()._day_vote(tied, tied.others_alive)


@pytest.mark.parametrize("factory", FACTORIES)
def test_strong_two_night_inference_can_override_additive_vote_suspicion_without_hard_verdict(factory):
    v = day_three()
    p = factory().ww
    assert WerewolfPolicy()._day_vote(v, v.others_alive)[0] == 4
    result = p._day_vote(v, v.others_alive)
    assert result[0] == 1 and "not a verified role" in result[1]
    assert p.beliefs(v).hard_wolves == frozenset({3})
    assert p.act(raw(v))["action_id"] == "1"
    for message in p.chat(raw(v)):
        assert not read_chat(message["content"], v.me).wolves
        assert len(message["content"].split()) <= 50


@pytest.mark.parametrize("factory", FACTORIES)
@pytest.mark.parametrize("text", ["I am the Seer. Player4 is a wolf.", "I am the Seer. Player1 is clear.",
                                 "I am the Seer.", "I am the Seer. Player6 is a wolf."])
def test_other_claims_preserve_reference_including_contradictions_and_dead_seer(factory, text):
    for sender in (1, 5):
        v = replace(day_three(), messages=messages((sender, text)))
        a, b = factory().ww, WerewolfPolicy()
        assert a._day_vote(v, v.others_alive) == b._day_vote(v, v.others_alive)
        assert a.chat(raw(v)) == b.chat(raw(v))


@pytest.mark.parametrize("factory", FACTORIES)
@pytest.mark.parametrize("role,phase", [("wolf", "day_vote"), ("wolf", "night_wolf"),
                                       ("seer", "day_vote"), ("seer", "night_seer")])
def test_wolves_seer_investigation_private_result_priority_and_chat_unchanged(factory, role, phase):
    v = view(role=role, day=2, sub_phase=phase, allies=[0] if role == "wolf" else [],
             investigations={1: "wolf", 2: "villager"} if role == "seer" else {},
             messages=messages((3, "I am the Seer. Player2 is a wolf.")))
    a, b = factory().ww, WerewolfPolicy()
    assert a.act(raw(v)) == b.act(raw(v))
    assert a.chat(raw(v)) == b.chat(raw(v))
    if role == "seer" and phase == "day_vote":
        assert a.act(raw(v))["action_id"] == "1"


@pytest.mark.parametrize("factory", FACTORIES)
@pytest.mark.parametrize("evidence", [RoleEvidence(), RoleEvidence(observed=True, hint="villager"),
                                      RoleEvidence(text="villager", engine="seer")])
def test_unknown_or_conflicting_own_role_cannot_enable_override(factory, evidence):
    v = replace(day_three(), evidence=evidence)
    assert factory().ww._day_vote(v, v.others_alive) == WerewolfPolicy()._day_vote(v, v.others_alive)


class Forbidden:
    def __getattribute__(self, name):
        raise AssertionError(f"forbidden input read: {name}")

    def __str__(self):
        raise AssertionError("forbidden input rendered")


@pytest.mark.parametrize("factory", FACTORIES)
def test_no_hidden_seed_opponent_identity_or_role_access_and_no_input_mutation(factory):
    normal = raw(day_three())
    before = deepcopy(normal)
    altered = deepcopy(normal)
    for key in ("roles", "seed", "opponent_type", "other_observations", "simulator"):
        altered[key] = Forbidden()
        altered["game_state"][key] = Forbidden()
    for player in altered["players"]:
        player["role"] = player["opponent_type"] = Forbidden()
    a, b = factory().ww, factory().ww
    assert a.act(normal) == b.act(altered)
    assert a.chat(normal) == b.chat(altered)
    assert normal == before


@pytest.mark.parametrize("factory", FACTORIES)
def test_display_names_cannot_select_a_model_or_change_inference(factory):
    a, b = raw(day_three()), raw(day_three())
    rename = {f"Player{s}": f"arbitrary-{6-s}" for s in range(7)}
    for player in b["players"]:
        player["name"] = rename[player["name"]]
    gs = b["game_state"]
    gs["alive"] = [rename[s] for s in gs["alive"]]
    for d in gs["dead"]:
        d["player"] = rename[d["player"]]
    for row in gs["vote_history"]:
        row["votes"] = {rename[s]: rename.get(t, t) for s, t in row["votes"].items()}
        row["eliminated"] = rename.get(row["eliminated"])
    assert factory().ww.act(a) == factory().ww.act(b)


@pytest.mark.parametrize("factory", FACTORIES)
def test_legal_subset_and_abstain_only_remain_legal(factory):
    state = raw(day_three())
    state["legal_actions"]["actions"] = [{"action_id": "4"}]
    assert factory().ww.act(state)["action_id"] == "4"
    state["legal_actions"]["actions"] = [{"action_id": "7"}]
    assert factory().ww.act(state)["action_id"] == "7"


@pytest.mark.parametrize("factory", FACTORIES)
def test_restart_preserves_only_existing_transcript_and_new_match_resets(factory):
    a, b = factory(), factory()
    a.ww.observe(raw(replace(day_three(), messages=messages((5, "I am the Seer. Player4 is a wolf.")))))
    b.ww.import_memory(deepcopy(a.ww.export_memory()))
    assert a.ww.act(raw(day_three())) == b.ww.act(raw(day_three()))
    model = b.ww.model
    b._fresh()
    assert b.ww.model == model and not b.ww.transcript
    assert b.ww.act(raw(day_three()))["action_id"] == "1"
