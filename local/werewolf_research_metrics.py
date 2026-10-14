from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations, product
import math
import re


ROLES = ("wolf", "seer", "villager")
SIDE = {"wolf": "wolf", "seer": "villager", "villager": "villager"}
METRIC_CONTRACT = {
    "scope": "Evaluator-only, after Game.play(); never give this output to a policy.",
    "denominators": "Each ratio retains numerator and denominator; no exposure gives value=None.",
    "pivotality": "Replace one legal ballot, hold others fixed; compare eliminated faction, not future wins.",
    "claims": "Only same-day literal broadcasts are recognized; alignment is not an observed check or causal uptake.",
    "seer": "Observed usability counts distinct (vote day, target) own facts, not messages or guessed receipt.",
    "night": "No first-night kill exposure; seats with process failures have no certified choice traces.",
    "independence": "Independent unit is a synthetic deal seed; seats/timings/opponent swaps may share it.",
    "intervals": "Cluster-robust normal 95% intervals are approximate, unreliable with few clusters; no win guarantee.",
}


def ratio(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None}


def _mean(values):
    values = list(values)
    return sum(values) / len(values) if values else None


def _roles(roles):
    roles = tuple(roles)
    if Counter(roles) != Counter({"wolf": 2, "seer": 1, "villager": 4}):
        raise ValueError("expected the seven-seat 2 wolf / 1 seer / 4 villager deal")
    return roles


def _seats(seats):
    seats = tuple(seats)
    if any(type(s) is not int or s not in range(7) for s in seats) or len(set(seats)) != len(seats):
        raise ValueError("invalid or duplicate seats")
    return seats


def _eliminated(ballots):
    tally = Counter(t for t in ballots.values() if t is not None)
    leaders = [t for t in tally if tally[t] == max(tally.values())]
    return leaders[0] if len(leaders) == 1 else None


def _terminal(roles, living, stall):
    wolves = sum(roles[s] == "wolf" for s in living)
    if not wolves:
        return "villager", "no_wolves"
    if 2 * wolves >= len(living):
        return "wolf", "parity"
    if stall >= 3:
        return "wolf", "stalls"
    return None, None


def _vote_result(roles, ballots, prior_stall):
    target = _eliminated(ballots)
    living = set(ballots) - {target}
    winner, _ = _terminal(roles, living, prior_stall + 1 if target is None else 0)
    return target, SIDE[roles[target]] if target is not None else None, winner


def vote_diagnostics(roles, ballots, controlled_seats=None, prior_stall=0):
    roles = _roles(roles)
    living = set(_seats(ballots))
    controlled = set(_seats(range(7) if controlled_seats is None else controlled_seats)) & living
    if not living or type(prior_stall) is not int or not 0 <= prior_stall < 3:
        raise ValueError("vote needs living voters and a nonterminal prior stall count")
    ballots = {s: None if type(t) is int and t == 7 else t for s, t in ballots.items()}
    if any(t is not None and (type(t) is not int or t not in living or t == s) for s, t in ballots.items()):
        raise ValueError("illegal ballot: self, dead, or invalid target")
    target, faction, immediate_winner = _vote_result(roles, ballots, prior_stall)
    pivotal, terminal_pivotal = 0, 0
    for seat in controlled:
        alternatives = [_vote_result(roles, ballots | {seat: other}, prior_stall)
                        for other in [None, *sorted(living - {seat})] if other != ballots[seat]]
        pivotal += any(result[1] != faction for result in alternatives)
        terminal_pivotal += any(result[2] != immediate_winner for result in alternatives)
    village_ballots = [t for s, t in ballots.items() if s in controlled and roles[s] != "wolf"]
    village_cast = [t for t in village_ballots if t is not None]
    wolf_votes = sum(roles[t] == "wolf" for t in village_cast)
    all_village_cast = [t for s, t in ballots.items() if roles[s] != "wolf" and t is not None]
    village_tally = Counter(all_village_cast)
    wolf_tally = Counter(t for t in all_village_cast if roles[t] == "wolf")
    tally = Counter(t for t in ballots.values() if t is not None)
    counts = sorted(tally.values(), reverse=True) + [0, 0]
    return {
        "eliminated": target, "eliminated_faction": faction, "immediate_winner": immediate_winner,
        "village_wolf_vote": ratio(wolf_votes, len(village_ballots)),
        "village_wolf_vote_when_cast": ratio(wolf_votes, len(village_cast)),
        "controlled_abstention": ratio(sum(ballots[s] is None for s in controlled), len(controlled)),
        "faction_pivotal": ratio(pivotal, len(controlled)),
        "immediate_terminal_pivotal": ratio(terminal_pivotal, len(controlled)),
        "village_split_pressure": ratio(len(all_village_cast) - max(village_tally.values(), default=0),
                                         len(all_village_cast)),
        "village_wolf_split_pressure": ratio(sum(wolf_tally.values()) - max(wolf_tally.values(), default=0),
                                              sum(wolf_tally.values())),
        "leader_margin": counts[0] - counts[1],
        "majority_margin": counts[0] - (len(living) // 2 + 1),
        "wolf_vs_innocent_margin": max((n for s, n in tally.items() if roles[s] == "wolf"), default=0)
                                    - max((n for s, n in tally.items() if roles[s] != "wolf"), default=0),
        "tie": bool(tally) and target is None, "all_abstain": not tally,
    }


def compact_decision_trace(raw, choice, kind="action"):
    gs = raw["game_state"]
    lines = str(raw.get("observation", "")).splitlines()
    observed = None
    if "Your role: SEER" in lines:
        for line in lines:
            if line in ("You have not investigated anyone yet.", "Your investigations: none yet"):
                observed = {}
            elif line.startswith("Your investigations: "):
                entries = line[len("Your investigations: "):].split(", ")
                parsed = [re.fullmatch(r"Player([0-6]) is (wolf|villager)", entry) for entry in entries]
                if all(parsed):
                    observed = {int(m[1]): m[2] for m in parsed}
    return {"day": gs["day"], "phase": gs["phase"], "kind": kind,
            "choice": choice if kind == "action" and isinstance(choice, (str, int)) else None,
            "observed_checks": observed}


def _alive_before(dead, day, phase):
    return set(range(7)) - {d["seat"] for d in dead
                            if d["day"] < day or (d["day"] == day and phase == "day"
                                                  and d["cause"] == "night_kill")}


def _trace_map(traces):
    result = {}
    for seat, rows in (traces or {}).items():
        seat = int(seat)
        _seats([seat])
        if seat in result:
            raise ValueError("duplicate trace seat")
        result[seat] = list(rows)
    return result


def _clean_action_choices(game, traces):
    actions = {}
    for seat, rows in traces.items():
        failed = any(game.stats.get(key, [0] * 7)[seat] for key in ("crash", "illegal", "timeout", "bad_chat"))
        for row in rows:
            if row.get("kind") != "action" or row.get("phase") not in ("night_seer", "night_wolf"):
                continue
            key = (seat, row["day"], row["phase"])
            if key in actions:
                raise ValueError("duplicate night decision trace")
            actions[key] = None
            if failed:
                continue
            try:
                target = int(row["choice"])
            except (ValueError, TypeError):
                continue
            living = _alive_before(game.dead, row["day"], "night")
            expected_role = "seer" if row["phase"] == "night_seer" else "wolf"
            if (1 <= row["day"] <= game.day and seat in living and game.roles[seat] == expected_role
                    and target in living - {seat}
                    and (expected_role != "wolf" or game.roles[target] != "wolf")):
                actions[key] = target
    return actions


def _seer_metrics(game, traces):
    seer = game.roles.index("seer")
    opportunities = sum(seer in _alive_before(game.dead, day, "night") for day in range(1, game.day + 1))
    unique = len(game.invest)
    if unique > opportunities or any(t == seer or t not in range(7) or result != SIDE[game.roles[t]]
                                     for t, result in game.invest.items()):
        raise ValueError("invalid completed investigation record")
    observed_pairs, usable_pairs, observed_targets = set(), set(), set()
    uptake, uptake_opportunities = 0, 0
    vote_by_day = {v["day"]: v["votes"] for v in game.votes}
    seen_votes = set()
    for row in traces.get(seer, []):
        facts = row.get("observed_checks")
        day = row.get("day")
        if facts is None or row.get("phase") != "day_vote" or day not in vote_by_day:
            continue
        facts = {int(t): result for t, result in facts.items()}
        if any(t not in game.invest or game.invest[t] != result for t, result in facts.items()):
            raise ValueError("observed checks contradict the completed engine record")
        living = set(vote_by_day[day])
        if seer not in living:
            raise ValueError("dead seer cannot have a day decision trace")
        observed_targets.update(facts)
        observed_pairs.update((day, t) for t in facts)
        usable_pairs.update((day, t) for t in facts if t in living)
        if row.get("kind") == "action":
            if day in seen_votes:
                raise ValueError("duplicate seer vote trace")
            seen_votes.add(day)
            known_wolves = {t for t, result in facts.items() if result == "wolf" and t in living}
            if known_wolves:
                uptake_opportunities += 1
                uptake += vote_by_day[day][seer] in known_wolves
    return {
        "check_opportunities": opportunities, "unique_checks": unique,
        "repeat_checks": opportunities - unique,
        "unique_check_efficiency": ratio(unique, opportunities),
        "unique_wolf_discovery_efficiency": ratio(sum(r == "wolf" for r in game.invest.values()), opportunities),
        "observed_fact_coverage": ratio(len(observed_targets), unique),
        "observed_alive_usable": ratio(len(usable_pairs), len(observed_pairs)),
        "own_observed_wolf_vote_uptake": ratio(uptake, uptake_opportunities),
    }


def _literal_claim(content):
    match = re.fullmatch(r"(?:I am (?:the |a )?seer|I'm (?:the |a )?seer|Seer here)[.!]?"
                         r"((?:\s+Player[0-6] is (?:a )?(?:wolf|villager)[.!])*)", content.strip(), re.I)
    if not match:
        return None
    return [(int(t), result.lower()) for t, result in
            re.findall(r"Player([0-6]) is (?:a )?(wolf|villager)", match[1], re.I)]


def _claim_metrics(game):
    broadcasts = [m for m in game.messages if m.get("visibility") == "broadcast"]
    recognized = 0
    claims = defaultdict(dict)
    for message in broadcasts:
        parsed = _literal_claim(message.get("content", ""))
        if parsed is not None:
            recognized += 1
            claims[message["day"]].setdefault(message["sender"], set()).update(parsed)
    days, collisions, pair_days, double_wolf, conflicting = 0, 0, 0, 0, 0
    aligned, opportunities = 0, 0
    true_claim_days, true_opportunities, false_claim_days = 0, 0, 0
    for vote in game.votes:
        day, ballots = vote["day"], vote["votes"]
        alive = set(ballots)
        today = {s: reports for s, reports in claims.get(day, {}).items() if s in alive}
        days += bool(game.messaging)
        collisions += len(today) >= 2
        live_wolves = {s for s in alive if game.roles[s] == "wolf"}
        pair_days += len(live_wolves) == 2 and game.messaging
        double_wolf += len(live_wolves) == 2 and live_wolves <= today.keys()
        reports = defaultdict(set)
        for results in today.values():
            for target, result in results:
                reports[target].add(result)
        conflicting += sum(len(results) > 1 for results in reports.values())
        seer = game.roles.index("seer")
        true_opportunities += seer in alive and game.messaging
        true_claim_days += seer in today
        false_claim_days += sum(game.roles[s] != "seer" for s in today)
        seer_reports = today.get(seer, set())
        truthful_wolf_reports = {t for t, result in seer_reports
                                if result == "wolf" and game.roles[t] == "wolf" and t in alive
                                and (t, "villager") not in seer_reports}
        if truthful_wolf_reports:
            voters = [s for s in alive if game.roles[s] == "villager"]
            opportunities += len(voters)
            aligned += sum(ballots[s] in truthful_wolf_reports for s in voters)
    return {"literal_claim_message_coverage": ratio(recognized, len(broadcasts)),
            "true_seer_literal_claim_day_rate": ratio(true_claim_days, true_opportunities),
            "nonseer_literal_claimant_days": false_claim_days,
            "multiple_literal_claimant_day_rate": ratio(collisions, days),
            "wolf_pair_double_literal_claim_rate": ratio(double_wolf, pair_days),
            "conflicting_literal_target_days": conflicting,
            "truthful_seer_report_vote_alignment": ratio(aligned, opportunities)}


def game_metrics(game, controlled_seats=None, traces=None):
    roles = _roles(game.roles)
    controlled = _seats(range(7) if controlled_seats is None else controlled_seats)
    if game.winner not in ("wolf", "villager") or game.day < 1:
        raise ValueError("metrics require a completed Game")
    if len(game.alive) != 7:
        raise ValueError("invalid alive seats")
    dead_seats = _seats(d["seat"] for d in game.dead)
    if set(dead_seats) != {s for s in range(7) if not game.alive[s]}:
        raise ValueError("deaths do not match final survivors")
    if any(d["cause"] not in ("night_kill", "vote", "lynch") or not 1 <= d["day"] <= game.day
           or d["role"] != roles[d["seat"]] for d in game.dead):
        raise ValueError("invalid death history")
    traces = _trace_map(traces)
    outcomes = [{"seat": s, "role": roles[s], "team_win": int(SIDE[roles[s]] == game.winner),
                 "team_score": 1 if SIDE[roles[s]] == game.winner else -1, "survived": bool(game.alive[s])}
                for s in controlled]
    role_means = {r: _mean(o["team_win"] for o in outcomes if o["role"] == r) for r in ROLES}
    per_vote, stall, longest = [], 0, 0
    days = [v["day"] for v in game.votes]
    if days != list(range(1, len(days) + 1)) or len(days) not in (game.day, game.day - 1):
        raise ValueError("incomplete or duplicate vote history")
    for vote in game.votes:
        if set(vote["votes"]) != _alive_before(game.dead, vote["day"], "day"):
            raise ValueError("voters do not match living seats")
        diagnostics = vote_diagnostics(roles, vote["votes"], controlled, stall)
        deaths = [d["seat"] for d in game.dead if d["day"] == vote["day"] and d["cause"] in ("vote", "lynch")]
        expected_deaths = [] if vote["eliminated"] is None else [vote["eliminated"]]
        if vote["eliminated"] != diagnostics["eliminated"] or deaths != expected_deaths:
            raise ValueError("recorded elimination disagrees with ballots or deaths")
        per_vote.append(diagnostics)
        stall = stall + 1 if vote["eliminated"] is None else 0
        longest = max(longest, stall)
    final_winner, reason = _terminal(roles, [s for s in range(7) if game.alive[s]], stall)
    if final_winner != game.winner or stall != game.stall:
        raise ValueError("winner or stall count inconsistent with completed game")
    votes = {}
    ratio_names = ("village_wolf_vote", "village_wolf_vote_when_cast", "controlled_abstention",
                   "faction_pivotal", "immediate_terminal_pivotal", "village_split_pressure",
                   "village_wolf_split_pressure")
    for key in ratio_names:
        votes[key] = ratio(sum(v[key]["numerator"] for v in per_vote),
                           sum(v[key]["denominator"] for v in per_vote))
    for key in ("leader_margin", "majority_margin", "wolf_vs_innocent_margin"):
        votes[key] = ratio(sum(v[key] for v in per_vote), len(per_vote))
    eliminations = {}
    for scope, selected in (("table", set(range(7))), ("controlled", set(controlled))):
        eliminations[scope] = {}
        for role in (*ROLES, "innocent"):
            members = {s for s in selected if roles[s] == role or (role == "innocent" and roles[s] != "wolf")}
            day_deaths = sum(d["seat"] in members and d["cause"] in ("vote", "lynch") for d in game.dead)
            night_deaths = sum(d["seat"] in members and d["cause"] == "night_kill" for d in game.dead)
            day_exposures = sum(len(members & set(v["votes"])) for v in game.votes)
            night_exposures = sum(len(members & _alive_before(game.dead, day, "night"))
                                  for day in range(2, game.day + 1))
            eliminations[scope][role] = {
                "day": ratio(day_deaths, day_exposures), "night": ratio(night_deaths, night_exposures),
                "ever_eliminated": ratio(day_deaths + night_deaths, len(members)),
            }
    actions = _clean_action_choices(game, traces)
    observed_kills = [t for (s, day, phase), t in actions.items()
                      if s in controlled and day > 1 and phase == "night_wolf" and t is not None]
    kill_opportunities = sum(sum(roles[s] == "wolf" for s in set(controlled) & _alive_before(game.dead, day, "night"))
                             for day in range(2, game.day + 1))
    pair_observed, disagreements = 0, 0
    for day in range(2, game.day + 1):
        wolves = [s for s in _alive_before(game.dead, day, "night") if roles[s] == "wolf"]
        choices = [actions.get((s, day, "night_wolf")) for s in wolves]
        if len(wolves) == 2 and all(t is not None for t in choices):
            pair_observed += 1
            disagreements += choices[0] != choices[1]
    night_deaths = [d for d in game.dead if d["cause"] == "night_kill"]
    first = next((v for v in game.votes if v["day"] == 1), None)
    return {
        "schema_version": 1, "days": game.day, "vote_rounds": len(per_vote), "terminal_reason": reason,
        "outcome": {"seats": outcomes, "seat_mean_win": _mean(o["team_win"] for o in outcomes),
                    "role_mean_win": role_means,
                    "present_role_balanced_win": _mean(v for v in role_means.values() if v is not None)},
        "first_day_wolf_elimination": ratio(int(first is not None and first["eliminated"] is not None
                                                and roles[first["eliminated"]] == "wolf"), int(first is not None)),
        "eliminations": eliminations, "votes": votes,
        "stalls": {"no_elimination": ratio(sum(v["eliminated"] is None for v in per_vote), len(per_vote)),
                   "ties": sum(v["tie"] for v in per_vote), "all_abstain": sum(v["all_abstain"] for v in per_vote),
                   "longest_run": longest, "final_run": stall},
        "seer": _seer_metrics(game, traces), "claims": _claim_metrics(game),
        "night": {"observed_choice_coverage": ratio(len(observed_kills), kill_opportunities),
                  "observed_seer_target": ratio(sum(roles[t] == "seer" for t in observed_kills), len(observed_kills)),
                  "observed_civilian_target": ratio(sum(roles[t] == "villager" for t in observed_kills), len(observed_kills)),
                  "seer_kill": ratio(sum(d["role"] == "seer" for d in night_deaths), len(night_deaths)),
                  "civilian_kill": ratio(sum(d["role"] == "villager" for d in night_deaths), len(night_deaths)),
                  "observed_wolf_disagreement": ratio(disagreements, pair_observed)},
    }


def summarize_outcomes(metrics):
    cells = defaultdict(list)
    tables = []
    for game in metrics:
        outcomes = game["outcome"]["seats"]
        _seats(o["seat"] for o in outcomes)
        tables.append([o["team_win"] for o in outcomes])
        for outcome in outcomes:
            cells[(outcome["role"], outcome["seat"])].append(outcome["team_win"])
    cell_means = {f"{r}/{s}": _mean(cells[(r, s)]) for r in ROLES for s in range(7)}
    available = [v for v in cell_means.values() if v is not None]
    return {"games": len(tables), "seat_weighted_win": _mean(v for table in tables for v in table),
            "game_mean_win": _mean(_mean(table) for table in tables if table),
            "role_seat_cell_win": cell_means, "covered_cells": len(available),
            "observed_cell_balanced_win": _mean(available),
            "role_seat_balanced_win": _mean(available) if len(available) == 21 else None}


def clustered_mean(values, clusters):
    values, clusters = list(values), list(clusters)
    if len(values) != len(clusters) or any(not math.isfinite(v) for v in values):
        raise ValueError("finite values and one cluster id per value required")
    if not values:
        return {"n": 0, "clusters": 0, "mean": None, "se": None, "ci95": [None, None]}
    mean = _mean(values)
    residuals = defaultdict(float)
    for value, cluster in zip(values, clusters):
        residuals[cluster] += value - mean
    count = len(residuals)
    se = (math.sqrt(count / (count - 1) * sum(r * r for r in residuals.values())) / len(values)
          if count > 1 else None)
    return {"n": len(values), "clusters": count, "mean": mean, "se": se,
            "ci95": [mean - 1.96 * se, mean + 1.96 * se] if se is not None else [None, None]}


def factorial_contrasts(records, factors, expected_ids=None, metric=None):
    factors = tuple(factors)
    if (not 1 <= len(factors) <= 6 or any(not isinstance(f, str) or not f or ":" in f for f in factors)
            or len(set(factors)) != len(factors)):
        raise ValueError("one to six unique named binary factors required")
    corners = tuple(product((0, 1), repeat=len(factors)))
    rows = defaultdict(dict)
    for record in records:
        if "harness_error" in record:
            raise ValueError("harness error in factorial record")
        levels = record["factors"]
        if set(levels) != set(factors) or any(type(v) not in (int, bool) or v not in (0, 1) for v in levels.values()):
            raise ValueError("factor labels must be the specified binary components")
        corner = tuple(int(levels[f]) for f in factors)
        if corner in rows[record["id"]]:
            raise ValueError("duplicate scenario/factor cell")
        rows[record["id"]][corner] = record
    if expected_ids is not None:
        expected = list(expected_ids)
        if len(set(expected)) != len(expected) or set(expected) != set(rows):
            raise ValueError("scenario set differs from planned manifest")
    role_by_seed_seat = {}
    for row in rows.values():
        if set(row) != set(corners):
            raise ValueError("missing factorial pair/cell")
        base = row[corners[0]]["deal"]
        if not {"seed", "seats", "roles", "background"} <= base.keys():
            raise ValueError("missing deal audit fields")
        if any(r["deal"] != base or r.get("scenario") != row[corners[0]].get("scenario") for r in row.values()):
            raise ValueError("paired deal/roles/seats/background/scenario mismatch")
        seats = _seats(base["seats"])
        roles = base["roles"]
        if not seats or len(roles) != len(seats) or any(r not in ROLES for r in roles) or type(base["seed"]) is not int:
            raise ValueError("invalid deal audit fields")
        for seat, role in zip(seats, roles):
            key = (base["seed"], seat)
            if key in role_by_seed_seat and role_by_seed_seat[key] != role:
                raise ValueError("same deal seed has contradictory seat roles")
            role_by_seed_seat[key] = role
        for rec in row.values():
            wins = rec["wins"]
            if len(wins) != len(seats) or any(type(v) not in (bool, int) or v not in (0, 1) for v in wins):
                raise ValueError("one binary team outcome per controlled seat required")
            if len({int(w) if SIDE[r] == "wolf" else 1 - int(w) for r, w in zip(roles, wins)}) > 1:
                raise ValueError("seat outcomes disagree with faction scoring")
            if metric is not None and (not isinstance(rec.get(metric), (float, int)) or not math.isfinite(rec[metric])):
                raise ValueError("metric must be finite and present in every factorial cell")
    result = {}
    for scope in ("seat", "role_balanced", *ROLES):
        samples, clusters = [], []
        for row in rows.values():
            deal = row[corners[0]]["deal"]
            roles = deal["roles"]
            indices = [i for i, role in enumerate(roles) if scope in ("seat", "role_balanced") or role == scope]
            if not indices:
                continue
            sample = {}
            for corner, rec in row.items():
                if metric is not None:
                    sample[corner] = rec[metric]
                elif scope == "role_balanced":
                    sample[corner] = _mean(_mean(rec["wins"][i] for i, r in enumerate(roles) if r == role)
                                          for role in set(roles))
                else:
                    sample[corner] = _mean(rec["wins"][i] for i in indices)
            samples.append(sample)
            clusters.append(deal["seed"])
        cells = {"".join(map(str, corner)): clustered_mean([s[corner] for s in samples], clusters) for corner in corners}
        effects = {}
        for size in (1, 2):
            for selected in combinations(range(len(factors)), size):
                differences = [sum(value * math.prod(1 if corner[i] else -1 for i in selected)
                                   for corner, value in sample.items()) / (2 ** (len(factors) - size))
                               for sample in samples]
                effects[":".join(factors[i] for i in selected)] = clustered_mean(differences, clusters)
        result[scope] = {"cells": cells, "effects": effects}
    return {"factors": list(factors), "metric": metric or "team_win", "scenarios": len(rows),
            "cluster_unit": "deal.seed; reuse this cluster across timings, seats and opponent swaps",
            "interval_method": "CR1 normal approximation; fewer than two clusters gives no interval",
            "role_conditioning": "same controlled role present; not a side-isolated intervention unless runner makes it so",
            "scopes": result}
