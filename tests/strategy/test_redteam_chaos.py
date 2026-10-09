"""The red-team chaos backgrounds (local/redteam_chaos.py) play by the platform's rules: legal actions only, chats of
1..50 words and at most 1024 characters, broadcast or one other seat, at most 5 per window."""
import os
from collections import Counter

import pytest

from local import redteam_chaos as rc
from local.eval_werewolf import BACKGROUNDS, parse_bg

KINDS = sorted(rc.CHAOS_BACKGROUNDS)


def _check(game):
    for key in ("crash", "illegal", "bad_chat", "timeout"):
        assert sum(game.stats[key]) == 0, (key, game.stats[key])
    per_window = Counter()
    for m in game.messages:
        assert 1 <= len(m["content"].split()) <= rc.MAX_WORDS, m
        assert len(m["content"]) <= rc.MAX_CHARS, m
        assert len(m["recipients"]) <= 1 and m["sender"] not in m["recipients"], m
        per_window[(m["day"], m["sender"])] += 1
    assert max(per_window.values(), default=0) <= rc.MAX_CHATS


@pytest.mark.parametrize("kind", KINDS)
def test_chaos_tables_obey_the_platform_limits(kind, monkeypatch):
    monkeypatch.delenv("CHAOS_PROBE", raising=False)
    assert kind in BACKGROUNDS and parse_bg(kind)[1] == [kind]
    for g in range(6):
        game, ours, log = rc.play("agent", 2400, g, kind, 0)
        assert game.winner in ("wolf", "villager")
        _check(game)


@pytest.mark.parametrize("probe", ["fmt:buried:Player", "fmt:split:bold", "noise:oor", "noise:long", "noise:many",
                                   "noise:dmclaim", "noise:wolffirst", "noise:echo"])
def test_probe_tables_obey_the_platform_limits(probe, monkeypatch):
    monkeypatch.setenv("CHAOS_PROBE", probe)
    for g in range(4):
        game, ours, log = rc.play("agent", 2400, g, "chaos", 0)
        _check(game)


def test_the_driver_records_our_agent_seat(monkeypatch):
    monkeypatch.delenv("CHAOS_PROBE", raising=False)
    os.environ["ARENA_RECORD"] = "0"
    game, ours, log = rc.play("agent", 2400, 0, "chaos1", 1)
    assert len(ours) == 1 and any(e["kind"] == "act" for e in log)
    _check(game)
