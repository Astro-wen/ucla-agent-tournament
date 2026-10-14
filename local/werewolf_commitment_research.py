"""Preregistered own-public-vote commitment experiment; never imported by production.

This is NOT the old coalition strategy: only our own actual public target may be
retained, on B0's two soft suspicion branches, with subsequent living echoers.
Evaluation helpers below the factories may inspect finished synthetic games;
the policy itself receives only the ordinary seat-local View.
"""
from __future__ import annotations

from dataclasses import replace

from agent.agent import Contestant
from agent.werewolf.policy import WerewolfPolicy
from local.werewolf_coordination_research import CoordinationPolicy

MODES = ("any_echo", "two_echo", "credible_echo")
SOFT_REASONS = frozenset((
    "highest suspicion among unread seats",
    "highest public suspicion (never abstain — stall rule favors wolves)",
))


class CommitmentPolicy(WerewolfPolicy):
    commitments = staticmethod(CoordinationPolicy.commitments)

    def __init__(self, mode="any_echo"):
        if mode not in MODES:
            raise ValueError(f"unknown commitment mode: {mode}")
        super().__init__()
        self.mode = mode
        self._suppress_commitment = False

    @classmethod
    def own_publication(cls, v):
        """(target, transcript position), or None. No fabricated sender/knowledge.

        me=None only disables the shared parser's self-speaker exclusion, on a
        copy containing solely this seat's actual envelopes. Never pass that
        parser-only copy to claims(), seer_knowledge(), or any decision helper.
        """
        if type(v.day) is not int or v.day < 1:
            return None
        own = [(i, m) for i, m in enumerate(v.messages)
               if isinstance(m, dict) and type(m.get("sender")) is int and m["sender"] == v.me]
        parser_view = replace(v, me=None, messages=[m for _, m in own])
        target = cls.commitments(parser_view).get(v.me)
        if target is None:
            return None
        for i, message in reversed(own):
            if cls.commitments(replace(parser_view, messages=[message])).get(v.me) == target:
                return target, i
        return None

    def echoers(self, v, publication):
        target, position = publication
        c = self.claims(v, private=True)
        excluded = set(c.liars)
        excluded.update(self.seer_knowledge(v)[0] if v.role == "seer" else c.trusted_wolves(v.alive))
        # Only commitments after our actual publication are echoes, not earlier
        # unrelated calls or a claim of "following" without a personal ballot.
        calls = self.commitments(replace(v, messages=v.messages[position + 1:]))
        return {speaker for speaker, choice in calls.items() if choice == target and speaker not in excluded}

    def retain(self, v, targets, original):
        """Only change B0's soft final ranking; return the entire B0 tuple otherwise."""
        pick, reason = original
        if (self._suppress_commitment or v.role not in ("villager", "seer")
                or reason not in SOFT_REASONS or pick is None):
            return original
        c = self.claims(v, private=True)
        wolves, clears = self.seer_knowledge(v) if v.role == "seer" else (c.trusted_wolves(v.alive), [])
        # Villager B0 represents exposed liars inside its final score, rather
        # than a separately labelled hard branch. Preserve that precedence too.
        if (wolves or any(s in targets and s not in clears for s in c.liars)
                or len(c.live(v)) >= 2):
            return original
        publication = self.own_publication(v)
        if publication is None:
            return original
        target, _ = publication
        protected = set(clears) | c.trusted_clears() | {v.me}
        if target == pick or target not in targets or target not in v.alive or target in protected:
            return original
        echoers = self.echoers(v, publication)
        if len(echoers) < (2 if self.mode == "two_echo" else 1):
            return original
        if self.mode == "credible_echo" and not echoers & CoordinationPolicy.credible(self, v):
            return original
        return target, "keep our published vote echoed by living players; not a verified role"

    def _day_vote(self, v, targets):
        return self.retain(v, targets, super()._day_vote(v, targets))

    def _wolf_vote(self, v, targets):
        # Even optional B0 public_vote tactics internally construct a villager
        # view. That must not accidentally enable a good-role experiment.
        previous, self._suppress_commitment = self._suppress_commitment, True
        try:
            return super()._wolf_vote(v, targets)
        finally:
            self._suppress_commitment = previous

    def _opening(self, v):
        previous = self._suppress_commitment
        self._suppress_commitment = previous or v.role == "wolf"
        try:
            return super()._opening(v)
        finally:
            self._suppress_commitment = previous


class CommitmentContestant(Contestant):
    def __init__(self, mode="any_echo"):
        self.commitment_mode = mode
        super().__init__()
        self.ww = CommitmentPolicy(mode)

    def _fresh(self):
        super()._fresh()
        self.ww = CommitmentPolicy(self.commitment_mode)


def any_echo():
    return CommitmentContestant("any_echo")


def two_echo():
    return CommitmentContestant("two_echo")


def credible_echo():
    return CommitmentContestant("credible_echo")


# Evaluator-only orchestration. Not reachable from policy factories.
def _paths():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    return root, root / "data/werewolf_commitment_development.json"


def _resources():
    from datetime import datetime
    import shutil
    root, _ = _paths()
    free = shutil.disk_usage(root).free
    if free < 2 * 1024 ** 3:
        raise RuntimeError("stop new experiments: free disk below 2 GiB")
    if datetime.now().timestamp() >= datetime.fromisoformat("2026-10-13T21:00:00-07:00").timestamp():
        raise RuntimeError("research handoff deadline reached")
    return free


def _hash(path):
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _update(section, value):
    import json
    _, report_path = _paths()
    report = json.loads(report_path.read_text())
    report[section] = value
    report_path.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n")


def _backgrounds():
    import json
    root, _ = _paths()
    return json.loads((root / "data/werewolf_current_baseline.json").read_text())["protocol"]["backgrounds"]


def _verify_baseline():
    import json
    root, _ = _paths()
    path = root / "data/werewolf_current_baseline.json"
    baseline = json.loads(path.read_text())
    actual = {name: _hash(root / name) for name in baseline["checked_werewolf_and_shared_runtime_files"]}
    if actual != baseline["checked_werewolf_and_shared_runtime_files"]:
        raise RuntimeError("B0 selected Werewolf/shared sources differ; do not run a mislabelled reference")
    result = {"id": baseline["id"], "baseline_json_sha256": _hash(path),
              "checked_werewolf_and_shared_runtime_files": actual, "matches": True}
    _update("baseline_verification", result)
    return result


def _source_delta(before, after):
    return {name: [before["files"].get(name), after["files"].get(name)]
            for name in sorted(before["files"].keys() | after["files"].keys())
            if before["files"].get(name) != after["files"].get(name)}


def diagnose(out):
    """Baseline-only 231 diagnostic, using exact copied seat memory pre-action."""
    from collections import Counter, defaultdict
    from copy import deepcopy
    from datetime import datetime
    import gzip
    import json
    import random
    from local import eval_werewolf as ev
    from local import werewolf_benchmark as wb
    from local import werewolf_research as wr
    from local.werewolf_research_metrics import _eliminated

    free = _resources()
    _verify_baseline()
    if out.exists():
        raise ValueError("diagnostic output must be new")
    out.mkdir(parents=True)
    source = wb.source_manifest()
    module_hash = _hash(_paths()[0] / "local/werewolf_commitment_research.py")
    scenarios = wb.build_scenarios([231], _backgrounds(), [1], 5)
    settings = {"ARENA_WW_TIMING": "rt", "ARENA_WW_TACTICS": "off", "ARENA_WW_DM": "off",
                "ARENA_RECORD": "0", "ARENA_STATE": "0", "ARENA_SUMMARY": "0"}
    manifest = {"source": source, "module_sha256": module_hash, "seed_registry": "strength",
                "split": "development", "seed": 231, "games_per_background": 5,
                "backgrounds": _backgrounds(), "settings": settings, "visibility": "strict",
                "started": datetime.now().astimezone().isoformat(), "free_disk_bytes": free}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    totals, groups, failures, examples = Counter(), defaultdict(Counter), Counter(), []
    with wb.environment(settings), gzip.open(out / "games.jsonl.gz", "wt", compresslevel=3) as stream:
        for scenario in scenarios:
            _resources()
            gseed, ours, types = ev.deal(scenario.seed, scenario.game, scenario.background, 1)
            me = ours[0]
            controlled = wb.MeasuredSeat(ev.factory("agent"), {})
            observations = []

            class DiagnosticGame(wr.ResearchGame):
                def _choose(self, seat, sub, legal):
                    row = None
                    if seat == me and sub == "day_vote":
                        frozen = deepcopy(controlled.agent.ww)
                        v = frozen._view(self.state(seat, "moving", sub, legal))
                        targets = [s for s in legal if s != 7]
                        original = frozen._day_vote(v, targets)
                        p = CommitmentPolicy()
                        publication = p.own_publication(v)
                        row = {"day": v.day, "role": v.role, "baseline": original,
                               "own_target": publication[0] if publication else None,
                               "echoers": sorted(p.echoers(v, publication)) if publication else [],
                               "candidates": {}, "observed_messages": deepcopy(v.messages)}
                        for mode in MODES:
                            p = CommitmentPolicy(mode)
                            p.import_memory(deepcopy(frozen.export_memory()))
                            row["candidates"][mode] = p.retain(v, targets, original)[0]
                    action = super()._choose(seat, sub, legal)
                    if row is not None:
                        row["actual"] = action
                        observations.append(row)
                    return action

            seats = [controlled if i == me else ev.make_seat(types[i]) for i in range(7)]
            game = DiagnosticGame(seats, random.Random(gseed), strict_messaging=True)
            returns = game.play()
            fail = {k: game.stats[k][me] for k in ev.FAIL_KEYS}
            fail.update(fallbacks=controlled.agent.fallbacks, overruns=controlled.agent.overruns,
                        policy_errors=controlled.policy_errors, policy_fallbacks=controlled.policy_fallbacks,
                        logged_errors=controlled.logged_errors,
                        listen_errors=controlled.agent.listen_stats.get("errors", 0))
            failures.update(fail)
            group = groups[f"{scenario.background}/{game.roles[me]}"]
            for counter in (totals, group):
                counter["games"] += 1
            for row in observations:
                ballot = next(r for r in game.votes if r["day"] == row["day"])
                mismatch = row["own_target"] is not None and row["own_target"] != row["actual"]
                flags = {"day_votes": 1, "baseline_reconstruction_mismatch": row["baseline"][0] != row["actual"],
                         "own_published": row["own_target"] is not None,
                         "own_actual_mismatch": mismatch,
                         "good_day_votes": row["role"] != "wolf",
                         "good_own_actual_mismatch": mismatch and row["role"] != "wolf"}
                row["pivotal"] = {}
                for mode, candidate in row["candidates"].items():
                    changed = candidate != row["actual"]
                    eliminated = _eliminated(ballot["votes"] | {me: candidate}) if changed else ballot["eliminated"]
                    pivotal = changed and eliminated != ballot["eliminated"]
                    faction = lambda target: None if target is None else ("wolf" if game.roles[target] == "wolf" else "good")
                    faction_pivotal = pivotal and faction(eliminated) != faction(ballot["eliminated"])
                    flags.update({f"{mode}/eligible_mismatch": changed, f"{mode}/elimination_pivotal": pivotal,
                                  f"{mode}/faction_pivotal": faction_pivotal})
                    row["pivotal"][mode] = {"elimination": pivotal, "faction": faction_pivotal,
                                              "actual_eliminated": ballot["eliminated"], "alternative_eliminated": eliminated}
                for counter in (totals, group):
                    counter.update(flags)
                if mismatch and len(examples) < 6 and row["role"] != "wolf":
                    examples.append({"scenario": scenario.key, "seat": me, **deepcopy(row), "ballots": ballot["votes"]})
                row.pop("observed_messages")
            record = {"id": scenario.key, "seat": me, "role": game.roles[me], "deal_seed": gseed,
                      "win": returns[me] > 0, "failures": fail, "decisions": observations}
            if scenario.game == 0:
                record["trace"] = {"messages": game.messages[:100], "actions": game.action_trace, "votes": game.votes}
            stream.write(json.dumps(record, separators=(",", ":")) + "\n")
    end = wb.source_manifest()
    result = {"out": str(out), "totals": totals, "by_background_role": groups, "failures": failures,
              "examples": examples, "source_digest_start": source["digest"], "source_digest_end": end["digest"],
              "source_changes": _source_delta(source, end), "module_sha256": module_hash,
              "module_unchanged": module_hash == _hash(_paths()[0] / "local/werewolf_commitment_research.py"),
              "scope": "baseline-only development subset, reused in 231 screen; not independent win evidence",
              "ended": datetime.now().astimezone().isoformat()}
    (out / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    _update("diagnostic_results", result)
    print(json.dumps(dict(totals), sort_keys=True), flush=True)
    return result


def run_screen(stage, out):
    """Invoke the unchanged existing runner with the exact preregistered panel."""
    import json
    from local import werewolf_benchmark as wb
    from local import werewolf_research as wr
    _resources()
    _verify_baseline()
    root, report_path = _paths()
    report = json.loads(report_path.read_text())
    if stage == "development":
        modes, seed, games = MODES, 231, 100
    else:
        metrics = report["development_results"]["summary"]["metrics"]
        tie_order = ("two_echo", "credible_echo", "any_echo")
        chosen = max(tie_order, key=lambda mode: metrics[mode]["all"]["seat"]["delta"])
        modes, seed, games = (chosen,), 232, 200
        _update("selection_result", {"mode": chosen, "seed231_delta": metrics[chosen]["all"]["seat"]["delta"],
                                     "positive": metrics[chosen]["all"]["seat"]["delta"] > 0,
                                     "rule": report["selection"], "new_variants": False})
    module_hash = _hash(root / "local/werewolf_commitment_research.py")
    argv = ["--profile", "reference=agent"]
    for mode in modes:
        argv += ["--profile", f"{mode}=local.werewolf_commitment_research:{mode}"]
    argv += ["--seed-registry", "strength", "--split", "development", "--seeds", str(seed),
             "--games", str(games), "--backgrounds", ",".join(_backgrounds()), "--timing", "rt",
             "--visibility", "strict", "--jobs", "1", "--trace-games", "3",
             "--stop-at", "2026-10-13T20:58:00-07:00", "--out", str(out)]
    _update(f"{stage}_started", {"argv": argv, "module_sha256": module_hash})
    summary = wr.main(argv)
    manifest = json.loads((out / "manifest.json").read_text())
    end_source = wb.source_manifest()
    result = {"out": str(out), "summary": summary, "module_sha256": module_hash,
              "module_unchanged": module_hash == _hash(root / "local/werewolf_commitment_research.py"),
              "source_digest_start": manifest["source"]["digest"],
              "source_changes": _source_delta(manifest["source"], end_source),
              "raw_sha256": _hash(out / "games.jsonl.gz"),
              "manifest_sha256": _hash(out / "manifest.json"),
              "summary_sha256": _hash(out / "summary.json"),
              "classification": "exploratory local development; never promotion evidence"}
    _update(f"{stage}_results", result)
    return result


def main(argv=None):
    import argparse
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("diagnostic", "development", "replication"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    return diagnose(args.out) if args.stage == "diagnostic" else run_screen(args.stage, args.out)


if __name__ == "__main__":
    main()
