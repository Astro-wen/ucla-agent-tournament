#!/usr/bin/env python3
"""Paired, no-network HoK diagnostic evaluation. NOT a live-strength benchmark.

PYTHONPATH=. ARENA_RECORD=0 python scripts/hok_eval.py --games 2 --max-steps 100 --json
--games counts paired seeds: each plays twice, swapping camps. The engine and
profiles are frozen independently of policies. Use --profile all --mode both
to expose sensitivity rather than picking the most favorable configuration.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # this worktree, not an editable install's checkout
os.environ["ARENA_RECORD"] = "0"

from local.hok_env import (PolicyInterfaceError, accepts_clock, candidate_factory, candidate_revision_factory, configured_factory,
                           frozen_import_factory, opponent_factory, play_game, source_unchanged)
from local.hok_sim import (ASSUMPTIONS, MODEL_VERSION, OFFICIAL_GUIDE_SHA256, OFFICIAL_REFERENCE, PROFILES,
                           SENSITIVITY_PROFILES, SENSITIVITY_REGISTRATION, SENSITIVITY_REGISTRATION_SHA256,
                           SENSITIVITY_VERSION, UPSTREAM_REFERENCE, digest, profile_config)

PROTECTED_SEEDS = frozenset((1201, 8101, 8102))
FINAL_OPPONENTS = ("research:idle", "research:first_legal", "research:uniform_random",
                   "research:relay_pressure", "research:counterpunch_interceptor")


class ReservedSeedError(ValueError):
    pass


def resolve_revision(root, revision):
    return subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--end-of-options",
                           revision + "^{commit}"], check=True, capture_output=True, text=True).stdout.strip()


def load_candidate(args, root):
    if args.candidate_revision:
        candidate, info = candidate_revision_factory(root, args.candidate_revision)
    elif args.candidate_factory:
        candidate, info = frozen_import_factory(args.candidate_factory, args.factory_root or root, args.factory_revision)
    else:
        candidate, info = candidate_factory(args.candidate_dir or root)
    if not accepts_clock(candidate):
        raise PolicyInterfaceError("clock_injection_not_supported")
    config = json.loads(args.factory_config)
    if not isinstance(config, dict):
        raise ValueError("factory config must be a JSON object")
    info["instance_config"] = config
    return configured_factory(candidate, config) if config else candidate, info


def interface_report(args):
    """Import/signature check only: no decide(), simulator, seed draw or score."""
    root = Path(__file__).resolve().parents[1]
    _, info = load_candidate(args, root)
    opponents = []
    for name in args.opponent or []:
        factory, opponent = opponent_factory(name, revision=args.opponent_revision, repository=root)
        if not accepts_clock(factory):
            raise PolicyInterfaceError("opponent_clock_injection_not_supported")
        opponents.append(opponent)
    return {"schema_version": 1, "status": "interface_compatible_static_check", "games": 0,
            "candidate": info, "opponents": opponents, "seed_used": None,
            "warning": "No policy decision or match was run; this is not an outcome or a strength check."}


def prepare_final_plan():
    """Deliberately returns commands with an unresolved FINAL_SHA. Never executes them."""
    root = Path(__file__).resolve().parents[1]
    main = resolve_revision(root, "fb421a2")
    opponent_revision = resolve_revision(root, "503c36e")
    subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", "12deb84", opponent_revision],
                   check=True, capture_output=True)
    _, main_info = candidate_revision_factory(root, main)
    frozen_opponents = [opponent_factory(name, revision=opponent_revision, repository=root)[1]
                        for name in FINAL_OPPONENTS]
    commands = []
    for role, revision in (("current_main", main), ("final_candidate", "{FINAL_SHA}")):
        for seed in (8101, 8102):
            for mode in ("realtime", "lockstep"):
                command = [sys.executable, str(Path(__file__).resolve()), "--model", "v3",
                           "--candidate-revision", revision, "--opponent-revision", opponent_revision,
                           "--games", "1", "--seed", str(seed), "--mode", mode, "--profile", "all",
                           "--max-steps", "3000", "--calls-per-step", "6", "--decision-steps", "1",
                           "--diagnostics", "--progress", "--authorize-reserved-with-final-sha", "{FINAL_SHA}",
                           "--json", f"{{REPORT_DIR}}/{role}-{seed}-{mode}.json"]
                for opponent in FINAL_OPPONENTS:
                    command += ["--opponent", opponent]
                commands.append({"role": role, "seed": seed, "mode": mode, "argv": command})
    return {"schema": "hok-final-plan-v1", "status": "awaiting_parent_final_sha", "autostart": False,
            "games_run": 0, "max_workers": 4, "planned_games_if_both_compatible": 400,
            "planned_paired_conditions_per_candidate": 100, "selection_seed_1201_included": False,
            "model": MODEL_VERSION, "model_source_sha256": hashlib.sha256((root / "local/hok_sim.py").read_bytes()).hexdigest(),
            "evaluator_source_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                                       for name in ("local/hok_sim.py", "local/hok_env.py", "scripts/hok_eval.py",
                                                    "altruagent/models.py", "altruagent/runner.py")},
            "current_main": main_info, "opponents": frozen_opponents, "commands": commands,
            "authorization": "Do not execute until the parent supplies and approves FINAL_SHA. Replace both "
                             "{FINAL_SHA} occurrences and {REPORT_DIR}; at most four scoring commands concurrently. "
                             "Check both candidate interfaces first. Incompatibility is not a loss.",
            "counting_limits": "400 planned synthetic legs are 100 matched conditions, each with two camps "
                               "for each of two policies. Only two world seeds; modes, profiles and opponents "
                               "share code and seeds. No IID or real-game claim."}


def public_archive_index(reports):
    """Public-safe paired scores, not observations/decisions or a synthetic Elo.
    Deduplicate exact repeated pairs; preserve conflicting repeats as audit failures.
    """
    entries, policies, opponents, configs, pairs = [], {}, {}, {}, {}
    source_groups, condition_map = {}, {}
    raw_pairs = duplicates = conflicts = 0
    families, conditions, seeds, modes = set(), set(), set(), set()
    for report in reports:
        if not isinstance(report.get("cells"), list) and report.get("status") not in (
                "interface_incompatible", "interface_compatible_static_check", "authorization_required"):
            raise ValueError("archive input is not an evaluation report")
        candidate = report.get("candidate", {})
        policy = {k: candidate.get(k) for k in ("git_commit", "source_sha256", "entrypoint",
                                               "interface_status", "interface_adapter", "clock_binding")}
        options = candidate.get("instance_config", {})
        policy["instance_config_sha256"] = digest(options)
        disclosed_profiles = {"baseline", "objective_guarded_v1", "observed_damage_v1",
                              "bounded_recovery_v1", "late_objective_v1", "skill_residency_v1"}
        policy["registered_profile"] = options.get("profile") if isinstance(options, dict) \
            and options.get("profile") in disclosed_profiles else None
        policy_id = digest(policy)
        policies[policy_id] = policy
        report_id = report.get("reproducibility_sha256") or digest(report)
        source_id = digest(report.get("source_sha256", {}))
        source_groups[source_id] = report.get("source_sha256", {})
        entries.append({"report_id": report_id, "status": report.get("status", "completed"),
                        "policy_id": policy_id, "model": report.get("model"),
                        "candidate_revision": report.get("candidate_revision"),
                        "interface_error": report.get("interface_error"),
                        "source_id": source_id,
                        "source_unchanged": report.get("artifact_source_unchanged_during_run")})
        for cell in report.get("cells", []):
            opp = {k: cell["opponent"].get(k) for k in ("name", "git_commit", "source_sha256")}
            opponent_id = digest(opp)
            opponents[opponent_id] = opp
            config_id = cell["config_sha256"]
            configs[config_id] = cell["config"]
            for pair in cell["pairs"]:
                raw_pairs += 1
                mode, seed = cell["config"]["mode"], pair["seed"]
                seeds.add(seed)
                modes.add(mode)
                families.add((seed, opponent_id))
                condition = {"model": report["model"], "config_sha256": config_id,
                             "treatment_sha256": cell.get("treatment_sha256"),
                             "sensitivity": cell.get("sensitivity"), "profile": cell["profile"],
                             "seed": seed, "mode": mode, "opponent_id": opponent_id,
                             "decision_steps": report["harness"]["decision_steps"],
                             "calls_per_step": report["harness"]["calls_per_step"],
                             "source_id": source_id}
                condition_id = digest(condition)
                conditions.add(condition_id)
                condition_map[condition_id] = condition
                record = {"policy_id": policy_id, "condition_id": condition_id, "legs": []}
                for game in pair["legs"]:
                    camp = game["candidate_camp"]
                    own, foe = str(camp), str(3 - camp)
                    scores = game["result"]["outcome"]["score"]
                    hero_metrics = (game.get("diagnostics") or {}).get("per_hero", {})
                    seats = [str(s) for s in range((camp - 1) * 5, camp * 5)]
                    record["legs"].append({
                        "camp": camp, "score": game["candidate_score"], "steps": game["steps"],
                        "termination": game["result"]["termination_reason"],
                        "objective_damage": [scores[c]["enemy_structure_damage"] for c in (own, foe)],
                        "hero_kills": [scores[c]["kills"] for c in (own, foe)],
                        "invalid_actions": {r: game["stats"][r]["invalid"] for r in ("candidate", "opponent")},
                        "protocol_failure": game["protocol_failure"],
                        "native_policy_errors": game.get("native_policy_errors", {}),
                        "clean_outcome": not game["protocol_failure"]
                                         and not any(game.get("native_policy_errors", {}).values())
                                         and not any(game["stats"][r]["invalid"] for r in ("candidate", "opponent")),
                        "max_no_improvement_retreat_s_by_hero":
                            [hero_metrics.get(s, {}).get("max_stationary_no_gain_retreat_s") for s in seats],
                        "max_no_improvement_idle_s_by_hero":
                            [hero_metrics.get(s, {}).get("max_stationary_no_gain_idle_s") for s in seats],
                        "trajectory_sha256": game["trajectory_sha256"],
                    })
                key = digest({"policy": policy_id, "condition": condition_id})
                if key in pairs:
                    previous = {k: v for k, v in pairs[key].items() if k != "report_ids"}
                    if previous == record:
                        duplicates += 1
                        pairs[key]["report_ids"].append(report_id)
                        continue
                    conflicts += 1
                    key += f"-conflict-{conflicts}"
                pairs[key] = {**record, "report_ids": [report_id]}
    return {"schema": "hok-public-paired-index-v1", "warning": "Synthetic diagnostics only; zero real matches. "
            "Do not treat modes, mirrored camps, repeated policies or assumption grids as IID real-game samples.",
            "status": "audit_conflict" if conflicts else "indexed",
            "counting": {"input_reports": len(reports), "raw_paired_records": raw_pairs,
                         "raw_leg_records": raw_pairs * 2, "unique_paired_records": len(pairs),
                         "unique_generated_legs": sum(len(p["legs"]) for p in pairs.values()),
                         "duplicate_pairs_removed": duplicates, "conflicting_repeat_pairs": conflicts,
                         "unique_condition_keys_across_policies": len(conditions),
                         "shared_seed_opponent_families": len(families),
                         "world_seeds": sorted(seeds), "modes_are_strata": sorted(modes),
                         "independent_real_games": 0,
                         "limits": "No count above asserts independence. Compare policies within the same "
                                   "condition; audit each mode and both camps. Never pool v3/v4 treatment arms "
                                   "into actual win-rate claims. Interface-incompatible entries have no games."},
            "reports": entries, "policies": policies, "opponents": opponents, "configs": configs,
            "sources": source_groups, "conditions": condition_map,
            "paired_outcomes": list(pairs.values())}

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    candidate = p.add_mutually_exclusive_group()
    candidate.add_argument("--candidate-dir", help="repo or directory containing HokPolicy's policy.py")
    candidate.add_argument("--candidate-factory", help="importable.module:factory accepting optional seed/clock")
    candidate.add_argument("--candidate-revision", help="local Git commit/ref, loaded as a fixed HoK package snapshot")
    p.add_argument("--factory-root", help="source repository/root for --candidate-factory (default this worktree)")
    p.add_argument("--factory-revision", help="optional local Git snapshot for --candidate-factory")
    p.add_argument("--factory-config", default="{}", help="JSON keyword arguments copied into each policy instance")
    p.add_argument("--diagnostics", action="store_true", help="bounded post-hoc damage/occupancy/event summaries")
    p.add_argument("--progress", action="store_true", help="bounded per-cell aggregate progress on stderr")
    p.add_argument("--check-interface", action="store_true", help="static setup check only, no game/decision")
    p.add_argument("--prepare-final", action="store_true", help="emit gated final plan; never run reserved games")
    p.add_argument("--archive-index", nargs="+", metavar="REPORT", help="compact public-safe index of existing reports")
    p.add_argument("--authorize-reserved-with-final-sha",
                   help="explicit parent-approved final SHA required for seeds 1201/8101/8102")
    p.add_argument("--opponent", action="append", help="random, first-legal, idle, push, research:<name>, module:factory")
    p.add_argument("--opponent-revision", help="freeze opponent factory/package from this local Git commit")
    p.add_argument("--games", type=int, default=2, help="paired seeds per configuration (TWO games each; default 2)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--mode", choices=("realtime", "lockstep", "both"),
                   help="override config mode (default config value, otherwise realtime)")
    p.add_argument("--profile", choices=(*PROFILES, "all"), default="default")
    p.add_argument("--model", choices=("v3", "v4"), default="v3",
                   help="frozen v3 baseline or separately preregistered v4 sensitivity extension")
    p.add_argument("--sensitivity", choices=tuple(SENSITIVITY_PROFILES),
                   help="v4 treatment (default neutral skill-1_heal-1); not permitted in v3")
    p.add_argument("--config", help="JSON file with explicit SimConfig overrides (recorded in report)")
    p.add_argument("--max-steps", type=int)
    p.add_argument("--decision-steps", type=int, default=1, help="realtime decision interval in model steps")
    p.add_argument("--calls-per-step", type=int, default=6, help="per-team decision call cap, >=5")
    p.add_argument("--json", nargs="?", const="-", metavar="PATH", help="aggregate report to PATH, or stdout")
    return p


def evaluate(args) -> dict:
    root = Path(__file__).resolve().parents[1]
    source_paths = (root / "local/hok_sim.py", root / "local/hok_env.py", Path(__file__).resolve(),
                    root / "altruagent/runner.py", root / "altruagent/models.py")
    def source_digests():
        return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in source_paths}
    source_hashes = source_digests()
    if args.games < 1:
        raise ValueError("--games must be positive")
    protected = PROTECTED_SEEDS.intersection(range(args.seed, args.seed + args.games))
    approved_final_sha = None
    if protected:
        if not args.authorize_reserved_with_final_sha:
            raise ReservedSeedError("parent_final_sha_required_before_protected_seed")
        try:
            approved_final_sha = resolve_revision(root, args.authorize_reserved_with_final_sha)
        except Exception as exc:
            raise ReservedSeedError("unresolved_final_sha_no_reserved_game_started") from exc
    if args.decision_steps < 1 or args.calls_per_step < 5:
        raise ValueError("decision-steps >= 1 and calls-per-step >= 5 required")
    if args.model == "v3" and args.sensitivity is not None:
        raise ValueError("v4 sensitivity must not be silently applied to v3")
    treatment_name = (args.sensitivity or "skill-1_heal-1") if args.model == "v4" else None
    sensitivity = SENSITIVITY_PROFILES[treatment_name] if treatment_name else None
    candidate, candidate_info = load_candidate(args, root)
    factory_config = candidate_info["instance_config"]
    overrides = json.loads(Path(args.config).read_text()) if args.config else {}
    if not isinstance(overrides, dict):
        raise ValueError("--config must contain a JSON object")
    if args.max_steps is not None:
        overrides["max_steps"] = args.max_steps
    profiles = list(PROFILES) if args.profile == "all" else [args.profile]
    modes = ["realtime", "lockstep"] if args.mode == "both" else [args.mode or overrides.get("mode", "realtime")]
    cells = []
    for name in args.opponent or ["random"]:
        opponent, opponent_info = opponent_factory(name, revision=args.opponent_revision, repository=root)
        if not accepts_clock(opponent) or not (opponent_info["clock_parameter"]
                                               or opponent_info["accepts_extra_kwargs"]):
            raise ValueError("opponent factory must accept a deterministic clock")
        for profile in profiles:
            for mode in modes:
                config = profile_config(profile, **{**overrides, "mode": mode})
                pairs = []
                for i in range(args.games):
                    legs = [play_game(candidate, opponent, config=config, seed=args.seed + i,
                                      candidate_camp=camp, decision_steps=args.decision_steps,
                                      calls_per_step=args.calls_per_step, diagnostics=args.diagnostics,
                                      sensitivity=sensitivity)
                            for camp in (1, 2)]
                    pairs.append({"seed": args.seed + i, "legs": legs,
                                  "candidate_pair_score": statistics.mean(g["candidate_score"] for g in legs)})
                games = [g for pair in pairs for g in pair["legs"]]
                valid = [g for g in games if not g["protocol_failure"] and not any(g["native_policy_errors"].values())]
                cells.append({
                    "opponent": opponent_info, "profile": profile, "config": asdict(config),
                    "opponent_source_unchanged": source_unchanged(opponent_info),
                    "config_sha256": config.config_hash, "pairs": pairs,
                    "physics_config_sha256": digest({k: v for k, v in asdict(config).items() if k != "mode"}),
                    "sensitivity_name": treatment_name,
                    "sensitivity": asdict(sensitivity) if sensitivity else None,
                    "treatment_sha256": sensitivity.treatment_hash if sensitivity else None,
                    "effective_physics_sha256": digest({
                        "base": {k: v for k, v in asdict(config).items() if k != "mode"},
                        "treatment": asdict(sensitivity) if sensitivity else None}),
                    "summary": {
                        "paired_seeds": len(pairs), "games": len(games), "valid_games": len(valid),
                        "protocol_forfeits": sum(g["protocol_failure"] is not None for g in games),
                        "mean_pair_score_including_protocol_forfeits":
                            statistics.mean(p["candidate_pair_score"] for p in pairs),
                        "valid_outcomes": {k: sum(g["candidate_score"] == value for g in valid)
                                           for k, value in (("wins", 1.0), ("draws", 0.5), ("losses", 0.0))},
                        "candidate_invalid": sum(g["stats"]["candidate"]["invalid"] for g in games),
                        "opponent_invalid": sum(g["stats"]["opponent"]["invalid"] for g in games),
                        "native_policy_errors": sum(sum(g["native_policy_errors"].values()) for g in games),
                    },
                })
                if args.progress:
                    print(json.dumps({"progress": True, "opponent": name, "profile": profile, "mode": mode,
                                      "policy_config": factory_config, "sensitivity": treatment_name,
                                      **cells[-1]["summary"]}, sort_keys=True), file=sys.stderr, flush=True)
    source_hashes_end = source_digests()
    unchanged = (source_hashes == source_hashes_end and source_unchanged(candidate_info)
                 and all(source_unchanged(c["opponent"]) for c in cells))
    report = {
        "schema_version": 3, "model": SENSITIVITY_VERSION if sensitivity else MODEL_VERSION,
        "base_model": MODEL_VERSION, "fidelity": "uncalibrated_contract_diagnostic_only",
        "sensitivity_registration": SENSITIVITY_REGISTRATION if sensitivity else None,
        "sensitivity_registration_sha256": SENSITIVITY_REGISTRATION_SHA256 if sensitivity else None,
        "warning": "Synthetic outcomes are NOT evidence of live HoK strength; no promotion claim is justified.",
        "official_reference": OFFICIAL_REFERENCE, "official_reference_checked": "2026-10-16",
        "official_guide_sha256": OFFICIAL_GUIDE_SHA256,
        "upstream_reference": UPSTREAM_REFERENCE, "assumptions": ASSUMPTIONS,
        "candidate": candidate_info, "source_sha256": source_hashes,
        "reserved_authorization_final_sha": approved_final_sha,
        "artifact_source_unchanged_during_run": unchanged,
        "source_sha256_end": source_hashes_end,
        "harness": {"ARENA_RECORD": os.environ["ARENA_RECORD"], "workers": 1,
                    "decision_steps": args.decision_steps, "calls_per_step": args.calls_per_step,
                    "diagnostics": args.diagnostics,
                    "latency": "in-process decide CPU/wall duration; not network latency or realtime pacing",
                    "pairing": "same world and role RNG seeds, fresh policies, camps swapped",
                    "determinism": "all evaluation factories receive scalar game clocks; policy wall-time/global RNG "
                                   "and dependencies outside each recorded hash scope remain unaudited",
                    "scheduler": "equal fixed slots, interleaved camps with alternating initiative; WAIT releases "
                                 "own remaining slots; realtime budget exhaustion advances time, never forfeits",
                    "failure_model": "explicit evaluation forfeits, NOT platform random takeover",
                    "recording": "aggregates and hashes only; no raw observations/actions/tags"},
        "cells": cells,
    }
    # Latency is deliberately excluded from the reproducibility digest.
    stable = json.loads(json.dumps(report))
    for cell in stable["cells"]:
        for pair in cell["pairs"]:
            for game in pair["legs"]:
                for stats in game["stats"].values():
                    stats.pop("latency_ms", None)
    report["reproducibility_sha256"] = digest(stable)
    return report


def main(argv=None) -> int:
    p = parser()
    args = p.parse_args(argv)
    setup_status = None
    try:
        if args.prepare_final:
            report = prepare_final_plan()
        elif args.check_interface:
            report = interface_report(args)
        elif args.archive_index:
            report = public_archive_index([json.loads(Path(path).read_text()) for path in args.archive_index])
        else:
            report = evaluate(args)
    except PolicyInterfaceError as exc:
        setup_status = 2
        report = {"status": "interface_incompatible", "interface_error": exc.code,
                  "candidate_revision": args.candidate_revision, "games": 0, "cells": [],
                  "warning": "No game played. Interface incompatibility is NOT a loss."}
    except ReservedSeedError as exc:
        setup_status = 2
        report = {"status": "authorization_required", "error": str(exc), "games": 0, "cells": [],
                  "warning": "No reserved game was constructed or played."}
    except Exception as exc:
        # Do not emit candidate exception messages, which may contain decisions.
        p.exit(2, f"Evaluation setup failed ({type(exc).__name__}); check policy/factory/config paths and values.\n")
    if args.json:
        text = json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n"
        if args.json == "-":
            sys.stdout.write(text)
        else:
            Path(args.json).write_text(text)
    if args.json != "-":
        print(report.get("warning", report.get("status")))
        for cell in report.get("cells", []):
            s = cell["summary"]
            print(f"{cell['opponent']['name']} {cell['profile']} {cell['config']['mode']}: "
                  f"{s['games']} games / {s['paired_seeds']} pairs; {s['valid_outcomes']}; "
                  f"protocol_forfeits={s['protocol_forfeits']}; "
                  f"invalid candidate/opponent={s['candidate_invalid']}/{s['opponent_invalid']}")
    if setup_status:
        return setup_status
    if args.prepare_final or args.check_interface or args.archive_index:
        return 1 if report.get("status") == "audit_conflict" else 0
    if not report["artifact_source_unchanged_during_run"]:
        print("Model/harness/policy source changed during evaluation; discard this run and rerun frozen files.",
              file=sys.stderr)
        return 2
    return 1 if any(c["summary"]["protocol_forfeits"] or c["summary"]["candidate_invalid"]
                    or c["summary"]["opponent_invalid"] or c["summary"]["native_policy_errors"]
                    for c in report["cells"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
