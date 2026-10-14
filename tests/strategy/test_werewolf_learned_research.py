from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import math
from pathlib import Path
import time

import pytest

from agent.werewolf.policy import WerewolfPolicy
from agent.werewolf.state import View, read_chat
from local.werewolf_belief_research import DEALS, role_of
from local.werewolf_learned_research import (
    FEATURES, HELD_FAMILIES, MODEL_PATH, PROFILES, TRAIN_FAMILIES, ActionOnlyPolicy, Example, LearnedPolicy, Model,
    action_only, calibration_report, clipped, feature_vector, fit_weights, learned, load_model, posterior,
    public_evidence, select_target, uniform_control, validate_training_split,
)


FACTORIES = (learned, clipped, uniform_control, action_only)


def view(role="villager", **changes):
    return replace(View(me=6, role=role, day=2, sub_phase="day_vote", alive=list(range(7)),
                        dead={}, dead_cause={}, allies=[0] if role == "wolf" else []), **changes)


def chats(*items):
    return [{"sender": s, "content": text, "seq": i, "day": 2, "type": "chat", "recipients": []}
            for i, (s, text) in enumerate(items)]


def raw(v):
    obs = f"Your role: {v.role.upper()}"
    if v.allies:
        obs += "\nYour fellow wolves: " + ", ".join(f"Player{s}" for s in v.allies)
    if v.investigations:
        obs += "\nYour investigations: " + ", ".join(f"Player{s} is {r}" for s, r in v.investigations.items())
    return {"your_position": v.me, "observation": obs, "phase": "moving",
            "players": [{"position": s, "name": f"Player{s}"} for s in range(7)],
            "game_state": {"day": v.day, "phase": v.sub_phase, "alive": [f"Player{s}" for s in v.alive],
                           "dead": [{"player": f"Player{s}", "role": r, "cause": "vote"} for s, r in v.dead.items()],
                           "vote_history": [{"day": day,
                                             "votes": {f"Player{s}": f"Player{t}" if t is not None else "abstain"
                                                       for s, t in votes.items()},
                                             "eliminated": f"Player{elim}" if elim is not None else None}
                                            for day, votes, elim in v.votes]},
            "new_messages": deepcopy(v.messages),
            "legal_actions": {"actions": [{"action_id": str(s)} for s in v.others_alive] + [{"action_id": "7"}]}}


def model(weights=(2.0, 0.3, 3.0, -0.2, -1.0, 0.5)):
    return Model(tuple(weights), "a" * 64, 20, "b" * 64)


def example(v, truth, baseline=0, family="first"):
    b = posterior(v, None)
    return Example(b.deals, tuple(feature_vector(public_evidence(v), d) for d in b.deals),
                   b.deals.index(truth), v.me, v.role, tuple(v.alive), baseline, (), family, 0, v.day)


def assert_posterior(b):
    assert b.consistent
    assert len(b.deals) <= 105
    assert all(math.isfinite(p) and p > 0 for p in b.probabilities)
    assert sum(b.probabilities) == pytest.approx(1)
    assert sum(b.wolf) == pytest.approx(2)
    assert sum(b.seer) == pytest.approx(1)


def test_fixed_small_feature_contract_and_disjoint_families():
    assert len(FEATURES) == 6 and len(set(FEATURES)) == 6
    assert set(TRAIN_FAMILIES).isdisjoint(HELD_FAMILIES)
    assert set(HELD_FAMILIES) == {"rt_detective", "pop_observed_switch"}
    assert PROFILES == {"learned": (1.0, 4.0), "clipped": (0.5, 2.0), "uniform_control": (0.0, 2.0)}


@pytest.mark.parametrize("seed", [203, 204, 33101, 33102, 28101])
def test_fitter_rejects_nontraining_seeds(seed):
    with pytest.raises(ValueError, match="202"):
        validate_training_split(seed, TRAIN_FAMILIES)


@pytest.mark.parametrize("family", HELD_FAMILIES)
def test_held_family_can_never_enter_training(family):
    with pytest.raises(ValueError, match="famil"):
        validate_training_split(202, (*TRAIN_FAMILIES, family))


def test_every_true_deal_remains_possible_despite_wrong_claims_and_bus_votes():
    for truth in DEALS:
        v = view(role_of(truth, 6), allies=[s for s in truth.wolves if s != 6] if 6 in truth.wolves else [],
                 dead={0: role_of(truth, 0)}, alive=list(range(1, 7)),
                 messages=chats(*[(s, "I am the Seer. Player6 is a wolf. Player4 is clear.") for s in range(6)]),
                 votes=[(1, {s: (s + 1) % 7 for s in range(7)}, None)])
        if v.role == "seer":
            v.investigations = {4: "wolf" if 4 in truth.wolves else "villager"}
        b = posterior(v, model((8.0, -8.0, 8.0, -8.0, -8.0, 8.0)))
        assert_posterior(b)
        assert truth in b.deals and b.probabilities[b.deals.index(truth)] > 0
        assert all(-1 <= f <= 1 for f in feature_vector(public_evidence(v), truth))


@pytest.mark.parametrize("profile", PROFILES)
def test_hard_truth_overrides_any_learned_likelihood(profile):
    v = view("seer", investigations={1: "villager", 4: "wolf"},
             messages=chats((1, "I am the Seer. Player4 is clear. Player2 is a wolf.")))
    pol = LearnedPolicy(profile=profile, model=model())
    b = pol.beliefs(v)
    assert_posterior(b)
    assert b.wolf[1] == 0 and b.wolf[4] == 1
    assert pol._day_vote(v, [0, 1, 2, 3, 4, 5])[0] == 4


def test_soft_claims_do_not_change_hard_feasible_worlds():
    a = view()
    b = view(messages=chats((1, "I am the Seer. Player4 is a wolf.")))
    assert posterior(a, model()).deals == posterior(b, model()).deals
    assert posterior(b, model()).hard_wolves == frozenset()
    assert posterior(b, model()).wolf[4] < 1


def test_repetitions_rephrasings_and_correlated_copies_cannot_amplify_likelihood():
    v = view(messages=chats((1, "I am the Seer. Player4 is a wolf.")))
    repeated = replace(v, messages=v.messages + chats(*([(1, "I am the Seer. Player4 is a werewolf.")] * 20
                                                       + [(2, "I am the Seer. Player4 is a wolf.")]
                                                       + [(3, "Player4 is a wolf.")] * 10)))
    assert public_evidence(v) == public_evidence(repeated)
    assert posterior(v, model()) == posterior(repeated, model())


def test_repeated_past_ballots_do_not_amplify_or_use_current_vote_intents():
    v = view(votes=[(1, {s: (s + 1) % 7 for s in range(7)}, None)])
    repeated = replace(v, votes=v.votes * 40,
                       messages=chats(*[(s, "I vote Player4.") for s in range(6)]))
    assert public_evidence(v) == public_evidence(repeated)
    assert posterior(v, model()) == posterior(repeated, model())


def test_abstention_alone_and_silence_have_no_role_likelihood():
    v = view(votes=[(1, {s: None for s in range(7)}, None)])
    a, b = posterior(v, model()), posterior(view(), model())
    assert a.probabilities == b.probabilities
    assert a.probabilities == pytest.approx([1 / 60] * 60)


def test_own_chats_and_ballots_do_not_feed_back_as_observed_evidence():
    v = view(votes=[(1, {6: 2}, None)], messages=chats((6, "I am the Seer. Player4 is a wolf.")))
    assert posterior(v, model()) == posterior(view(), model())


@pytest.mark.parametrize("updates", [{"visibility": "direct"}, {"recipients": [6]}])
def test_direct_messages_are_never_behavioral_features(updates):
    v = view(messages=chats((1, "I am the Seer. Player4 is a wolf.")))
    v.messages[0].update(updates)
    assert public_evidence(v) == public_evidence(view())


def test_seat_permutation_and_display_labels_do_not_change_learned_features():
    v = view(messages=chats((1, "I am the Seer. Player4 is a wolf.")),
             votes=[(1, {s: (s + 1) % 7 for s in range(7)}, None)])
    truth = next(d for d in DEALS if d.wolves == (2, 4) and d.seer == 1)
    expected = feature_vector(public_evidence(v), truth)
    for shift in range(7):
        rename = lambda s: (s + shift) % 7
        changed = replace(v, me=rename(6), names={s: f"opponent-private-label-{s}" for s in range(7)},
                          messages=chats((rename(1), f"I am the Seer. Player{rename(4)} is a wolf.")),
                          votes=[(1, {rename(s): rename((s + 1) % 7) for s in range(7)}, None)])
        deal = next(d for d in DEALS if d.wolves == tuple(sorted(rename(s) for s in truth.wolves))
                    and d.seer == rename(truth.seer))
        assert feature_vector(public_evidence(changed), deal) == pytest.approx(expected)


class Forbidden:
    def __getattribute__(self, name):
        raise AssertionError(f"private metadata read: {name}")

    def __str__(self):
        raise AssertionError("private metadata rendered")


def test_policy_ignores_hidden_metadata_and_does_not_mutate_observations():
    clean = raw(view(messages=chats((1, "I am the Seer. Player4 is a wolf."))))
    before = deepcopy(clean)
    poisoned = deepcopy(clean)
    for key in ("roles", "seed", "opponent_type", "simulator", "other_observations"):
        poisoned[key] = Forbidden()
        poisoned["game_state"][key] = Forbidden()
    for player in poisoned["players"]:
        player["role"] = Forbidden()
    a, b = LearnedPolicy(model=model()), LearnedPolicy(model=model())
    assert a.beliefs(a._view(clean)) == b.beliefs(b._view(poisoned))
    assert a.act(clean) == b.act(poisoned)
    assert a.chat(clean) == b.chat(poisoned)
    assert clean == before


def test_uniform_and_unconfident_selectors_preserve_baseline():
    b = posterior(view(), model(), strength=0)
    assert select_target(b, [0, 1, 2, 3, 4, 5], 4) == 4
    v = view("seer", investigations={4: "wolf", 1: "villager"})
    b = posterior(v, model())
    assert select_target(b, [0, 1, 2, 3, 4, 5], 1) == 4
    assert select_target(b, [1, 3], 1) == 3
    assert select_target(b, [], None) is None


@pytest.mark.parametrize("factory", FACTORIES)
def test_truthful_rebuttal_never_announces_unchecked_liar_as_wolf(factory):
    v = view("seer", investigations={2: "villager"},
             messages=chats((1, "I am the Seer. Player4 is a wolf.")))
    sent = factory().ww.chat(raw(v))
    assert sent
    assert all(not read_chat(m["content"], 6).wolves for m in sent)
    assert all(len(m["content"].split()) <= 50 for m in sent)


@pytest.mark.parametrize("factory", FACTORIES)
@pytest.mark.parametrize("checks", [{4: "wolf"}, {2: "villager"}, {}])
def test_ordinary_seer_chat_helpers_do_not_silently_fall_back(factory, checks):
    v = view("seer", investigations=checks)
    pol = factory().ww
    response = pol._seer_chat(v, pol.claims(v))
    if checks.get(4) == "wolf":
        assert response is not None and "Player4 is a WOLF" in response[1]["content"]
    sent = pol.chat(raw(v))
    assert sent


@pytest.mark.parametrize("factory", FACTORIES)
@pytest.mark.parametrize("tactic", ["off", "public_vote", "no_skip"])
def test_wolf_policy_stays_identical_even_with_public_persona(factory, tactic, monkeypatch):
    monkeypatch.setenv("ARENA_WW_TACTICS", tactic)
    v = view("wolf", day=1, messages=chats((3, "Player4 is a wolf.")))
    a, b = factory().ww, WerewolfPolicy()
    assert a.act(raw(v)) == b.act(raw(v))
    assert a.chat(raw(v)) == b.chat(raw(v))


@pytest.mark.parametrize("factory", FACTORIES)
def test_restart_fresh_match_and_immutable_parameters(factory):
    a, b = factory(), factory()
    v = view("seer", messages=chats((1, "I am the Seer. Player4 is a wolf.")))
    original_model = a.ww.model
    a.ww.chat(raw(v))
    b.ww.import_memory(deepcopy(a.ww.export_memory()))
    later = raw(replace(v, messages=[]))
    assert a.ww.act(later) == b.ww.act(later)
    assert a.ww.chat(later) == b.ww.chat(later) == []
    assert a.ww.model == original_model
    with pytest.raises(FrozenInstanceError):
        a.ww.model.weights = ()
    profile = a.ww.profile
    a._fresh()
    assert isinstance(a.ww, LearnedPolicy) and a.ww.profile == profile
    assert a.ww.model == original_model and not a.ww.transcript


def test_model_file_is_small_deterministic_and_contains_no_identity_or_training_observations():
    data = MODEL_PATH.read_bytes()
    assert len(data) < 16000
    payload = json.loads(data)
    assert set(payload) == {"schema_version", "features", "weights", "training_digest", "samples"}
    a, b = load_model(), load_model()
    assert a == b and a.sha256 == hashlib.sha256(data).hexdigest()
    assert len(a.weights) == 6 and all(math.isfinite(x) and abs(x) <= 8 for x in a.weights)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "secret", 1000, True])
def test_invalid_coefficients_fail_closed_without_partial_model(tmp_path, bad):
    data = json.loads(MODEL_PATH.read_text())
    data["weights"][0] = bad
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_model(path)


def test_runtime_does_not_reopen_or_update_model_or_access_training(monkeypatch):
    pol = LearnedPolicy(model=model())
    original = pol.model
    def forbidden(*args, **kwargs):
        raise AssertionError("runtime touched filesystem or offline trainer")
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr("local.werewolf_learned_research.fit_weights", forbidden)
    for _ in range(5):
        assert_posterior(pol.beliefs(view()))
        assert pol.act(raw(view()))["action_id"] in {str(s) for s in range(6)}
    assert pol.model == original


def test_missing_model_falls_back_to_the_uniform_control(monkeypatch):
    def missing(*args, **kwargs):
        raise OSError("missing fixed model")
    monkeypatch.setattr("local.werewolf_learned_research.load_model", missing)
    a, b = learned().ww, uniform_control().ww
    st = raw(view(messages=chats((1, "I am the Seer. Player4 is a wolf."))))
    assert a.act(st) == b.act(st)


@pytest.mark.parametrize("seed", [203, 204])
def test_fitter_rejects_validation_examples_even_when_family_is_familiar(seed):
    v = view()
    truth = posterior(v, None).deals[0]
    held = replace(example(v, truth), seed=seed)
    with pytest.raises(ValueError, match="202"):
        fit_weights([held], iterations=1)


def test_fitter_learns_signal_and_calibration_metrics_are_not_accuracy_only():
    samples = []
    for target in range(1, 6):
        v = view(messages=chats((0, f"I am the Seer. Player{target} is a wolf.")))
        for truth in DEALS:
            if truth.seer == 0 and target in truth.wolves and role_of(truth, 6) == "villager":
                samples.append(example(v, truth, baseline=0))
    weights, history = fit_weights(samples, iterations=100)
    assert weights == fit_weights(samples, iterations=100)[0]
    assert history[-1] < history[0]
    fitted = model(weights)
    scores = calibration_report(samples, fitted)
    assert scores["learned"]["wolf_brier"] < scores["uniform_control"]["wolf_brier"]
    assert scores["learned"]["world_log_loss"] < scores["uniform_control"]["world_log_loss"]
    assert scores["learned"]["true_world_mass"] > scores["uniform_control"]["true_world_mass"]
    for metrics in scores.values():
        assert {"wolf_brier", "wolf_log_loss", "world_log_loss", "true_world_mass", "ece",
                "map_wolf_hit_rate", "selector_wolf_hit_rate", "baseline_wolf_hit_rate",
                "switches", "fixed_ballot_improvements", "fixed_ballot_regressions"} <= metrics.keys()
        assert all(math.isfinite(metrics[k]) for k in ("wolf_brier", "wolf_log_loss", "world_log_loss", "ece"))
        assert 0 <= metrics["ece"] <= 1


def test_uniform_world_mass_and_calibration_values_have_an_independent_oracle():
    v = view()
    worlds = posterior(v, None).deals
    samples = [example(v, d) for d in worlds]
    metrics = calibration_report(samples, model())["uniform_control"]
    assert metrics["wolf_brier"] == pytest.approx(2 / 9)
    assert metrics["world_log_loss"] == pytest.approx(math.log(60))
    assert metrics["true_world_mass"] == pytest.approx(1 / 60)
    assert metrics["ece"] == pytest.approx(0, abs=1e-12)


def test_action_only_diagnostic_holds_speech_fixed_but_uses_identical_classifier():
    fitted = model((-6, 6, 0, -6, 0, 0))
    full = LearnedPolicy(model=fitted)
    action = ActionOnlyPolicy(model=fitted)
    control = LearnedPolicy(profile="uniform_control", model=fitted)
    st = raw(view(messages=chats((1, "I am the Seer. Player4 is a wolf."))))
    assert full.act(st) == action.act(st)
    assert action.act(st)["action_id"] != control.act(st)["action_id"]
    full_sent, action_sent, control_sent = full.chat(st), action.chat(st), control.chat(st)
    assert action_sent == control_sent
    assert full_sent != action_sent
    assert not action._reference_chat


def test_runtime_bound_and_valid_actions_under_long_duplicate_transcript():
    v = view(messages=chats(*([(1, "I am the Seer. Player4 is a wolf.")] * 200)),
             votes=[(1, {s: (s + 1) % 7 for s in range(7)}, None)] * 40)
    pol = LearnedPolicy(model=model())
    started = time.perf_counter()
    for _ in range(50):
        assert_posterior(pol.beliefs(v))
        assert pol._day_vote(v, v.others_alive)[0] in v.others_alive
    assert time.perf_counter() - started < 5
