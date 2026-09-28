"""v3-harden: v2.1 port consistency, Drafter robustness (set fingerprints, two-feature prior, node budget, near-ties,
stderr-only reports) and preview-matrix's work budget."""
import io
import json
import random
import time

import pytest

from agent.pokemon import calc as calc_mod
from agent.pokemon import dex, drafter, preview
from agent.pokemon.calc import DamageCalc, _js_dir
from agent.pokemon.dex import to_id
from agent.pokemon.drafter import Drafter, DraftModel, card_fingerprint
from agent.pokemon.policy import SPREAD_TARGETS, PokemonPolicy, card_score
from tests.strategy import test_lookahead as T

needs_calc = pytest.mark.skipif(_js_dir() is None, reason="agent/js has no node_modules")
CAT = dex.card_catalog()


def prior(c):
    return card_score(c, [])


# ================================================================ A. v2.1 port
@needs_calc
def test_ally_entries_of_the_exact_table_do_not_leak_into_speed_protect_or_the_exact_pick():
    """v2.1 adds our single-target attacks into our own partner to the exact table (("me", i, mid, "me", 1 - i)) for
    the lookahead. speed-protect's threat model only reads foe -> our slot entries (the merge's k[3] == "me" fix) and
    the exact scorer only reads our allAdjacent self-hits: with or without the new entries both must be identical."""
    st = T.state([T.view(T.CHIYU), T.view(T.INC)], [T.view(T.RILL, 0.6, foe=True), T.view(T.AMOO, foe=True)])
    obs = st["observation"]
    tmpl = st["legal_actions"]["actions"][0]["input"]["action"]
    actives, opps = obs["active_pokemon"], obs["opponent_active_pokemon"]
    pol = T.policy()
    table = pol._exact_table(obs, tmpl, actives, opps)
    ally = {k for k in table if k[0] == "me" and k[3] == "me" and dex.move(k[2]).get("target") not in SPREAD_TARGETS}
    assert ally, "the v2.1 exact table should hold our single-target attacks into the partner"
    stripped = {k: v for k, v in table.items() if k not in ally}
    out = []
    for tb in (table, stripped):
        p = T.policy()
        ctx = p._exact_context(obs, tmpl["slots"], actives, opps, obs["turn"], tb)
        pick = p._doubles_exact(ctx)
        sm = p.last_speed
        solos = [[round(c["solo"], 9) for c in cands] for _, cands in ctx["per_slot"]]
        out.append((pick, sm["threat"], sm["focus"], sm["foe_prio"], sm["fake_out"], solos, ctx["danger"]))
    assert out[0] == out[1]


@needs_calc
def test_warm_up_never_enters_the_cache_and_keeps_the_clear_before_insert_order(monkeypatch):
    dc = DamageCalc()
    monkeypatch.setattr(DamageCalc, "_inst", dc)
    monkeypatch.setattr(calc_mod, "CACHE_MAX", 3)
    try:
        assert dc.warm() and dc.starts == 1 and dc.cache == {}
        a = calc_mod.calc_pokemon(None, CAT["vgc-chi-yu"])
        b = calc_mod.calc_pokemon(None, CAT["vgc-incineroar"])
        mk = lambda mv, x, y: {"attacker": x, "defender": y, "move": mv, "field": {"gameType": "Doubles"}}  # noqa: E731
        first = dc.batch([mk("Dark Pulse", a, b), mk("Heat Wave", a, b)])
        assert all(first) and len(dc.cache) == 2
        # 2 cached + 2 new > CACHE_MAX: cleared BEFORE the insert, the batch still answers (the calc-fixes bug read
        # the batch back from the emptied cache and returned all None)
        second = dc.batch([mk("Flare Blitz", b, a), mk("Knock Off", b, a)])
        assert all(second) and len(dc.cache) == 2
        assert dc.warm() and dc.starts == 1                      # warm again: no new process, no cache change
        assert len(dc.cache) == 2
    finally:
        if dc.proc is not None and dc.proc.poll() is None:
            dc.proc.kill()
            dc.proc.wait()


def test_policy_warms_the_calc_once_in_the_pre_battle_phases(monkeypatch):
    calls = []
    monkeypatch.setattr(DamageCalc, "warm", lambda self: calls.append(1) or True)
    pol = PokemonPolicy()
    pool = list(CAT.values())[:18]
    st = {"phase": "draft", "current_actor": {"agent_id": "me"},
          "observation": {"phase": "draft", "current_seat": "me", "available_cards": pool, "rosters": {"me": [], "o": []}},
          "legal_actions": {"actions": [{"action_id": f"draft_pick:{c['card_id']}",
                                         "input": {"action": {"card_id": c["card_id"]}}} for c in pool]}}
    pol.act(st)
    pol.act(st)
    assert calls == [1] and pol.warmed


# ================================================================ B. Drafter robustness
def test_fingerprint_normalises_presentation_but_not_the_set():
    c = dict(CAT["vgc-incineroar"])
    fp = card_fingerprint(c)
    assert fp and len(fp) == 16
    same = dict(c, moves=list(reversed(c["moves"])), ivs={k: 31 for k in ("hp", "atk", "def", "spa", "spd", "spe")},
                evs={**c["evs"], "spe": 0}, card_id="renamed")
    same["item"] = c["item"].upper()
    assert card_fingerprint(same) == fp                       # order, 31 IVs, zero EVs, case and card_id ignored
    for change in ({"item": "Sitrus Berry"}, {"nature": "Adamant"}, {"ability": "Blaze"}, {"level": 51},
                   {"moves": c["moves"][:3] + ["U-turn"]}, {"evs": {**c["evs"], "hp": 4}},
                   {"ivs": {"spe": 0}}, {"species": "Arcanine"}):
        assert card_fingerprint({**c, **change}) != fp, change
    assert card_fingerprint({"card_id": "x", "species": "Pikachu"}) is None        # a species-only stub


def test_local_sim_cards_and_the_shipped_model_match_the_catalog_fingerprints():
    """The local sim fills 31 IVs, the platform sends {}: both must hash like the catalog, or every local ladder
    would treat every card as unseen."""
    from local.pool import POOL
    cat_fp = {cid: card_fingerprint(c) for cid, c in CAT.items()}
    assert all(card_fingerprint(c) == cat_fp[c["card_id"]] for c in POOL if c["card_id"] in cat_fp)
    d = json.loads(drafter.MODEL_PATH.read_text())
    assert d["fingerprints"] == cat_fp
    assert set(d["prior"]) >= {"a", "b", "c", "z_mean"}
    m = DraftModel.load()
    assert all(m.key(c) == c["card_id"] for c in POOL)
    assert all(m.key(dict(c, ivs={})) == c["card_id"] for c in CAT.values())


def test_a_changed_set_is_unseen_and_an_identical_set_under_a_new_id_is_known():
    base = CAT["vgc-incineroar"]
    other = CAT["vgc-rillaboom"]
    fps = {c["card_id"]: card_fingerprint(c) for c in (base, other)}
    m = DraftModel({"main": {base["card_id"]: 0.4, other["card_id"]: -0.1}, "fingerprints": fps,
                    "cross": {"vgc-incineroar|vgc-rillaboom": 0.2}, "species": {base["card_id"]: base["species"]},
                    "prior": {"a": 0.01, "b": 0.0, "c": 0.0}})
    assert m.key(base) == base["card_id"] and m.strength(base, prior) == 0.4
    changed = dict(base, item="Sitrus Berry")                  # same card_id, new set
    assert m.key(changed) is None and m.strength(changed, prior) == pytest.approx(0.01)
    assert m.x(m.key(changed), m.key(other)) == 0.0            # no learned counter terms either
    renamed = dict(base, card_id="vgc-incineroar-2")
    assert m.key(renamed) == base["card_id"]                   # identical set: the learned weights still apply
    new_species_twin = dict(base, card_id="vgc-incineroar-3", moves=["Fake Out", "Flare Blitz", "Knock Off", "U-turn"])
    assert m.key(new_species_twin) is None                     # same species, other set: no species borrowing
    assert m.key({"card_id": base["card_id"], "species": base["species"]}) == base["card_id"]   # stub: by id
    legacy = DraftModel({"main": {base["card_id"]: 0.4}, "species": {base["card_id"]: base["species"]}})
    assert legacy.key(new_species_twin) == base["card_id"]     # old files keep the old rule


def test_unseen_strength_is_the_two_feature_prior_with_this_drafts_mean_duel(monkeypatch):
    cards = [dict(CAT[k]) for k in ("vgc-incineroar", "vgc-rillaboom", "vgc-amoonguss", "vgc-chi-yu")]
    cards[0]["card_id"] = "vgc-brand-new"                      # not in the model
    cards[0]["item"] = "Sitrus Berry"
    beats = {("vgc-brand-new", "vgc-rillaboom"): 0.9, ("vgc-brand-new", "vgc-amoonguss"): 0.7,
             ("vgc-brand-new", "vgc-chi-yu"): 0.2}

    class FakeTT:
        def ensure(self, cards):
            pass

        def beats(self, a, b):
            if (a, b) in beats:
                return beats[(a, b)]
            if (b, a) in beats:
                return 1.0 - beats[(b, a)]
            return 0.5

    monkeypatch.setattr(drafter, "threat_table", lambda: FakeTT())
    fps = {c["card_id"]: card_fingerprint(c) for c in cards[1:]}
    m = DraftModel({"main": {c["card_id"]: 0.0 for c in cards[1:]}, "fingerprints": fps, "beta": 0.2,
                    "prior": {"a": -0.05, "b": 0.01, "c": -0.3, "z_mean": 0.0}})
    d = Drafter(m, prior_score=prior)
    mvec, P, _ = d._tables(cards)
    duel = ((0.9 - 0.5) + (0.7 - 0.5) + (0.2 - 0.5)) / 3
    assert mvec[0] == pytest.approx(-0.05 + 0.01 * prior(cards[0]) - 0.3 * duel)
    assert mvec[1:] == [0.0, 0.0, 0.0] and d.unseen == ["vgc-brand-new"]
    assert P[0][1] == pytest.approx(0.2 * 0.4) and P[1][0] == pytest.approx(-0.2 * 0.4)   # calc prior still applies
    assert m.strength(cards[0], prior) == pytest.approx(-0.05 + 0.01 * prior(cards[0]))   # no table: z_mean


def test_cards_outside_the_catalog_go_to_stderr_once_and_never_to_the_reasoning(monkeypatch, capsys):
    monkeypatch.setattr(drafter, "_REPORTED", set())
    pool = [dict(c) for c in random.Random(4).sample(list(CAT.values()), 18)]
    pool[0] = dict(pool[0], item="Sitrus Berry" if pool[0]["item"] != "Sitrus Berry" else "Leftovers")
    pool[1] = dict(pool[1], card_id="vgc-brand-new-card")
    st = {"phase": "draft", "current_actor": {"agent_id": "me"},
          "observation": {"phase": "draft", "current_seat": "me", "available_cards": pool,
                          "rosters": {"me": [], "o": []}, "pick_number": 1},
          "legal_actions": {"actions": [{"action_id": f"draft_pick:{c['card_id']}",
                                         "input": {"action": {"card_id": c["card_id"]}}} for c in pool]}}
    pol = PokemonPolicy()
    out = pol.act(st)
    err = capsys.readouterr().err
    assert f"card {pool[0]['card_id']}" in err and "set differs from the card catalog" in err
    assert "card vgc-brand-new-card" in err and "not in the card catalog" in err
    assert err.count("[agent.drafter]") == 2 and "unseen" in err
    assert "catalog" not in out["reasoning_summary"] and "vgc-brand-new" not in out["reasoning_summary"]
    assert "catalog" not in json.dumps(pol.draft_info)
    PokemonPolicy().act(st)                                    # the same cards again: nothing new on stderr
    assert "[agent.drafter]" not in capsys.readouterr().err
    catalog_only = [dict(c) for c in list(CAT.values())[:18]]
    assert drafter.report_unknown_cards(catalog_only, stream=io.StringIO()) == []


def first_pick_state(seed=9):
    pool = random.Random(seed).sample(list(CAT.values()), 18)
    return pool, [], [], {c["card_id"] for c in pool}


def test_node_budget_is_deterministic_and_load_independent(monkeypatch, capsys):
    pool, mine, opp, legal = first_pick_state()
    d = Drafter(prior_score=prior)
    want = d.pick(pool, mine, opp, legal, n_taken=0)
    assert d.info["depth"] == drafter.MAX_DEPTH and not d.info["budget_hit"]    # the default budget never binds here
    assert d.info["nodes"] < drafter.NODE_BUDGET / 3
    small = Drafter(prior_score=prior, node_budget=3000)
    got = small.pick(pool, mine, opp, legal, n_taken=0)
    ref = dict(small.info)
    assert ref["budget_hit"] and ref["depth"] < drafter.MAX_DEPTH
    # a heavily loaded machine: every clock read advances 50 ms (the old 0.3 s budget would stop after ~6 nodes)
    clock = [time.monotonic()]

    def slow():
        clock[0] += 0.05
        return clock[0]

    monkeypatch.setattr(drafter.time, "monotonic", slow)
    for budget in (3000, None):
        d2 = Drafter(prior_score=prior, node_budget=budget, wall_cap=1e9)
        assert d2.pick(pool, mine, opp, legal, n_taken=0) == (got if budget else want)
        if budget:
            assert (d2.info["depth"], d2.info["nodes"]) == (ref["depth"], ref["nodes"])
        else:
            assert d2.info["depth"] == drafter.MAX_DEPTH
    # the wall-clock guard still exists for pathological cases, and says so on stderr
    d3 = Drafter(prior_score=prior, wall_cap=1.0)
    assert d3.pick(pool, mine, opp, legal, n_taken=0) in legal
    assert d3.info["depth"] < drafter.MAX_DEPTH and "safety wall cap" in capsys.readouterr().err


def C(cid, item):
    return {"card_id": cid, "species": "Delibird", "item": item, "ability": "Hustle", "moves": ["Present"], "evs": {},
            "nature": "Hardy", "level": 50}


def test_near_ties_go_to_the_larger_immediate_gain():
    """Taking D (0.29) keeps the opponent off a small counter (+0.04 vs our R): search value -0.01 vs -0.03 for G
    (0.30). A 0.02 logit edge is inside TIE_EPS: the immediate gain (G) decides; with TIE_EPS 0 the search does."""
    mine = [C("r", "i1"), C("m2", "i2"), C("m3", "i3")]
    opp = [C("o1", "i4"), C("o2", "i5"), C("o3", "i6")]
    pool = [C("g", "Life Orb"), C("d", "Leftovers")] + [C(f"f{i}", f"filler {i}") for i in range(6)]
    model = DraftModel({"main": {"g": 0.30, "d": 0.29}, "cross": {"d|r": 0.04}, "beta": 0.0,
                        "prior": {"a": 0.0, "b": 0.0}})
    legal = {c["card_id"] for c in pool}
    strict = Drafter(model, use_matchups=False, tie_eps=0.0)
    assert strict.pick(pool, mine, opp, legal) == "d"
    d = Drafter(model, use_matchups=False)
    assert d.pick(pool, mine, opp, legal) == "g" and d.info["ties"] == 2
    assert 0.0 < strict.info["value"] - d.info["value"] == pytest.approx(0.02)
    assert Drafter(model, use_matchups=False).pick(list(reversed(pool)), mine, opp, legal) == "g"   # order-free


# ================================================================ C. preview-matrix work budget
def preview_inputs(seed=21):
    pick = random.Random(seed).sample(list(CAT.values()), 12)
    mine, theirs = pick[:6], pick[6:]
    mv = [{"species": to_id(c["species"])} for c in mine]
    ov = [{"species": to_id(c["species"]), "item": "unknown_item", "ability": None, "moves": []} for c in theirs]
    return mv, mine, ov, theirs


@needs_calc
def test_preview_budget_counts_work_not_cpu_time(monkeypatch):
    args = preview_inputs()
    ref = preview.choose(*args, seed_text="w")
    assert ref["info"]["segments"] <= preview.KNOBS["work_units"] * preview.KNOBS["search_share"] + 1000
    assert not ref["info"]["wall_hit"]
    # a loaded machine: CPU time races ahead (the old 1.6 s CPU budget would have fired at once) -- same answer
    t = [time.process_time()]

    def cpu():
        t[0] += 10.0
        return t[0]

    monkeypatch.setattr(preview.time, "process_time", cpu)
    got = preview.choose(*args, seed_text="w")
    keys = ("row", "rows", "pr", "cols", "pc")
    assert {k: got[k] for k in keys} == {k: ref[k] for k in keys}
    assert got["info"]["segments"] == ref["info"]["segments"] and got["info"]["rounds"] == ref["info"]["rounds"]


@needs_calc
def test_preview_small_work_budget_is_reproducible_and_zero_falls_back():
    args = preview_inputs(5)
    full = preview.choose(*args, seed_text="w", knobs={"work_units": 1e12})
    small = {"work_units": 2000, "search_share": 0.25}          # the search stops at 500 segments
    a = preview.choose(*args, seed_text="w", knobs=small)
    b = preview.choose(*args, seed_text="w", knobs=small)
    assert a["info"]["segments"] < full["info"]["segments"] and a["info"]["rounds"] <= full["info"]["rounds"]
    assert (a["row"], a["pr"], a["info"]["segments"]) == (b["row"], b["pr"], b["info"]["segments"])
    with pytest.raises(TimeoutError):
        preview.choose(*args, seed_text="w", knobs={"work_units": 0})
