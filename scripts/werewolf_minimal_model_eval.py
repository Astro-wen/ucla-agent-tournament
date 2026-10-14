from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import random
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for _name in ("ARENA_RECORD", "ARENA_STATE", "ARENA_SUMMARY"):
    os.environ[_name] = "0"

from agent.werewolf.state import read_chat
from local.werewolf_env import deal_roles
from scripts import werewolf_local_model_eval as lm
from scripts import werewolf_model_scale_eval as scale

SEED = 214
ROLES = ("villager", "seer", "wolf")
BACKGROUNDS = ("llmq", "pop_prompt_seer", "pop_coordinated")
PROFILES = (("agent", "agent"), ("firstclaim_highlook", "local.werewolf_minimal_research:firstclaim_highlook"),
            ("firstclaim_direct", "local.werewolf_minimal_research:firstclaim_direct"))
MODELS = (("qwen2.5:7b-instruct-q4_K_M", "845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e"),
          ("qwen2.5:14b-instruct-q4_K_M", "7cdf5a0187d5c58cc5d369b255592f7841d1c4696d45a8c8a9489440385b22f6"))
EXTRA_SOURCES = ("scripts/werewolf_minimal_model_eval.py", "tests/strategy/test_werewolf_minimal_model.py",
                 "scripts/werewolf_model_scale_eval.py", "tests/strategy/test_werewolf_model_scale.py")
GUARD = Path("logs/werewolf_benchmarks/scale-simple-model-guard-213/summary.json")
OUTPUT = Path("logs/werewolf_benchmarks/scale-simple-model-games-214")
REPORT = Path("data/werewolf_minimal_model_summary.json")
HARD_STOP = datetime(2026, 10, 13, 16, 30, tzinfo=lm.PDT)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    data = (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
    temporary = path.with_name(path.name + ".partial")
    temporary.write_bytes(data)
    temporary.replace(path)
    return {"path": str(path), "sha256": digest_bytes(data)}


def llm_placements(background, game, ours):
    seats = [s for s in range(7) if s != ours]
    rng = random.Random(f"minimal-model-placements:{SEED}:{background}:{game}")
    return sorted(rng.sample(seats, 2))


def build_plan():
    scenarios, selection, seen = [], [], set()
    for background in BACKGROUNDS:
        counts = Counter()
        for index in range(1000):
            seed, ours, types = lm.ev.deal(SEED, index, background, 1)
            role = deal_roles(random.Random(seed))[ours[0]]
            accepted = counts[role] < 2 and seed not in seen
            selection.append({"background": background, "game": index, "deal_seed": seed, "ours": ours[0],
                              "our_role": role, "accepted": accepted,
                              "reason": "quota_available" if accepted else "duplicate_deal" if seed in seen else "role_quota_full"})
            if accepted:
                scenarios.append({"id": f"{SEED}/{background}/{index}/llm2", "seed": SEED, "game": index,
                                  "background": background, "deal_seed": seed, "ours": ours[0], "our_role": role,
                                  "types": types, "llm_count": 2, "llm_seats": llm_placements(background, index, ours[0])})
                counts[role] += 1
                seen.add(seed)
            if all(counts[r] == 2 for r in ROLES):
                break
        else:
            raise ValueError("fixed role quotas unavailable within the predeclared scan bound")
    result = {"seed": SEED, "scenarios": scenarios, "selection": selection,
              "skipped": [s for s in selection if not s["accepted"]],
              "sampling": "role_balanced: two seats of each controlled role per background; six per role, eighteen unique deals",
              "placement": "Two other seats drawn with a private evaluator RNG keyed only by development seed/background/index, never candidate or model.",
              "schedule": "Entire 7B block then entire14B block; identical scenario and profile order; no selection using game outcomes."}
    result["sha256"] = digest_bytes(canonical(result).encode())
    return result


def validate_deadline(value, now=None):
    try:
        stop = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("explicit timezone-aware deadline required") from None
    if stop.tzinfo is None or stop > HARD_STOP or stop.timestamp() <= (time.time() if now is None else now):
        raise ValueError("deadline must be future and no later than 2026-10-13 16:30 PDT")
    return stop


def check_guard(path):
    try:
        data = path.read_bytes()
        value = json.loads(data)
    except (OSError, ValueError):
        raise ValueError("parent guard report missing or invalid; no model requests allowed") from None
    models = value.get("models", {})
    if (value.get("status") != "complete" or set(models) != {m for m, _ in MODELS}
            or not all(models[m].get("unloaded_after_run") is True for m, _ in MODELS)):
        raise ValueError("parent guard not complete with every model unloaded; no model requests allowed")
    return {"path": str(path), "sha256": digest_bytes(data), "status": value["status"],
            "unloaded_models": sorted(models)}


def source_manifest():
    source = lm.source_manifest()
    for relative in EXTRA_SOURCES:
        source["files"][relative] = digest_bytes((ROOT / relative).read_bytes())
    source["digest"] = digest_bytes(canonical(source["files"]).encode())
    return source


class PilotStop(BaseException):
    pass


class SourceLock:
    def __init__(self, manifest, root=None):
        self.root = ROOT if root is None else root
        self.files = {**manifest.get("loaded_python_files", {}), **manifest["files"]}
        self.signatures = {name: self.signature(self.root / name) for name in self.files}

    @staticmethod
    def signature(path):
        value = path.stat()
        return value.st_size, value.st_mtime_ns, value.st_ino

    def check(self):
        for name, previous in self.signatures.items():
            try:
                current = self.signature(self.root / name)
            except OSError:
                raise PilotStop(f"source_missing:{name}") from None
            if current != previous:
                raise PilotStop(f"source_changed:{name}")


@contextmanager
def wall_limit(seconds):
    if seconds <= 0:
        raise PilotStop("deadline_partial")
    def stop(_signum, _frame):
        raise PilotStop("deadline_partial")
    old = signal.signal(signal.SIGALRM, stop)
    previous = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
        if previous[0]:
            signal.setitimer(signal.ITIMER_REAL, *previous)


class PilotProvider(scale.RulesProvider):
    def __init__(self, *, check_run, **kwargs):
        self.check_run = check_run
        super().__init__(explicit_rules=True, num_ctx=16384, output_tokens=192, encoding="label_object", **kwargs)

    def complete_structured(self, *args, **kwargs):
        self.check_run()
        return super().complete_structured(*args, **kwargs)

    def request(self, method, path, body=None, timeout=None):
        if method == "POST" and body and body.get("keep_alive") != 0:
            self.check_run()
        return super().request(method, path, body, timeout)


def verify_cache_keys(events, model, pin):
    indexed = {}
    for event in events:
        key = event.get("key")
        if key is not None:
            identity = {"model_digest": pin, "schema_name": event["schema_name"], "input_schema": event["input_schema"],
                        "provider_order": event["provider_order"], "encoding": event["encoding"],
                        "original_messages": event["original_messages"], "request": event["request"]}
            if event["request"]["model"] != model or digest_bytes(lm.dump(identity).encode()) != key:
                raise ValueError("cache digest identity mismatch")
            if event.get("cache_hit"):
                source = indexed.get(event["source_event"])
                if source is None or source.get("key") != key or "error" in source or source.get("answer") != event.get("answer"):
                    raise ValueError("cache hit has no matching earlier validated response")
        indexed[event["index"]] = event
    return True


def pair_report(counter):
    report = counter.report()
    report["ci95"] = report.get("conservative_ci95")
    report["interval_method"] = "Bonferroni-Wilson paired bound; no zero-width equivalence claim"
    report["small_sample_warning"] = "Exploratory tiny sample: background-by-role cells have at most two deals, aggregate cells at most eighteen; no promotion or equivalence inference."
    return report


def behavior_metrics(row):
    llm = {s["seat"] for s in row.get("llm_seats", [])}
    roles = row.get("roles", [])
    correct = checked = 0
    for action in row.get("synthetic_trace", {}).get("actions", []):
        if action["seat"] not in llm or action["phase"] != "day_vote" or roles[action["seat"]] != "seer":
            continue
        known = {int(s) for s, role in action.get("investigations_before", {}).items()
                 if role == "wolf" and int(s) in action["legal"]}
        if known:
            checked += 1
            correct += action["action"] in known
    claims = set()
    for message in row.get("synthetic_trace", {}).get("messages", []):
        seat = message.get("sender")
        if seat not in llm or roles[seat] != "seer" or message.get("recipients") or message.get("visibility") == "direct":
            continue
        facts = read_chat(message.get("content"), seat)
        if facts.claim and not facts.injection:
            claims.add((seat, message.get("day")))
    return {"known_checked_wolf_vote": {"numerator": correct, "denominator": checked},
            "reader_detected_true_seer_claim_days": len(claims),
            "scope": "Evaluator-only truth. Claim count is reader detection, not semantic recall or proof every claim was understood by opponents."}


def summarize(records, plan):
    scenarios = {s["id"]: s for s in plan}
    if len(scenarios) != len(plan):
        raise ValueError("duplicate planned scenario")
    models, profiles = [m for m, _ in MODELS], [p for p, _ in PROFILES]
    groups = defaultdict(dict)
    failures = defaultdict(lambda: defaultdict(Counter))
    individual = defaultdict(lambda: defaultdict(Counter))
    for row in records:
        model, ident, profile = row["model"], row["id"], row["profile"]
        if model not in models or ident not in scenarios or profile not in profiles:
            raise ValueError("unexpected pair identity")
        group = groups[(model, ident)]
        if profile in group:
            raise ValueError("duplicate model/scenario/profile row")
        group[profile] = row
        if not row.get("complete") or "harness_error" in row:
            continue
        s = scenarios[ident]
        if (canonical(row["scenario"]) != canonical(s) or row["seat"] != s["ours"] or row["role"] != s["our_role"]
                or row["roles"] != deal_roles(random.Random(s["deal_seed"])) or type(row["win"]) is not bool):
            raise ValueError("unpaired scenario or role identity")
        individual[model][profile].update(games=1, wins=int(row["win"]), clean_games=int(row["clean"]))
        for prefix, field in (("controlled", "failures"), ("background", "background_failures"),
                              ("llm", "llm_stats"), ("provider", "provider_stats")):
            failures[model][profile].update({f"{prefix}.{k}": v for k, v in row.get(field, {}).items()})
    complete, missing = {}, []
    for model in models:
        for ident in scenarios:
            group = groups[(model, ident)]
            absent = [p for p in profiles if p not in group]
            invalid = [p for p, r in group.items() if not r.get("complete") or "harness_error" in r]
            if absent or invalid:
                missing.append({"model": model, "id": ident, "missing_profiles": absent, "incomplete_profiles": invalid})
            else:
                complete[(model, ident)] = group
    common = {ident for ident in scenarios if all((model, ident) in complete
              and all(r["clean"] for r in complete[(model, ident)].values()) for model in models)}
    metrics, llm_results = {}, {}
    scopes = ["all", *(f"role:{role}" for role in ROLES), *(f"background:{b}" for b in BACKGROUNDS),
              *(f"background:{b}/role:{r}" for b in BACKGROUNDS for r in ROLES)]
    for model in models:
        metrics[model], llm_results[model] = {}, {}
        for subset in ("all_attempts", "common_clean"):
            counts = defaultdict(lm.PairCounts)
            own = defaultdict(Counter)
            for ident, s in scenarios.items():
                group = complete.get((model, ident))
                if group is None or subset == "common_clean" and ident not in common:
                    continue
                reference = group["agent"]
                here = ("all", f"role:{s['our_role']}", f"background:{s['background']}",
                        f"background:{s['background']}/role:{s['our_role']}")
                for profile in profiles[1:]:
                    for scope in here:
                        counts[(profile, scope)].add(int(group[profile]["win"]), int(reference["win"]))
                for profile, r in group.items():
                    for seat in r.get("llm_seats", []):
                        own[f"{profile}/{seat['role']}"].update(seats=1, wins=int(seat["win"]))
                    behavior = behavior_metrics(r)
                    own[f"{profile}/behavior"].update(checked_wolf_opportunities=behavior["known_checked_wolf_vote"]["denominator"],
                        checked_wolf_votes=behavior["known_checked_wolf_vote"]["numerator"],
                        reader_detected_true_seer_claim_days=behavior["reader_detected_true_seer_claim_days"])
            metrics[model][subset] = {p: {scope: pair_report(counts[(p, scope)]) for scope in scopes} for p in profiles[1:]}
            llm_results[model][subset] = dict(own)
    return {"metrics": metrics, "llm_results": llm_results, "individual_completed_games": individual,
            "failures": failures, "games_attempted": len(records), "games_completed": sum(r.get("complete", False) for r in records),
            "complete_groups": len(complete), "planned_groups": len(models) * len(plan), "common_clean_scenarios": len(common),
            "missing_groups": missing, "partial": bool(missing),
            "sampling": "role_balanced one-third each; not the natural4/7 villager1/7 seer2/7 wolf mixture. No reweighted estimate is claimed.",
            "clean_definition": "Identical scenarios clean in all three profiles AND both models; successful-call conditioning can bias this subset.",
            "llm_scope": "LLM seat wins are descriptive correlated team outcomes, not independent trials. Known-wolf votes are executed ballots, including auto-actions in dirty games, not raw model correctness. Reader-detected claims are not recall estimates.",
            "warning": "18 unique deals only; model/profile repetitions are paired, not108 independent trials. Background/role cells have2 deals; no promotion, universal superiority, or equivalence claim."}


def compact_provider(provider):
    full = lm.provider_summary(provider)
    result = {key: full[key] for key in ("stats", "request_scope", "max_s", "mean_s", "model", "model_digest", "options", "encoding", "all_errors")}
    result["incomplete_provider_attempts"] = sum("answer" not in event and "error" not in event for event in provider.events)
    return result


def index_requests(output, models):
    artifacts = []
    complete = True
    for model, report in models.items():
        directory = output / model.replace(":", "_")
        entries = []
        for path in sorted((directory / "requests").glob("*.json")):
            data = path.read_bytes()
            try:
                event = json.loads(data)
                valid = event["index"] == int(path.stem)
            except (ValueError, KeyError, TypeError):
                valid = False
            entries.append({"path": str(path), "sha256": digest_bytes(data), "valid_event_json": valid})
        expected = report["stats"].get("attempts", 0)
        valid = (len(entries) == expected and all(e["valid_event_json"] for e in entries)
                 and [Path(e["path"]).stem for e in entries] == [f"{i:06d}" for i in range(expected)])
        complete = complete and valid
        artifacts.append(save(directory / "request-index.json", {"expected": expected, "complete": valid, "files": entries}))
    return {"complete": complete, "indexes": artifacts}


def run_pilot(plan, output, stop, source, guard, *, provider_factory=None, game_runner=None, resource_reader=None, clock=None):
    provider_factory = provider_factory or PilotProvider
    game_runner = game_runner or lm.play_profile
    resource_reader = resource_reader or scale.resource_state
    clock = clock or time.time
    scenarios = plan["scenarios"]
    lock = SourceLock(source)
    resources, records, model_reports, artifacts = [], [], {}, []
    status, error = "complete", None
    def check_run():
        if clock() >= stop.timestamp():
            raise PilotStop("deadline_partial")
        lock.check()
    try:
        if check_guard(Path(guard["path"]))["sha256"] != guard["sha256"]:
            raise ValueError("parent guard changed after the fixed plan; no requests allowed")
        resources.append(resource_reader())
        if not scale.resource_safe(resources[-1], resources[0]):
            raise PilotStop("resource_stop_partial")
        for model, pin in MODELS:
            check_run()
            current = resource_reader()
            resources.append(current)
            if not scale.resource_safe(current, resources[0]):
                raise PilotStop("resource_stop_partial")
            model_dir = output / model.replace(":", "_")
            model_dir.mkdir()
            (model_dir / "requests").mkdir()
            provider = provider_factory(model=model, expected_digest=pin, out=model_dir, cache={}, check_run=check_run)
            owned = False
            model_reports[model] = {"unloaded_after_run": False, "cache_keys_verified": False, "context_verified_end": False}
            try:
                with wall_limit(stop.timestamp() - clock()):
                    initial = provider.inspect_server()
                    if initial["/api/ps"].get("models"):
                        raise RuntimeError("server has a loaded model before our block; no automatic repair")
                    owned = True
                    started = time.monotonic()
                    warm = provider.request("POST", "/api/chat", {"model": model, "messages": [{"role": "user", "content": "Reply OK."}],
                             "stream": False, "keep_alive": -1, "options": dict(provider.options, num_predict=8)}).json()
                    health = provider.inspect_server()
                    if not warm.get("done") or warm.get("model") != model:
                        raise RuntimeError("preload response did not finish on the requested model")
                    if not provider.context_verified(health) or len(health["/api/ps"].get("models", [])) != 1:
                        raise RuntimeError("preload tag, pin, context or single-loaded-model verification failed")
                    artifacts.append(save(model_dir / "preload.json", {"not_scored": True, "elapsed_s": time.monotonic() - started,
                                          "response": warm, "health": health}))
                    provider.cache.clear()
                    with lm.wb.environment(lm.SETTINGS | {"ARENA_TC_WATCHDOG": "0"}):
                        for scenario in scenarios:
                            for profile, factory in PROFILES:
                                check_run()
                                current = resource_reader()
                                resources.append(current)
                                if not scale.resource_safe(current, resources[0]):
                                    raise PilotStop("resource_stop_partial")
                                pending = {"id": scenario["id"], "model": model, "profile": profile, "complete": False,
                                           "started": datetime.now(lm.PDT).isoformat(), "request_range": [len(provider.events), None]}
                                records.append(pending)
                                save(output / "in_progress.json", pending)
                                try:
                                    result = game_runner(deepcopy(scenario), profile, factory, provider)
                                except (Exception, PilotStop) as exc:
                                    pending.update(harness_error=f"{type(exc).__name__}: {exc}", request_range=[pending["request_range"][0], len(provider.events)])
                                    artifacts.append(save(output / f"game-{len(records):04d}.json", pending))
                                    raise
                                result.update(model=model, complete=True)
                                result["behavior"] = behavior_metrics(result)
                                records[-1] = result
                                artifacts.append(save(output / f"game-{len(records):04d}.json", result))
                                save(output / "progress.json", {"status": "running", "games_completed": len(records),
                                     "latest": {key: result[key] for key in ("id", "model", "profile", "role", "win", "clean")}})
                                print(lm.dump({"game": len(records), "model": model, "profile": profile, "id": scenario["id"],
                                      "role": result["role"], "win": result["win"], "clean": result["clean"],
                                      "provider": dict(provider.stats), "time": datetime.now(lm.PDT).isoformat()}), flush=True)
                    check_run()
                    model_reports[model]["context_verified_end"] = provider.context_verified(provider.inspect_server())
                    if not model_reports[model]["context_verified_end"]:
                        raise RuntimeError("final model context mismatch")
            finally:
                if owned:
                    try:
                        provider.request("POST", "/api/chat", {"model": model, "messages": [], "stream": False, "keep_alive": 0})
                        after = provider.request("GET", "/api/ps").json()
                        model_reports[model]["unloaded_after_run"] = after.get("models") == []
                        model_reports[model]["unload_ps"] = after
                    except Exception as exc:
                        model_reports[model]["unload_error"] = f"{type(exc).__name__}: {exc}"
                model_reports[model].update(compact_provider(provider))
                model_reports[model]["cache_keys_verified"] = verify_cache_keys(provider.events, model, pin)
                artifacts.append(save(model_dir / "provider-summary.json", model_reports[model]))
            if not model_reports[model]["unloaded_after_run"]:
                raise RuntimeError("model failed to unload; no next model will be requested")
    except PilotStop as exc:
        status = "deadline_partial" if str(exc) == "deadline_partial" else "blocked_partial"
        error = str(exc)
    except Exception as exc:
        status, error = "blocked_partial", f"{type(exc).__name__}: {exc}"
    if records and not records[-1].get("complete"):
        pending = records[-1]
        pending.setdefault("harness_error", error or "incomplete match")
        pending["request_range"][1] = len(provider.events)
        path = output / f"game-{len(records):04d}.json"
        if str(path) not in {entry["path"] for entry in artifacts}:
            artifacts.append(save(path, pending))
    summary = summarize(records, scenarios)
    if status == "complete" and summary["partial"]:
        status = "incomplete_partial"
    summary.update(status=status, error=error, models=model_reports, resources=resources, game_artifacts=artifacts,
                   guard=guard, deadline=stop.isoformat(), ended=datetime.now(lm.PDT).isoformat(), promotion="not assessed",
                   planned_games=len(scenarios) * len(PROFILES) * len(MODELS), source_digest_start=source["digest"])
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop-at", required=True)
    args = parser.parse_args(argv)
    try:
        stop = validate_deadline(args.stop_at)
        guard = check_guard(ROOT / GUARD)
    except ValueError as exc:
        parser.error(str(exc))
    output, report = ROOT / OUTPUT, ROOT / REPORT
    if output.exists() or report.exists():
        parser.error("new pilot output and report paths required; never overwrite prior evidence")
    output.mkdir()
    for _, factory in PROFILES:
        lm.ev.factory(factory)
    plan = build_plan()
    source = source_manifest()
    plan_file = save(output / "plan.json", plan)
    manifest_file = save(output / "manifest.json", {"plan_sha256": plan["sha256"], "profiles": PROFILES, "models": MODELS,
        "source": source, "guard": guard, "stop_at": stop.isoformat(), "context": 16384, "encoding": "label_object",
        "output_tokens": 192, "temperature": 0, "decoder_seed": 205, "threads": 4, "explicit_rules": scale.RULES,
        "settings": lm.SETTINGS | {"ARENA_TC_WATCHDOG": "0"}, "source_frozen_before_requests": True,
        "observer": "Unmodified scripts.werewolf_local_model_eval.play_profile with passive ObservingResearchGame delivery",
        "cache": "Fresh per model; exact requests only within that model, digest identities verified; preload not scored",
        "bounded_runner": "SIGALRM at the explicit deadline, no faulthandler. An interrupted match is incomplete and excluded from paired scores.",
        "scope": "Development214 role-balanced local pilot; same18 deals, not108 independent games; not promotion evidence."})
    summary = run_pilot(plan, output, stop, source, guard)
    end_source = source_manifest()
    summary["source_digest_end"] = end_source["digest"]
    summary["source_files_unchanged"] = source["digest"] == end_source["digest"]
    request_index = index_requests(output, summary["models"])
    zero_sdk_errors = all(not any(counts.get(f"llm.{key}", 0) for key in ("invalid_answers", "fallbacks"))
                         for per_model in summary["failures"].values() for counts in per_model.values())
    protocol_keys = ("crash", "illegal", "bad_chat", "timeout", "chat_after_terminate", "fallbacks", "overruns",
                     "policy_fallbacks", "policy_errors", "logged_errors", "listen_errors")
    zero_protocol = all(not any(counts.get(f"{kind}.{key}", 0) for kind in ("controlled", "background") for key in protocol_keys)
                        for per_model in summary["failures"].values() for counts in per_model.values())
    summary["gates"] = {"complete": summary["status"] == "complete" and not summary["partial"],
                        "source_unchanged": summary["source_files_unchanged"], "request_logs_complete": request_index["complete"],
                        "both_model_blocks_attempted": len(summary["models"]) == len(MODELS),
                        "all_models_unloaded": bool(summary["models"]) and all(r["unloaded_after_run"] for r in summary["models"].values()),
                        "all_cache_keys_verified": len(summary["models"]) == len(MODELS) and all(r["cache_keys_verified"] for r in summary["models"].values()),
                        "zero_model_errors": zero_sdk_errors and all(not r["stats"].get("errors", 0) and not r["incomplete_provider_attempts"] for r in summary["models"].values()),
                        "zero_protocol_errors": zero_protocol,
                        "all_scenarios_common_clean": summary["common_clean_scenarios"] == len(plan["scenarios"])}
    summary["artifacts"] = {"plan": plan_file, "manifest": manifest_file, "request_indexes": request_index["indexes"],
                            "requests": [str(output / m.replace(":", "_") / "requests") for m, _ in MODELS]}
    final_file = save(output / "summary.json", summary)
    save(report, summary | {"artifacts": summary["artifacts"] | {"summary": final_file}})
    print(lm.dump({"status": summary["status"], "games": summary["games_completed"], "gates": summary["gates"], "report": str(report)}), flush=True)
    return 0 if summary["gates"]["complete"] and summary["gates"]["source_unchanged"] and summary["gates"]["all_models_unloaded"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
