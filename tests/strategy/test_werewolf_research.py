import copy
import random

import pytest

from local.werewolf_env import Game, OfficialBaseline
from local.werewolf_research import ResearchGame


def make_game(strict=True):
    game = ResearchGame([OfficialBaseline() for _ in range(7)], random.Random(201), strict_messaging=strict)
    game.roles = ["wolf", "villager", "seer", "villager", "wolf", "villager", "villager"]
    game.messages = [
        {"seq": 0, "sender": 2, "content": "I am the Seer. Player0 is a wolf.", "recipients": [], "visibility": "broadcast", "type": "chat", "day": 1},
        {"seq": 1, "sender": 0, "content": "Tonight: Player2.", "recipients": [4], "visibility": "direct", "type": "chat", "day": 1},
    ]
    return game


@pytest.mark.parametrize("seat", range(7))
@pytest.mark.parametrize("phase", ["night_wolf", "night_seer", "day_vote"])
def test_strict_moving_payload_does_not_reintroduce_expired_messages(seat, phase):
    game = make_game()
    assert game.state(seat, "moving", phase, [1, 7])["new_messages"] == []
    assert len(game.messages) == 2


@pytest.mark.parametrize("seat", range(7))
def test_discussion_retains_broadcast_and_only_permitted_private_messages(seat):
    game = make_game()
    state = game.state(seat, "messaging", "day_vote")
    assert [m["seq"] for m in state["new_messages"]] == ([0, 1] if seat in (0, 4) else [0])
    assert "hidden_roles" not in state and "diagnostics" not in state


def test_visibility_ablation_changes_nothing_except_new_messages():
    strict, legacy = make_game(), make_game(False)
    a, b = strict.state(0, "moving", "night_wolf", [1, 2]), legacy.state(0, "moving", "night_wolf", [1, 2])
    assert a.pop("new_messages") == []
    assert len(b.pop("new_messages")) == 2
    assert a == b


def test_diagnostic_trace_is_not_visible_to_policies_and_records_legal_choices():
    game = make_game()
    before = copy.deepcopy(game.state(0, "moving", "night_wolf", [1, 2]))
    assert game._choose(0, "night_wolf", [1, 2]) == 1
    trace = game.action_trace
    assert len(trace) == 1
    assert trace[0]["seat"] == 0 and trace[0]["action"] == 1
    assert trace[0]["legal"] == [1, 2]
    after = game.state(0, "moving", "night_wolf", [1, 2])
    assert set(after) == set(before)
    assert "action_trace" not in after


@pytest.mark.parametrize("seed", range(5))
def test_instrumentation_preserves_mechanical_games(seed):
    plain = Game([OfficialBaseline() for _ in range(7)], random.Random(seed))
    traced = ResearchGame([OfficialBaseline() for _ in range(7)], random.Random(seed))
    assert plain.play() == traced.play()
    assert plain.votes == traced.votes and plain.dead == traced.dead
    assert len(traced.action_trace) <= 100


def test_streaming_pair_statistics_match_audited_reference():
    from local.werewolf_benchmark import paired_stats
    from local.werewolf_research import PairCounts
    pairs = [(1, 0)] * 17 + [(0, 1)] * 5 + [(1, 1)] * 30 + [(0, 0)] * 10
    counter = PairCounts()
    for pair in pairs:
        counter.add(*pair)
    actual, expected = counter.report(), paired_stats(pairs)
    for key in ("n", "candidate_rate", "reference_rate", "delta", "ci95", "conservative_ci95", "mcnemar_p"):
        assert actual[key] == pytest.approx(expected[key])
    with pytest.raises(ValueError):
        counter.add(.5, 1)


def test_multiple_profiles_mirror_without_changing_deal_or_outcomes():
    from local.werewolf_benchmark import Scenario
    from local.werewolf_research import run_task
    rows = run_task((Scenario(201, 0, "llmq", 1), [("reference", "agent", {}), ("mirror", "agent", {})],
                     {"ARENA_WW_TIMING": "rt"}, True, True))
    for key in ("id", "role", "seat", "deal_seed", "winner", "win", "trace", "failures"):
        assert rows[0][key] == rows[1][key]


def test_default_public_opening_preserves_coordination_against_echo_players(monkeypatch):
    from local.werewolf_benchmark import Scenario
    from local.werewolf_research import run_task
    monkeypatch.delenv("ARENA_WW_DM", raising=False)
    row = run_task((Scenario(207, 31, "guard_echo", 1), [("reference", "agent", {})],
                    {"ARENA_WW_TIMING": "rt", "ARENA_WW_TACTICS": "off"}, True, True))[0]
    first_public = next(m for m in row["trace"]["messages"] if not m["recipients"])
    assert first_public["sender"] == row["seat"]
    assert row["dms"] == 0 and row["win"]
    assert row["trace"]["votes"][0]["eliminated"] != row["seat"]


def test_profile_environment_is_explicit_scoped_and_nonsecret():
    from local.werewolf_research import parse_profiles
    profiles = parse_profiles(["reference=agent", "public=agent"], ["public:ARENA_WW_TACTICS=public_vote"])
    assert profiles[0][2] == {}
    assert profiles[1][2] == {"ARENA_WW_TACTICS": "public_vote"}
    for bad in (["missing:ARENA_WW_DM=on"], ["public:API_KEY=value"], ["public:ARENA_WW_SECRET=value"]):
        with pytest.raises(ValueError):
            parse_profiles(["reference=agent", "public=agent"], bad)


def test_bounded_dispatch_does_not_queue_the_entire_experiment():
    from local.werewolf_research import bounded_outputs

    class Future:
        cancelled = False

        def __init__(self, chunk):
            self.chunk = chunk

        def result(self):
            return list(self.chunk)

        def cancel(self):
            self.cancelled = True

    class Pool:
        def __init__(self):
            self.sent = []

        def submit(self, fn, chunk):
            result = Future(chunk)
            self.sent.append(result)
            return result

    pool = Pool()
    outputs = bounded_outputs(pool, range(1000), 2)
    assert next(outputs) == 0
    assert len(pool.sent) == 2
    outputs.close()
    assert pool.sent[1].cancelled


def test_terminal_parity_is_not_a_playable_three_player_two_wolf_endgame():
    game = make_game()
    game.alive = [True, True, False, False, True, False, False]
    assert game.check() and game.winner == "wolf"
    game.alive = [True, True, True, True, True, False, False]
    game.winner = None
    assert not game.check()


def test_a_tie_and_an_innocent_elimination_are_not_interchangeable():
    game = make_game()
    game.alive = [True, True, True, True, True, False, False]
    game._kill(1, "vote")
    assert game.check() and game.winner == "wolf"
    tied = make_game()
    tied.alive = [True, True, True, True, True, False, False]
    tied.stall = 1
    assert not tied.check()
