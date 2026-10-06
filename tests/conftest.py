"""Test-wide defaults.

The deployed battle policy is the engine search (ARENA_BATTLE=search, agent/pokemon/policy.py). Every test except
tests/strategy/test_engine_search.py and test_engine_load.py pins ARENA_BATTLE=classic: those tests check the classic
scorer's picks (the search's fallback and one of its candidate rows) and would otherwise start the engine's Node
process and spend ~1 s of search on every doubles decision. The search itself is covered by those two files (they set
the mode they test) and by scripts/replay_real.py, which replays the real platform states with the default policy.

The match-state store (agent/matchstate.py) is off in every test (ARENA_STATE=0) unless the test turns it on with its
own directory: tests reuse session ids, and a Contestant must never pick up another test's match memory. The
per-match summary at exit is off too (ARENA_SUMMARY=0).
"""
import os

import pytest

SEARCH_TESTS = {"test_engine_search.py", "test_engine_load.py"}
os.environ.setdefault("ARENA_SUMMARY", "0")          # (also at the test process's exit)


@pytest.fixture(autouse=True)
def _classic_battle_policy(request, monkeypatch):
    if request.node.path.name not in SEARCH_TESTS:
        monkeypatch.setenv("ARENA_BATTLE", "classic")


@pytest.fixture(autouse=True)
def _no_match_state(monkeypatch):
    monkeypatch.setenv("ARENA_STATE", "0")
    monkeypatch.setenv("ARENA_SUMMARY", "0")
