from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import gzip
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local import eval_werewolf as ev
from local.werewolf_factorial_research import FACTORS
from local.werewolf_research_metrics import factorial_contrasts


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def analyze(directory):
    manifest = json.loads((directory / "manifest.json").read_text())
    summary = json.loads((directory / "summary.json").read_text())
    if not summary["gates"]["complete"]:
        raise ValueError("incomplete experimental grid")
    profiles = manifest["profiles"]
    mapping = {label: 0 if label == "reference" else int(label.removeprefix("f")) for label, _, _ in profiles}
    if set(mapping.values()) != set(range(64)) or len(mapping) != 64:
        raise ValueError("expected the complete six-factor design")
    factor_values = [{name: int(bool(mask & (1 << i))) for i, name in enumerate(FACTORS)} for mask in range(64)]
    rows, identities = [], {}
    grouped = defaultdict(list)
    raw_count = 0
    data = directory / "games.jsonl.gz"
    with gzip.open(data, "rt") as stream:
        for line in stream:
            raw = json.loads(line)
            raw_count += 1
            if "harness_error" in raw:
                raise ValueError("harness error in factorial sample")
            if any(v for k, v in raw["failures"].items() if k != "window_close"):
                raise ValueError("strategy or protocol failure in sample")
            if any(v for k, v in raw["background_failures"].items() if k != "window_close"):
                raise ValueError("background failure in sample")
            if raw["id"] not in identities:
                s = raw["scenario"]
                gseed, ours, background = ev.deal(s["seed"], s["game"], s["background"], 1)
                if raw["deal_seed"] != gseed or [raw["seat"]] != ours:
                    raise ValueError("deal differs from deterministic manifest")
                identities[raw["id"]] = (s, {"seed": gseed, "seats": ours, "roles": [raw["role"]], "background": background})
            scenario, deal = identities[raw["id"]]
            if raw["scenario"] != scenario or raw["role"] != deal["roles"][0] or raw["seat"] != deal["seats"][0] or raw["deal_seed"] != deal["seed"]:
                raise ValueError("pair metadata changed between profiles")
            rec = {"id": raw["id"], "scenario": scenario, "deal": deal, "wins": [raw["win"]],
                   "factors": factor_values[mapping[raw["profile"]]]}
            rows.append(rec)
            grouped[scenario["background"]].append(rec)
    expected = [f"{seed}/{bg}/k1/{g}" for seed in manifest["config"]["seeds"]
                for bg in manifest["config"]["backgrounds"].split(",") for g in range(manifest["config"]["games"])]
    if raw_count != manifest["planned_games"]:
        raise ValueError("record count differs from planned simulations")
    overall = factorial_contrasts(rows, FACTORS, expected_ids=expected)
    per_opponent = {}
    for background, samples in grouped.items():
        report = factorial_contrasts(samples, FACTORS)
        per_opponent[background] = {role: value["effects"] for role, value in report["scopes"].items() if role != "role_balanced"}
    relevant = ["agent/agent.py", "agent/werewolf/policy.py", "agent/werewolf/state.py", "local/eval_werewolf.py",
                "local/werewolf_env.py", "local/werewolf_research.py", "local/werewolf_belief_research.py",
                "local/werewolf_coordination_research.py", "local/werewolf_factorial_research.py",
                "local/werewolf_population_research.py", "local/werewolf_guard_bots.py", "local/werewolf_simple.py"]
    unchanged = {name: digest(ROOT / name) == manifest["source"]["files"][name] for name in relevant}
    output = {"manifest": manifest, "summary_gates": summary["gates"], "raw_sha256": digest(data),
              "raw_records": raw_count, "factorial": overall, "opponent_effects": per_opponent,
              "summary_sha256": digest(directory / "summary.json"), "relevant_sources_still_match": unchanged,
              "diagnostic_totals_and_pair_metrics": str(directory / "summary.json"),
              "latency": summary["latency"], "failures": summary["failures"], "background_failures": summary["background_failures"],
              "scope": "development only; normal cluster intervals not adjusted for selection or 21 effect tests"}
    print(directory.name, "simulations", raw_count, "core source matching", all(unchanged.values()), flush=True)
    for role in ("seat", "wolf", "seer", "villager"):
        effects = overall["scopes"][role]["effects"]
        print(role, {name: {"pp": 100 * value["mean"], "ci": [None if x is None else 100*x for x in value["ci95"]],
                            "n": value["n"]} for name, value in effects.items() if ":" not in name}, flush=True)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("directories", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.out.exists():
        parser.error("refuse to overwrite previous analysis")
    report = {"created": datetime.now().astimezone().isoformat(), "analysis_script_sha256": digest(Path(__file__)),
              "runs": {directory.name: analyze(directory) for directory in args.directories}}
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    main()
