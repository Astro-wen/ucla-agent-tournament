"""Draft opponent model (agent/pokemon/drafter.py ARENA_DRAFT_OPP) and the real-contestant evaluation opponent
(local/meta_opp.py, scripts/fit_meta_drafter.py, scripts/draft_sim.py)."""
import importlib
import json
import random

import pytest

from agent.pokemon import dex, drafter
from agent.pokemon.drafter import SNAKE, Drafter, DraftMeta
from agent.pokemon.policy import card_score


@pytest.fixture(autouse=True)
def _no_draft_knobs(monkeypatch):
    for k in ("ARENA_DRAFT_OPP", "ARENA_DRAFT_OPPPRIOR", "ARENA_DRAFT_OPPTEMP", "ARENA_DRAFT_OPPK", "ARENA_DRAFT_OPPW",
              "ARENA_DRAFT_META", "ARENA_DRAFT_OPPCTR", "ARENA_DRAFT_METASET"):
        monkeypatch.delenv(k, raising=False)


def catalog():
    cat = list(dex.card_catalog().values())
    if len(cat) < 18:
        pytest.skip("no card catalog")
    return cat


def states(n, seed):
    """n random mid-draft states over the real catalog: (rest of the pool, mine, opp, legal ids, picks made)."""
    cat = catalog()
    rng = random.Random(seed)
    out = []
    while len(out) < n:
        pool = rng.sample(cat, 18)
        taken = rng.randrange(0, 12)
        me = SNAKE[taken]
        mine, opp = [], []
        for k in range(taken):
            (mine if SNAKE[k] == me else opp).append(pool[k])
        rest = pool[taken:]
        held = {c["item"] for c in mine}
        legal = {c["card_id"] for c in rest if c["item"] not in held}
        if legal:
            out.append((rest, mine, opp, legal, taken))
    return out


V43 = {"opp_mode": "off", "resid": 0}          # the fallback switches: exactly the v4.3 drafter


def test_off_is_the_v43_drafter_exactly(monkeypatch):
    """Both switches off (by argument or ARENA_DRAFT_OPP=off + ARENA_DRAFT_RESID=0), and meta mode with a fixed weight
    0, search exactly like the v4.3 drafter (a frozen copy: versions/v4_engine_search, same drafter as v4.3)."""
    old_mod = importlib.import_module("versions.v4_engine_search.agent.pokemon.drafter")
    old_policy = importlib.import_module("versions.v4_engine_search.agent.pokemon.policy")
    for rest, mine, opp, legal, taken in states(8, 11):
        old = old_mod.Drafter(prior_score=lambda c: old_policy.card_score(c, []))
        a = old.pick(rest, mine, opp, legal, n_taken=taken)
        for kw in (V43, {"opp_mode": "meta", "opp_w": 0.0, "resid": 0}, "env"):
            if kw == "env":
                monkeypatch.setenv("ARENA_DRAFT_OPP", "off")
                monkeypatch.setenv("ARENA_DRAFT_RESID", "0")
                new = Drafter(prior_score=lambda c: card_score(c, []))
                monkeypatch.delenv("ARENA_DRAFT_OPP")
                monkeypatch.delenv("ARENA_DRAFT_RESID")
            else:
                new = Drafter(prior_score=lambda c: card_score(c, []), **kw)
            assert new.pick(rest, mine, opp, legal, n_taken=taken) == a
            for k in ("depth", "nodes", "value", "top", "ties"):
                assert new.info[k] == old.info[k], k


def test_the_defaults_are_the_1013_draft(monkeypatch):
    d = Drafter(prior_score=lambda c: card_score(c, []))
    assert d.opp_mode == "meta" and d.resid == 1.0 and d.meta_set == "1013"
    assert d.opp_prior == pytest.approx(0.5) and d.opp_ctr == 1.0 and d.meta.gamma > 0
    monkeypatch.setenv("ARENA_DRAFT_METASET", "v44")          # the fallback switch: the v4.4 opponent model
    d = Drafter(prior_score=lambda c: card_score(c, []))
    assert d.opp_prior == pytest.approx(0.2) and d.opp_ctr == 0.0 and d.meta.gamma == 0.0


def test_meta_mode_is_legal_and_within_the_node_budget():
    for rest, mine, opp, legal, taken in states(6, 12):
        d = Drafter(prior_score=lambda c: card_score(c, []), opp_mode="meta")
        cid = d.pick(rest, mine, opp, legal, n_taken=taken)
        assert cid in legal
        assert d.info["depth"] >= 1 and 0.0 <= d.info["opp_w"] <= 1.0
        assert d.info["nodes"] <= drafter.NODE_BUDGET + 200


def _play_out(opp_picker, our_seat_first: bool, n_opp_picks: int, seed=3):
    """Draft until the opponent has made n_opp_picks; we pick by the v4.3 drafter. Returns our next Drafter's info."""
    cat = catalog()
    pool = random.Random(seed).sample(cat, 18)
    mine, opp = [], []
    for n, letter in enumerate(SNAKE):
        ours = (letter == "A") == our_seat_first
        rest = [c for c in pool if c not in mine and c not in opp]
        side = mine if ours else opp
        held = {c["item"] for c in side}
        legal = {c["card_id"] for c in rest if c["item"] not in held} or {c["card_id"] for c in rest}
        if ours and len(opp) >= n_opp_picks:
            d = Drafter(prior_score=lambda c: card_score(c, []), opp_mode="meta", opp_prior=0.5, resid=0)
            d.pick(rest, mine, opp, legal, n_taken=n)
            return d.info
        if ours:
            cid = Drafter(prior_score=lambda c: card_score(c, []), **V43).pick(rest, mine, opp, legal, n_taken=n)
        else:
            cid = opp_picker(rest, opp, mine, legal, n)
        side.append(next(c for c in rest if c["card_id"] == cid))
    raise AssertionError("draft ended")


def test_the_weight_follows_how_the_opponent_drafts(monkeypatch):
    monkeypatch.setenv("ARENA_DRAFT_METASET", "v44")          # the v4.4 prior: the meta-like picker below uses it
    meta = DraftMeta.load()
    if not meta:
        pytest.skip("no draft meta prior")

    def meta_like(rest, own, other, legal, n):
        return max((c for c in rest if c["card_id"] in legal), key=lambda c: (meta.utility(c), c["card_id"]))["card_id"]

    def model_like(rest, own, other, legal, n):            # the v4.3 drafter on the opponent's side
        return Drafter(prior_score=lambda c: card_score(c, []), **V43).pick(rest, own, other, legal, n_taken=n)

    w_meta, w_model = [], []
    for seed in (1, 2, 3):
        for first in (True, False):
            w_meta.append(_play_out(meta_like, first, 2, seed)["opp_w"])
            w_model.append(_play_out(model_like, first, 4, seed)["opp_w"])
    # (the v4.3 drafter often takes meta cards itself, so a model-like opponent is recognised only in some pools;
    # over 145 simulated drafts the mean weight after three of its picks was 0.16)
    assert min(w_meta) > 0.8 and sum(w_meta) / len(w_meta) > 0.9
    assert sum(w_model) / len(w_model) < sum(w_meta) / len(w_meta) - 0.3 and min(w_model) < 0.1
    # before the opponent's first pick: the prior
    d = Drafter(prior_score=lambda c: card_score(c, []), opp_mode="meta", opp_prior=0.3)
    rest, mine, opp, legal, taken = states(1, 1)[0]
    d.pick(rest + mine + opp, [], [], {c["card_id"] for c in rest + mine + opp}, n_taken=0)
    assert d.info["opp_w"] == pytest.approx(0.3, abs=1e-3)


def _meta_with_gamma(gamma):
    base = DraftMeta.load()
    if not base:
        pytest.skip("no draft meta prior")
    return DraftMeta({"u": dict(base.u), "mu": base.mu, "gamma": gamma})


def test_counter_term_off_is_the_v44_opponent_model_exactly():
    """OPP_CTR 0 ignores a prior file's gamma, and OPP_CTR 1 with gamma 0 changes nothing: both search exactly like
    the v4.4 opponent model (same pick, root values, depth and nodes)."""
    for rest, mine, opp, legal, taken in states(8, 21):
        ref = Drafter(prior_score=lambda c: card_score(c, []), opp_mode="meta", opp_prior=0.5,
                      meta=_meta_with_gamma(0.0), opp_ctr=0)
        a = ref.pick(rest, mine, opp, legal, n_taken=taken)
        for gamma, ctr in ((3.0, 0), (0.0, 1)):
            d = Drafter(prior_score=lambda c: card_score(c, []), opp_mode="meta", opp_prior=0.5,
                        meta=_meta_with_gamma(gamma), opp_ctr=ctr)
            assert d.pick(rest, mine, opp, legal, n_taken=taken) == a
            assert d.root_values == ref.root_values
            for k in ("depth", "nodes", "opp_w"):
                assert d.info[k] == ref.info[k], k


def test_counter_term_on_is_legal_within_budget_and_matters(monkeypatch):
    changed = 0
    for rest, mine, opp, legal, taken in states(10, 22):
        off = Drafter(prior_score=lambda c: card_score(c, []), opp_mode="meta", opp_prior=0.5,
                      meta=_meta_with_gamma(3.0), opp_ctr=0)
        on = Drafter(prior_score=lambda c: card_score(c, []), opp_mode="meta", opp_prior=0.5,
                     meta=_meta_with_gamma(3.0), opp_ctr=1)
        off.pick(rest, mine, opp, legal, n_taken=taken)
        cid = on.pick(rest, mine, opp, legal, n_taken=taken)
        assert cid in legal and 0.0 <= on.info["opp_w"] <= 1.0
        assert on.info["nodes"] <= drafter.NODE_BUDGET + 200
        changed += on.root_values != off.root_values or on.info["opp_w"] != off.info["opp_w"]
    assert changed >= 3
    monkeypatch.setenv("ARENA_DRAFT_OPPCTR", "1")
    assert Drafter().opp_ctr == 1.0


def test_meta_sets_select_file_prior_and_counter_term(monkeypatch):
    """ARENA_DRAFT_METASET picks the prior file, OPP_PRIOR and OPP_CTR together; the explicit knobs still override."""
    if not drafter.META_PATH_1013.exists():
        pytest.skip("no 1013 prior")
    f13 = json.loads(drafter.META_PATH_1013.read_text())
    assert f13["games"] >= 36 and f13["gamma"] > 0 and f13["u"]
    monkeypatch.setenv("ARENA_DRAFT_METASET", "v44")
    d = Drafter(prior_score=lambda c: card_score(c, []))
    assert (d.opp_prior, d.opp_ctr) == (pytest.approx(0.2), 0.0) and d.meta.gamma == 0.0
    assert d.meta.u == DraftMeta(json.loads(drafter.META_PATH.read_text())).u
    monkeypatch.setenv("ARENA_DRAFT_METASET", "1013")
    d = Drafter(prior_score=lambda c: card_score(c, []))
    assert (d.opp_prior, d.opp_ctr) == (pytest.approx(0.5), 1.0)
    assert d.meta.gamma == pytest.approx(f13["gamma"]) and d.meta.u == DraftMeta(f13).u
    monkeypatch.setenv("ARENA_DRAFT_OPPPRIOR", "0.3")
    monkeypatch.setenv("ARENA_DRAFT_OPPCTR", "0")
    d = Drafter(prior_score=lambda c: card_score(c, []))
    assert (d.opp_prior, d.opp_ctr) == (pytest.approx(0.3), 0.0)
    rest, mine, opp, legal, taken = states(1, 23)[0]
    monkeypatch.delenv("ARENA_DRAFT_OPPPRIOR")
    monkeypatch.delenv("ARENA_DRAFT_OPPCTR")
    d = Drafter(prior_score=lambda c: card_score(c, []))
    assert d.pick(rest, mine, opp, legal, n_taken=taken) in legal
    assert d.info["nodes"] <= drafter.NODE_BUDGET + 200


def test_singles_and_a_missing_prior_turn_it_off(tmp_path, monkeypatch):
    assert Drafter(singles=True, opp_mode="meta").opp_mode == "off"
    monkeypatch.setenv("ARENA_DRAFT_META", str(tmp_path / "missing.json"))
    monkeypatch.setattr(DraftMeta, "_cache", None)
    try:
        d = Drafter(prior_score=lambda c: card_score(c, []), opp_mode="meta", resid=0)
        assert not d.meta
        rest, mine, opp, legal, taken = states(1, 2)[0]
        base = Drafter(prior_score=lambda c: card_score(c, []), **V43)
        assert d.pick(rest, mine, opp, legal, n_taken=taken) == base.pick(rest, mine, opp, legal, n_taken=taken)
        assert d.info["opp_w"] == 0.0
    finally:
        monkeypatch.setattr(DraftMeta, "_cache", None)


def test_residuals_shift_both_sides_by_species(tmp_path, monkeypatch):
    rest, mine, opp, legal, taken = states(1, 6)[0]
    cards = mine + opp + rest
    base = Drafter(prior_score=lambda c: card_score(c, []), resid=0)
    m0, _, _ = base._tables(cards)
    mo0 = list(base.m_opp)
    target = dex.to_id(cards[0]["species"])
    f = tmp_path / "resid.json"
    f.write_text(json.dumps({"d": {target: 0.5}}))
    monkeypatch.setenv("ARENA_DRAFT_RESID_FILE", str(f))
    monkeypatch.setattr(drafter, "_RESID", None)
    try:
        d = Drafter(prior_score=lambda c: card_score(c, []), resid=1)
        m1, _, _ = d._tables(cards)
        assert m1[0] == pytest.approx(m0[0] + 0.5) and d.m_opp[0] == pytest.approx(mo0[0] + 0.5)
        assert m1[1:] == pytest.approx(m0[1:]) and d.m_opp[1:] == pytest.approx(mo0[1:])
        assert Drafter(singles=True, resid=1).resid == 0.0
        monkeypatch.setenv("ARENA_DRAFT_RESID", "0")
        assert Drafter().resid == 0.0
    finally:
        monkeypatch.setattr(drafter, "_RESID", None)


def test_shipped_residuals_exist_and_are_small():
    p = drafter.RESID_PATH
    if not p.exists():
        pytest.skip("no residual file")
    d = json.loads(p.read_text())
    assert d["d"] and max(abs(v) for v in d["d"].values()) < 0.5
    assert not set(d["seeds"]) & {7, 11, 31, 45, 73, 75, 83}          # the gate seeds stay out of sample


def test_policy_drafts_with_the_v44_defaults_and_the_switches_restore_v43(monkeypatch):
    seen = {}
    real = drafter.Drafter.pick

    def spy(self, *a, **k):
        seen["mode"], seen["resid"] = self.opp_mode, self.resid
        return real(self, *a, **k)

    monkeypatch.setattr(drafter.Drafter, "pick", spy)
    from agent.pokemon.policy import PokemonPolicy
    rest, mine, opp, legal, taken = states(1, 4)[0]
    obs = {"phase": "draft", "current_seat": "me", "pick_number": taken + 1, "available_cards": rest,
           "rosters": {"me": [{"card_id": c["card_id"], "species": c["species"]} for c in mine],
                       "op": [{"card_id": c["card_id"], "species": c["species"]} for c in opp]}, "picks": []}
    for env, want in (({}, ("meta", 1.0)), ({"ARENA_DRAFT_OPP": "off", "ARENA_DRAFT_RESID": "0"}, ("off", 0.0))):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        pk = PokemonPolicy()
        pk.draft_pool.update({c["card_id"]: c for c in mine + opp + rest})
        cid = pk._drafter_pick(obs, "me", legal)
        assert cid in legal and (seen["mode"], seen["resid"]) == want
        assert ("opp_w" in pk.draft_info) == (want[0] == "meta")


# ---- the evaluation opponent ------------------------------------------------------------------------------------
def test_panel_knows_the_meta_opponents():
    from local import panel
    assert panel.resolve_opps("meta,meta_soft,meta_v3") == ["meta", "meta_soft", "meta_v3"]
    assert "meta" not in panel.PANEL                       # opt-in only: the default panel is unchanged


def test_meta_seat_drafts_by_the_fitted_scores_and_fixes_its_pilot_memory():
    from local.meta_opp import MetaSeat
    from local.pokemon_env import Match, RandomPlayer
    from local.showdown import Showdown
    seat = MetaSeat("meta_v3")
    with Showdown() as sd:
        m = Match(sd, [seat, RandomPlayer()], random.Random(7))
        m.draft()
    six = [c["card_id"] for c in m.rosters[0]]
    assert six == seat.picks and len(six) == 6
    pk = seat.agent.pk
    for c in m.rosters[0]:
        assert pk.cards[dex.to_id(c["species"])]["card_id"] == c["card_id"]
    # the first pick (or the first after the opponent's) is the best-scored legal card
    assert m.invalid == [0, 0]


def test_fit_prefers_the_card_that_is_always_taken():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "fit_meta_drafter_under_test", Path(__file__).resolve().parents[2] / "scripts" / "fit_meta_drafter.py")
    fm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fm)
    cat = catalog()[:18]
    pool = {c["card_id"]: c for c in cat}
    star = cat[5]["card_id"]
    games = []
    for g in range(4):
        order = [c["card_id"] for c in cat if c["card_id"] != star]
        random.Random(g).shuffle(order)
        picks = [("them", star)] + [("us" if SNAKE[i] == "B" else "them", order[i - 1]) for i in range(1, 12)]
        games.append({"session": f"s{g}", "pool": pool, "picks": picks})

    class Flat:
        def ensure(self, cards):
            pass

        def beats(self, a, b):
            return 0.5

    evs = fm.events_of(games, Flat())
    species = sorted({x for e in evs for x in e["legal"]})
    u, mu, gam = fm.fit(evs, species, 1.5, False, iters=400)
    assert max(u, key=u.get) == dex.to_id(pool[star]["species"])


def test_shipped_meta_files_are_consistent():
    p = drafter.META_PATH
    if not p.exists():
        pytest.skip("no draft meta prior")
    d = json.loads(p.read_text())
    assert d["gamma"] == 0.0 and d["u"]
    from local.meta_opp import DATA
    e = json.loads(DATA.read_text())
    assert e["sessions"] == d["sessions"] and e["u"]
