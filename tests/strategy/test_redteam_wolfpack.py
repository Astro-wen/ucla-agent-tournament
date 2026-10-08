"""The red-team `wolfpack` backgrounds (local/redteam_wolfpack.py) play by the platform's rules and stay coordinated."""
import random

from local.eval_werewolf import BACKGROUNDS, MIXED
from local.redteam_wolfpack import KINDS, VARIANTS, WolfpackHSeat, WolfpackSeat
from local.werewolf_env import MAX_CHATS, MAX_WORDS, Game


def _play(cls_by_seat, seed):
    game = Game([c() for c in cls_by_seat], random.Random(seed))
    game.play()
    return game


def test_registered_without_touching_existing_backgrounds():
    assert BACKGROUNDS["wolfpack"] is WolfpackSeat and BACKGROUNDS["wolfpack_h"] is WolfpackHSeat
    assert {"first", "heuristic", "random", "llm", "llmx", "llmq"} <= set(BACKGROUNDS)
    assert MIXED == ("first", "heuristic", "random", "llm")
    assert set(KINDS) == {"wolfpack", "wolfpack_h"} and "wolfpack_v0" in VARIANTS


def test_legal_moves_and_chat_within_platform_limits():
    for seed in range(40):
        cls = WolfpackSeat if seed % 2 else WolfpackHSeat
        game = _play([cls] * 7, 9000 + seed)
        for key in ("crash", "illegal", "bad_chat", "timeout", "chat_after_terminate"):
            assert sum(game.stats[key]) == 0, (seed, key)
        per_day = {}
        for m in game.messages:
            assert 0 < len(m["content"].split()) <= MAX_WORDS and len(m["content"]) <= 1024
            assert len(m["recipients"]) <= 1 and m["sender"] not in m["recipients"]
            per_day[(m["day"], m["sender"])] = per_day.get((m["day"], m["sender"]), 0) + 1
        assert max(per_day.values(), default=0) <= MAX_CHATS


def test_wolves_never_split_a_vote():
    for seed in range(40):
        game = _play([WolfpackSeat] * 7, 9100 + seed)
        wolves = [i for i, r in enumerate(game.roles) if r == "wolf"]
        for v in game.votes:
            ballots = {v["votes"][w] for w in wolves if w in v["votes"]}
            assert len(ballots) <= 1, (seed, v)
