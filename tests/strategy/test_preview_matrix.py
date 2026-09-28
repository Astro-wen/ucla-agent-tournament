"""Team preview as a matrix game (agent/pokemon/preview.py). Calc-backed tests skip without agent/js/node_modules."""
import math
import time

import pytest

from agent.pokemon import preview
from agent.pokemon.calc import DamageCalc, _js_dir
from agent.pokemon.dex import card_catalog, to_id
from agent.pokemon.policy import PokemonPolicy
from local.pokemon_env import preview_view

needs_calc = pytest.mark.skipif(_js_dir() is None, reason="agent/js has no node_modules")
CATALOG = {to_id(c["species"]): c for c in card_catalog().values()}
MINE = ["chiyu", "incineroar", "rillaboom", "amoonguss", "garchomp", "whimsicott"]
THEIRS = ["kingambit", "tornadus", "ironhands", "farigiraf", "urshifurapidstrike", "dragonite"]


def preview_state(mine=MINE, theirs=THEIRS, known_opp=True):
    pol = PokemonPolicy()
    for s in mine:
        pol.cards[s] = CATALOG[s]
    if known_opp:
        for s in theirs:
            pol.opp_cards[s] = CATALOG[s]
    else:
        pol.seen = {}           # nothing to recover the opponent's sets from
    obs = {"your_roster": [preview_view(CATALOG[s], hidden=False) for s in mine],
           "opponent_roster": [preview_view(CATALOG[s], hidden=True) for s in theirs]}
    return pol, obs


def valid(out: dict, roster) -> bool:
    a = out["action"]
    return (a["type"] == "select_lineup" and len(set(a["bring"])) == 4 and set(a["bring"]) <= set(roster)
            and len(set(a["leads"])) == 2 and set(a["leads"]) <= set(a["bring"]))


# ---------------------------------------------------------------- pure game-theory pieces (no calc)
def test_lineups_are_the_90_bring4_lead2_choices():
    lus = preview.lineups(6)
    assert len(lus) == 90 and len(set(lus)) == 90
    assert all(len(set(t)) == 4 for t in lus)
    assert len({(frozenset(t[:2]), frozenset(t)) for t in lus}) == 90


def test_rm_plus_solves_rock_paper_scissors_and_a_dominated_game():
    rps = [[0, -1, 1], [1, 0, -1], [-1, 1, 0]]
    pr, pc, v = preview.solve_rm_plus(rps, 2000)
    assert all(abs(p - 1 / 3) < 0.02 for p in pr + pc) and abs(v) < 0.01
    assert preview.exploitability(rps, pr, pc) < 0.02
    dom = [[1.0, 0.5], [0.0, -0.5]]                 # row 0 strictly dominates, column 1 is better for the minimiser
    pr, pc, v = preview.solve_rm_plus(dom, 500)
    assert pr[0] > 0.99 and pc[1] > 0.99 and abs(v - 0.5) < 0.01


class _Stub:
    def __init__(self, M):
        self.M, self.la, self.lb = M, list(range(len(M))), list(range(len(M[0])))
        self.calls = 0

    def __call__(self, r, c):
        self.calls += 1
        return self.M[r][c]


def test_double_oracle_matches_the_full_equilibrium():
    import random
    rng = random.Random(3)
    M = [[math.tanh(rng.gauss(0, 1) + 0.3 * (r % 5) - 0.2 * (c % 7)) for c in range(40)] for r in range(40)]
    _, _, v_full = preview.solve_rm_plus(M, 3000)
    stub = _Stub(M)
    R, pr, C, pc, v, gap, rounds = preview.double_oracle(stub, preview.Deadline(30, 30), eps=0.002, iters=1500)
    prf, pcf = [0.0] * 40, [0.0] * 40
    for r, p in zip(R, pr):
        prf[r] += p
    for c, p in zip(C, pc):
        pcf[c] += p
    assert abs(v - v_full) < 0.02
    assert preview.exploitability(M, prf, pcf) < 0.03


def test_pick_is_deterministic_per_match_and_respects_mode():
    pr = [0.5, 0.3, 0.2, 0.0]
    a = [preview.pick(pr, None, "sample", f"match-{k}") for k in range(200)]
    b = [preview.pick(pr, None, "sample", f"match-{k}") for k in range(200)]
    assert a == b                                   # same match id -> same lineup (replays reproduce)
    assert set(a) == {0, 1, 2}                      # a genuine mixed strategy across matches
    assert abs(a.count(0) / 200 - 0.5) < 0.12
    assert preview.pick(pr, None, "argmax", "x") == 0


def test_masked_formes_resolve_to_the_drafted_card():
    store = {"urshifurapidstrike": CATALOG["urshifurapidstrike"], "kingambit": CATALOG["kingambit"]}
    assert preview.resolve_card("urshifu", store)["species"] == "Urshifu-Rapid-Strike"
    assert preview.resolve_card("kingambit", store)["species"] == "Kingambit"
    assert preview.resolve_card("ironhands", store) is None
    assert preview.resolve_card("ironhands", {}, {"ironhands": CATALOG["ironhands"]})["species"] == "Iron Hands"


def test_local_preview_view_hides_what_the_real_platform_hides():
    c = CATALOG["urshifurapidstrike"]
    theirs = preview_view(c, hidden=True)
    assert theirs["species"] == "urshifu" and theirs["types"] == ["FIGHTING", "DARK"]   # as in logs/*/537eb2be
    assert theirs["item"] == "unknown_item" and theirs["ability"] is None and theirs["moves"] == []
    ours = preview_view(c, hidden=False)
    assert ours["species"] == "urshifurapidstrike" and ours["moves"] and ours["item"] == "mysticwater"


# ---------------------------------------------------------------- fallbacks (no calc needed)
def test_calc_unavailable_or_crashing_falls_back_to_heuristic(monkeypatch):
    pol, obs = preview_state()
    monkeypatch.setattr(DamageCalc.get(), "batch", lambda calcs: None)
    out = pol._preview(obs)
    assert valid(out, MINE) and "matrix" not in out["reasoning_summary"]

    def boom(calcs):
        raise RuntimeError("bridge died")
    monkeypatch.setattr(DamageCalc.get(), "batch", boom)
    out = pol._preview(obs)
    assert valid(out, MINE) and "matrix" not in out["reasoning_summary"]


def test_malformed_rosters_do_not_crash_the_matrix_path():
    pol = PokemonPolicy()
    obs = {"your_roster": [None, {"species": "chiyu"}, {"species": "incineroar"}, {}, {"species": "garchomp"},
                           {"species": "rillaboom"}, {"species": "amoonguss"}],
           "opponent_roster": [None, {"species": "kingambit"}]}
    assert pol._preview_matrix(obs, "") is None      # too few opponents: declines, the old path answers
    obs["opponent_roster"] = [{"species": s} for s in THEIRS]
    obs["your_roster"] = [{"species": "chiyu"}] * 6  # duplicates: declines
    assert pol._preview_matrix(obs, "") is None


def test_singles_preview_keeps_the_heuristic():
    pol, obs = preview_state()
    obs["battle_format"] = "gen9customgame"          # pokemon_gen9same: singles with a 6-bring-4 preview
    assert pol._preview_matrix(obs, "") is None
    assert valid(pol._preview(obs), MINE)


def test_opponent_guess_matches_what_the_heuristic_opponent_plays():
    pol, obs = preview_state()
    them = PokemonPolicy()                          # the opponent, running the old heuristic preview
    them.seen = {}
    for s in THEIRS:
        them.cards[s] = CATALOG[s]
    their_obs = {"your_roster": [preview_view(CATALOG[s], hidden=False) for s in THEIRS],
                 "opponent_roster": [preview_view(CATALOG[s], hidden=True) for s in MINE]}
    actual = them._preview_heuristic(their_obs)["action"]
    opp_cards = [preview.resolve_card(p["species"], pol.opp_cards) for p in obs["opponent_roster"]]
    guess = pol._guess_opponent_lineup(obs["your_roster"], obs["opponent_roster"], opp_cards)
    assert {THEIRS[i] for i in guess[:2]} == set(actual["leads"])
    assert {THEIRS[i] for i in guess} == set(actual["bring"])


def test_mode_off_uses_the_heuristic(monkeypatch):
    monkeypatch.setitem(preview.KNOBS, "mode", "off")
    pol, obs = preview_state()
    out = pol._preview(obs)
    assert valid(out, MINE) and "matrix" not in out["reasoning_summary"]


# ---------------------------------------------------------------- calc-backed
@needs_calc
def test_matrix_preview_is_legal_fast_and_reproducible(monkeypatch):
    pol, obs = preview_state()
    DamageCalc.get()._start()
    t = time.process_time()                          # the work budget is ~1.6 s of CPU
    out = pol.act({"phase": "team_preview", "observation": obs, "session_id": "s-1"})
    assert time.process_time() - t < 2.0
    assert valid(out, MINE) and "matrix" in out["reasoning_summary"]
    # with work to spare the search always finishes, so the same match id gives the same lineup
    monkeypatch.setitem(preview.KNOBS, "work_units", 1e9)
    pol, obs = preview_state()
    out = pol.act({"phase": "team_preview", "observation": obs, "session_id": "s-1"})
    pol2, obs2 = preview_state()
    out2 = pol2.act({"phase": "team_preview", "observation": obs2, "session_id": "s-1"})
    assert out2["action"] == out["action"]


@needs_calc
def test_over_budget_falls_back(monkeypatch):
    monkeypatch.setitem(preview.KNOBS, "work_units", 0)
    pol, obs = preview_state()
    out = pol._preview(obs)
    assert valid(out, MINE) and "matrix" not in out["reasoning_summary"]


@needs_calc
def test_unknown_opponent_sets_use_generic_moves():
    pol, obs = preview_state(known_opp=False)
    out = pol._preview(obs)
    assert valid(out, MINE) and "matrix" in out["reasoning_summary"]


@needs_calc
def test_race_is_sane():
    ours = [preview.Unit(0, None, CATALOG[s]) for s in ("chiyu", "fluttermane", "incineroar", "rillaboom")]
    theirs = [preview.Unit(1, None, CATALOG[s]) for s in ("amoonguss", "whimsicott", "grimmsnarl", "klefki")]
    tb = preview.Tables(ours + theirs)
    race = preview.Race(tb)
    m = race.run((0, 1, 2, 3), (4, 5, 6, 7))
    assert -4.0 <= m <= 4.0
    assert m > 0                                    # heavy hitters vs a support pile
    # a mirror (same four on both sides) is close to even
    mirror = preview.Tables([preview.Unit(0, None, CATALOG[s]) for s in ("chiyu", "garchomp", "incineroar", "rillaboom")]
                            + [preview.Unit(1, None, CATALOG[s]) for s in ("chiyu", "garchomp", "incineroar", "rillaboom")])
    assert abs(preview.Race(mirror).run((0, 1, 2, 3), (4, 5, 6, 7))) < 1.5


# ---------------------------------------------------------------- review regressions
def test_masked_full_forme_resolves_through_the_fallback_pool():
    card = CATALOG["urshifurapidstrike"]
    assert preview.resolve_card("urshifurapidstrike", {}, {"urshifurapidstrike": card}) is card
    # a full forme id never borrows another forme's card
    single = {"species": "Urshifu"}
    assert preview.resolve_card("urshifurapidstrike", {"urshifu": single}) is None
    assert preview.resolve_card("urshifurapidstrike", {}, {"urshifu": single}) is None
    assert preview.resolve_card("urshifu", {}, {"urshifurapidstrike": card}) is card


def test_own_masked_forme_keeps_its_set_without_draft_memory(monkeypatch):
    """A policy that never saw the draft (e.g. a process restart) still simulates our Urshifu-Rapid-Strike with
    its catalog set instead of the generic 252/252 spread."""
    mine = ["urshifurapidstrike"] + MINE[1:]
    pol, obs = preview_state(mine=mine)
    pol.cards = {}
    got = {}

    def spy(my_views, my_cards, opp_views, opp_cards, **kw):
        got["mine"] = my_cards
        return None                                 # -> the heuristic answers
    monkeypatch.setattr(preview, "choose", spy)
    out = pol._preview(obs)
    assert valid(out, mine)
    assert all(c is not None and c.get("evs") for c in got["mine"])
    assert got["mine"][0]["species"] == "Urshifu-Rapid-Strike"


def test_calc_answering_errors_falls_back_to_heuristic(monkeypatch):
    """calc.js answering {"error"} to every calc (batch -> [None] * n) used to give an all-zero game whose
    'equilibrium' is just the first four roster slots; now the old preview answers."""
    pol, obs = preview_state()
    expected = pol._preview_heuristic(obs)["action"]
    dc = DamageCalc.get()
    monkeypatch.setattr(dc, "enabled", True)
    monkeypatch.setattr(dc, "batch", lambda calcs: [None] * len(calcs))
    pol, obs = preview_state()
    out = pol._preview(obs)
    assert valid(out, MINE) and "matrix" not in out["reasoning_summary"]
    assert out["action"] == expected


@needs_calc
def test_a_few_failed_calcs_keep_the_game_but_mostly_failed_does_not(monkeypatch):
    dc = DamageCalc.get()
    real = dc.batch
    units = [preview.Unit(0, None, CATALOG[s]) for s in MINE] + [preview.Unit(1, None, CATALOG[s]) for s in THEIRS]
    monkeypatch.setattr(dc, "batch", lambda calcs: [None if i % 10 == 0 else r for i, r in enumerate(real(calcs))])
    preview.Tables(units)                           # 10% missing: still a usable table
    monkeypatch.setattr(dc, "batch", lambda calcs: [None if i % 10 else r for i, r in enumerate(real(calcs))])
    with pytest.raises(RuntimeError):
        preview.Tables(units)                       # 90% missing: refuse


@needs_calc
def test_calc_cache_overflow_during_preview_keeps_the_matrix_game(monkeypatch):
    """Merge step 3 (was `..._falls_back`): DamageCalc used to clear an overflowing cache AFTER inserting the batch
    and then read the batch back from the empty cache, so a preview batch crossing 20000 entries came back all None
    and preview fell back to the heuristic (the guard in `Tables` kept it from playing lineup 0). calc.py now clears
    before inserting (merge step 1), so the same state keeps its matrix-game lineup; the guard itself is still
    covered by the two tests above."""
    dc = DamageCalc.get()
    dc._start()
    monkeypatch.setattr(dc, "cache", {f"dummy{k}": {"rolls": [1]} for k in range(19990)})
    pol, obs = preview_state()
    out = pol._preview(obs)
    assert valid(out, MINE) and "matrix" in out["reasoning_summary"]
    assert not any(k.startswith("dummy") for k in dc.cache)          # cleared before the preview batch went in
    assert len(dc.cache) > 50                                         # ... and the batch itself is cached


@needs_calc
def test_speed_ties_are_a_coin_flip_not_ours():
    """An exact speed tie across the sides used to go to side 0 (us) every time. Now swapping the seats negates
    every payoff, and a team's payoff against itself is exactly 0."""
    # rosters with cross-side ties (Tornadus / Thundurus 179, Incineroar / Grimmsnarl 80, Dragonite / Milotic
    # 101, Farigiraf / Porygon2 72, Chi-Yu / Salamence 167): A[r][c] of us vs them == -A[c][r] of them vs us
    a = ["tornadus", "incineroar", "dragonite", "farigiraf", "chiyu", "rillaboom"]
    b = ["thundurus", "grimmsnarl", "milotic", "porygon2", "salamence", "amoonguss"]
    t1 = preview.Tables([preview.Unit(0, None, CATALOG[s]) for s in a] + [preview.Unit(1, None, CATALOG[s]) for s in b])
    t2 = preview.Tables([preview.Unit(0, None, CATALOG[s]) for s in b] + [preview.Unit(1, None, CATALOG[s]) for s in a])
    A1 = preview.LazyPayoff(t1, list(range(6)), list(range(6, 12)), {})
    A2 = preview.LazyPayoff(t2, list(range(6)), list(range(6, 12)), {})
    cells = [divmod(n, 90) for n in range(0, 8100, 37)]
    for r, c in cells:
        assert A1(r, c) == pytest.approx(-A2(c, r), abs=1e-9)
    tied = 0
    for r, c in cells:
        A1.race.run(A1.la[r], A1.lb[c])
        tied += A1.race.tied
    assert tied > 10                                # the check above really exercised ties
    # mirror match: the tie decides something, and the two orders are exact mirror images
    four = ("chiyu", "garchomp", "incineroar", "rillaboom")
    tb = preview.Tables([preview.Unit(0, None, CATALOG[s]) for s in four]
                        + [preview.Unit(1, None, CATALOG[s]) for s in four])
    race = preview.Race(tb)
    m0 = race.run((0, 1, 2, 3), (4, 5, 6, 7), 0)
    assert race.tied
    m1 = race.run((0, 1, 2, 3), (4, 5, 6, 7), 1)
    assert abs(m0) > 0.05 and m0 == pytest.approx(-m1, abs=1e-9)
    assert race.value((0, 1, 2, 3), (4, 5, 6, 7)) == pytest.approx(0.0, abs=1e-9)


@needs_calc
def test_fake_out_flinches_and_intimidate_lowers_attack():
    units = [preview.Unit(0, None, CATALOG[s]) for s in ("incineroar", "chiyu")] + \
            [preview.Unit(1, None, CATALOG[s]) for s in ("garchomp", "thundurus")]
    tb = preview.Tables(units)
    race = preview.Race(tb)
    P = {x: u.fresh_state() for x, u in enumerate(units)}
    active = [[0, 1], [2, 3]]
    g = [0] * 6
    for s in (0, 1):
        for x in active[s]:
            race._enter(x, s, active, P, g)
    assert P[2][preview.ATK] == -1                   # Garchomp intimidated
    assert P[3][preview.ATK] == 1                    # Thundurus is Defiant: -1 then +2
    acts = race._decide(0, 1, active, P, g)
    kinds = {x: k for x, k, *_ in acts}
    assert kinds[0] == preview.K_FAKE                # Incineroar Fake Outs on turn 1
