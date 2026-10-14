"""Frozen, local-only B0 wolf role-bluff replication; never an authority/instruction spoof.

Three predeclared variants change ONLY _wolf_chat. A public default target includes
our seat (an information-less table prediction), and ignores all own private facts.
The fake result names the highest-public-suspicion living nonwolf, low seat on ties.
The unchanged `counter` topic is only the existing guarded wolf-claim/dedup slot:
there need not have been an accuser. The text never invents an accuser. Actual B0
counterclaims/bus speech take priority; later claim fights/night kills remain B0.
`f0` is the byte-action/speech mirror; Contestant._fresh preserves each profile.

No policy gets a Game, seed, opponent label, or another seat's private facts.
Evaluator code below main's boundary may inspect completed synthetic games only.
"""
from __future__ import annotations

from agent.agent import Contestant
from agent.werewolf.policy import WerewolfPolicy
from agent.werewolf.state import View

PROFILES = ("proactive_all", "only_me_public_target", "only_ally_public_target")
TIE_ORDER = ("only_me_public_target", "only_ally_public_target", "proactive_all")
CLAIM_TEMPLATE = "I am the Seer. Player{target} is a WOLF. Everyone vote Player{target} today."
PREDECLARATION = {
    "hypothesis": "Risk-gated ordinary false claims might rescue a wolf targeted by the public default; not assumed stronger.",
    "profiles": {
        "proactive_all": "Day 1 before any public seer claim, any wolf: broad proactive control.",
        "only_me_public_target": "Same, only if the public default target equals our seat.",
        "only_ally_public_target": "Same, only if the public default target is our known wolf ally.",
    },
    "public_target": "B0 public suspicion on an explicit fields-only villager View, private=False, all living seats including self, low-seat ties; no private evidence, allies, checks or DMs.",
    "fake_target": "Same public scores restricted to living other seats minus the known ally; require a known ally.",
    "template": CLAIM_TEMPLATE,
    "topic": "counter means existing guarded wolf-claim dedup slot, NOT that there was an accuser",
    "unchanged": ["private_knowledge_role", "outgoing_chat_guard", "all nonwolf behavior", "all ballots", "night kills", "bus behavior", "wrapper_fresh", "50 word/1024 byte/5 chat limits"],
    "development": {"registry": "strength", "seed": 231, "games_per_background": 100},
    "replication": {"registry": "strength", "seed": 232, "games_per_background": 200},
    "selection": "Exactly one replication: max overall seat delta among integrity-clean positive seat+wolf deltas; otherwise max seat delta diagnostic. Ties: only_me, only_ally, proactive_all. No edits/tuning after results.",
    "evaluation": "All 28 B0 backgrounds, reference=agent, k=1, strict moving-message clear, rt, one worker; f0 mirror included. Existing benchmark gates unchanged.",
    "scope": "Development and replication are exploratory local scripted tables, not heldout/live strength. No model calls.",
}


def public_view(v):
    """Whitelist public View fields; do not carry even known private-field names."""
    return View(me=v.me, role="villager", day=v.day, sub_phase=v.sub_phase,
                alive=list(v.alive), dead=dict(v.dead), dead_cause=dict(v.dead_cause),
                votes=[(day, dict(votes), eliminated) for day, votes, eliminated in v.votes],
                names=dict(v.names), messages=[dict(m) for m in v.messages if isinstance(m, dict)
                                               and m.get("visibility") != "direct" and not m.get("recipients")])


def public_default_target(v):
    public = public_view(v)
    return WerewolfPolicy._top(WerewolfPolicy._scores(public, private=False), public.alive)


class BluffPolicy(WerewolfPolicy):
    def __init__(self, profile="f0"):
        if profile not in ("f0", *PROFILES):
            raise ValueError("unknown fixed bluff profile")
        super().__init__()
        self.profile = profile

    def _wolf_chat(self, v):
        if v.role != "wolf":
            return None
        original = super()._wolf_chat(v)
        if original is not None or self.profile == "f0":
            return original
        if (v.day != 1 or v.sub_phase != "day_vote" or v.me not in v.alive
                or "counter" in self._topics or self.claims(v, private=False).order):
            return None
        # Missing ally knowledge must not accidentally frame a real teammate.
        if len(v.allies) != 1 or v.allies[0] == v.me or v.allies[0] not in range(7):
            return None
        public = public_view(v)
        scores = WerewolfPolicy._scores(public, private=False)
        predicted = WerewolfPolicy._top(scores, public.alive)
        if self.profile == "only_me_public_target" and predicted != v.me:
            return None
        if self.profile == "only_ally_public_target" and predicted not in v.allies:
            return None
        pool = [s for s in v.others_alive if s not in v.allies]
        target = WerewolfPolicy._top(scores, pool)
        if target is None:
            return None
        return "counter", {"content": CLAIM_TEMPLATE.format(target=target), "recipients": []}


class BluffContestant(Contestant):
    def __init__(self, profile="f0"):
        if profile not in ("f0", *PROFILES):
            raise ValueError("unknown fixed bluff profile")
        self.bluff_profile = profile
        super().__init__()
        self.ww = BluffPolicy(profile)

    def _fresh(self):
        super()._fresh()
        self.ww = BluffPolicy(self.bluff_profile)


def f0():
    return BluffContestant()


def proactive_all():
    return BluffContestant("proactive_all")


def only_me_public_target():
    return BluffContestant("only_me_public_target")


def only_ally_public_target():
    return BluffContestant("only_ally_public_target")


# Evaluator boundary: these functions are never called from policy decisions.
def select_replication(reports):
    clean = [p for p in PROFILES if reports[p]["integrity_clean"]
             and reports[p]["primary"]["delta"] > 0
             and reports[p]["metrics"]["all"]["wolf"]["delta"] > 0]
    pool = clean or list(PROFILES)
    selected = max(pool, key=lambda p: (reports[p]["primary"]["delta"], -TIE_ORDER.index(p)))
    return selected, "positive_promising" if clean else "closest_diagnostic_only"


def _compact_record(record):
    import hashlib
    import json
    import re
    trace = record.pop("trace")
    record["trace_sha256"] = hashlib.sha256(json.dumps(trace, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    me = record["seat"]
    messages = [m["content"] for m in trace["messages"] if m.get("sender") == me
                and m.get("day") == 1 and not m.get("recipients")]
    targets = [int(m[1]) for text in messages if (m := re.fullmatch(
        r"I am the Seer\. Player([0-6]) is a WOLF\. Everyone vote Player\1 today\.", text))]
    first = next((v for v in trace["votes"] if v["day"] == 1), None)
    eliminated = first["eliminated"] if first else None
    roles = trace["roles"]
    record["bluff"] = {"day1_own_broadcasts": messages, "proactive_claim": bool(targets) and record["role"] == "wolf",
                       "claim_target": targets[0] if targets else None,
                       "day1_self_eliminated": eliminated == me,
                       "day1_wolf_eliminated": eliminated is not None and roles[eliminated] == "wolf",
                       "day1_ally_eliminated": record["role"] == "wolf" and eliminated is not None and eliminated != me and roles[eliminated] == "wolf",
                       "day1_fake_target_eliminated": bool(targets) and eliminated == targets[0]}
    record.pop("diagnostics", None)
    return record


def _benchmark_record(record, variant):
    return {"id": record["id"], "variant": variant, "deal": {"seed": record["deal_seed"],
            "seats": [record["seat"]], "roles": [record["role"]], "background": record["scenario"]["background"]},
            "wins": [record["win"]], "failures": record["failures"],
            "background_failures": record["background_failures"], "latency_histogram": record["latency_histogram"],
            "max_ms": record["max_ms"]}


def run_study(out, stop_at):
    """One bounded process, fixed seeds, compressed minimal records, no silent errors."""
    from collections import Counter
    from datetime import datetime
    import gzip
    import hashlib
    import json
    from pathlib import Path
    import shutil
    import time
    from . import werewolf_benchmark as wb
    from .werewolf_research import run_task

    root = wb.ROOT
    out = Path(out).resolve()
    summary_path = root / "data/werewolf_bluff_development.json"
    if out.exists() or summary_path.exists() or out.parent != Path("/tmp").resolve():
        raise ValueError("use a NEW direct /tmp directory and a NEW owned data summary")
    if stop_at.tzinfo is None or stop_at.timestamp() <= time.time():
        raise ValueError("future timezone-aware deadline required")
    if shutil.disk_usage(root).free < 2 * 1024**3:
        raise RuntimeError("less than 2 GiB disk free")
    baseline = json.loads((root / "data/werewolf_current_baseline.json").read_text())
    backgrounds = baseline["protocol"]["backgrounds"]
    assert len(backgrounds) == 28 and len(set(backgrounds)) == 28
    assert {231, 232} <= set(wb.SEED_REGISTRIES["strength"][0])
    source_start = wb.source_manifest()
    module_rel = "local/werewolf_bluff_research.py"
    module_hash = source_start["files"][module_rel]
    baseline_mismatches = [p for p, h in baseline["checked_werewolf_and_shared_runtime_files"].items()
                           if source_start["files"].get(p) != h]
    if baseline_mismatches:
        raise RuntimeError(f"B0 source mismatch: {baseline_mismatches}")
    settings = {"ARENA_WW_TIMING": "rt", "ARENA_WW_TACTICS": "off", "ARENA_WW_DM": "off",
                "ARENA_WW_WORDING": "new", "ARENA_WW_MESSAGING": "on", "ARENA_WW_SAME_NAMES": "off",
                "ARENA_RECORD": "0", "ARENA_STATE": "0", "ARENA_SUMMARY": "0", "ARENA_TC_WATCHDOG": "0"}
    out.mkdir()
    summary = {"predeclaration": PREDECLARATION, "backgrounds": backgrounds, "source_start": source_start,
               "module_sha256_frozen": module_hash, "baseline_id": baseline["id"], "baseline_mismatches": baseline_mismatches,
               "test_sha256": hashlib.sha256((root / "tests/strategy/test_werewolf_bluff_research.py").read_bytes()).hexdigest(),
               "settings": settings, "workers": 1, "out": str(out), "stop_at": stop_at.isoformat(),
               "started": datetime.now().astimezone().isoformat(), "stages": {}, "promotion": "not authorized or assessed"}

    def save():
        summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")

    # Written before the first strength call: immutable profiles, seed schedule and source hash.
    (out / "manifest.json.gz").write_bytes(gzip.compress(json.dumps(summary, sort_keys=True).encode()))
    save()
    selected = None
    started = time.monotonic()
    for stage, seed, games in (("development", 231, 100), ("replication", 232, 200)):
        names = list(PROFILES) if selected is None else [selected]
        profiles = [("reference", "agent", {}), ("f0", f"{__package__}.werewolf_bluff_research:f0", {})]
        profiles += [(p, f"{__package__}.werewolf_bluff_research:{p}", {}) for p in names]
        scenarios = wb.build_scenarios([seed], backgrounds, [1], games)
        records = []
        completed = []
        issues = []
        mechanism = {p: Counter() for p in ("reference", "f0", *names)}
        comparisons = {p: [] for p in names}
        start_source = wb.source_manifest()
        status = "complete"
        logfile = out / f"{stage}.jsonl.gz"
        with gzip.open(logfile, "wt", compresslevel=3) as stream:
            for scenario in scenarios:
                if time.time() >= stop_at.timestamp():
                    status = "deadline_stop"
                    break
                if shutil.disk_usage(root).free < 2 * 1024**3:
                    status = "disk_stop_below_2GiB"
                    break
                with wb.environment(settings):
                    batch = run_task((scenario, profiles, settings, True, True))
                if any("harness_error" in r for r in batch):
                    issues.extend(r for r in batch if "harness_error" in r)
                    for record in batch:
                        record.pop("trace", None)
                        stream.write(json.dumps(record, separators=(",", ":")) + "\n")
                    status = "harness_error_stop"
                    break
                ref = batch[0]
                signature = tuple(ref[k] for k in ("id", "seat", "role", "deal_seed"))
                for record in batch:
                    assert tuple(record[k] for k in ("id", "seat", "role", "deal_seed")) == signature
                    must_mirror = record["profile"] == "f0" or record["role"] != "wolf"
                    if must_mirror and any(record[k] != ref[k] for k in ("win", "days", "chats", "dms", "trace")):
                        issues.append({"id": scenario.key, "profile": record["profile"], "error": "B0 mirror mismatch"})
                for record in batch:
                    _compact_record(record)
                    for key, value in record["bluff"].items():
                        if isinstance(value, bool) and record["role"] == "wolf":
                            mechanism[record["profile"]][key] += value
                    if record["role"] == "wolf":
                        mechanism[record["profile"]]["wolf_games"] += 1
                    stream.write(json.dumps(record, separators=(",", ":")) + "\n")
                for record in batch[2:]:
                    comparisons[record["profile"]].extend((_benchmark_record(ref, "reference"),
                                                            _benchmark_record(record, "candidate")))
                records.extend(batch)
                completed.append(scenario)
                if len(completed) % games == 0:
                    stream.flush()
                    print(f"{stage}: {len(completed)}/{len(scenarios)} scenarios; {time.monotonic()-started:.1f}s", flush=True)
        end_source = wb.source_manifest()
        changed = {p: {"start": start_source["files"].get(p), "end": end_source["files"].get(p)}
                   for p in start_source["files"].keys() | end_source["files"].keys()
                   if start_source["files"].get(p) != end_source["files"].get(p)}
        module_unchanged = end_source["files"].get(module_rel) == module_hash
        b0_unchanged = all(end_source["files"].get(p) == h for p, h in baseline["checked_werewolf_and_shared_runtime_files"].items())
        reports = {}
        for name in names:
            report = wb.summarize(comparisons[name], scenarios)
            report["gates"].update({"source_unchanged_during_run": start_source["digest"] == end_source["digest"],
                                   "module_frozen_unchanged": module_unchanged, "B0_unchanged": b0_unchanged,
                                   "f0_and_nonwolf_byte_trace_mirror": not issues,
                                   "all_planned_completed": len(completed) == len(scenarios)})
            report["integrity_clean"] = (not issues and len(completed) == len(scenarios) and module_unchanged and b0_unchanged
                                          and report["gates"]["zero_protocol_and_strategy_failures"]
                                          and report["gates"]["zero_background_protocol_failures"])
            report["local_evidence_pass"] = all(report["gates"].values())
            reports[name] = report
            print(stage, name, json.dumps({"all_roles": report["metrics"]["all"], "gates": report["gates"],
                                           "failures": report["failures"], "background_failures": report["background_failures"]}), flush=True)
            for bg in backgrounds:
                print(stage, name, bg, json.dumps(report["metrics"].get(f"opponent:{bg}", {})), flush=True)
        all_failures = {p: Counter() for p in ("reference", "f0", *names)}
        all_background_failures = {p: Counter() for p in ("reference", "f0", *names)}
        for record in records:
            all_failures[record["profile"]].update(record["failures"])
            all_background_failures[record["profile"]].update(record["background_failures"])
        print(stage, "all_failures_including_f0", json.dumps(all_failures), flush=True)
        print(stage, "all_background_failures_including_f0", json.dumps(all_background_failures), flush=True)
        summary["stages"][stage] = {"seed": seed, "games_per_background": games, "status": status,
                                   "completed_scenarios": len(completed), "simulations": len(records), "reports": reports,
                                   "all_failures": all_failures, "all_background_failures": all_background_failures,
                                   "mechanism_counts_wolf_only": mechanism, "issues": issues,
                                   "source_start_digest": start_source["digest"], "source_end_digest": end_source["digest"],
                                   "source_changes": changed, "exploratory_due_to_source_changes": bool(changed),
                                   "record_log": str(logfile), "record_log_bytes": logfile.stat().st_size}
        if stage == "development" and status == "complete" and len(completed) == len(scenarios):
            selected, reason = select_replication(reports)
            summary["replication_selection"] = {"profile": selected, "reason": reason}
            print("replication_selection", summary["replication_selection"], flush=True)
        save()
        if status != "complete" or not module_unchanged or not b0_unchanged or issues:
            break
    summary["elapsed_s"] = time.monotonic() - started
    summary["ended"] = datetime.now().astimezone().isoformat()
    source_end = wb.source_manifest()
    summary["source_end_digest"] = source_end["digest"]
    summary["global_source_unchanged"] = source_start["digest"] == source_end["digest"]
    summary["global_source_changes"] = {p: {"start": source_start["files"].get(p), "end": source_end["files"].get(p)}
                                         for p in source_start["files"].keys() | source_end["files"].keys()
                                         if source_start["files"].get(p) != source_end["files"].get(p)}
    summary["scope"] = "exploratory scripted development/replication; no heldouts/model calls/deployment; parent confirmation only if positive"
    save()
    return summary


def main():
    import argparse
    from datetime import datetime
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--stop-at", default="2026-10-13T20:55:00-07:00")
    args = parser.parse_args()
    run_study(args.out, datetime.fromisoformat(args.stop_at))


if __name__ == "__main__":
    main()
