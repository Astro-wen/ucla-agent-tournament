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
import sys

os.environ["ARENA_RECORD"] = "0"

from local.hok_env import (accepts_clock, candidate_factory, candidate_revision_factory, import_factory,
                           opponent_factory, play_game, source_info, source_unchanged)
from local.hok_sim import (ASSUMPTIONS, MODEL_VERSION, OFFICIAL_GUIDE_SHA256, OFFICIAL_REFERENCE, PROFILES,
                           UPSTREAM_REFERENCE, digest, profile_config)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    candidate = p.add_mutually_exclusive_group()
    candidate.add_argument("--candidate-dir", help="repo or directory containing HokPolicy's policy.py")
    candidate.add_argument("--candidate-factory", help="importable.module:factory accepting optional seed/clock")
    candidate.add_argument("--candidate-revision", help="local Git commit/ref, loaded as a fixed HoK package snapshot")
    p.add_argument("--opponent", action="append", help="random, first-legal, idle, push, research:<name>, module:factory")
    p.add_argument("--games", type=int, default=2, help="paired seeds per configuration (TWO games each; default 2)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--mode", choices=("realtime", "lockstep", "both"),
                   help="override config mode (default config value, otherwise realtime)")
    p.add_argument("--profile", choices=(*PROFILES, "all"), default="default")
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
    if args.decision_steps < 1 or args.calls_per_step < 5:
        raise ValueError("decision-steps >= 1 and calls-per-step >= 5 required")
    if args.candidate_revision:
        candidate, candidate_info = candidate_revision_factory(root, args.candidate_revision)
    elif args.candidate_factory:
        candidate = import_factory(args.candidate_factory)
        candidate_info = source_info(candidate)
    else:
        candidate, candidate_info = candidate_factory(args.candidate_dir or Path(__file__).resolve().parents[1])
    if not accepts_clock(candidate):
        raise ValueError("evaluation factory must accept a deterministic clock")
    overrides = json.loads(Path(args.config).read_text()) if args.config else {}
    if not isinstance(overrides, dict):
        raise ValueError("--config must contain a JSON object")
    if args.max_steps is not None:
        overrides["max_steps"] = args.max_steps
    profiles = list(PROFILES) if args.profile == "all" else [args.profile]
    modes = ["realtime", "lockstep"] if args.mode == "both" else [args.mode or overrides.get("mode", "realtime")]
    cells = []
    for name in args.opponent or ["random"]:
        opponent, opponent_info = opponent_factory(name)
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
                                      calls_per_step=args.calls_per_step) for camp in (1, 2)]
                    pairs.append({"seed": args.seed + i, "legs": legs,
                                  "candidate_pair_score": statistics.mean(g["candidate_score"] for g in legs)})
                games = [g for pair in pairs for g in pair["legs"]]
                valid = [g for g in games if not g["protocol_failure"]]
                cells.append({
                    "opponent": opponent_info, "profile": profile, "config": asdict(config),
                    "opponent_source_unchanged": source_unchanged(opponent_info),
                    "config_sha256": config.config_hash, "pairs": pairs,
                    "physics_config_sha256": digest({k: v for k, v in asdict(config).items() if k != "mode"}),
                    "summary": {
                        "paired_seeds": len(pairs), "games": len(games), "valid_games": len(valid),
                        "protocol_forfeits": len(games) - len(valid),
                        "mean_pair_score_including_protocol_forfeits":
                            statistics.mean(p["candidate_pair_score"] for p in pairs),
                        "valid_outcomes": {k: sum(g["candidate_score"] == value for g in valid)
                                           for k, value in (("wins", 1.0), ("draws", 0.5), ("losses", 0.0))},
                        "candidate_invalid": sum(g["stats"]["candidate"]["invalid"] for g in games),
                        "opponent_invalid": sum(g["stats"]["opponent"]["invalid"] for g in games),
                    },
                })
    source_hashes_end = source_digests()
    unchanged = (source_hashes == source_hashes_end and source_unchanged(candidate_info)
                 and all(source_unchanged(c["opponent"]) for c in cells))
    report = {
        "schema_version": 2, "model": MODEL_VERSION, "fidelity": "uncalibrated_contract_diagnostic_only",
        "warning": "Synthetic outcomes are NOT evidence of live HoK strength; no promotion claim is justified.",
        "official_reference": OFFICIAL_REFERENCE, "official_reference_checked": "2026-10-16",
        "official_guide_sha256": OFFICIAL_GUIDE_SHA256,
        "upstream_reference": UPSTREAM_REFERENCE, "assumptions": ASSUMPTIONS,
        "candidate": candidate_info, "source_sha256": source_hashes,
        "artifact_source_unchanged_during_run": unchanged,
        "source_sha256_end": source_hashes_end,
        "harness": {"ARENA_RECORD": os.environ["ARENA_RECORD"], "workers": 1,
                    "decision_steps": args.decision_steps, "calls_per_step": args.calls_per_step,
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
    try:
        report = evaluate(args)
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
        print(report["warning"])
        for cell in report["cells"]:
            s = cell["summary"]
            print(f"{cell['opponent']['name']} {cell['profile']} {cell['config']['mode']}: "
                  f"{s['games']} games / {s['paired_seeds']} pairs; {s['valid_outcomes']}; "
                  f"protocol_forfeits={s['protocol_forfeits']}; "
                  f"invalid candidate/opponent={s['candidate_invalid']}/{s['opponent_invalid']}")
    if not report["artifact_source_unchanged_during_run"]:
        print("Model/harness/policy source changed during evaluation; discard this run and rerun frozen files.",
              file=sys.stderr)
        return 2
    return 1 if any(c["summary"]["protocol_forfeits"] or c["summary"]["candidate_invalid"]
                    or c["summary"]["opponent_invalid"] for c in report["cells"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
