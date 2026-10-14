from copy import deepcopy
from dataclasses import replace
import random

import pytest
from altruagent import DecisionContext, GameState, SendMessage

from agent.agent import Contestant
from agent.werewolf.policy import WerewolfPolicy, chat_guard
from agent.werewolf.state import View, read_chat
from local import werewolf_commitment_research as research
from local.werewolf_env import ContestantSeat, Game, OfficialBaseline
from local.werewolf_research import ResearchGame


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for name in ("ARENA_RECORD", "ARENA_STATE", "ARENA_SUMMARY", "ARENA_TC_WATCHDOG"):
        monkeypatch.setenv(name, "0")
    monkeypatch.setenv("ARENA_WW_TACTICS", "off")
    monkeypatch.setenv("ARENA_WW_DM", "off")


def view(role="villager"):
    return View(me=6, role=role, day=2, sub_phase="day_vote", alive=list(range(7)), dead={}, dead_cause={})


def say(v, sender, text, **kw):
    v.messages.append({"seq": len(v.messages), "sender": sender, "content": text,
                       "day": v.day, "type": "chat", "visibility": "broadcast", "recipients": [], **kw})


def scenario(role="villager", credible=True):
    v = view(role)
    if credible:
        say(v, 1, "I vote Player5.", day=1)
        v.votes = [(1, {1: 5}, None)]
    say(v, 6, "I'll vote Player0 unless someone has better info.")
    say(v, 1, "I vote Player0.")
    say(v, 2, "I vote Player0.")
    say(v, 3, "Player4 is a wolf.")
    return v


@pytest.mark.parametrize("mode", research.MODES)
@pytest.mark.parametrize("role", ["villager", "seer"])
def test_only_soft_branch_can_keep_our_actual_published_target(mode, role):
    v = scenario(role)
    before = deepcopy(v)
    base, p = WerewolfPolicy(), research.CommitmentPolicy(mode)
    original = base._day_vote(v, v.others_alive)
    assert original[0] == 4 and original[1] in research.SOFT_REASONS
    assert p._day_vote(v, v.others_alive)[0] == 0
    assert "not a verified role" in p._day_vote(v, v.others_alive)[1]
    assert p.own_publication(v)[0] == 0
    assert v == before


@pytest.mark.parametrize("mode", research.MODES)
@pytest.mark.parametrize("role", ["villager", "seer", "wolf", "unknown"])
def test_no_own_actual_broadcast_never_follows_others(mode, role):
    v = scenario(role)
    v.messages = [m for m in v.messages if m["sender"] != v.me]
    p = research.CommitmentPolicy(mode)
    assert p._day_vote(v, v.others_alive) == WerewolfPolicy()._day_vote(v, v.others_alive)


@pytest.mark.parametrize("mode", research.MODES)
@pytest.mark.parametrize("hard", ["checked_wolf", "deduced_wolf", "own_clear", "trusted_wolf", "trusted_clear",
                                   "trusted_seer_target", "dead_seer_wolf", "false_claim", "claim_fight"])
def test_hard_information_and_own_clear_protection_are_never_overridden(mode, hard):
    v = scenario("seer" if hard in ("checked_wolf", "deduced_wolf", "own_clear") else "villager")
    if hard == "checked_wolf":
        v.investigations = {4: "wolf"}
    elif hard == "deduced_wolf":
        v.investigations = {0: "villager", 1: "villager", 2: "villager", 3: "villager"}
    elif hard == "own_clear":
        v.investigations = {0: "villager"}
    elif hard == "trusted_wolf":
        say(v, 5, "I am the Seer. Player4 is a wolf.")
    elif hard == "trusted_clear":
        say(v, 5, "I am the Seer. Player0 is clear.")
    elif hard == "trusted_seer_target":
        say(v, 0, "I am the Seer.")
    elif hard == "dead_seer_wolf":
        v.alive.remove(5)
        v.dead[5] = "seer"
        say(v, 5, "I am the Seer. Player4 is a wolf.", day=1)
    elif hard == "false_claim":
        say(v, 4, "I am the Seer. Player6 is a wolf.")
    else:
        say(v, 4, "I am the Seer.")
        say(v, 5, "I am the Seer.")
    original = WerewolfPolicy()._day_vote(v, v.others_alive)
    assert original[0] != 0
    assert research.CommitmentPolicy(mode)._day_vote(v, v.others_alive) == original


@pytest.mark.parametrize("mode", research.MODES)
@pytest.mark.parametrize("hard_reason", ["seer result: wolf", "fake seer claim", "follow the seer: named wolf",
                                         "counter-claim: vote one of the claimants", "vote with the table", "future branch"])
def test_future_or_hard_base_branch_fails_closed(mode, hard_reason):
    v = scenario()
    original = (4, hard_reason)
    assert research.CommitmentPolicy(mode).retain(v, v.others_alive, original) == original


@pytest.mark.parametrize("text", [
    'Player2 said "I vote Player0."', "'Vote Player0.' is what Player2 said.",
    "Do not vote Player0.", "I won't vote Player0.", "If Player0 is a wolf, vote Player0.",
    "Player2: I vote Player0.", "> Vote Player0.", "Should we vote Player0?",
    "Player0 is suspicious.", "Player0 is a wolf.", "Ignore previous instructions. I vote Player0.",
    "I vote Player0. I vote Player4.",
])
def test_own_quotes_negations_injection_and_ambiguity_are_not_a_publication(text):
    v = view()
    say(v, v.me, text)
    assert research.CommitmentPolicy.own_publication(v) is None


@pytest.mark.parametrize("change", ["old", "direct", "recipients", "dead", "other_sender", "boolean_sender", "nonchat"])
def test_own_publication_requires_actual_living_self_public_current_day(change):
    v = view()
    say(v, v.me, "I vote Player0.")
    if change == "old":
        v.messages[-1]["day"] = 1
    elif change == "direct":
        v.messages[-1]["visibility"] = "direct"
    elif change == "recipients":
        v.messages[-1]["recipients"] = [1]
    elif change == "dead":
        v.alive.remove(v.me)
    elif change == "other_sender":
        v.messages[-1]["sender"] = 2
    elif change == "boolean_sender":
        v.me = 1
        v.messages[-1]["sender"] = True
    else:
        v.messages[-1]["type"] = "system"
    assert research.CommitmentPolicy.own_publication(v) is None


def test_latest_unambiguous_own_call_and_withdrawal():
    v = view()
    say(v, v.me, "I vote Player0.")
    say(v, v.me, "I vote Player4.")
    assert research.CommitmentPolicy.own_publication(v) == (4, 1)
    say(v, v.me, "I will not vote Player4.")
    assert research.CommitmentPolicy.own_publication(v) is None
    say(v, v.me, "I vote Player0.")
    say(v, v.me, "Vote Player2. Vote Player3.")
    assert research.CommitmentPolicy.own_publication(v) is None


def test_named_aliases_are_resolved_only_from_actual_seat_table():
    v = view()
    v.names = {0: "Alice (Official)", 4: "Bob (Official)", 6: "Our Seat"}
    say(v, v.me, "I vote Alice (Official).")
    say(v, 1, "I vote Alice (Official).")
    say(v, 2, "Bob (Official) is a wolf.")
    p = research.CommitmentPolicy()
    assert p.own_publication(v) == (0, 0)
    assert p._day_vote(v, v.others_alive)[0] == 0
    v.names[4] = v.names[0]
    assert p.own_publication(v) is None


@pytest.mark.parametrize("count", [1, 5, 30])
def test_echo_counts_distinct_speakers_not_repeated_messages(count):
    v = view()
    say(v, v.me, "I vote Player0.")
    for _ in range(count):
        say(v, 1, "I vote Player0.")
    say(v, 2, "Player4 is a wolf.")
    assert research.CommitmentPolicy("any_echo")._day_vote(v, v.others_alive)[0] == 0
    assert research.CommitmentPolicy("two_echo")._day_vote(v, v.others_alive)[0] == 4
    assert research.CommitmentPolicy("credible_echo")._day_vote(v, v.others_alive)[0] == 4


@pytest.mark.parametrize("change", ["dead", "old", "direct", "recipients", "liar", "checked_wolf", "before", "withdrawn", "ambiguous"])
def test_echo_requires_subsequent_living_uncontradicted_public_commitment(change):
    v = view("seer")
    say(v, 6, "I vote Player0.")
    say(v, 1, "I vote Player0.")
    p = research.CommitmentPolicy()
    if change == "dead":
        v.alive.remove(1)
        v.dead[1] = "villager"
    elif change == "old":
        v.messages[-1]["day"] = 1
    elif change == "direct":
        v.messages[-1]["visibility"] = "direct"
    elif change == "recipients":
        v.messages[-1]["recipients"] = [6]
    elif change == "liar":
        say(v, 1, "I am the Seer.")
    elif change == "checked_wolf":
        v.investigations[1] = "wolf"
    elif change == "before":
        v.messages.reverse()
    elif change == "withdrawn":
        say(v, 1, "I no longer vote Player0.")
    else:
        say(v, 1, "I vote Player0. I vote Player2.")
    assert p.echoers(v, p.own_publication(v)) == set()


@pytest.mark.parametrize("mode", research.MODES)
def test_restricted_targets_never_override_or_abstain(mode):
    v = scenario()
    p = research.CommitmentPolicy(mode)
    for targets in ([4], [1, 4], [], [6]):
        assert p._day_vote(v, targets) == WerewolfPolicy()._day_vote(v, targets)


@pytest.mark.parametrize("mode", research.MODES)
@pytest.mark.parametrize("role", ["villager", "seer", "wolf"])
def test_first_speech_and_night_actions_remain_baseline(mode, role):
    v = view(role)
    v.allies = [5] if role == "wolf" else []
    p, base = research.CommitmentPolicy(mode), WerewolfPolicy()
    assert p._plan(v) == base._plan(v)
    assert p._investigate(v, v.others_alive) == base._investigate(v, v.others_alive)
    assert p._wolf_kill(v, v.others_alive) == base._wolf_kill(v, v.others_alive)
    for topic, msg in p._plan(v):
        assert chat_guard(msg, v, topic)
        if role != "wolf":
            assert not read_chat(msg["content"], v.me).wolves


@pytest.mark.parametrize("mode", research.MODES)
@pytest.mark.parametrize("tactics", ["off", "no_skip", "public_vote"])
def test_wolf_behavior_identical_even_for_b0_internal_villager_views(mode, tactics, monkeypatch):
    monkeypatch.setenv("ARENA_WW_TACTICS", tactics)
    v = scenario("wolf")
    v.day = 1
    for msg in v.messages:
        msg["day"] = 1
    v.allies = [5]
    p, base = research.CommitmentPolicy(mode), WerewolfPolicy()
    assert p._day_vote(v, v.others_alive) == base._day_vote(v, v.others_alive)
    assert p._plan(v) == base._plan(v)
    assert p.export_memory() == base.export_memory()


@pytest.mark.parametrize("mode", research.MODES)
def test_strict_moving_phase_restart_uses_only_observed_actual_own_publication(mode):
    g = Game([OfficialBaseline() for _ in range(7)], random.Random(231))
    g.roles = ["wolf", "villager", "wolf", "seer", "villager", "villager", "villager"]
    g.day = 2
    v = scenario()
    g.messages = deepcopy(v.messages)
    g.votes = [{"day": 1, "votes": {1: 5}, "eliminated": None}]
    p = research.CommitmentPolicy(mode)
    p.observe(g.state(6, "messaging", "day_vote"))
    restored = research.CommitmentPolicy(mode)
    restored.import_memory(deepcopy(p.export_memory()))
    moving = g.state(6, "moving", "day_vote", list(range(6)) + [7])
    moving["new_messages"] = []
    assert restored.act(moving) == p.act(moving)
    assert restored.act(moving)["action_id"] == "0"
    cold = research.CommitmentPolicy(mode)
    assert cold.own_publication(cold._view(moving)) is None


@pytest.mark.parametrize("mode", research.MODES)
def test_factory_fresh_and_contestant_memory_restart(mode):
    c = getattr(research, mode)()
    assert c.ww.mode == mode
    c.ww.transcript[0] = {"seq": 0, "sender": 6, "content": "I vote Player0.", "day": 2}
    restored = getattr(research, mode)()
    restored._import(deepcopy(c._export("werewolf", None)))
    assert restored.ww.transcript == c.ww.transcript
    c._fresh()
    assert c.ww.mode == mode and not c.ww.transcript
    assert type(Contestant().ww) is WerewolfPolicy


@pytest.mark.parametrize("mode", research.MODES)
def test_runtime_first_speech_is_identical_and_no_extra_soft_followup(mode):
    g = Game([OfficialBaseline() for _ in range(7)], random.Random(231))
    g.roles = ["villager", "villager", "wolf", "seer", "villager", "wolf", "villager"]
    g.clock = 0
    c, base = getattr(research, mode)(), Contestant()
    ctx = DecisionContext("commitment-test", None, "werewolf", "ours")
    raw = g.state(6, "messaging", "day_vote")
    first = c.message_step(GameState.from_mcp_state(raw), ctx)
    baseline = base.message_step(GameState.from_mcp_state(raw), ctx)
    assert isinstance(first, SendMessage) and first.content == baseline.content
    assert first.recipients == baseline.recipients
    g.messages.append({"seq": 0, "sender": 6, "content": first.content, "day": 1,
                       "type": "chat", "visibility": "broadcast", "recipients": []})
    for sender, text in ((1, "I vote Player0."), (2, "I vote Player0."), (3, "Player4 is a wolf.")):
        g.messages.append({"seq": len(g.messages), "sender": sender, "content": text, "day": 1,
                           "type": "chat", "visibility": "broadcast", "recipients": []})
    g.clock = 10
    response = c.message_step(GameState.from_mcp_state(g.state(6, "messaging", "day_vote")), ctx)
    assert not isinstance(response, SendMessage)
    assert c._chat_sent == 1


@pytest.mark.parametrize("mode", research.MODES)
@pytest.mark.parametrize("timing", ["rt", "rounds"])
def test_full_wolf_actions_public_private_trace_and_outcome_identical(mode, timing, monkeypatch):
    monkeypatch.setenv("ARENA_WW_DM", "on")
    outputs = []
    for factory in (Contestant, getattr(research, mode)):
        seats = [ContestantSeat(factory)] + [OfficialBaseline() for _ in range(6)]
        g = ResearchGame(seats, random.Random(231), timing=timing, strict_messaging=True)
        g.roles = ["wolf", "villager", "wolf", "seer", "villager", "villager", "villager"]
        returns = g.play()
        outputs.append((g.action_trace, g.messages, g.votes, g.dead, returns))
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("day", [None, True, "2", 0, -1])
def test_unknown_or_malformed_day_cannot_manufacture_current_publication(day):
    v = view()
    v.day = day
    say(v, v.me, "I vote Player0.")
    assert research.CommitmentPolicy.own_publication(v) is None


def test_parser_only_me_replacement_never_reaches_knowledge_helpers(monkeypatch):
    v = scenario("seer")
    p = research.CommitmentPolicy()
    original_claims, original_knowledge = p.claims, p.seer_knowledge

    def claims(actual, private=True):
        assert actual.me == 6 and actual.role == "seer"
        return original_claims(actual, private=private)

    def knowledge(actual):
        assert actual.me == 6 and actual.role == "seer"
        return original_knowledge(actual)

    monkeypatch.setattr(p, "claims", claims)
    monkeypatch.setattr(p, "seer_knowledge", knowledge)
    assert p._day_vote(v, v.others_alive)[0] == 0


def test_invalid_mode_fails_closed():
    with pytest.raises(ValueError):
        research.CommitmentPolicy("unconditional_follow_others")


def test_completed_artifacts_are_paired_frozen_and_auditable():
    """Opt-in, read-only audit of the fixed experiments; no additional games."""
    import gzip
    import hashlib
    import json
    import os
    from collections import Counter, defaultdict
    from pathlib import Path
    if os.environ.get("ARENA_WW_COMMITMENT_AUDIT") != "1":
        pytest.skip("set ARENA_WW_COMMITMENT_AUDIT=1 after both fixed runs complete")
    root = Path(__file__).resolve().parents[2]
    report = json.loads((root / "data/werewolf_commitment_development.json").read_text())
    source_hash = hashlib.sha256((root / "local/werewolf_commitment_research.py").read_bytes()).hexdigest()
    backgrounds = json.loads((root / "data/werewolf_current_baseline.json").read_text())["protocol"]["backgrounds"]
    global_seeds = set()
    for stage, seed, games in (("development", 231, 100), ("replication", 232, 200)):
        result = report[f"{stage}_results"]
        directory = root / result["out"]
        manifest = json.loads((directory / "manifest.json").read_text())
        summary = result["summary"]
        assert summary == json.loads((directory / "summary.json").read_text())
        assert manifest["config"]["seeds"] == [seed]
        assert manifest["config"]["backgrounds"].split(",") == backgrounds
        assert manifest["config"]["games"] == games and manifest["config"]["jobs"] == 1
        assert manifest["config"]["seed_registry"] == "strength" and manifest["config"]["split"] == "development"
        assert manifest["config"]["visibility"] == "strict" and manifest["config"]["timing"] == "rt"
        assert result["module_unchanged"] and result["module_sha256"] == source_hash
        for filename, key in (("games.jsonl.gz", "raw_sha256"), ("manifest.json", "manifest_sha256"),
                              ("summary.json", "summary_sha256")):
            assert hashlib.sha256((directory / filename).read_bytes()).hexdigest() == result[key]
        profiles = [p[0] for p in manifest["profiles"]]
        by_id = defaultdict(list)
        with gzip.open(directory / "games.jsonl.gz", "rt") as stream:
            for line in stream:
                row = json.loads(line)
                assert "harness_error" not in row
                by_id[row["id"]].append(row)
        expected_ids = {f"{seed}/{bg}/k1/{i}" for bg in backgrounds for i in range(games)}
        assert set(by_id) == expected_ids
        seeds, differences, wolf_rows, raw_rows = set(), Counter(), 0, 0
        for rows in by_id.values():
            assert [r["profile"] for r in rows] == profiles
            base = rows[0]
            assert base["deal_seed"] not in seeds and base["deal_seed"] not in global_seeds
            seeds.add(base["deal_seed"])
            for row in rows:
                raw_rows += 1
                assert all(row[k] == base[k] for k in ("id", "scenario", "seat", "role", "deal_seed"))
                assert not any(v for k, v in row["failures"].items() if k != "window_close")
                assert not any(v for k, v in row["background_failures"].items() if k != "window_close")
                if row["profile"] != "reference":
                    fields = ("win", "winner", "days", "chats", "dms", "diagnostics")
                    differences[row["profile"]] += any(row[k] != base[k] for k in fields)
                    if row["role"] == "wolf":
                        wolf_rows += 1
                        assert all(row[k] == base[k] for k in fields)
                        if "trace" in base:
                            assert row["trace"] == base["trace"]
        global_seeds.update(seeds)
        print("AUDIT", stage, "unique_deals", len(seeds), "raw_rows", raw_rows,
              "wolf_candidate_rows_equal", wolf_rows, "observable_metric_differences", dict(differences),
              "gates", summary["gates"], "module", source_hash)
        for profile, scopes in summary["metrics"].items():
            print("OVERALL", stage, profile, {role: {key: values[key] for key in
                  ("n", "candidate_wins", "reference_wins", "delta", "conservative_ci95")}
                  for role, values in scopes["all"].items()})
        first = next(iter(summary["metrics"].values()))
        for bg in backgrounds:
            role_rows = first[f"opponent:{bg}"]
            compact = [role_rows["seat"]["candidate_wins"]] + [
                [role_rows[role]["candidate_wins"], role_rows[role]["n"]] for role in ("villager", "seer", "wolf")]
            assert report["final_summary"][f"backgrounds_{seed}"][bg] == compact
            print("BACKGROUND", stage, bg, " ".join(
                f"{role}={role_rows[role]['candidate_wins']}/{role_rows[role]['n']}"
                f"(B0={role_rows[role]['reference_wins']})" for role in ("seat", "villager", "seer", "wolf")))
    diagnostic = root / report["diagnostic_results"]["out"]
    counts = Counter()
    with gzip.open(diagnostic / "games.jsonl.gz", "rt") as stream:
        for line in stream:
            row = json.loads(line)
            for decision in row["decisions"]:
                if decision["own_target"] is not None and decision["own_target"] != decision["actual"]:
                    counts[(decision["role"], decision["baseline"][1], bool(decision["echoers"]))] += 1
    print("DIAGNOSTIC_MISMATCH_BREAKDOWN", dict(counts))
    print("TOTAL_UNIQUE_EXPERIMENT_DEALS", len(global_seeds), "diagnostic_is_reused_subset", True)
