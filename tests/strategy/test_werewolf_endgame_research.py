import copy
import hashlib
import inspect
import itertools
import json
import random
import time
from collections import Counter
from dataclasses import replace

import pytest

from agent.werewolf.policy import WerewolfPolicy
from agent.werewolf.state import View
from local import werewolf_endgame_research as research
from local.werewolf_endgame_research import dawn_features, plurality, recommend
from local.werewolf_env import Game, OfficialBaseline


def msg(sender, target, seq=None, day=2):
    return {"sender": sender, "seq": sender if seq is None else seq, "day": day,
            "type": "chat", "visibility": "broadcast", "recipients": [],
            "content": f"I will vote Player{target}."}


def wolf_tactical_view():
    return View(me=0, role="wolf", day=2, sub_phase="day_vote", alive=[0, 1, 2, 3, 4],
                dead={5: "villager", 6: "villager"}, dead_cause={5: "vote", 6: "night_kill"}, allies=[1],
                messages=[msg(1, 2), msg(2, 1), msg(3, 1), msg(4, 2)])


def test_tactical_save_ally_by_competing_plurality_not_unconditional_pressure():
    view = wolf_tactical_view()
    result = recommend(view, (1, 2, 3, 4, 7), 1, side="wolf")
    assert result.action in (2, 7)
    assert result.lower_advantage >= 0.04
    assert plurality([1, 2, 1, 1, 2]) == 1
    assert plurality([2, 2, 1, 1, 2]) == 2
    assert plurality([7, 2, 1, 1, 2]) is None
    assert dawn_features(view.alive, (0, 1), 1) == (0.0, 1.0)
    assert dawn_features(view.alive, (0, 1), None) == (1.0, 0.0)


def test_tactical_good_even_four_tie_is_not_same_as_wrong_execution():
    alive, wolves = (0, 1, 2, 3), (1,)
    assert plurality([1, 2, 3, 2]) == 2
    assert plurality([3, 2, 3, 2]) is None
    assert dawn_features(alive, wolves, 2) == (1.0, 0.0)
    assert dawn_features(alive, wolves, None) == (0.0, 1.0)
    assert dawn_features(alive, wolves, 1) == (0.0, 0.0)
    view = View(me=0, role="seer", day=2, sub_phase="day_vote", alive=list(alive),
                dead={4: "villager", 5: "villager", 6: "wolf"}, dead_cause={}, investigations={2: "villager"},
                messages=[msg(1, 2), msg(2, 3), msg(3, 2)])
    assert recommend(view, (1, 2, 3, 7), 1, side="good").action == 1


@pytest.mark.parametrize("n", (3, 4, 5))
def test_exhaustive_plurality_counterfactual_tables(n):
    alive = tuple(range(n))
    options = [tuple(s for s in alive if s != voter) + (7,) for voter in alive]
    for ballots in itertools.product(*options):
        tally = {s: ballots.count(s) for s in alive}
        maximum = max(tally.values())
        expected = [s for s in alive if tally[s] == maximum and maximum > 0]
        assert plurality(ballots) == (expected[0] if len(expected) == 1 else None)
        for wolf_count in (1, 2):
            if wolf_count * 2 >= n:
                continue
            for wolves in itertools.combinations(alive, wolf_count):
                eliminated = plurality(ballots)
                survivors = set(alive) - ({eliminated} if eliminated is not None else set())
                live_wolves = set(wolves) & survivors
                actual = dawn_features(alive, wolves, eliminated)
                if not live_wolves:
                    assert actual == (0.0, 0.0)
                elif len(live_wolves) * 2 >= len(survivors):
                    assert actual == (1.0, 0.0)
                else:
                    outcomes = set()
                    for kill in survivors - live_wolves:
                        outcomes.add((1.0, 0.0) if len(live_wolves) * 2 >= len(survivors - {kill}) else (0.0, 1.0))
                    assert outcomes == {actual}


@pytest.mark.parametrize("text", [
    'Example: "I will vote Player2."', "I will not vote Player2.", "I won't vote Player2.",
    "I will vote Player2 unless the Seer objects.", "If I survive I will vote Player2.",
    "I might vote Player2.", "I withdraw my vote.", "I retract that vote.",
    "Did I say I will vote Player2?", "Everyone vote Player2.",
    "I will vote Player2. I will vote Player3.", "'I will vote Player2.'",
])
def test_quoted_negated_conditional_withdrawn_or_ambiguous_ballots_remove_commitment(text):
    view = wolf_tactical_view()
    view.messages = [msg(3, 1), dict(msg(3, 2, 9), content=text)]
    assert research.commitments(view) == {}
    assert recommend(view, (1, 2, 3, 4, 7), 1).action == 1


@pytest.mark.parametrize("noise", [
    {"sender": True}, {"sender": "3"}, {"sender": 9}, {"seq": True}, {"seq": -1}, {"seq": "4"},
    {"type": "terminate"}, {"visibility": "direct"}, {"recipients": [0]}, {"day": 3}, {"day": True},
    {"content": "x" * 1025}, {"content": "word " * 51}, {"content": None},
])
def test_invalid_chat_noise_cannot_create_commitment_or_change_result(noise):
    view = wolf_tactical_view()
    expected = recommend(view, (1, 2, 3, 4, 7), 1)
    view.messages += [dict(msg(3, 4, 99), **noise)]
    assert recommend(view, (1, 2, 3, 4, 7), 1) == expected


def test_messages_are_one_commitment_per_living_speaker_not_repetition():
    view = wolf_tactical_view()
    view.messages = [msg(2, 1, n) for n in range(5)]
    assert research.commitments(view) == {2: 1}
    assert recommend(view, (1, 2, 3, 4, 7), 1).action == 1
    view.messages += [msg(5, 1, 9), msg(3, 1, 10, day=1), msg(0, 2, 11)]
    assert research.commitments(view) == {2: 1}
    view.messages += [dict(msg(2, 3, 12), content="I will abstain.")]
    assert research.commitments(view) == {2: 7}
    view.messages += [msg(2, 4, 12)]
    assert research.commitments(view) == {2: 1}


def test_current_and_future_ballots_are_not_public_history_evidence():
    view = wolf_tactical_view()
    baseline = recommend(view, (1, 2, 3, 4, 7), 1)
    view.votes = [(2, {1: 2, 2: 1, 3: 1, 4: 2}, None), (3, {1: 2, 2: 1, 3: 1, 4: 2}, None)]
    assert recommend(view, (1, 2, 3, 4, 7), 1) == baseline


def test_public_history_only_calibrates_bounded_honoring_probability():
    view = wolf_tactical_view()
    view.messages += [msg(3, 5, 90, day=1)]
    view.votes = [(1, {3: 5}, 5)]
    assert 0.45 < research._reliability(view, 3, 0.45) < 1
    view.votes = [(1, {3: 4}, 5)]
    assert 0 < research._reliability(view, 3, 0.45) < 0.45
    assert research._reliability(view, 2, 0.45) == 0.45


@pytest.mark.parametrize("side", ("wolf", "good", "both"))
def test_minimum_advantage_budget_and_insufficient_evidence_preserve_reference(side):
    view = wolf_tactical_view()
    assert recommend(view, (1, 2, 3, 4, 7), 1, side=side, max_profiles=10).action == 1
    assert recommend(view, (1, 2, 3, 4, 7), 1, side=side, min_advantage=1.1).action == 1
    result = recommend(view, (1, 2, 3, 4, 7), 1, side=side)
    assert result.profiles <= 8000
    if result.action != 1:
        assert result.lower_advantage >= 0.04
    view.messages = []
    assert recommend(view, (1, 2, 3, 4, 7), 1, side=side).action == 1


def test_every_possible_ally_ballot_is_allowed_to_lie():
    view = wolf_tactical_view()
    results = []
    for target in (0, 2, 3, 4, 7):
        content = "I will abstain." if target == 7 else f"I will vote Player{target}."
        view.messages[0] = dict(msg(1, target), content=content)
        results.append(recommend(view, (1, 2, 3, 4, 7), 1))
    assert len({(result.action, result.lower_advantage) for result in results}) == 1


def test_no_new_ally_bus_and_only_current_legal_targets():
    view = wolf_tactical_view()
    for reference in (2, 3, 4, 7):
        result = recommend(view, (1, 2, 3, 4, 7), reference)
        assert result.action in (2, 3, 4, 7)
    for legal in ((1,), (1, 3), (1, 4, 7), (7,)):
        assert recommend(view, legal, legal[0]).action in legal


def test_good_worlds_do_not_assume_a_living_seer_claim_is_true():
    view = View(me=0, role="villager", day=3, sub_phase="day_vote", alive=[0, 1, 2, 3],
                dead={4: "wolf", 5: "villager", 6: "seer"}, dead_cause={})
    view.messages = [dict(msg(2, 1), content="I am the Seer. Player1 is a wolf.")]
    assert set(research.role_worlds(view)) == {(1,), (2,), (3,)}
    view.messages = [dict(msg(6, 1), content="I am the Seer. Player1 is a wolf.")]
    assert research.role_worlds(view) == ((1,),)
    view.messages += [dict(msg(6, 1, 9), content="Player1 is clear.")]
    assert research.role_worlds(view) == ()


def test_hard_seer_wolf_and_clears_take_priority_over_noisy_tactical_models():
    view = View(me=0, role="seer", day=3, sub_phase="day_vote", alive=[0, 1, 2, 3],
                dead={4: "wolf", 5: "villager", 6: "villager"}, dead_cause={},
                investigations={1: "wolf", 2: "villager"}, messages=[msg(1, 3, day=3), msg(2, 3, day=3), msg(3, 2, day=3)])
    assert recommend(view, (1, 2, 3, 7), 1, side="good").action == 1
    assert research.role_worlds(view) == ((1,),)
    view.investigations = {2: "villager"}
    assert set(research.role_worlds(view)) == {(1,), (3,)}
    assert recommend(view, (1, 2, 3, 7), 1, side="good").action != 2


@pytest.mark.parametrize("alive,dead,allies", [
    ([0, 1, 2], {3: "villager", 4: "villager", 5: "villager", 6: "seer"}, [1]),
    ([0, 1, 2, 3], {4: "villager", 5: "villager", 6: "seer"}, [1]),
    ([0, 1, 2, 3, 4], {5: "wolf", 6: "wolf"}, []),
    ([0, 1, 2, 3, 4], {5: "villager", 6: "seer"}, []),
    ([1, 2, 3, 4], {0: "wolf", 5: "villager", 6: "seer"}, [1]),
])
def test_terminal_or_inconsistent_role_states_are_not_optimized(alive, dead, allies):
    view = replace(wolf_tactical_view(), alive=alive, dead=dead, allies=allies)
    assert research.role_worlds(view) == ()
    assert recommend(view, (1, 2, 3, 4, 7), 1).action == 1


def test_three_stalls_are_terminal_before_next_night_and_night_is_not_optional():
    assert dawn_features((0, 1, 2, 3), (1,), None, 1) == (0.0, 1.0)
    assert dawn_features((0, 1, 2, 3), (1,), None, 2) == (1.0, 0.0)
    assert dawn_features((0, 1, 2), (1,), None) == (1.0, 0.0)
    view = replace(wolf_tactical_view(), sub_phase="night_wolf")
    assert recommend(view, (2, 3, 4), 2).action == 2


class ViewBoundary:
    allowed = {"me", "role", "day", "sub_phase", "alive", "dead", "dead_cause", "allies",
               "investigations", "votes", "messages"}

    def __init__(self, view):
        self.view = view

    def __getattr__(self, name):
        assert name in self.allowed, f"Not a permitted observation field: {name}"
        return getattr(self.view, name)


def test_information_boundary_determinism_and_no_input_mutation():
    view = wolf_tactical_view()
    before = copy.deepcopy(view)
    baseline = recommend(ViewBoundary(view), (1, 2, 3, 4, 7), 1)
    assert view == before
    view.hidden_roles = ["wolf"] * 7
    view.game_seed = 199
    view.controlled_seats = [0, 1, 3]
    view.opponent_types = ["candidate"] * 7
    view.names = {s: "candidate Player0" for s in range(7)}
    assert recommend(ViewBoundary(view), (7, 4, 3, 2, 1), 1) == baseline
    view.messages = list(reversed(view.messages)) * 3
    assert recommend(ViewBoundary(view), (1, 2, 3, 4, 7), 1) == baseline


def raw_tactical_state():
    game = Game([OfficialBaseline() for _ in range(7)], random.Random(197))
    game.roles = ["wolf", "wolf", "seer", "villager", "villager", "villager", "villager"]
    game.day = 2
    game.alive = [True] * 5 + [False, False]
    game.dead = [{"day": 1, "seat": 5, "role": "villager", "cause": "vote"},
                 {"day": 2, "seat": 6, "role": "villager", "cause": "night_kill"}]
    raw = game.state(0, "moving", "day_vote", [1, 2, 3, 4, 7])
    raw["new_messages"] = wolf_tactical_view().messages
    raw["new_messages"][0]["content"] = "I am the Seer. Player2 is a wolf. I will vote Player2."
    raw["new_messages"][1]["content"] = "I am the Seer. Player1 is a wolf. I will vote Player1."
    return raw


def test_wrapper_changes_actual_reference_bus_and_restarts_without_hidden_memory():
    raw = raw_tactical_state()
    original = WerewolfPolicy().act(raw)
    assert original["action_id"] == "1"
    policy = research.EndgamePolicy()
    changed = policy.act(raw)
    assert changed["action_id"] in ("2", "7")
    restored = research.EndgamePolicy()
    restored.import_memory(copy.deepcopy(policy.export_memory()))
    empty_transcript = dict(raw, new_messages=[])
    assert restored.act(empty_transcript) == changed
    assert research.wolf().ww.side == "wolf"
    assert research.good().ww.side == "good"
    for factory in (research.wolf, research.good, research.combined):
        agent = factory()
        agent._fresh()
        assert isinstance(agent.ww, research.EndgamePolicy)
        assert agent.ww.side == agent.side


def test_abstain_is_only_returned_if_legal_and_chat_never_announces_player7(monkeypatch):
    raw = raw_tactical_state()
    monkeypatch.setattr(research, "recommend", lambda *args, **kwargs: research.Recommendation(7, 0.1))
    policy = research.EndgamePolicy()
    assert policy.act(raw)["action_id"] == "7"
    raw["legal_actions"]["actions"] = [a for a in raw["legal_actions"]["actions"] if a["action_id"] != "7"]
    assert policy.act(raw)["action_id"] == "1"
    raw["phase"] = "messaging"
    messages = [m for _ in range(10) for m in policy.chat(raw)]
    assert messages
    assert all("Player7" not in m["content"] and len(m["content"].split()) <= 50 for m in messages)


def test_opponents_can_break_the_commitment_model_so_advantage_is_not_guarantee():
    view = wolf_tactical_view()
    choice = recommend(view, (1, 2, 3, 4, 7), 1).action
    deltas = []
    options = [tuple(s for s in view.alive if s != voter) + (7,) for voter in (1, 2, 3, 4)]
    for ballots in itertools.product(*options):
        a = dawn_features(view.alive, (0, 1), plurality((*ballots, choice)))
        b = dawn_features(view.alive, (0, 1), plurality((*ballots, 1)))
        deltas.append(a[0] + 0.5 * a[1] - b[0] - 0.5 * b[1])
    assert min(deltas) < 0 < max(deltas)


def test_serial_small_state_compute_budget():
    view = wolf_tactical_view()
    started = time.perf_counter()
    for _ in range(5):
        result = recommend(view, (1, 2, 3, 4, 7), 1)
        assert result.profiles <= 8000
    assert time.perf_counter() - started < 5.0


def test_good_component_refuses_two_hidden_wolves_in_five_player_states():
    view = replace(wolf_tactical_view(), me=4, role="villager", allies=[])
    assert research.role_worlds(view) == ()
    assert recommend(view, (0, 1, 2, 3, 7), 0, side="good").action == 0


def screen():
    import argparse
    import sys

    from local import eval_werewolf as ev
    from local import werewolf_benchmark as benchmark
    from local.werewolf_guard_bots import GUARD_BACKGROUNDS
    from local.werewolf_population_research import POPULATION_BACKGROUNDS

    parser = argparse.ArgumentParser()
    parser.add_argument("--screen", action="store_true", required=True)
    parser.add_argument("--games", type=int, default=20)
    parser.add_argument("--seeds", type=int, nargs="+", default=[197, 198, 199])
    parser.add_argument("--timings", nargs="+", choices=("rt", "rounds"), default=["rt", "rounds"])
    args = parser.parse_args()
    if not 1 <= args.games <= 100 or not set(args.seeds) <= {197, 198, 199}:
        parser.error("Exploratory screen is limited to 1..100 games and development seeds 197..199")
    backgrounds = (("first", "random", "heuristic", "llm", "llmx", "llmq", "mixed")
                   + tuple(GUARD_BACKGROUNDS) + tuple(ev.RT_BACKGROUNDS) + tuple(POPULATION_BACKGROUNDS))
    assert len(backgrounds) == len(set(backgrounds)) and all(not ev.uses_llm_real(bg) for bg in backgrounds)

    def hashes():
        return {name: hashlib.sha256(inspect.getsource(module).encode()).hexdigest()
                for name, module in (("policy", research), ("tests", sys.modules[__name__]))}

    frozen = hashes()
    global_start = benchmark.source_manifest()["digest"]
    print(json.dumps({"scope": "exploratory; k=1; combined equals side-isolated factories by role",
                      "local_frozen": frozen, "global_start": global_start, "config": vars(args),
                      "backgrounds": backgrounds}), flush=True)
    original_recommend = research.recommend
    interventions = Counter()
    active = [None, None]

    def measured(view, legal, reference, **kwargs):
        result = original_recommend(view, legal, reference, **kwargs)
        if result.action != reference:
            interventions[(active[0], active[1], view.role)] += 1
        return result

    research.recommend = measured
    records = {}
    failures = Counter()
    max_ms = 0.0
    started = time.perf_counter()
    try:
        for timing in args.timings:
            for background in backgrounds:
                active[:] = timing, background
                for seed in args.seeds:
                    for game in range(args.games):
                        scenario = benchmark.Scenario(seed, game, background, 1)
                        ref, candidate = benchmark.play_pair((scenario, "local.werewolf_endgame_research:combined", "agent", {},
                            {"ARENA_WW_TIMING": timing, "ARENA_RECORD": "0", "ARENA_STATE": "0", "ARENA_SUMMARY": "0"}, False))
                        assert "harness_error" not in ref and "harness_error" not in candidate, (ref, candidate)
                        assert ref["deal"] == candidate["deal"]
                        role = ref["deal"]["roles"][0]
                        before, after = int(ref["wins"][0]), int(candidate["wins"][0])
                        for key in ((timing, background, role), (timing, "ALL", role), (timing, str(seed), role)):
                            count = records.setdefault(key, Counter())
                            count.update(n=1, reference=before, candidate=after, improved=int(after > before), worsened=int(after < before))
                        for tag, record in (("reference", ref), ("candidate", candidate)):
                            for key, count in record["failures"].items():
                                failures[f"{tag}.{key}"] += count
                            for key, count in record["background_failures"].items():
                                failures[f"{tag}.background.{key}"] += count
                            max_ms = max(max_ms, record["max_ms"])
                print(json.dumps({"timing": timing, "background": background,
                    "roles": {role: dict(records.get((timing, background, role), {}))
                              for role in ("villager", "seer", "wolf")},
                    "overrides": {role: interventions[timing, background, role] for role in ("villager", "seer", "wolf")}}), flush=True)
    finally:
        research.recommend = original_recommend
    for timing in args.timings:
        for group in ("ALL", *(str(s) for s in args.seeds)):
            print(json.dumps({"timing": timing, "group": group,
                "roles": {role: dict(records.get((timing, group, role), {})) for role in ("villager", "seer", "wolf")}}), flush=True)
    final = hashes()
    print(json.dumps({"local_final": final, "local_unchanged": final == frozen,
                      "global_final": benchmark.source_manifest()["digest"], "failures": dict(failures),
                      "max_ms": max_ms, "elapsed_s": time.perf_counter() - started,
                      "pairs": args.games * len(args.seeds) * len(backgrounds) * len(args.timings)}), flush=True)
    assert final == frozen


if __name__ == "__main__":
    screen()
