"""Legal, deterministic, observation-only cheap opponents for the guard panel."""
import copy
import random

import pytest

from agent.werewolf.state import read_chat
from local import eval_werewolf as ev
from local.werewolf_env import Game, OfficialBaseline, same_names
from local.werewolf_guard_bots import (
    GUARD_BACKGROUNDS, AbstainSeat, AccuseSeat, BaitSeat, ContradictSeerSeat,
    FakeSeerSeat, RepeatAccuseSeat, VoteEchoSeat,
)


def state(role="villager", *, phase="messaging", day=1):
    game = Game([OfficialBaseline() for _ in range(7)], random.Random(161))
    game.roles = ["wolf", "wolf", "seer", "villager", "villager", "villager", "villager"]
    me = {"wolf": 0, "seer": 2, "villager": 3}[role]
    game.day = day
    game.invest = {6: "villager"}
    game.clock = 0
    return game.state(me, phase, "day_vote", [i for i in range(7) if i != me] + [7])


def msg(sender, text, seq=0, recipients=(), day=1):
    return {"sender": sender, "content": text, "seq": seq, "recipients": list(recipients),
            "visibility": "direct" if recipients else "broadcast", "type": "chat", "day": day}


def outputs(kind, raw):
    bot = kind()
    bot.reset()
    return bot.choose(copy.deepcopy(raw)), [bot.message(copy.deepcopy(raw)) for _ in range(7)]


@pytest.mark.parametrize("kind", GUARD_BACKGROUNDS.values(), ids=GUARD_BACKGROUNDS.keys())
@pytest.mark.parametrize("timing", ["rt", "rounds"])
def test_complete_games_are_legal_and_reproducible(kind, timing):
    snapshots = []
    for _ in range(2):
        game = Game([kind() for _ in range(7)], random.Random(161), timing=timing)
        game.play()
        assert game.winner in ("wolf", "villager")
        for key in ("crash", "illegal", "bad_chat", "timeout", "chat_after_terminate", "window_close"):
            assert not any(game.stats[key]), (kind.name, timing, key)
        snapshots.append((game.winner, game.votes, game.messages, game.dead))
    assert snapshots[0] == snapshots[1]


@pytest.mark.parametrize("kind", GUARD_BACKGROUNDS.values(), ids=GUARD_BACKGROUNDS.keys())
@pytest.mark.parametrize("role", ["villager", "wolf", "seer"])
def test_hidden_metadata_and_other_seats_private_data_cannot_change_behavior(kind, role):
    clean = state(role)
    polluted = copy.deepcopy(clean)
    # These fields are not part of an ordinary seat's observations. A simulator
    # adapter must never turn them into shortcuts for a bot that happens to see them.
    polluted["hidden_roles"] = ["seer"] * 7
    polluted["controlled_seats"] = [0, 3, 5]
    polluted["game_seed"] = 999999
    polluted["opponent_types"] = ["agent"] * 7
    polluted["game_state"]["roles"] = ["wolf"] * 7
    polluted["game_state"]["all_investigations"] = {i: {6: "wolf"} for i in range(7)}
    for player in polluted["players"]:
        player["role"] = "wolf"
        player["agent_id"] = "pretend-hidden-identity"
    polluted["new_messages"] = [msg(5, "Vote Player4.", recipients=[0, 1])]
    assert outputs(kind, polluted) == outputs(kind, clean)


@pytest.mark.parametrize("kind", GUARD_BACKGROUNDS.values(), ids=GUARD_BACKGROUNDS.keys())
def test_legal_action_filter_budget_termination_reset_and_duplicate_names(kind):
    raw = state("wolf")
    raw["legal_actions"]["actions"] = [{"action_id": "4"}]
    bot = kind()
    bot.reset()
    assert bot.choose(raw) == "4"
    emitted = []
    for _ in range(8):
        output = bot.message(raw)
        if output:
            assert 0 < len(output["content"].split()) <= 50
            assert len(output["content"].encode()) <= 1024
            assert output["recipients"] == []
            emitted.append(output)
    assert len(emitted) <= 5
    assert bot.message(raw) is None
    assert outputs(kind, same_names(raw)) == outputs(kind, raw)
    blocked = state("wolf", day=2)
    blocked["terminated_messaging"] = [0]
    assert bot.message(blocked) is None
    blocked["terminated_messaging"] = []
    assert bot.message(blocked) is None  # Dropping the server echo cannot reopen this window.
    bot.reset()
    assert bot.message(raw) == outputs(kind, raw)[1][0]


def test_abstain_is_only_a_legal_day_idle_proxy():
    raw = state(phase="moving")
    bot = AbstainSeat()
    bot.reset()
    assert bot.choose(raw) == "7"
    raw["game_state"]["phase"] = "night_seer"
    raw["legal_actions"]["actions"] = [{"action_id": "1"}, {"action_id": "5"}]
    assert bot.choose(raw) == "1"
    raw["game_state"]["phase"] = "day_vote"
    assert bot.choose(raw) in ("1", "5")


def test_repeat_contrast_changes_only_chat_count_and_shares_swapped_deals():
    raw = state("wolf")
    once_action, once = outputs(AccuseSeat, raw)
    repeat_action, repeat = outputs(RepeatAccuseSeat, raw)
    assert once_action == repeat_action
    assert [m for m in repeat if m] == [once[0]] * 5
    assert len([m for m in once if m]) == 1
    for seed in (161, 162):
        for game in range(10):
            a = ev.deal(seed, game, "heuristic+1guard_accuse", 1)
            b = ev.deal(seed, game, "heuristic+1guard_repeat", 1)
            assert a[:2] == b[:2]
            assert {s for s, k in a[2].items() if k == "guard_accuse"} == {
                s for s, k in b[2].items() if k == "guard_repeat"}
            assert sum(k == "guard_repeat" for k in b[2].values()) == 1


def test_echo_uses_public_observation_only_and_preserves_private_known_results():
    raw = state()
    raw["new_messages"] = [msg(2, "Vote Player5.", 0), msg(4, "Vote Player6.", 1)]
    assert outputs(VoteEchoSeat, raw)[0] == "5"
    raw["new_messages"] = [msg(2, "Vote Player5.", recipients=[raw["your_position"]])]
    assert outputs(VoteEchoSeat, raw)[0] == outputs(VoteEchoSeat, state())[0]
    seer = state("seer")
    seer["observation"] = seer["observation"].replace("Player6 is villager", "Player6 is wolf")
    seer["new_messages"] = [msg(1, "Vote Player5.")]
    assert outputs(VoteEchoSeat, seer)[0] == "6"
    wolf = state("wolf")
    wolf["new_messages"] = [msg(2, "Vote Player1.")]
    assert outputs(VoteEchoSeat, wolf)[0] != "1"


def test_fake_claims_use_only_the_speakers_role_and_contradict_same_target():
    assert outputs(FakeSeerSeat, state())[1] == [None] * 7
    text = outputs(FakeSeerSeat, state("seer"))[1][0]["content"]
    facts = read_chat(text, sender=2)
    assert facts.claim and facts.clears == {6} and not facts.wolves
    claims = [m for m in outputs(ContradictSeerSeat, state("wolf"))[1] if m]
    assert len(claims) == 2
    first, second = [read_chat(m["content"], sender=0) for m in claims]
    assert first.claim and second.claim and first.wolves == second.clears == {6}


def test_bait_is_semantically_a_quote_and_hypothetical_not_a_result():
    messages = [m for m in outputs(BaitSeat, state())[1] if m]
    assert len(messages) == 2
    for message in messages:
        facts = read_chat(message["content"], sender=3)
        assert not facts.claim and not facts.wolves and not facts.clears


@pytest.mark.parametrize("kind", GUARD_BACKGROUNDS)
def test_one_attacker_in_an_ordinary_table_is_legal(kind):
    record = ev.play_one("agent", 162, 1, f"heuristic+1{kind}", 1)
    assert not any(v for k, v in record["fail"].items() if k != "window_close")
    assert not any(v for k, v in record["bg_fail"].items() if k != "window_close")
