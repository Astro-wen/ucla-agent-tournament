"""ARENA_SEARCH_SUCKER (10-15): in the engine search's blend, a row where one of our slots uses Sucker Punch keeps only
(1 - q) of the greedy column's weight (agent/js/search.js). Off (unset or 0) = unchanged."""
import copy

import pytest

from agent.pokemon import battlestate
from agent.pokemon import policy as pol
from agent.pokemon.dex import to_id

from .test_engine_search import FAST, MINE, THEIRS, cards, eng, needs_engine, real_obs, real_state, real_tmpl  # noqa: F401


def test_the_knob_is_off_by_default_and_read_from_the_environment(monkeypatch):
    for var, _ in pol.SEARCH_KNOBS.values():
        monkeypatch.delenv(var, raising=False)
    assert "sucker_q" not in pol.search_params()
    monkeypatch.setenv("ARENA_SEARCH_SUCKER", "0.7")
    assert pol.search_params()["sucker_q"] == pytest.approx(0.7)
    monkeypatch.setenv("ARENA_SEARCH_SUCKER", "bogus")
    assert "sucker_q" not in pol.search_params()


def sucker_state(**kw):
    """The real state of test_engine_search, with our Rillaboom carrying Sucker Punch instead of U-turn."""
    mine = copy.deepcopy(cards(*MINE))
    rb = mine["rillaboom"]
    rb["moves"] = ["Sucker Punch" if to_id(m) == "uturn" else m for m in rb["moves"]]
    tmpl = real_tmpl()
    for o in tmpl["slots"][1]["options"]:
        if o.get("move_id") == "uturn":
            o["move_id"] = "suckerpunch"
    st = battlestate.build(real_obs(), tmpl, mine, cards(*THEIRS), opp_roster=[to_id(x) for x in THEIRS], **kw)
    return st, tmpl


def _same(a, b):
    assert a["action"] == b["action"]
    for k in ("sims", "rows", "cols", "pick_value", "value", "mix", "opp_mix"):
        assert a["info"][k] == b["info"][k], k


@needs_engine
def test_no_sucker_row_or_q_zero_is_exactly_the_old_search(eng):  # noqa: F811
    work = dict(FAST, samples=2, rng=11, mode="blend", fixed=1, hard_ms=60000)
    off = eng.search(real_state(), work, 60.0)
    on = eng.search(real_state(), dict(work, sucker_q=1.0), 60.0)
    assert off is not None and on is not None, eng.last_error
    _same(off, on)                                   # no Sucker Punch on our side: nothing changes
    assert on["info"]["sucker_rows"] == 0 and "sucker_rows" not in off["info"]
    st, _ = sucker_state()
    off = eng.search(st, work, 60.0)
    zero = eng.search(sucker_state()[0], dict(work, sucker_q=0), 60.0)
    assert off is not None and zero is not None, eng.last_error
    _same(off, zero)
    assert "sucker_rows" not in zero["info"]


@needs_engine
def test_sucker_rows_lose_the_greedy_weight(eng):  # noqa: F811
    picks = {None: 0, 1.0: 0}
    rows = 0
    for rng in (3, 5, 7):
        work = dict(FAST, samples=1, rng=rng, mode="blend", fixed=1, hard_ms=60000, keep=30, slot_keep=6)
        for q in picks:
            st, tmpl = sucker_state()
            ans = eng.search(st, work if q is None else dict(work, sucker_q=q), 60.0)
            assert ans is not None, eng.last_error
            assert pol._template_ok(tmpl, ans["action"])
            if q is not None:
                rows += ans["info"]["sucker_rows"]
            picks[q] += ans["action"]["slot_1"].get("move_id") == "suckerpunch"
    assert rows > 0                                         # Sucker Punch rows were in the matrix
    assert picks[1.0] <= picks[None]
