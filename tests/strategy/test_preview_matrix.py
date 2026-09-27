"""Team preview via the exact-damage matchup matrix (calc-backed tests skip without agent/js/node_modules)."""
import time

import pytest

from agent.pokemon import matchup
from agent.pokemon.calc import DamageCalc, _js_dir
from agent.pokemon.dex import card_catalog, to_id
from agent.pokemon.policy import PokemonPolicy

needs_calc = pytest.mark.skipif(_js_dir() is None, reason="agent/js has no node_modules")
CATALOG = {to_id(c["species"]): c for c in card_catalog().values()}
MINE = ["chiyu", "incineroar", "rillaboom", "amoonguss", "garchomp", "whimsicott"]
THEIRS = ["kingambit", "tornadus", "ironhands", "farigiraf", "arcanine", "dragonite"]


def view(sid: str, hidden: bool) -> dict:
    c = CATALOG[sid]
    if hidden:      # the real platform hides the opponent's item / ability / moves at team preview
        return {"species": sid, "item": "unknown_item", "ability": None, "moves": []}
    return {"species": sid, "item": c["item"], "ability": c["ability"], "moves": [to_id(m) for m in c["moves"]]}


def preview_state(mine=MINE, theirs=THEIRS, known_opp=True):
    pol = PokemonPolicy()
    for s in mine:
        pol.cards[s] = CATALOG[s]
    if known_opp:
        for s in theirs:
            pol.opp_cards[s] = CATALOG[s]
    else:
        pol.seen = {}           # nothing to recover the opponent's sets from
    obs = {"your_roster": [view(s, False) for s in mine], "opponent_roster": [view(s, True) for s in theirs]}
    return pol, obs


def valid(out: dict, roster) -> bool:
    a = out["action"]
    return (a["type"] == "select_lineup" and len(set(a["bring"])) == 4 and set(a["bring"]) <= set(roster)
            and len(set(a["leads"])) == 2 and set(a["leads"]) <= set(a["bring"]))


@needs_calc
def test_matrix_preview_returns_a_legal_lineup_quickly():
    pol, obs = preview_state()
    t = time.perf_counter()
    out = pol.act({"phase": "team_preview", "observation": obs})
    assert time.perf_counter() - t < 3.0
    assert valid(out, MINE)
    assert "matchup matrix" in out["reasoning_summary"]


@needs_calc
def test_matrix_preview_with_unknown_opponent_sets_uses_generic_moves():
    pol, obs = preview_state(known_opp=False)
    out = pol._preview(obs)
    assert valid(out, MINE) and "matchup matrix" in out["reasoning_summary"]


@needs_calc
def test_matrix_directions_are_sane():
    ours = [matchup.Mon(view("chiyu", False), CATALOG["chiyu"])]
    theirs = [matchup.Mon(view("rillaboom", True), CATALOG["rillaboom"]),
              matchup.Mon(view("garchomp", True), CATALOG["garchomp"])]
    mx = matchup.matchup_matrix(ours, theirs)
    assert mx is not None
    assert mx["out"][0][0] > mx["in"][0][0]          # Chi-Yu's fire STAB into Rillaboom beats Rillaboom's hits back
    assert mx["in"][0][1] > 0.5                      # Garchomp's ground STAB hurts Chi-Yu badly
    assert mx["first"][0][0] is not False            # Chi-Yu outspeeds Rillaboom (Grassy Glide aside)


def test_calc_unavailable_or_crashing_falls_back_to_heuristic(monkeypatch):
    pol, obs = preview_state()
    monkeypatch.setattr(DamageCalc.get(), "batch", lambda calcs: None)
    out = pol._preview(obs)
    assert valid(out, MINE) and "matchup matrix" not in out["reasoning_summary"]

    def boom(calcs):
        raise RuntimeError("bridge died")
    monkeypatch.setattr(DamageCalc.get(), "batch", boom)
    out = pol._preview(obs)
    assert valid(out, MINE)


def test_malformed_rosters_do_not_crash(monkeypatch):
    monkeypatch.setattr(DamageCalc.get(), "batch", lambda calcs: None)
    pol = PokemonPolicy()
    obs = {"your_roster": [None, {"species": "chiyu"}, {"species": "incineroar"}, {}, {"species": "garchomp"},
                           {"species": "rillaboom"}, {"species": "amoonguss"}],
           "opponent_roster": [None, {"species": "kingambit"}]}
    out = pol._preview(obs)
    assert len(out["action"]["bring"]) == 4


def _synthetic(n=6, m=6):
    """A hand-built matrix: nobody threatens anyone except ours 5 -> theirs 0 (a 2HKO nobody else has)."""
    mx = {k: [[0.0] * m for _ in range(n)] for k in ("out", "out_min", "in", "in_min")}
    mx["first"] = [[None] * m for _ in range(n)]
    for i in range(n):
        for j in range(m):
            mx["out"][i][j], mx["out_min"][i][j] = 0.3, 0.25
            mx["in"][i][j], mx["in_min"][i][j] = 0.3, 0.25
    mx["out"][5][0], mx["out_min"][5][0] = 0.6, 0.5
    return mx


def test_coverage_brings_the_only_answer_to_a_threat():
    ours = [matchup.Mon({"species": s}, CATALOG[s]) for s in MINE]
    theirs = [matchup.Mon({"species": s}, CATALOG[s]) for s in THEIRS]
    bring, leads, _ = matchup.choose_lineup(ours, theirs, _synthetic())
    assert 5 in bring and len(set(bring)) == 4 and set(leads) <= set(bring)
