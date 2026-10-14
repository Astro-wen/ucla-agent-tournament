from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from altruagent import GameState
from local.werewolf_env import OfficialBaseline
from local.werewolf_research import ResearchGame
from local.werewolf_research_metrics import clustered_mean
from scripts import werewolf_local_model_eval as lm

RULES = (
    "Public match rules: Seven seats Player0 through Player6, numbered from zero. Exactly two wolves, "
    "one seer and four ordinary villagers. Roles never change. A seer's private investigations are accurate "
    "and return wolf or villager; the seer is on the village side. The first night has no kill. From night "
    "two the wolves kill one living nonwolf; their disagreement selects the lower target seat. The seer "
    "investigates one other living player each night. Death publicly reveals the true role. Day votes are "
    "hidden until resolution; unique plurality eliminates a player, a tie eliminates nobody. Self-votes "
    "are illegal; the Abstain option skips a day vote. The village wins when all wolves are dead; wolves win at parity "
    "or after three consecutive days with no elimination. Dead teammates share their faction's result. "
    "There is no doctor or resurrection. Chat is a player's claim and may be false; it is not an engine fact."
)


class RulesProvider(lm.LocalProvider):
    def __init__(self, *, explicit_rules=False, **kwargs):
        super().__init__(**kwargs)
        self.explicit_rules = explicit_rules

    def complete_structured(self, messages, schema_name, schema, *, timeout=None):
        if self.explicit_rules:
            messages = deepcopy(messages)
            if not messages or messages[0]["role"] != "system":
                raise ValueError("official system message required")
            messages[0]["content"] += "\n\n" + RULES
        return super().complete_structured(messages, schema_name, schema, timeout=timeout)


def new_game(me, wolves):
    game = ResearchGame([OfficialBaseline() for _ in range(7)], random.Random(213))
    game.roles = ["seer" if s == me else "wolf" if s in wolves else "villager" for s in range(7)]
    return game


def public_vote(game, victim):
    living = game.living()
    ballots = {s: victim if s != victim else min(t for t in living if t != s) for s in living}
    game.votes.append({"day": game.day, "votes": ballots, "eliminated": victim})
    game._kill(victim, "vote")


def validation_cases():
    cases = []
    for me in range(7):
        p = lambda offset: (me + offset) % 7
        for family in ("checked_wolf", "private_over_false_claim", "clear_endgame", "two_checked_wolves", "unread_investigation"):
            phase = "day_vote"
            if family in ("checked_wolf", "private_over_false_claim"):
                game = new_game(me, {p(1), p(4)})
                game.invest = {p(1): "wolf"}
                expected = {p(1)}
                if family == "private_over_false_claim":
                    game.messages = [{"seq": 0, "sender": p(4), "content": f"I am the Seer. Player{p(2)} is a wolf. I vote Player{p(2)}.",
                                      "recipients": [], "visibility": "broadcast", "type": "chat", "day": 1}]
            elif family == "clear_endgame":
                clear, wolf = p(3), p(5)
                dead = [s for s in range(7) if s not in (me, clear, wolf)]
                game = new_game(me, {wolf, dead[0]})
                public_vote(game, dead[0])
                game.day = 2
                game._kill(dead[1], "night_kill")
                public_vote(game, dead[2])
                game.day = 3
                game._kill(dead[3], "night_kill")
                game.invest = {clear: "villager"}
                expected = {wolf}
            elif family == "two_checked_wolves":
                wolves = {p(1), p(4)}
                civilians = [s for s in range(7) if s != me and s not in wolves]
                game = new_game(me, wolves)
                public_vote(game, civilians[-1])
                game.day = 2
                game._kill(civilians[-2], "night_kill")
                game.invest = {s: "wolf" for s in sorted(wolves)}
                expected = wolves
            else:
                phase = "night_seer"
                wolves, checked = {p(1), p(5)}, p(3)
                game = new_game(me, wolves)
                dead = next(s for s in range(7) if s not in {me, checked, *wolves})
                public_vote(game, dead)
                game.day = 2
                game.invest = {checked: "villager"}
                expected = set(game.living()) - {me, checked}
            legal = [s for s in game.living() if s != me] + ([7] if phase == "day_vote" else [])
            assert not game.check()
            for order in ("ascending", "descending"):
                choices = sorted(legal, reverse=order == "descending")
                raw = game.state(me, "moving", phase, choices)
                discussion = game.state(me, "messaging", "day_vote") if game.messages else None
                cases.append({"id": f"{family}/seat{me}/{order}", "cluster": f"{family}/seat{me}",
                              "family": family, "seat": me, "order": order, "raw": raw,
                              "discussion": discussion, "expected": sorted(str(s) for s in expected),
                              "truth": {"roles": game.roles, "dead": game.dead, "votes": game.votes}})
    return cases


def one_decision(provider, case):
    seat = lm.LocalLLMSeat(provider)
    if case["discussion"] is not None:
        seat.agent._remember(GameState.from_mcp_state(case["discussion"]))
    before = len(provider.events)
    choice = seat.choose(deepcopy(case["raw"]))
    events = provider.events[before:]
    legal = {a["action_id"] for a in case["raw"]["legal_actions"]["actions"]}
    fallback = bool(seat.stats["fallbacks"])
    return {"id": case["id"], "cluster": case["cluster"], "family": case["family"], "seat": case["seat"],
            "order": case["order"], "expected": case["expected"], "choice": choice,
            "phase": case["raw"]["game_state"]["phase"],
            "selected_true_role": case["truth"]["roles"][int(choice)] if choice in {str(s) for s in range(7)} else None,
            "legal": choice in legal, "correct": choice in case["expected"] and not fallback,
            "fallback": fallback, "stats": dict(seat.stats), "requests": [e["index"] for e in events],
            "network_latency_s": sum(e["latency_s"] for e in events if e["network"])}


def summarize(rows, cases):
    outcomes = {}
    for model, rules in sorted({(r["model"], r["rules"]) for r in rows}):
        selected = [r for r in rows if r["model"] == model and r["rules"] == rules]
        scope = {"n": len(selected), "correct": sum(r["correct"] for r in selected),
                 "legal": sum(r["legal"] for r in selected), "fallbacks": sum(r["fallback"] for r in selected),
                 "failures": [r for r in selected if not r["correct"]],
                 "day_vote_count": sum(r.get("phase") == "day_vote" for r in selected),
                 "actual_wolf_votes_without_fallback": sum(r.get("phase") == "day_vote" and r.get("selected_true_role") == "wolf" and not r["fallback"] for r in selected)}
        scope["families"] = {family: {"n": sum(r["family"] == family for r in selected),
                                      "correct": sum(r["family"] == family and r["correct"] for r in selected)}
                             for family in sorted({c["family"] for c in cases})}
        outcomes[f"{model}/{rules}"] = scope
    comparisons = {}
    models = sorted({r["model"] for r in rows})
    for rules in ("generic", "explicit"):
        if len(models) < 2:
            continue
        lower = next((m for m in models if ":7b-" in m), models[0])
        for upper in models:
            if upper == lower:
                continue
            a = {r["id"]: r for r in rows if r["model"] == upper and r["rules"] == rules}
            b = {r["id"]: r for r in rows if r["model"] == lower and r["rules"] == rules}
            if set(a) != {c["id"] for c in cases} or set(b) != set(a):
                continue
            ids = sorted(a)
            comparisons[f"{upper}-vs-{lower}/{rules}"] = clustered_mean(
                [int(a[key]["correct"]) - int(b[key]["correct"]) for key in ids], [a[key]["cluster"] for key in ids])
    return {"outcomes": outcomes, "comparisons": comparisons,
            "scope": "70 fixed diagnostic views of35 base positions; order variants are clustered, not independent field games",
            "correctness_contract": "Predeclared private-check priority, avoiding known clears and repeat checks; not proof of the globally optimal vote under all possible other ballots. True-role selection is reported separately."}


def resource_state():
    pressure = int(subprocess.check_output(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"], text=True))
    swap = subprocess.check_output(["/usr/sbin/sysctl", "vm.swapusage"], text=True)
    return {"pressure": pressure, "swap_used_mib": float(re.search(r"used = ([\d.]+)M", swap).group(1)),
            "disk_free": shutil.disk_usage("/tmp").free, "time": datetime.now().astimezone().isoformat()}


def resource_safe(current, baseline):
    return current["pressure"] < 4 and current["swap_used_mib"] - baseline["swap_used_mib"] <= 1024 \
        and current["disk_free"] >= 1.5 * 1024**3


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", action="append", required=True, help="exact local tag=manifest SHA256")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--stop-at", required=True)
    args = parser.parse_args(argv)
    stop = datetime.fromisoformat(args.stop_at)
    if stop.tzinfo is None or stop.timestamp() <= time.time():
        parser.error("future timezone-aware deadline required")
    models = []
    for item in args.model:
        name, sep, pin = item.partition("=")
        if not sep or name in {m[0] for m in models}:
            parser.error("unique pinned models required")
        RulesProvider(model=name, expected_digest=pin)
        models.append((name, pin))
    if args.out.exists() or args.out.resolve().parent != (ROOT / "logs/werewolf_benchmarks").resolve():
        parser.error("new logs/werewolf_benchmarks subdirectory required")
    args.out.mkdir()
    cases = validation_cases()
    source = lm.source_manifest()
    for relative in ("scripts/werewolf_model_scale_eval.py", "tests/strategy/test_werewolf_model_scale.py"):
        path = ROOT / relative
        source["files"][relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    source["digest"] = hashlib.sha256(json.dumps(source["files"], sort_keys=True).encode()).hexdigest()
    lm.write_json(args.out / "manifest.json", {"models": models, "context": 16384, "encoding": "label_object",
                  "decoder_seed": lm.SEED, "temperature": 0, "output_tokens": 192, "threads": 4,
                  "conditions": ["generic", "explicit"], "public_rules": RULES, "cases": cases,
                  "source": source, "stop_at": args.stop_at,
                  "scope": "new fixed diagnostics, no old29-case selection; family/seat clusters, not strength proof"})
    rows, summaries = [], {}
    resources = [resource_state()]
    status = "complete"
    for name, pin in models:
        model_dir = args.out / name.replace(":", "_")
        model_dir.mkdir()
        (model_dir / "requests").mkdir()
        provider = RulesProvider(model=name, expected_digest=pin, out=model_dir, num_ctx=16384, encoding="label_object")
        try:
            provider.inspect_server()
            start = time.monotonic()
            warm = provider.request("POST", "/api/chat", {"model": name, "messages": [{"role": "user", "content": "Reply OK."}],
                                    "stream": False, "keep_alive": -1, "options": dict(provider.options, num_predict=8)}).json()
            health = provider.inspect_server()
            if not provider.context_verified(health):
                raise RuntimeError("loaded context/model verification failed")
            lm.write_json(model_dir / "warm.json", {"elapsed_s": time.monotonic() - start, "response": warm, "health": health})
            for rules in ("generic", "explicit"):
                provider.explicit_rules = rules == "explicit"
                for case in cases:
                    if time.time() >= stop.timestamp():
                        status = "deadline_partial"
                        break
                    resources.append(resource_state())
                    if not resource_safe(resources[-1], resources[0]):
                        raise RuntimeError("resource safety stop: critical pressure, new swap or low disk")
                    row = one_decision(provider, case) | {"model": name, "rules": rules}
                    rows.append(row)
                    lm.write_json(args.out / "decisions.json", rows)
                    if len(rows) % 14 == 0:
                        resources.append(resource_state())
                        if not resource_safe(resources[-1], resources[0]):
                            raise RuntimeError("resource safety stop: critical pressure, new swap or low disk")
                        print(name, rules, len(rows), datetime.now().astimezone().isoformat(), flush=True)
                if status != "complete":
                    break
        except Exception as exc:
            status = "blocked_partial"
            summaries[name] = {"error": f"{type(exc).__name__}: {exc}"}
        summaries.setdefault(name, {}).update(lm.provider_summary(provider))
        try:
            provider.request("POST", "/api/chat", {"model": name, "messages": [], "stream": False, "keep_alive": 0})
            summaries[name]["unloaded_after_run"] = True
        except Exception as exc:
            summaries[name]["unload_error"] = type(exc).__name__
        if status != "complete":
            break
    final = summarize(rows, cases)
    final.update(status=status, models=summaries, decisions=rows, resources=resources, ended=datetime.now().astimezone().isoformat())
    final["source_files_unchanged"] = all(hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest
                                        for path, digest in source["files"].items())
    lm.write_json(args.out / "summary.json", final)
    print(json.dumps({k: {"n": v["n"], "correct": v["correct"], "legal": v["legal"]} for k, v in final["outcomes"].items()}), flush=True)
    print(status, "source unchanged", final["source_files_unchanged"])
    return 0 if status == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
