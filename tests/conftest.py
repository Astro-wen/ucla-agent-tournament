"""Test-wide defaults.

The deployed battle policy is the engine search (ARENA_BATTLE=search, agent/pokemon/policy.py). Every test except
tests/strategy/test_engine_search.py pins ARENA_BATTLE=classic: those tests check the classic scorer's picks (the
search's fallback and one of its candidate rows) and would otherwise start the engine's Node process and spend ~1 s
of search on every doubles decision. The search itself is covered by test_engine_search.py (it sets the mode it
tests) and by scripts/replay_real.py, which replays the real platform states with the default policy.
"""
import pytest

SEARCH_TESTS = {"test_engine_search.py"}


@pytest.fixture(autouse=True)
def _classic_battle_policy(request, monkeypatch):
    if request.node.path.name not in SEARCH_TESTS:
        monkeypatch.setenv("ARENA_BATTLE", "classic")
