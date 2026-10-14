from dataclasses import replace

from agent.agent import Contestant
from agent.werewolf.policy import PRESSURE, WerewolfPolicy
from agent.werewolf.state import read_chat


INVESTIGATIONS = ("baseline", "intended")
DISCLOSURES = ("baseline", "first_clear", "at_risk")
FACTORIES = ("baseline", "target_only", "clear_only", "joint_clear", "risk_only", "joint_risk")
BACKGROUNDS = ("first", "random", "heuristic", "llm", "llmx", "llmq", "mixed", "rt_wolfpack",
               "rt_talker", "rt_detective", "rt_chaos", "pop_late_claim", "pop_coordinated",
               "pop_pressure_bus", "pop_observed_switch")


class SeerDesignPolicy(WerewolfPolicy):
    MATCH_MEMORY = WerewolfPolicy.MATCH_MEMORY + ("_first_intent",)

    def __init__(self, investigation="baseline", disclosure="baseline"):
        if investigation not in INVESTIGATIONS or disclosure not in DISCLOSURES:
            raise ValueError("unknown seer-design factor level")
        super().__init__()
        self.investigation = investigation
        self.disclosure = disclosure
        self._first_intent = None

    @staticmethod
    def intended_public_target(v, targets=None):
        pool = [s for s in (v.others_alive if targets is None else targets) if s in v.alive and s != v.me]
        public = replace(v, role="villager", allies=[], investigations={}, evidence=None,
                         messages=[m for m in v.messages if isinstance(m, dict)
                                   and not m.get("recipients") and m.get("visibility") != "direct"])
        return WerewolfPolicy._top(WerewolfPolicy._scores(public, private=False), pool)

    def _investigate(self, v, targets):
        living = [s for s in targets if s in v.alive and s != v.me]
        unread = [s for s in living if s not in v.investigations]
        if v.role == "seer" and v.day == 1 and self._first_intent is None:
            self._first_intent = [1, self.intended_public_target(v)]
        if self.investigation == "intended" and v.day == 1 and unread:
            return self.intended_public_target(v, unread), "check an anticipated public vote target, not a known future vote"
        return super()._investigate(v, living)

    def first_intent(self, v):
        saved = self._first_intent
        if isinstance(saved, (tuple, list)) and len(saved) == 2 and saved[0] == 1:
            return saved[1]
        return self.intended_public_target(replace(v, messages=[]))

    def _seer_chat(self, v, c):
        if v.role != "seer" or v.me not in v.alive or v.me in v.dead:
            return None
        original = super()._seer_chat(v, c)
        if original is not None or self.disclosure == "baseline" or v.day != 1:
            return original
        _, clears = self.seer_knowledge(v)
        if not clears:
            return None
        intended = self.first_intent(v)
        endangered = intended in clears or any(self.pressure_on(v, s) >= PRESSURE for s in clears)
        if self.disclosure == "at_risk" and not endangered:
            return None
        target, _ = self._day_vote(v, v.others_alive)
        text = "I am the Seer. " + " ".join(f"Player{s} is clear." for s in clears[:4])
        text += " Do not vote them."
        if target is not None and target not in clears:
            text += f" I vote Player{target}."
        return "claim", {"content": text, "recipients": []}

    def chat(self, st):
        v = self._view(st)
        if v.role == "seer" and (v.me not in v.alive or v.me in v.dead or st.get("eliminated")
                                 or st.get("is_terminal") or st.get("retired")):
            return []
        return super().chat(st)


class SeerDesignContestant(Contestant):
    def __init__(self, investigation="baseline", disclosure="baseline"):
        self.design = investigation, disclosure
        super().__init__()
        self.ww = SeerDesignPolicy(*self.design)

    def _fresh(self):
        super()._fresh()
        self.ww = SeerDesignPolicy(*self.design)


def baseline():
    return SeerDesignContestant()


def target_only():
    return SeerDesignContestant("intended")


def clear_only():
    return SeerDesignContestant("baseline", "first_clear")


def joint_clear():
    return SeerDesignContestant("intended", "first_clear")


def risk_only():
    return SeerDesignContestant("baseline", "at_risk")


def joint_risk():
    return SeerDesignContestant("intended", "at_risk")


def seer_design_metrics(record, trace):
    if record["role"] != "seer":
        return {}
    me = record["seat"]
    roles, deaths, actions = trace["roles"], trace["dead"], trace["actions"]
    first = next((v for v in trace["votes"] if v["day"] == 1), None)
    eliminated = first["eliminated"] if first else None
    death = next((d for d in deaths if d["seat"] == me), None)
    n2_death = bool(death and death["day"] == 2 and death["cause"] == "night_kill")
    n2_exposed = any(a["seat"] == me and a["phase"] == "night_seer" and a["day"] == 2 for a in actions)
    checked = {}
    for action in actions:
        if action["seat"] == me and action["phase"] == "night_seer" and action["day"] <= 2:
            checked.setdefault(int(action["choice"]), action["day"])
    reports = set()
    clear_claim = False
    for m in trace["messages"]:
        if m.get("day") != 1 or m.get("sender") != me or m.get("recipients") or m.get("visibility") != "broadcast":
            continue
        facts = read_chat(m.get("content"), me)
        if facts.injection:
            continue
        clear_claim |= facts.claim and bool(facts.clears)
        for target, day in checked.items():
            if day <= m["day"] and ((roles[target] == "wolf" and target in facts.wolves)
                                    or (roles[target] != "wolf" and target in facts.clears)):
                reports.add(target)
    first_check = next((a for a in actions if a["seat"] == me and a["phase"] == "night_seer" and a["day"] == 1), None)
    n1_target = int(first_check["choice"]) if first_check else None
    return {"first_day_innocent_elimination": int(eliminated is not None and roles[eliminated] != "wolf"),
            "first_day_wolf_elimination": int(eliminated is not None and roles[eliminated] == "wolf"),
            "first_day_no_elimination": int(first is not None and eliminated is None),
            "first_day_seer_elimination": int(eliminated == me),
            "first_day_clear_claim": int(clear_claim), "night_two_exposure": int(n2_exposed),
            "night_two_seer_death": int(n2_death),
            "unpublished_checks_at_night_two_death": len(set(checked) - reports) if n2_death else 0,
            "completed_checks_at_night_two_death": len(checked) if n2_death else 0,
            "first_check_clear": int(n1_target is not None and roles[n1_target] != "wolf")}


def study_task(task):
    from .werewolf_research import run_task
    scenario, profiles, settings = task
    records = run_task((scenario, profiles, settings, True, True))
    reference = records[0]
    mirror = next(r for r in records if r["profile"] == "baseline")
    equal = not any("harness_error" in r for r in (reference, mirror)) and all(
        reference.get(key) == mirror.get(key) for key in ("seat", "role", "deal_seed", "win", "days", "chats", "dms", "trace"))
    for record in records:
        if "harness_error" not in record:
            record["seer_design"] = seer_design_metrics(record, record.pop("trace"))
            record["alloff_mirror"] = equal
    return records


def study_chunk(tasks):
    return [study_task(task) for task in tasks]


def bounded_study_outputs(pool, tasks, workers):
    from collections import deque
    from itertools import islice
    tasks, pending = iter(tasks), deque()

    def submit():
        chunk = tuple(islice(tasks, 10))
        if chunk:
            pending.append(pool.submit(study_chunk, chunk))

    for _ in range(workers * 2):
        submit()
    try:
        while pending:
            yield from pending.popleft().result()
            submit()
    finally:
        for future in pending:
            future.cancel()


def interaction_report(records, clear_kind):
    from .werewolf_research_metrics import factorial_contrasts
    selected = {"baseline": (0, 0), "target_only": (1, 0),
                "clear_only" if clear_kind == "first_clear" else "risk_only": (0, 1),
                "joint_clear" if clear_kind == "first_clear" else "joint_risk": (1, 1)}
    rows = []
    for r in records:
        if r["profile"] not in selected:
            continue
        target, disclosure = selected[r["profile"]]
        rows.append({"id": r["id"], "scenario": r["scenario"], "wins": [r["win"]],
                     "deal": {"seed": r["deal_seed"], "seats": [r["seat"]], "roles": [r["role"]],
                              "background": r["scenario"]["background"]},
                     "factors": {"intended_target": target, "disclosure": disclosure}, **r["seer_design"]})
    overall = factorial_contrasts(rows, ("intended_target", "disclosure"))
    seer_rows = [r for r in rows if r["deal"]["roles"] == ["seer"]]
    diagnostics = {metric: factorial_contrasts(seer_rows, ("intended_target", "disclosure"), metric=metric)
                   for metric in ("first_day_innocent_elimination", "night_two_seer_death",
                                  "unpublished_checks_at_night_two_death")}
    opponents = {bg: factorial_contrasts([r for r in rows if r["scenario"]["background"] == bg],
                                        ("intended_target", "disclosure"))
                 for bg in sorted({r["scenario"]["background"] for r in rows})}
    return {"win": overall, "diagnostics": diagnostics, "opponents": opponents}


def run_study(argv=None):
    import argparse
    from collections import Counter, defaultdict
    from concurrent.futures import ProcessPoolExecutor
    from datetime import datetime
    import gzip
    import json
    from pathlib import Path
    import time
    from . import werewolf_benchmark as wb
    from .werewolf_research import PairCounts, summarize_batch

    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, choices=(206, 208), required=True)
    parser.add_argument("--games", type=int, default=300)
    parser.add_argument("--backgrounds", default=",".join(BACKGROUNDS))
    parser.add_argument("--jobs", type=int, choices=(1, 2), default=2)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.seed not in wb.SEED_REGISTRIES["overnight"][0]:
        parser.error("unregistered development seed")
    if args.out.resolve().parent != (wb.ROOT / "logs" / "werewolf_benchmarks").resolve() or args.out.exists():
        parser.error("a new direct child of logs/werewolf_benchmarks is required")
    scenarios = wb.build_scenarios([args.seed], args.backgrounds.split(","), [1], args.games)
    profiles = [("reference", "agent", {})] + [
        (name, f"local.werewolf_seer_design_research:{name}", {}) for name in FACTORIES]
    settings = {"ARENA_RECORD": "0", "ARENA_STATE": "0", "ARENA_SUMMARY": "0", "ARENA_WW_TIMING": "rt",
                "ARENA_WW_TACTICS": "off", "ARENA_WW_DM": "off"}
    source = wb.source_manifest()
    args.out.mkdir()
    manifest = {"config": vars(args) | {"out": str(args.out)}, "profiles": profiles, "settings": settings,
                "source": source, "planned_scenarios": len(scenarios), "planned_games": len(scenarios) * len(profiles),
                "visibility": "strict", "started": datetime.now().astimezone().isoformat(),
                "scope": "paired local development; diagnostic conditioning is not a causal mechanism estimate"}
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    names = [p[0] for p in profiles]
    counts, failures = defaultdict(PairCounts), defaultdict(Counter)
    background, latency, maxima = defaultdict(Counter), defaultdict(Counter), defaultdict(float)
    diagnostic = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    compact = []
    completed = errors = mirror_errors = 0
    started = time.monotonic()
    tasks = [(s, profiles, settings) for s in scenarios]
    pool = ProcessPoolExecutor(args.jobs) if args.jobs > 1 else None
    outputs = bounded_study_outputs(pool, tasks, args.jobs) if pool else map(study_task, tasks)
    try:
        with gzip.open(args.out / "games.jsonl.gz", "wt", compresslevel=3) as stream:
            for records in outputs:
                stream.writelines(json.dumps(r, separators=(",", ":")) + "\n" for r in records)
                good = summarize_batch(records, names, counts, failures, background, latency, maxima)
                errors += not good
                if good:
                    mirror_errors += not records[0]["alloff_mirror"]
                    for r in records:
                        compact.append({k: r[k] for k in ("id", "scenario", "profile", "seat", "role", "deal_seed", "win", "seer_design")})
                        for scope in ("all", r["scenario"]["background"]):
                            for metric, value in r["seer_design"].items():
                                diagnostic[f"{r['profile']}/{scope}"][metric][0] += value
                                diagnostic[f"{r['profile']}/{scope}"][metric][1] += 1
                completed += 1
                if completed % 100 == 0:
                    stream.flush()
                    print(f"{completed}/{len(scenarios)} complete paired scenarios", flush=True)
    finally:
        if hasattr(outputs, "close"):
            outputs.close()
        if pool:
            pool.shutdown(wait=True, cancel_futures=True)
    metrics = defaultdict(lambda: defaultdict(dict))
    for (profile, scope, role), counter in counts.items():
        metrics[profile][scope][role] = counter.report()
    end = wb.source_manifest()
    own = "local/werewolf_seer_design_research.py"
    summary = {"metrics": metrics, "seer_diagnostics": diagnostic,
               "interactions": {level: interaction_report(compact, level) for level in ("first_clear", "at_risk")} if not errors else {},
               "completed_scenarios": completed, "games": completed * len(profiles),
               "gates": {"complete": completed == len(scenarios) and errors == 0,
                         "alloff_wholegame_mirror": mirror_errors == 0,
                         "source_unchanged": source["digest"] == end["digest"],
                         "own_source_unchanged": source["files"][own] == end["files"][own],
                         "no_policy_or_protocol_failures": all(not any(v for k, v in c.items() if k != "window_close") for c in failures.values()),
                         "no_background_failures": all(not any(v for k, v in c.items() if k != "window_close") for c in background.values())},
               "failures": failures, "background_failures": background,
               "latency": {p: {"max_ms": maxima[p], "p99_upper_ms": wb.histogram_percentile(latency[p], .99)} for p in names},
               "elapsed_s": time.monotonic() - started, "source_end": end,
               "promotion": "not assessed; development only; interactions use approximate deal-clustered normal intervals"}
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    for profile in FACTORIES:
        print(profile, json.dumps(metrics[profile]["all"], sort_keys=True), flush=True)
    print("integrity", summary["gates"], flush=True)
    return summary


if __name__ == "__main__":
    run_study()
