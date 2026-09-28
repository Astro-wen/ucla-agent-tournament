"""Drafter (agent/pokemon/drafter.py): legal + fast picks, Item Clause in the look-ahead, deny picks, safe fallback."""
import json
import random
import time

import pytest

from agent.pokemon import dex, drafter
from agent.pokemon.dex import to_id
from agent.pokemon.drafter import Drafter, DraftModel
from agent.pokemon.matchup import _mean_damage
from agent.pokemon.policy import PokemonPolicy, card_score


def C(cid, item, species=None):
    return {"card_id": cid, "species": species or "Delibird", "item": item, "ability": "Hustle",
            "moves": ["Present"], "evs": {}, "nature": "Hardy", "level": 50}


def synthetic(main, cross=None):
    return DraftModel({"main": main, "cross": cross or {}, "beta": 0.0, "prior": {"a": 0.0, "b": 0.0}})


def fillers(n, start=0):
    return [C(f"f{i:02d}", f"filler item {i}") for i in range(start, start + n)]


def test_item_clause_in_lookahead_takes_the_card_the_opponent_cannot_take_later():
    # A is the best card but the opponent already holds A's item, so it cannot take it: grab B first, A later.
    a, b = C("a", "Booster Energy"), C("b", "Life Orb")
    opp = [C("o1", "Booster Energy")]
    pool = [a, b] + fillers(8)
    d = Drafter(synthetic({"a": 1.0, "b": 0.9}), use_matchups=False)
    # taken = 1 -> it is pick 2 (B side) and we also make pick 3: the opponent picks 4 and 5
    mine = []
    assert d.pick(pool, mine, opp, {c["card_id"] for c in pool}) in ("a", "b")
    # a sharper case: we pick once (pick 7), the opponent picks twice right after
    mine = [C("m1", "i1"), C("m2", "i2"), C("m3", "i3")]
    opp = [C("o1", "Booster Energy"), C("o2", "i5"), C("o3", "i6")]
    pool = [a, b] + fillers(6)
    assert d.pick(pool, mine, opp, {c["card_id"] for c in pool}) == "b"
    # without the clause the opponent would take whichever is left: then the stronger card first
    opp = [C("o1", "Choice Specs"), C("o2", "i5"), C("o3", "i6")]
    assert d.pick(pool, mine, opp, {c["card_id"] for c in pool}) == "a"


def test_deny_pick_takes_the_counter_the_opponent_needs_most():
    # G is slightly better on paper, but D hard-counters our R: leaving D means the opponent takes it.
    mine = [C("r", "i1"), C("m2", "i2"), C("m3", "i3")]
    opp = [C("o1", "i4"), C("o2", "i5"), C("o3", "i6")]
    g, dd = C("g", "Life Orb"), C("d", "Leftovers")
    pool = [g, dd] + fillers(6)
    model = synthetic({"g": 0.3, "d": 0.1}, cross={"d|r": 0.5})     # D (owner) vs R: +0.5 for D's owner
    d = Drafter(model, use_matchups=False)
    assert d.pick(pool, mine, opp, {c["card_id"] for c in pool}) == "d"
    # no counter relationship: plain best card
    d = Drafter(synthetic({"g": 0.3, "d": 0.1}), use_matchups=False)
    assert d.pick(pool, mine, opp, {c["card_id"] for c in pool}) == "g"


def test_only_legal_cards_are_returned():
    a, b = C("a", "Booster Energy"), C("b", "Life Orb")
    pool = [a, b] + fillers(8)
    d = Drafter(synthetic({"a": 5.0, "b": 0.1}), use_matchups=False)
    assert d.pick(pool, [C("m", "Booster Energy")], [], {"b", "f00"}) == "b"
    with pytest.raises(ValueError):
        d.pick(pool, [], [], {"zzz"})


def test_real_catalog_pick_is_legal_and_fast():
    cat = list(dex.card_catalog().values())
    if len(cat) < 18:
        pytest.skip("no card catalog")
    rng = random.Random(5)
    for trial in range(3):
        pool = rng.sample(cat, 18)
        taken = rng.randrange(0, 11)
        order = drafter.SNAKE
        mine, opp = [], []
        me = order[taken]
        for k in range(taken):
            (mine if order[k] == me else opp).append(pool[k])
        rest = pool[taken:]
        held = {c["item"] for c in mine}
        legal = {c["card_id"] for c in rest if c["item"] not in held}
        if not legal:
            continue
        d = Drafter(prior_score=lambda c: card_score(c, []))
        t = time.monotonic()
        cid = d.pick(rest, mine, opp, legal)
        assert cid in legal
        assert time.monotonic() - t < 5.0            # includes the one-off calc matchup table
        assert d.info["depth"] >= 1


def test_unknown_cards_use_the_card_score_prior():
    m = DraftModel({"main": {"vgc-x": 0.2}, "prior": {"a": 0.1, "b": 0.5}})
    assert m.strength({"card_id": "vgc-x"}, lambda c: 99) == 0.2
    assert m.strength({"card_id": "new", "species": "Foo"}, lambda c: 2.0) == pytest.approx(1.1)
    assert m.strength({"card_id": "new"}, lambda c: 1 / 0) == pytest.approx(0.1)   # prior crash -> intercept


def test_corrupt_model_file_falls_back_to_the_heuristic_prior(tmp_path, monkeypatch):
    bad = tmp_path / "m.json"
    bad.write_text(json.dumps({"main": {"x": 1.0}, "cross": {"no-separator": 1.0}}))
    monkeypatch.setenv("ARENA_DRAFT_MODEL", str(bad))
    monkeypatch.setattr(DraftModel, "_cache", None)
    m = DraftModel.load()
    assert not m.fitted and m.beta == drafter.DEFAULT_BETA
    monkeypatch.setattr(DraftModel, "_cache", None)            # do not leak the fallback into other tests


def test_shipped_model_covers_the_catalog():
    p = drafter.MODEL_PATH
    if not p.exists():
        pytest.skip("no fitted draft model")
    d = json.loads(p.read_text())
    cat = dex.card_catalog()
    from local.pool import NOT_IN_DRAFT_MODEL          # documented catalog cards the model was not fitted on
    assert set(cat) - set(d["main"]) == set(NOT_IN_DRAFT_MODEL)
    assert all(abs(v) < 5 for v in d["main"].values()) and abs(d["beta"]) < 5
    for k in d["cross"]:
        a, b = k.split("|")
        assert a < b


def test_mean_damage_handles_the_multi_hit_calc_output():
    one_hit = [80 + i for i in range(16)]
    assert _mean_damage(one_hit, "closecombat") == pytest.approx(sum(one_hit) / 16)
    buggy = [16 * 30, 16 * 31, 16 * 29]                  # main calc.js: one 16-roll SUM per hit
    assert _mean_damage(buggy, "surgingstrikes") == pytest.approx(90)
    fixed = [90] * 16                                    # a fixed calc.js: 16 per-roll totals
    assert _mean_damage(fixed, "surgingstrikes") == pytest.approx(90)


def draft_state(avail, rosters, me="me", legal=None):
    legal = legal if legal is not None else [c["card_id"] for c in avail]
    return {"phase": "draft", "current_actor": {"agent_id": me},
            "observation": {"phase": "draft", "current_seat": me, "available_cards": avail, "rosters": rosters},
            "legal_actions": {"actions": [{"action_id": f"draft_pick:{c}", "input": {"action": {"card_id": c}}}
                                          for c in legal]}}


def test_policy_falls_back_to_card_score_when_the_drafter_crashes(monkeypatch):
    cat = list(dex.card_catalog().values())[:18]
    monkeypatch.setattr(Drafter, "pick", lambda *a, **k: 1 / 0)
    out = PokemonPolicy().act(draft_state(cat, {"me": [], "opp": []}))
    want = max(cat, key=lambda c: card_score(c, []))["card_id"]
    assert out["action"]["card_id"] == want


def test_policy_ignores_an_illegal_drafter_answer(monkeypatch):
    cat = list(dex.card_catalog().values())[:18]
    monkeypatch.setattr(Drafter, "pick", lambda *a, **k: "not-a-card")
    out = PokemonPolicy().act(draft_state(cat, {"me": [], "opp": []}))
    assert out["action"]["card_id"] in {c["card_id"] for c in cat}


def test_policy_remembers_item_blocked_cards_for_the_search(monkeypatch):
    """The local env hides our item-blocked cards from available_cards; the platform lists them but not as legal.
    Either way the search must still see them in the pool (the opponent can take them)."""
    cat = list(dex.card_catalog().values())
    pool = cat[:18]
    seen = {}

    items = [c["item"] for c in pool]
    shared = next(c["card_id"] for c in pool if items.count(c["item"]) > 1)

    def spy(self, pool_, mine, opp, legal, n_taken=None):
        seen["pool"] = {c["card_id"] for c in pool_}
        seen["mine"] = [c["card_id"] for c in mine]
        seen["legal"] = set(legal)
        seen["n_taken"] = n_taken
        return shared if shared in legal else sorted(legal)[0]

    monkeypatch.setattr(Drafter, "pick", spy)
    pol = PokemonPolicy()
    pol.act(draft_state(pool, {"me": [], "opp": []}))
    first = next(c for c in pool if c["card_id"] == shared)
    assert to_id(first["species"]) in pol.cards
    blocked = [c for c in pool if c["item"] == first["item"] and c["card_id"] != first["card_id"]]
    assert blocked
    rest = [c for c in pool if c["card_id"] != first["card_id"]]
    shown = [c for c in rest if c not in blocked][:15]
    rosters = {"me": [{"card_id": first["card_id"], "species": first["species"]}],
               "opp": [{"card_id": c["card_id"], "species": c["species"]} for c in shown[:2]]}
    pol.act(draft_state(shown[2:], rosters))
    assert set(c["card_id"] for c in blocked) <= seen["pool"]
    assert seen["mine"] == [first["card_id"]]
    assert not (seen["pool"] & {c["card_id"] for c in shown[:2]})
    assert seen["n_taken"] == 3


def test_turn_order_follows_pick_number_and_picks_backfill_rosters(monkeypatch):
    cat = list(dex.card_catalog().values())[:18]
    seen = {}

    def spy(self, pool_, mine, opp, legal, n_taken=None):
        seen.update(pool={c["card_id"] for c in pool_}, opp=[c["card_id"] for c in opp], n_taken=n_taken)
        return sorted(legal)[0]

    monkeypatch.setattr(Drafter, "pick", spy)
    st = draft_state(cat[1:], {"me": []})                  # opponent's roster missing from `rosters`
    st["observation"]["pick_number"] = 2
    st["observation"]["picks"] = [{"pick_number": 1, "seat_id": "opp", "card_id": cat[0]["card_id"],
                                   "species": cat[0]["species"]}]
    PokemonPolicy().act(st)
    assert seen["n_taken"] == 1 and seen["opp"] == [cat[0]["card_id"]] and cat[0]["card_id"] not in seen["pool"]


def test_whole_draft_between_two_policies_is_legal_and_respects_the_item_clause():
    cat = list(dex.card_catalog().values())
    if len(cat) < 18:
        pytest.skip("no card catalog")
    pool = random.Random(11).sample(cat, 18)
    pols = {"a": PokemonPolicy(), "b": PokemonPolicy()}
    rosters, picks = {"a": [], "b": []}, []
    for n, letter in enumerate(drafter.SNAKE, 1):
        seat = "a" if letter == "A" else "b"
        taken = {p["card_id"] for p in picks}
        held = {c["item"] for c in rosters[seat]}
        avail = [c for c in pool if c["card_id"] not in taken]
        legal = [c["card_id"] for c in avail if c["item"] not in held]
        st = draft_state(avail, {s: [{"card_id": c["card_id"], "species": c["species"]} for c in r]
                                 for s, r in rosters.items()}, me=seat, legal=legal)
        st["observation"].update(pick_number=n, picks=list(picks))
        cid = pols[seat].act(st)["action"]["card_id"]
        assert cid in legal
        card = next(c for c in pool if c["card_id"] == cid)
        rosters[seat].append(card)
        picks.append({"pick_number": n, "seat_id": seat, "card_id": cid, "species": card["species"]})
    for r in rosters.values():
        assert len(r) == 6 and len({c["item"] for c in r}) == 6


def test_opponent_first_card_outside_the_catalog_is_searched_not_skipped():
    """Picking second, the opponent's first card was never in our available_cards and is not in the catalog:
    rosters / picks only give card_id + species (no item). The search must still run (no silent fallback),
    count it as taken and return a legal card."""
    cat = list(dex.card_catalog().values())
    if len(cat) < 17:
        pytest.skip("no card catalog")
    pool = random.Random(3).sample(cat, 17)
    ghost = {"card_id": "vgc-not-in-catalog-001", "species": "Pikachu"}
    st = draft_state(pool, {"me": [], "opp": [dict(ghost)]})
    st["observation"].update(pick_number=2, picks=[{"pick_number": 1, "seat_id": "opp", **ghost}])
    pol = PokemonPolicy()
    out = pol.act(st)
    assert out["action"]["card_id"] in {c["card_id"] for c in pool}
    assert pol.draft_info.get("depth", 0) >= 1, "drafter fell back to card_score"
    assert "drafter" in out["reasoning_summary"]
