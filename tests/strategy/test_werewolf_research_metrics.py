import copy
from itertools import product
import json
import math
import random
from types import SimpleNamespace

import pytest

from local import werewolf_research_metrics as metrics
from local.werewolf_env import Game, RandomSeat


ROLES = ["wolf", "wolf", "seer", "villager", "villager", "villager", "villager"]


def oracle_vote(ballots, roles=ROLES, stall=0):
    targets = [t for t in ballots.values() if t not in (None, 7)]
    leaders = [s for s in ballots if targets.count(s) == max((targets.count(t) for t in targets), default=0)]
    eliminated = leaders[0] if targets and len(leaders) == 1 else None
    living = [s for s in ballots if s != eliminated]
    wolves = sum(roles[s] == "wolf" for s in living)
    faction = None if eliminated is None else "wolf" if roles[eliminated] == "wolf" else "villager"
    winner = ("villager" if wolves == 0 else "wolf" if wolves * 2 >= len(living)
              or (eliminated is None and stall == 2) else None)
    return eliminated, faction, winner


def snapshot(ballots, nights=(), invest=None, messages=(), day=None, winner=None):
    votes, dead, stall = [], [], 0
    for number, ballot in enumerate(ballots, 1):
        eliminated = oracle_vote(ballot)[0]
        votes.append({"day": number, "votes": ballot, "eliminated": eliminated})
        if eliminated is not None:
            dead.append({"day": number, "seat": eliminated, "role": ROLES[eliminated], "cause": "vote"})
        stall = stall + 1 if eliminated is None else 0
    dead += [{"day": number, "seat": seat, "role": ROLES[seat], "cause": "night_kill"} for number, seat in nights]
    dead.sort(key=lambda d: (d["day"], d["cause"] == "vote"))
    alive = [not any(d["seat"] == s for d in dead) for s in range(7)]
    wolves = sum(alive[s] and ROLES[s] == "wolf" for s in range(7))
    return SimpleNamespace(roles=ROLES[:], alive=alive, day=day or len(votes), votes=votes, dead=dead,
                           winner=winner or ("villager" if wolves == 0 else "wolf"),
                           invest=invest or {}, messages=list(messages), stall=stall, stats={}, messaging=True)


def village_win():
    return snapshot([{0: 2, 1: 2, 2: 0, 3: 0, 4: 0, 5: 0, 6: 1},
                     {1: 3, 3: 1, 4: 1, 5: 1, 6: 1}], nights=[(2, 2)], invest={0: "wolf", 1: "wolf"})


def seer_day_death():
    return snapshot([{0: 2, 1: 2, 2: 0, 3: 2, 4: 2, 5: 2, 6: 2},
                     {0: 4, 1: 4, 4: 0, 5: 4, 6: 4}], nights=[(2, 3)], invest={0: "wolf"})


def night_terminal():
    return snapshot([{0: 3, 1: 3, 2: 0, 3: 0, 4: 3, 5: 3, 6: 3},
                     {0: None, 1: None, 2: None, 5: None, 6: None}],
                    nights=[(2, 4), (3, 2)], day=3, invest={0: "wolf", 1: "wolf"})


def broadcast(day, sender, content, **extra):
    return {"day": day, "sender": sender, "content": content, "visibility": "broadcast", **extra}


def trace(day, phase, choice, observed=None, kind="action"):
    return {"day": day, "phase": phase, "kind": kind, "choice": choice, "observed_checks": observed}


def test_dead_winning_seer_gets_team_score_and_only_living_vote_opportunities():
    report = metrics.game_metrics(village_win(), [2])
    assert report["outcome"]["seats"] == [{"seat": 2, "role": "seer", "team_win": 1,
                                              "team_score": 1, "survived": False}]
    assert report["votes"]["village_wolf_vote"] == metrics.ratio(1, 1)
    assert report["eliminations"]["controlled"]["seer"]["day"] == metrics.ratio(0, 1)
    assert report["eliminations"]["controlled"]["seer"]["night"] == metrics.ratio(1, 1)
    assert report["first_day_wolf_elimination"] == metrics.ratio(1, 1)
    assert report["terminal_reason"] == "no_wolves"
    assert report["seer"]["check_opportunities"] == 2


def test_surviving_loser_not_misreported_as_team_win_and_role_balance_not_seat_average():
    report = metrics.game_metrics(seer_day_death(), [0, 1, 2, 5, 6])
    assert report["outcome"]["seat_mean_win"] == pytest.approx(2 / 5)
    assert report["outcome"]["present_role_balanced_win"] == pytest.approx(1 / 3)
    assert report["outcome"]["seats"][-1]["survived"] is True
    assert report["outcome"]["seats"][-1]["team_score"] == -1
    assert report["terminal_reason"] == "parity"


def test_day_death_removes_future_night_and_day_denominators_and_first_night_is_peaceful():
    report = metrics.game_metrics(seer_day_death(), [2])
    assert report["eliminations"]["controlled"]["seer"] == {
        "day": metrics.ratio(1, 1), "night": metrics.ratio(0, 0), "ever_eliminated": metrics.ratio(1, 1)}
    assert report["eliminations"]["table"]["innocent"]["day"] == metrics.ratio(2, 8)
    assert report["eliminations"]["table"]["innocent"]["night"] == metrics.ratio(1, 4)
    assert report["seer"]["check_opportunities"] == 1
    assert report["first_day_wolf_elimination"] == metrics.ratio(0, 1)


def test_night_parity_has_no_nonexistent_day_vote_or_seer_survival_opportunity():
    report = metrics.game_metrics(night_terminal(), [2])
    assert (report["days"], report["vote_rounds"], report["terminal_reason"]) == (3, 2, "parity")
    assert report["votes"]["village_wolf_vote"] == metrics.ratio(1, 2)
    assert report["eliminations"]["controlled"]["seer"]["day"] == metrics.ratio(0, 2)
    assert report["eliminations"]["controlled"]["seer"]["night"] == metrics.ratio(1, 2)
    assert report["seer"]["check_opportunities"] == 3
    assert report["seer"]["unique_check_efficiency"] == metrics.ratio(2, 3)
    assert report["seer"]["repeat_checks"] == 1


def test_vote_tie_abstention_split_pressure_and_plurality_majority_margin_are_distinct():
    ballots = {0: 3, 1: 3, 2: 0, 3: 0, 4: 0, 5: 3, 6: 7}
    report = metrics.vote_diagnostics(ROLES, ballots)
    assert report["eliminated"] is None and report["tie"]
    assert report["village_wolf_vote"] == metrics.ratio(3, 5)
    assert report["village_wolf_vote_when_cast"] == metrics.ratio(3, 4)
    assert report["village_split_pressure"] == metrics.ratio(1, 4)
    assert report["village_wolf_split_pressure"] == metrics.ratio(0, 3)
    assert report["leader_margin"] == 0 and report["majority_margin"] == -1
    assert report["wolf_vs_innocent_margin"] == 0
    ballots[6] = 4
    ballots[5] = 1
    plurality = metrics.vote_diagnostics(ROLES, ballots)
    assert plurality["eliminated"] == 0
    assert plurality["leader_margin"] == 1 and plurality["majority_margin"] == -1
    assert plurality["village_wolf_split_pressure"] == metrics.ratio(1, 4)


def test_faction_pivotality_can_change_a_vote_without_changing_any_immediate_winner():
    report = metrics.vote_diagnostics(ROLES, {0: 3, 1: 3, 2: 0, 3: 0, 4: 0, 5: 3, 6: None}, [6])
    assert report["faction_pivotal"] == metrics.ratio(1, 1)
    assert report["immediate_terminal_pivotal"] == metrics.ratio(0, 1)
    assert report["immediate_winner"] is None


def test_disagreement_and_wolf_vote_do_not_imply_pivotality_or_prevented_loss():
    report = metrics.vote_diagnostics(ROLES, {0: 3, 1: 3, 2: 3, 3: 0, 4: 3, 5: 3, 6: 0}, [6])
    assert report["village_wolf_vote"] == metrics.ratio(1, 1)
    assert report["faction_pivotal"] == metrics.ratio(0, 1)
    assert report["immediate_terminal_pivotal"] == metrics.ratio(0, 1)
    assert "prevented" not in json.dumps(report)


def test_abstain_can_be_legal_pivotal_alternate_and_parity_is_immediate_only():
    report = metrics.vote_diagnostics(ROLES, {0: 3, 1: 3, 2: 0, 3: 0, 4: 3}, [4])
    assert report["immediate_winner"] == "wolf"
    assert report["faction_pivotal"] == report["immediate_terminal_pivotal"] == metrics.ratio(1, 1)
    last_wolf = metrics.vote_diagnostics(ROLES, {0: 2, 2: 0, 3: 0}, [3], prior_stall=2)
    assert last_wolf["immediate_winner"] == "villager"
    assert last_wolf["immediate_terminal_pivotal"] == metrics.ratio(1, 1)
    tied = metrics.vote_diagnostics(ROLES, {0: 2, 2: 0, 3: None}, prior_stall=2)
    assert tied["immediate_winner"] == "wolf"


def test_exhaustive_small_votes_match_independent_legal_alternate_oracle():
    living = [0, 2, 3, 4]
    for choices in product(*[[None] + [t for t in living if t != s] for s in living]):
        ballots = dict(zip(living, choices))
        for stall in (0, 2):
            actual = oracle_vote(ballots, stall=stall)
            faction_count = terminal_count = 0
            for seat in living:
                alternates = [oracle_vote(ballots | {seat: target}, stall=stall)
                              for target in [None] + [t for t in living if t != seat] if target != ballots[seat]]
                faction_count += any(r[1] != actual[1] for r in alternates)
                terminal_count += any(r[2] != actual[2] for r in alternates)
            report = metrics.vote_diagnostics(ROLES, ballots, prior_stall=stall)
            assert (report["eliminated"], report["eliminated_faction"], report["immediate_winner"]) == actual
            assert report["faction_pivotal"] == metrics.ratio(faction_count, 4)
            assert report["immediate_terminal_pivotal"] == metrics.ratio(terminal_count, 4)


@pytest.mark.parametrize("ballots", [{0: 0, 1: None}, {0: 2, 1: 0}, {0: -1, 1: 0}, {0: True, 1: 0}, {}])
def test_illegal_ballots_are_rejected(ballots):
    with pytest.raises(ValueError):
        metrics.vote_diagnostics(ROLES, ballots)


def test_all_abstain_is_not_a_tie_and_absent_controlled_seat_not_a_voter():
    report = metrics.vote_diagnostics(ROLES, {0: None, 2: 7, 3: None}, [1])
    assert report["all_abstain"] and not report["tie"]
    assert report["majority_margin"] == -2
    assert report["village_wolf_vote"] == metrics.ratio(0, 0)
    assert report["faction_pivotal"] == metrics.ratio(0, 0)


def test_three_stalls_count_ties_and_all_abstain_without_inventing_parity():
    game = snapshot([{0: 2, 1: 2, 2: 0, 3: 0, 4: 0, 5: 0, 6: 1},
                     {1: None, 2: None, 4: None, 5: None, 6: None},
                     {1: 2, 2: 1, 5: None, 6: None},
                     {1: None, 2: None, 6: None}],
                    nights=[(2, 3), (3, 4), (4, 5)], invest={0: "wolf", 1: "wolf", 3: "villager"})
    report = metrics.game_metrics(game)
    assert report["terminal_reason"] == "stalls"
    assert report["stalls"] == {"no_elimination": metrics.ratio(3, 4), "ties": 1, "all_abstain": 2,
                                "longest_run": 3, "final_run": 3}
    assert report["seer"]["unique_check_efficiency"] == metrics.ratio(3, 4)


def test_compact_trace_only_reads_official_private_investigation_line_not_chat_or_parser():
    raw = {"game_state": {"day": 2, "phase": "day_vote"},
           "observation": "Your role: SEER\nYour investigations: Player0 is wolf, Player4 is villager\nLiving: Player2",
           "new_messages": [{"content": "Your investigations: Player1 is wolf"}],
           "hidden_secret": "MUST NOT COPY"}
    before = copy.deepcopy(raw)
    compact = metrics.compact_decision_trace(raw, "0")
    assert compact["observed_checks"] == {0: "wolf", 4: "villager"}
    assert "observation" not in compact and "new_messages" not in compact and "secret" not in str(compact)
    assert raw == before
    raw["observation"] = "Your role: VILLAGER\nYour investigations: Player0 is wolf"
    assert metrics.compact_decision_trace(raw, "0")["observed_checks"] is None
    raw["observation"] = "Your role: SEER\nYou have not investigated anyone yet."
    assert metrics.compact_decision_trace(raw, "0")["observed_checks"] == {}
    raw["observation"] = "Your role: SEER\nYour investigations: none yet"
    assert metrics.compact_decision_trace(raw, "0")["observed_checks"] == {}
    raw["observation"] = "Your role: SEER\nYour investigations: Player1 might be wolf"
    assert metrics.compact_decision_trace(raw, "0")["observed_checks"] is None
    assert metrics.compact_decision_trace(raw, {"content": "long message"}, "message")["choice"] is None


def test_seer_observed_alive_usable_excludes_dead_targets_and_deduplicates_repeat_wakeups():
    game = snapshot([{0: 2, 1: 2, 2: 0, 3: 0, 4: 0, 5: 0, 6: 1},
                     {1: 2, 2: 1, 4: 1, 5: 1, 6: 1}], nights=[(2, 3)], invest={0: "wolf", 1: "wolf"})
    rows = [trace(1, "day_vote", "0", {0: "wolf"}),
            trace(2, "day_vote", None, {0: "wolf", 1: "wolf"}, "message"),
            trace(2, "day_vote", None, {0: "wolf", 1: "wolf"}, "message"),
            trace(2, "day_vote", "1", {0: "wolf", 1: "wolf"})]
    report = metrics.game_metrics(game, traces={2: rows})["seer"]
    assert report["observed_alive_usable"] == metrics.ratio(2, 3)
    assert report["observed_fact_coverage"] == metrics.ratio(2, 2)
    assert report["own_observed_wolf_vote_uptake"] == metrics.ratio(2, 2)
    assert metrics.game_metrics(game)["seer"]["own_observed_wolf_vote_uptake"] == metrics.ratio(0, 0)


def test_night_death_check_does_not_become_observed_usable_and_partial_traces_show_coverage():
    game = village_win()
    rows = [trace(1, "night_seer", "0"), trace(1, "day_vote", "0", {0: "wolf"}),
            trace(2, "night_seer", "1", {0: "wolf"})]
    report = metrics.game_metrics(game, traces={"2": rows})["seer"]
    assert report["unique_checks"] == 2
    assert report["observed_fact_coverage"] == metrics.ratio(1, 2)
    assert report["observed_alive_usable"] == metrics.ratio(1, 1)
    assert report["own_observed_wolf_vote_uptake"] == metrics.ratio(1, 1)
    with pytest.raises(ValueError, match="contradict"):
        metrics.game_metrics(game, traces={2: [trace(1, "day_vote", "0", {4: "wolf"})]})


def test_literal_claim_alignment_is_not_hard_information_or_causal_uptake():
    game = village_win()
    game.invest = {1: "wolf"}
    game.messages = [broadcast(1, 2, "I am the Seer. Player0 is a WOLF."),
                     broadcast(1, 0, "I am the seer. Player0 is a villager."),
                     broadcast(1, 1, "Seer here! Player0 is a wolf."),
                     broadcast(1, 3, "If I am the seer, I will say Player1 is a wolf."),
                     broadcast(1, 4, 'Player2 said "I am the seer."'),
                     broadcast(1, 5, "I am not the seer."),
                     broadcast(1, 6, "Seer claim: none yet"),
                     broadcast(1, 3, "I am the seer.", visibility="direct")]
    report = metrics.game_metrics(game)
    claims = report["claims"]
    assert claims["literal_claim_message_coverage"] == metrics.ratio(3, 7)
    assert claims["true_seer_literal_claim_day_rate"] == metrics.ratio(1, 1)
    assert claims["multiple_literal_claimant_day_rate"] == metrics.ratio(1, 2)
    assert claims["wolf_pair_double_literal_claim_rate"] == metrics.ratio(1, 1)
    assert claims["conflicting_literal_target_days"] == 1
    assert claims["nonseer_literal_claimant_days"] == 2
    assert claims["truthful_seer_report_vote_alignment"] == metrics.ratio(3, 4)
    assert report["seer"]["own_observed_wolf_vote_uptake"] == metrics.ratio(0, 0)
    assert report["seer"]["observed_fact_coverage"] == metrics.ratio(0, 1)


def test_forged_seer_wolf_claim_not_used_as_true_seer_alignment():
    game = village_win()
    game.messages = [broadcast(1, 1, "I am the Seer. Player0 is a wolf.")]
    report = metrics.game_metrics(game)["claims"]
    assert report["truthful_seer_report_vote_alignment"] == metrics.ratio(0, 0)
    assert report["true_seer_literal_claim_day_rate"] == metrics.ratio(0, 1)


def test_wolf_night_target_choices_exclude_peaceful_night_and_do_not_infer_missing_choices():
    game = night_terminal()
    traces = {0: [trace(1, "night_wolf", "2"), trace(2, "night_wolf", "4"), trace(3, "night_wolf", "2")],
              1: [trace(1, "night_wolf", "2"), trace(2, "night_wolf", "5"), trace(3, "night_wolf", "2")]}
    report = metrics.game_metrics(game, traces=traces)["night"]
    assert report["observed_choice_coverage"] == metrics.ratio(4, 4)
    assert report["observed_seer_target"] == metrics.ratio(2, 4)
    assert report["observed_civilian_target"] == metrics.ratio(2, 4)
    assert report["observed_wolf_disagreement"] == metrics.ratio(1, 2)
    assert report["seer_kill"] == report["civilian_kill"] == metrics.ratio(1, 2)
    partial = metrics.game_metrics(game, traces={0: traces[0]})["night"]
    assert partial["observed_choice_coverage"] == metrics.ratio(2, 4)
    assert partial["observed_wolf_disagreement"] == metrics.ratio(0, 0)
    assert "prevented" not in json.dumps(report)


def test_failed_or_illegal_action_trace_is_not_an_executed_night_target():
    game = night_terminal()
    game.stats = {"timeout": [1, 0, 0, 0, 0, 0, 0]}
    report = metrics.game_metrics(game, traces={0: [trace(2, "night_wolf", "2")],
                                               1: [trace(2, "night_wolf", "1")]})["night"]
    assert report["observed_choice_coverage"] == metrics.ratio(0, 4)
    assert report["observed_seer_target"] == metrics.ratio(0, 0)
    assert report["seer_kill"] == metrics.ratio(1, 2)


def test_empty_controlled_metrics_and_empty_aggregate_preserve_unknown_not_zero():
    report = metrics.game_metrics(village_win(), [])
    assert report["outcome"]["seat_mean_win"] is None
    assert report["votes"]["village_wolf_vote"] == metrics.ratio(0, 0)
    assert report["eliminations"]["controlled"]["innocent"]["day"] == metrics.ratio(0, 0)
    empty = metrics.summarize_outcomes([])
    assert empty["games"] == empty["covered_cells"] == 0
    assert empty["role_seat_balanced_win"] is empty["seat_weighted_win"] is None
    assert metrics.clustered_mean([], [])["ci95"] == [None, None]


def test_role_seat_balancing_reweights_cells_and_does_not_claim_full_balance_when_missing():
    reports = [{"outcome": {"seats": [{"seat": seat, "role": role, "team_win": int(role == "wolf")} ]}}
               for role in metrics.ROLES for seat in range(7)]
    reports += [{"outcome": {"seats": [{"seat": 0, "role": "wolf", "team_win": 1}]}}] * 21
    summary = metrics.summarize_outcomes(reports)
    assert summary["role_seat_balanced_win"] == pytest.approx(1 / 3)
    assert summary["seat_weighted_win"] == pytest.approx(2 / 3)
    missing = metrics.summarize_outcomes(reports[1:])
    assert missing["covered_cells"] == 21
    missing = metrics.summarize_outcomes(reports[7:])
    assert missing["role_seat_balanced_win"] is None
    assert missing["covered_cells"] == 15


@pytest.mark.parametrize("change", ["winner", "dead", "ballot", "voters", "days", "stall", "duplicate_control"])
def test_completed_game_audit_rejects_inconsistent_inputs(change):
    game = village_win()
    controlled = None
    if change == "winner":
        game.winner = None
    elif change == "dead":
        game.alive[2] = True
    elif change == "ballot":
        game.votes[0]["eliminated"] = 1
    elif change == "voters":
        game.votes[1]["votes"][2] = 1
    elif change == "days":
        game.votes[1]["day"] = 1
    elif change == "stall":
        game.stall = 1
    else:
        controlled = [2, 2]
    with pytest.raises(ValueError):
        metrics.game_metrics(game, controlled)


class TracedRandomSeat(RandomSeat):
    def reset(self):
        self.trace = []

    def choose(self, raw):
        choice = super().choose(raw)
        self.trace.append(metrics.compact_decision_trace(raw, choice))
        return choice


@pytest.mark.parametrize("seed", range(10))
def test_completed_engine_integration_is_pure_bounded_and_does_not_change_rng_or_policy_inputs(seed):
    traced = Game([TracedRandomSeat() for _ in range(7)], random.Random(seed), messaging=False)
    plain = Game([RandomSeat() for _ in range(7)], random.Random(seed), messaging=False)
    returns = traced.play()
    assert returns == plain.play()
    assert traced.roles == plain.roles and traced.votes == plain.votes and traced.dead == plain.dead
    before = copy.deepcopy((traced.roles, traced.dead, traced.votes, traced.invest, traced.messages, traced.stats,
                            traced.rng.getstate(), [s.rng.getstate() for s in traced.seats]))
    report = metrics.game_metrics(traced, traces={s: seat.trace for s, seat in enumerate(traced.seats)})
    assert [o["team_score"] for o in report["outcome"]["seats"]] == list(returns.values())
    assert report["seer"]["check_opportunities"] == sum(row["phase"] == "night_seer"
                                                        for seat in traced.seats for row in seat.trace)
    assert report["night"]["observed_choice_coverage"]["value"] in (None, 1)
    assert before == (traced.roles, traced.dead, traced.votes, traced.invest, traced.messages, traced.stats,
                      traced.rng.getstate(), [s.rng.getstate() for s in traced.seats])
    assert len(json.dumps(report, allow_nan=False)) < 15000
    assert "observation" not in json.dumps(report) and "content" not in json.dumps(report)


def factorial_rows(wolf_results, seed=99, scenario="x", factors=("A", "B"), seats=(0,), roles=("wolf",), values=None):
    result = []
    for i, corner in enumerate(product((0, 1), repeat=len(factors))):
        row = {"id": scenario, "deal": {"seed": seed, "seats": list(seats), "roles": list(roles), "background": "first"},
               "factors": dict(zip(factors, corner)),
               "wins": [int(wolf_results[i]) if role == "wolf" else 1 - int(wolf_results[i]) for role in roles]}
        if values is not None:
            row["diagnostic"] = values[i]
        result.append(row)
    return result


def test_factorial_main_effects_are_paired_and_interaction_is_difference_in_differences():
    rows = factorial_rows([0, 0, 0, 1]) + factorial_rows([0, 0, 0, 1], seed=100, scenario="y")
    report = metrics.factorial_contrasts(rows, ("A", "B"), expected_ids=["x", "y"])
    scope = report["scopes"]["wolf"]
    assert scope["cells"]["11"]["mean"] == 1
    assert scope["effects"]["A"]["mean"] == scope["effects"]["B"]["mean"] == 0.5
    assert scope["effects"]["A:B"]["mean"] == 1
    assert scope["effects"]["A:B"]["n"] == scope["effects"]["A:B"]["clusters"] == 2
    assert report["scopes"]["seer"]["effects"]["A"]["mean"] is None
    xor = metrics.factorial_contrasts(factorial_rows([0, 1, 1, 0]), ("A", "B"))["scopes"]["seat"]
    assert xor["effects"]["A"]["mean"] == 0
    assert xor["effects"]["A:B"]["mean"] == -2
    assert xor["effects"]["A:B"]["ci95"] == [None, None]


def test_three_factor_contrasts_average_other_component_and_support_nonbinary_diagnostics():
    values = [2 + 3 * a - 4 * b + 5 * a * b + 7 * c for a, b, c in product((0, 1), repeat=3)]
    rows = factorial_rows([0] * 8, factors=("A", "B", "C"), values=values)
    effects = metrics.factorial_contrasts(rows, ("A", "B", "C"), metric="diagnostic")["scopes"]["seat"]["effects"]
    assert {name: e["mean"] for name, e in effects.items()} == {
        "A": 5.5, "B": -1.5, "C": 7, "A:B": 5, "A:C": 0, "B:C": 0}


def test_factorial_role_conditioning_does_not_count_same_side_seats_as_independent_samples():
    rows = factorial_rows([0, 0, 0, 1], seats=(0, 1, 2, 3), roles=("wolf", "wolf", "seer", "villager"))
    scopes = metrics.factorial_contrasts(rows, ("A", "B"))["scopes"]
    assert scopes["wolf"]["effects"]["A:B"]["mean"] == 1
    assert scopes["seer"]["effects"]["A:B"]["mean"] == -1
    assert scopes["villager"]["effects"]["A:B"]["mean"] == -1
    assert scopes["seat"]["effects"]["A:B"]["mean"] == 0
    assert scopes["role_balanced"]["effects"]["A:B"]["mean"] == pytest.approx(-1 / 3)
    assert all(scope["effects"]["A:B"]["n"] == 1 for scope in scopes.values())


def test_cluster_standard_error_matches_hand_calculation_and_shared_deal_duplication_adds_no_precision():
    direct = metrics.clustered_mean([1, 1, 0, 0], [4, 4, 5, 5])
    assert direct == {"n": 4, "clusters": 2, "mean": 0.5, "se": 0.5, "ci95": [-0.48, 1.48]}
    rows = factorial_rows([0, 0, 0, 1]) + factorial_rows([0, 0, 0, 0], seed=100, scenario="y")
    original = metrics.factorial_contrasts(rows, ("A", "B"))["scopes"]["seat"]["effects"]["A:B"]
    duplicated = rows + [dict(r, id=r["id"] + "-other-timing") for r in rows]
    repeated = metrics.factorial_contrasts(duplicated, ("A", "B"))["scopes"]["seat"]["effects"]["A:B"]
    assert original["ci95"] == repeated["ci95"] == [-0.48, 1.48]
    assert original["clusters"] == repeated["clusters"] == 2
    assert repeated["n"] == 4
    assert metrics.clustered_mean([0, 2, 4], [1, 1, 2])["se"] == pytest.approx(4 / 3)


@pytest.mark.parametrize("change", ["missing", "duplicate", "roles", "seat", "deal_seed", "background",
                                     "factor", "wins", "faction_score", "error", "nan", "cluster_role"])
def test_factorial_audit_rejects_incomplete_or_unpaired_or_nonfinite_evidence(change):
    rows = factorial_rows([0, 0, 0, 1], seats=(0, 1), roles=("wolf", "wolf"), values=[0, 0, 0, 1])
    if change == "missing":
        rows.pop()
    elif change == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    elif change == "roles":
        rows[0]["deal"]["roles"][0] = "seer"
    elif change == "seat":
        rows[0]["deal"]["seats"][0] = 4
    elif change == "deal_seed":
        rows[0]["deal"]["seed"] = 44
    elif change == "background":
        rows[0]["deal"]["background"] = "random"
    elif change == "factor":
        rows[0]["factors"]["A"] = "0"
    elif change == "wins":
        rows[0]["wins"] = []
    elif change == "faction_score":
        rows[0]["wins"] = [0, 1]
    elif change == "error":
        rows[0]["harness_error"] = "failed"
    elif change == "nan":
        rows[0]["diagnostic"] = math.nan
    else:
        rows += factorial_rows([0] * 4, scenario="other", roles=("seer",), values=[0] * 4)
    with pytest.raises(ValueError):
        metrics.factorial_contrasts(rows, ("A", "B"), metric="diagnostic")


def test_manifest_missing_whole_scenario_and_duplicate_expected_ids_rejected_even_if_observed_pairs_complete():
    rows = factorial_rows([0, 0, 0, 1])
    for expected in (["x", "missing"], ["x", "x"], []):
        with pytest.raises(ValueError, match="manifest"):
            metrics.factorial_contrasts(rows, ("A", "B"), expected_ids=expected)
    with pytest.raises(ValueError, match="manifest"):
        metrics.factorial_contrasts([], ("A", "B"), expected_ids=["x"])
    empty = metrics.factorial_contrasts([], ("A", "B"), expected_ids=[])
    assert empty["scopes"]["seat"]["effects"]["A:B"]["mean"] is None


def test_factor_labels_only_label_effects_and_never_draw_or_change_deal_rng():
    rows = factorial_rows([0, 0, 0, 1])
    before = copy.deepcopy(rows)
    state = random.getstate()
    first = metrics.factorial_contrasts(rows, ("A", "B"))
    renamed = [dict(r, factors={"new_A": r["factors"]["A"], "new_B": r["factors"]["B"]}) for r in rows]
    second = metrics.factorial_contrasts(renamed, ("new_A", "new_B"))
    assert rows == before and random.getstate() == state
    assert first["scopes"]["seat"]["effects"]["A:B"] == second["scopes"]["seat"]["effects"]["new_A:new_B"]


def test_contradictory_literal_report_is_not_unambiguous_alignment_and_disabled_chat_has_no_opportunities():
    game = village_win()
    game.messages = [broadcast(1, 2, "I am the seer. Player0 is a wolf. Player0 is a villager.")]
    claims = metrics.game_metrics(game)["claims"]
    assert claims["conflicting_literal_target_days"] == 1
    assert claims["truthful_seer_report_vote_alignment"] == metrics.ratio(0, 0)
    game.messages = []
    game.messaging = False
    claims = metrics.game_metrics(game)["claims"]
    assert claims["true_seer_literal_claim_day_rate"] == metrics.ratio(0, 0)
    assert claims["multiple_literal_claimant_day_rate"] == metrics.ratio(0, 0)
    assert claims["wolf_pair_double_literal_claim_rate"] == metrics.ratio(0, 0)


def test_legacy_lynch_death_cause_is_equivalent_to_vote():
    game = village_win()
    before = metrics.game_metrics(game)
    for death in game.dead:
        if death["cause"] == "vote":
            death["cause"] = "lynch"
    assert metrics.game_metrics(game) == before


def test_duplicate_trace_is_rejected_not_double_counted():
    game = village_win()
    for rows in ([trace(1, "night_seer", "0")] * 2,
                 [trace(1, "day_vote", "0", {0: "wolf"})] * 2):
        with pytest.raises(ValueError, match="duplicate"):
            metrics.game_metrics(game, traces={2: rows})
    with pytest.raises(ValueError, match="duplicate"):
        metrics.game_metrics(game, traces={2: [], "2": []})


def test_single_factor_is_a_paired_difference_without_invented_interactions():
    report = metrics.factorial_contrasts(factorial_rows([0, 1], factors=("A",)), ("A",))
    assert set(report["scopes"]["seat"]["effects"]) == {"A"}
    assert report["scopes"]["seat"]["effects"]["A"]["mean"] == 1


@pytest.mark.parametrize("change", ["missing_background", "missing_metric", "different_scenario", "fractional_abstain"])
def test_additional_audit_edge_cases(change):
    rows = factorial_rows([0, 0, 0, 1], values=[0, 0, 0, 1])
    with pytest.raises(ValueError):
        if change == "fractional_abstain":
            metrics.vote_diagnostics(ROLES, {0: 7.0, 1: 0})
        else:
            if change == "missing_background":
                for row in rows:
                    del row["deal"]["background"]
            elif change == "missing_metric":
                del rows[0]["diagnostic"]
            else:
                rows[0]["scenario"] = {"timing": "rt"}
            metrics.factorial_contrasts(rows, ("A", "B"), metric="diagnostic")


def test_invalid_statistics_and_factor_definitions_are_rejected():
    for factors in ((), ("A", "A"), ("",), ("A:B", "A", "B"), tuple("ABCDEFG")):
        with pytest.raises(ValueError):
            metrics.factorial_contrasts([], factors)
    with pytest.raises(ValueError):
        metrics.clustered_mean([1], [])
    with pytest.raises(ValueError):
        metrics.clustered_mean([math.inf], [0])
