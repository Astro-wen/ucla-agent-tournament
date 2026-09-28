"""Drafter refit track: platform pool frequencies, item-blocked listing and draft dead ends in the local env, the
harvested cards (and which of them the shipped model has not fitted), the asymmetric (main_opp) draft model, the
population fitter (orientation, family bias, held-out scoring) and the draft panel's resume."""
import collections
import importlib.util
import json
import random
from pathlib import Path

import pytest

from agent.pokemon import dex
from agent.pokemon.drafter import Drafter, DraftModel, card_fingerprint
from local import pool as lpool
from local.pokemon_env import SNAKE, Match, Player

ROOT = Path(__file__).resolve().parents[2]


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- catalog + real pools
NEW_CARDS = {
    "vgc-pelipper": ("Pelipper", "Damp Rock", "Drizzle", ["Hurricane", "Weather Ball", "Tailwind", "Protect"]),
    "vgc-scream-tail": ("Scream Tail", "Covert Cloak", "Protosynthesis", ["Dazzling Gleam", "Encore", "Wish", "Protect"]),
    "vgc-ursaluna": ("Ursaluna", "Flame Orb", "Guts", ["Facade", "Headlong Rush", "Earthquake", "Protect"]),
    "vgc-tyranitar": ("Tyranitar", "Chople Berry", "Sand Stream", ["Rock Slide", "Knock Off", "Low Kick", "Protect"]),
    "vgc-toxapex": ("Toxapex", "Black Sludge", "Regenerator", ["Liquidation", "Haze", "Toxic", "Protect"]),
}


def test_the_new_platform_cards_are_in_the_catalog_with_their_recorded_sets():
    cat = dex.card_catalog()
    for cid, (sp, item, ability, moves) in NEW_CARDS.items():
        c = cat[cid]
        assert (c["species"], c["item"], c["ability"], c["moves"]) == (sp, item, ability, moves), cid
    # every set in the catalog is exactly what the recordings show (one set per card_id across all recorded drafts)
    seen = collections.defaultdict(set)
    for _sid, o in lpool._draft_states(str(ROOT / "logs" / "*" / "*.jsonl")):
        for c in o.get("available_cards", []):
            seen[c["card_id"]].add(json.dumps(c, sort_keys=True))
    if not seen:
        pytest.skip("no recorded real logs in this checkout")
    for cid, sets in seen.items():
        assert len(sets) == 1, cid
        assert json.loads(next(iter(sets))) == cat[cid], cid


def test_recorded_real_pools_are_complete_and_known():
    pools = lpool.real_pools()
    assert len(pools) >= 39
    cat = dex.card_catalog()
    for sid, ids in pools.items():
        assert len(ids) == 18 and len(set(ids)) == 18, sid
        assert all(i in cat for i in ids), sid


# ---------------------------------------------------------------- sampling
def test_inclusion_probs_follow_the_real_counts_and_sum_to_the_pool_size():
    cards = [{"card_id": f"c{i}"} for i in range(30)]
    pools = {f"s{k}": [f"c{i}" for i in range(18)] for k in range(5)}      # c0..c17 always, c18..c29 never
    pi = lpool.inclusion_probs(cards, pools)
    assert abs(sum(pi) - 18) < 1e-9
    assert max(pi) <= lpool.POOL_PI_MAX + 1e-12
    assert min(pi[:18]) > max(pi[18:]) > 0                                 # smoothed: never impossible
    assert lpool.inclusion_probs(cards, {}) == [18 / 30] * 30               # no data: uniform
    assert lpool.inclusion_probs(cards[:10], pools) == [1.0] * 10           # fewer cards than a pool


def test_sample_by_inclusion_hits_the_marginals_exactly_k_distinct():
    rng = random.Random(3)
    items = list(range(12))
    pi = [0.95, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.3, 0.25, 0.1, 0.1]     # sums to 6
    cnt = collections.Counter()
    n = 6000
    for _ in range(n):
        s = lpool.sample_by_inclusion(items, pi, rng, 6)
        assert len(s) == 6 and len(set(s)) == 6
        cnt.update(s)
    for i, p in enumerate(pi):
        assert abs(cnt[i] / n - p) < 0.025, (i, cnt[i] / n, p)


def test_sample_pool_real_mode_matches_the_platform_frequencies(monkeypatch):
    monkeypatch.delenv("ARENA_POOL", raising=False)
    pi = dict(zip([c["card_id"] for c in lpool.POOL], lpool.inclusion_probs(lpool.POOL)))
    rng = random.Random(11)
    cnt = collections.Counter()
    n = 3000
    for _ in range(n):
        p = lpool.sample_pool(rng)
        assert len({c["card_id"] for c in p}) == 18
        cnt.update(c["card_id"] for c in p)
    for cid, p in pi.items():
        assert abs(cnt[cid] / n - p) < 0.04, cid
    # the platform's most and least frequent cards stay that way locally
    real = collections.Counter(c for ids in lpool.real_pools().values() for c in ids)
    top = max(pi, key=lambda c: real[c])
    low = min(pi, key=lambda c: real[c])
    assert pi[top] == max(pi.values()) and pi[low] == min(pi.values())
    assert pi["vgc-rillaboom"] > 0.8 > 0.2 > min(pi.values())


# Pools the env drew before 09-28 (aa48e95: 43-card catalog, Match's rng.sample(POOL, 18)), recorded from that tree:
# Random(5), and match seed 700000 (local.ladder --seed 7, game 0). All 1000 ladder s7 / s11 + benchmark s31 match
# seeds were checked equal the same way (work/pools_old.json vs work/pools_legacy.json of the fix round).
OLD_ENV_POOLS = {
    5: ["vgc-tornadus", "vgc-gyarados", "vgc-iron-treads", "vgc-sneasler", "vgc-arcanine", "vgc-porygon2",
        "vgc-grimmsnarl", "vgc-basculegion", "vgc-farigiraf", "vgc-cresselia", "vgc-iron-valiant", "vgc-rillaboom",
        "vgc-lucario", "vgc-wo-chien", "vgc-garchomp", "vgc-hatterene", "vgc-talonflame", "vgc-incineroar"],
    700000: ["vgc-sneasler", "vgc-iron-hands", "vgc-flutter-mane", "vgc-sableye", "vgc-klefki", "vgc-great-tusk",
             "vgc-urshifu-rapid-strike", "vgc-torkoal", "vgc-iron-bundle", "vgc-whimsicott", "vgc-thundurus",
             "vgc-sylveon", "vgc-tornadus", "vgc-cresselia", "vgc-iron-valiant", "vgc-ting-lu", "vgc-kingambit",
             "vgc-milotic"],
}


class _Idle:
    def reset(self):
        pass


def test_legacy_pool_mode_reproduces_the_old_env_pools(monkeypatch):
    monkeypatch.setenv("ARENA_POOL", "legacy")
    assert [c["card_id"] for c in lpool.sample_pool(random.Random(5))] == OLD_ENV_POOLS[5]
    for seed, ids in OLD_ENV_POOLS.items():
        assert [c["card_id"] for c in Match(None, [_Idle(), _Idle()], random.Random(seed)).pool] == ids
    # legacy = the pre-09-28 catalog: everything harvested since is left out
    assert len(lpool.LEGACY_CARD_IDS) == 43 and lpool.LEGACY_CARD_IDS <= set(dex.card_catalog())
    assert set(dex.card_catalog()) - lpool.LEGACY_CARD_IDS == set(NEW_CARDS)
    # a pool without the old catalog (handwritten fallback, hand-made pools) is sampled as it is
    small = [{"card_id": f"h{i}"} for i in range(20)]
    assert lpool.sample_pool(random.Random(3), small) == random.Random(3).sample(small, 18)
    # the default (real) mode draws new cards too; uniform mode samples the whole catalog
    monkeypatch.setenv("ARENA_POOL", "uniform")
    got = [c["card_id"] for c in lpool.sample_pool(random.Random(5))]
    assert got == [c["card_id"] for c in random.Random(5).sample(lpool.POOL, 18)]


def _draft_rec(sid, avail, picks, fmt="gen9vgc2025regi", game_type="pokemon_vgc_doubles_draft"):
    obs = {"phase": "draft", "available_cards": avail, "picks": picks}
    if fmt:
        obs["battle_format"] = fmt
    raw = {"session_id": sid, "observation": obs}
    if game_type:
        raw["game_type"] = game_type
    return json.dumps({"raw": raw})


def test_harvest_records_cards_and_only_complete_pools(tmp_path, monkeypatch):
    cat, pools = tmp_path / "vgc_cards.json", tmp_path / "vgc_pools.json"
    cat.write_text(json.dumps([{"card_id": "old", "species": "Delibird"}]))
    monkeypatch.setattr(lpool, "CATALOG", cat)
    monkeypatch.setattr(lpool, "POOLS", pools)
    logs = tmp_path / "logs" / "a"
    logs.mkdir(parents=True)
    full = [{"card_id": f"k{i}", "species": "Pikachu"} for i in range(18)]
    lines = [_draft_rec("s1", full[2:], [{"card_id": "k0"}, {"card_id": "k1"}]),   # 16 offered + 2 picked = full pool
             _draft_rec("s2", full[:10], [])]                                     # partial: not a pool
    (logs / "x.jsonl").write_text("\n".join(lines) + "\n")
    n = lpool.harvest(str(tmp_path / "logs" / "*" / "*.jsonl"))
    assert n == 1 + 18                          # the old card + every offered card (k0 / k1 were offered in s2)
    got = json.loads(pools.read_text())["pools"]
    assert list(got) == ["s1"] and sorted(got["s1"]) == sorted(c["card_id"] for c in full)


def test_harvest_and_the_recorded_draft_reader_skip_singles_ou_drafts(tmp_path, monkeypatch):
    """The singles OU draft (pokemon_gen9ou_draft) uses the same "draft" phase with `ou-` cards: it must not reach the
    VGC catalog / pools (the local VGC sampler would draw OU sets) nor the recorded-set test (KeyError on cat[cid])."""
    cat, pools = tmp_path / "vgc_cards.json", tmp_path / "vgc_pools.json"
    cat.write_text("[]")
    monkeypatch.setattr(lpool, "CATALOG", cat)
    monkeypatch.setattr(lpool, "POOLS", pools)
    logs = tmp_path / "logs" / "a"
    logs.mkdir(parents=True)
    vgc = [{"card_id": f"vgc-k{i}", "species": "Pikachu"} for i in range(18)]
    ou = [{"card_id": f"ou-k{i}", "species": "Pikachu"} for i in range(18)]
    lines = [_draft_rec("v1", vgc, []),
             _draft_rec("o1", ou, [], fmt="gen9ou", game_type="pokemon_gen9ou_draft"),
             _draft_rec("o2", ou, [], fmt=None, game_type="pokemon_gen9ou_draft"),        # only the game type
             _draft_rec("o3", ou, [], fmt="gen9ou", game_type=None),                      # only the battle format
             _draft_rec("v2", vgc, [], fmt=None, game_type="pokemon_vgc_doubles_draft"),
             _draft_rec("v3", vgc, [], game_type=None)]
    (logs / "x.jsonl").write_text("\n".join(lines) + "\n")
    glob_ = str(tmp_path / "logs" / "*" / "*.jsonl")
    assert [sid for sid, _o in lpool._draft_states(glob_)] == ["v1", "v2", "v3"]
    assert lpool.harvest(glob_) == 18
    assert not any(c["card_id"].startswith("ou-") for c in json.loads(cat.read_text()))
    assert set(json.loads(pools.read_text())["pools"]) == {"v1", "v2", "v3"}
    # the game type decides (the platform's own label for the game); the battle format only when it is missing
    assert lpool.is_vgc_draft({"game_type": lpool.VGC_DRAFT_GAME}, {})
    assert not lpool.is_vgc_draft({"game_type": "pokemon_gen9ou_draft"}, {"battle_format": "gen9vgc2025regi"})
    # every recorded real draft state is a VGC one
    real = list(lpool._draft_states(str(ROOT / "logs" / "*" / "*.jsonl")))
    assert all(o["battle_format"] == "gen9vgc2025regi" for _sid, o in real)


# ---------------------------------------------------------------- local env: item-blocked listing
class _FirstLegal(Player):
    def __init__(self, log):
        self.log = log

    def reset(self):
        pass

    def choose(self, raw):
        self.log.append(raw)
        return raw["legal_actions"]["actions"][0]["action_id"]


def _draft_log(monkeypatch, mode):
    if mode:
        monkeypatch.setenv("ARENA_POOL", mode)
    else:
        monkeypatch.delenv("ARENA_POOL", raising=False)
    pool = [{"card_id": f"p{i}", "species": "Pikachu", "item": f"item {i % 12}"} for i in range(18)]  # 6 items twice
    log = []
    m = Match(None, [_FirstLegal(log), _FirstLegal(log)], random.Random(1), pool=pool)
    m.draft()
    return m, log


def test_local_draft_lists_item_blocked_cards_like_the_platform(monkeypatch):
    m, log = _draft_log(monkeypatch, None)
    assert len(log) == 12 and m.invalid == [0, 0]
    blocked_listed = 0
    for raw in log:
        o = raw["observation"]
        me = raw["current_actor"]["agent_id"]
        taken = {p["card_id"] for p in o["picks"]}
        undrafted = [c["card_id"] for c in m.pool if c["card_id"] not in taken]
        assert o["available_card_ids"] == undrafted == [c["card_id"] for c in o["available_cards"]]
        assert o["unused_card_ids"] == o["available_card_ids"]
        my_items = {next(c["item"] for c in m.pool if c["card_id"] == r["card_id"]) for r in o["rosters"][me]}
        legal = {a["input"]["action"]["card_id"] for a in raw["legal_actions"]["actions"]}
        for c in o["available_cards"]:
            assert (c["card_id"] in legal) == (c["item"] not in my_items)
            blocked_listed += c["item"] in my_items
    assert blocked_listed > 0
    assert all(len(r) == 6 and len({c["item"] for c in r}) == 6 for r in m.rosters)   # Item Clause still holds


def test_a_draft_dead_end_waives_the_item_clause_instead_of_crashing(monkeypatch):
    monkeypatch.delenv("ARENA_POOL", raising=False)
    pool = [{"card_id": f"p{i}", "species": "Pikachu", "item": "Same" if i >= 4 else f"item {i}"} for i in range(18)]
    log = []
    m = Match(None, [_FirstLegal(log), _FirstLegal(log)], random.Random(1), pool=pool)
    m.draft()                                          # used to raise IndexError (rng.choice of no legal card)
    assert all(raw["legal_actions"]["actions"] for raw in log)
    assert all(len(r) == 6 for r in m.rosters) and m.invalid == [0, 0]
    assert sum(m.clause_waived) > 0
    _m, _log = _draft_log(monkeypatch, None)           # no dead end: nothing waived
    assert _m.clause_waived == [0, 0]
    # the count leaves the Match: every MatchResult carries it (ladder / benchmark / panels report it)
    assert m.forfeited(0).clause_waived == m.clause_waived and _m.forfeited(1).clause_waived == [0, 0]


def test_the_panel_reports_draft_dead_ends(capsys):
    from local import panel
    rows = [{"opp": "v1", "seed": 43, "g": 0, "win": True, "waive_cand": 1, "waive_opp": 0},
            {"opp": "v1", "seed": 43, "g": 1, "win": False, "waive_cand": 0, "waive_opp": 2},
            {"opp": "v2", "seed": 43, "g": 0, "win": True, "waive_cand": 0, "waive_opp": 0},
            {"opp": "v2", "seed": 43, "g": 1, "win": True}]                  # rows from an older panel: none
    s = panel.summarize(rows)
    assert s["per_opp"]["v1"]["dead_ends"] == 2 and s["per_opp"]["v2"]["dead_ends"] == 0 and s["dead_ends"] == 2
    panel.print_summary(s)
    assert "draft dead ends    2 games" in capsys.readouterr().out


def test_legacy_env_hides_item_blocked_cards(monkeypatch):
    _m, log = _draft_log(monkeypatch, "legacy")
    for raw in log:
        o = raw["observation"]
        legal = [a["input"]["action"]["card_id"] for a in raw["legal_actions"]["actions"]]
        assert o["available_card_ids"] == legal and "unused_card_ids" not in o


# ---------------------------------------------------------------- asymmetric model in the drafter
def _C(cid, item):
    return {"card_id": cid, "species": "Delibird", "item": item, "ability": "Hustle", "moves": ["Present"],
            "evs": {}, "nature": "Hardy", "level": 50}


def test_main_opp_makes_the_search_deny_what_the_opponent_values():
    mine = [_C(f"m{i}", f"mi{i}") for i in range(5)]
    opp = [_C(f"o{i}", f"oi{i}") for i in range(5)]
    x, y, z = _C("x", "X item"), _C("y", "Y item"), _C("z", "Z item")
    pool = [x, y, z]
    base = {"main": {"x": 0.0, "y": 0.2, "z": 0.0}, "beta": 0.0, "prior": {"a": 0.0, "b": 0.0}}
    # n_taken = 10: pick 11 is ours ('B'), pick 12 the opponent's ('A')
    assert SNAKE[10] != SNAKE[11]
    sym = Drafter(DraftModel(base), use_matchups=False)
    assert sym.pick(pool, mine, opp, {"x", "y", "z"}, n_taken=10) == "y"
    asym = Drafter(DraftModel({**base, "main_opp": {"x": 1.0, "y": 0.2, "z": 0.0}}), use_matchups=False)
    assert asym.pick(pool, mine, opp, {"x", "y", "z"}, n_taken=10) == "x"      # deny: x is worth 1.0 to them
    # main_opp entries for cards the model does not know are ignored (unseen cards stay symmetric)
    m = DraftModel({**base, "main_opp": {"q": 5.0}})
    assert m.main_opp == {}


def test_a_symmetric_model_file_searches_exactly_as_before():
    d = Drafter(DraftModel({"main": {"a": 0.1}, "beta": 0.0, "prior": {"a": 0.0, "b": 0.0}}), use_matchups=False)
    cards = [_C("a", "i1"), _C("b", "i2")]
    m, _P, _S = d._tables(cards)
    assert d.m_opp == m


# ---------------------------------------------------------------- fitter
def test_population_rows_are_oriented_ours_first(tmp_path):
    fit = _load_script("fit_draft_model")
    rows = [{"family": "v1", "ours": 1, "winner": 1, "rosters": [["a"] * 6, ["b"] * 6]},
            {"family": "v1", "ours": 1, "winner": 0, "rosters": [["a"] * 6, ["b"] * 6]},
            {"winner": 0, "rosters": [["a"] * 6, ["b"] * 6]},
            {"winner": None, "rosters": [["a"] * 6, ["b"] * 6]},
            {"family": "v1", "ours": 0, "winner": 1, "rosters": [["a"] * 5, ["b"] * 6]}]
    p = tmp_path / "r.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    got = fit.load_rows(str(p))
    assert [(r["ros"][0][0], r["y"], r["fam"]) for r in got] == [("b", 1.0, "v1"), ("b", 0.0, "v1"),
                                                                 ("a", 1.0, "selfplay")]


def test_family_bias_and_foreign_pilot_offset_features():
    fit = _load_script("fit_draft_model")
    cards = ["a", "b", "c", "d"]
    F = fit.Fitter(cards, None, False, False, False, families=["selfplay", "v1"], use_asym=True)
    row = {"ros": [["a", "b"], ["c", "d"]], "fam": "v1"}
    f = dict(F.features(row))
    assert f[F.off_fam + 1] == 1.0
    assert f[F.off_opp + 2] == -1.0 and f[F.off_opp + 3] == -1.0 and F.off_opp + 0 not in f
    self_row = {"ros": [["a", "b"], ["c", "d"]], "fam": "selfplay"}
    assert not any(i >= F.off_opp for i, _ in F.features(self_row))      # our own pilot on both sides: no d
    # vector() reads main_opp back as m + d
    w = F.vector({"main": {"c": 0.2}, "main_opp": {"c": 0.5}})
    assert w[F.off_main + 2] == 0.2 and abs(w[F.off_opp + 2] - 0.3) < 1e-12


def test_crossfit_intercept_recovers_a_shifted_family():
    fit = _load_script("fit_draft_model")
    rng = random.Random(0)
    zs = [rng.uniform(-1, 1) for _ in range(4000)]
    import math
    ys = [1.0 if rng.random() < 1 / (1 + math.exp(-(z + 1.5))) else 0.0 for z in zs]
    t = fit.fit_offset(zs, ys)
    assert abs(t - 1.5) < 0.15
    ll_shift, _ = fit.crossfit_logloss(zs, ys)
    ll_raw = sum(fit._ll(z, y) for z, y in zip(zs, ys)) / len(zs)
    assert ll_shift < ll_raw


def test_heldout_groups_never_train_on_the_held_out_pilot_and_score_each_family(capsys):
    fit = _load_script("fit_draft_model")
    rng = random.Random(4)
    cards = list("abcdefghijklmnopqrstuvwx")
    rows = []
    for k in range(2000):
        fam = ["f1", "f2", "f3", "selfplay"][k % 4]
        pick = rng.sample(cards, 12)
        A, B = pick[:6], pick[6:]
        z = 1.2 * (("a" in A) - ("a" in B)) + (0.4 if fam == "f2" else 0.0)
        y = 1.0 if rng.random() < 1 / (1 + 2.718281828 ** -z) else 0.0
        rows.append({"ros": [A, B], "y": y, "fam": fam})
    seen_train = []
    real_fit = fit.Fitter.fit

    def spy(self, X, Y, lam, **kw):
        seen_train.append(len(X))
        return real_fit(self, X, Y, lam, **kw)
    fit.Fitter.fit = spy
    try:
        out = fit.heldout_family(rows, cards, None, [("m", "all", ("main", (1.0, 0.0, 0.0)))], {}, epochs=4,
                                 groups=[["f1", "f2"]], test_without={"x"})
    finally:
        fit.Fitter.fit = real_fit
    assert seen_train == [sum(r["fam"] not in ("f1", "f2") for r in rows)]          # f1 / f2 rows never trained on
    n1 = sum(r["fam"] == "f1" and "x" not in r["ros"][0] + r["ros"][1] for r in rows)
    assert set(out["m"]) == {"f1", "f2"} and len(out["m"]["f1"]) == n1
    for F in ("f1", "f2"):                                                           # the card signal transfers
        assert sum(out["m"][F]) / len(out["m"][F]) < 0.66
    assert "pooled foreign pilots" in capsys.readouterr().out


def test_the_fitter_writes_only_cards_it_has_games_for(tmp_path, monkeypatch, capsys):
    """A catalog card that no training game contains must not come out as a fitted card (main 0.0 + a fingerprint):
    the drafter would score it as a known average card, and the NOT_IN_DRAFT_MODEL guard would pass unfitted."""
    fit = _load_script("fit_draft_model")
    cat = dex.card_catalog()
    used = sorted(lpool.LEGACY_CARD_IDS)[:14]
    rng = random.Random(2)
    rows = []
    for k in range(80):
        pick = rng.sample(used, 12)
        rows.append({"winner": k % 2, "rosters": [pick[:6], pick[6:]]})
    data = tmp_path / "rows.jsonl"
    data.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    out = tmp_path / "m.json"
    monkeypatch.setattr(fit, "beats_table", lambda cards: {(a, b): 0.5 for a in cards for b in cards})   # no calc
    monkeypatch.setattr(fit.sys, "argv", ["fit_draft_model.py", "--data", str(data), "--out", str(out),
                                          "--config", "main_beta", "--lam", "1,0,0", "--epochs", "2"])
    fit.main()
    m = json.loads(out.read_text())
    assert set(m["main"]) == set(m["fingerprints"]) == set(m["species"]) == set(used)
    assert "vgc-toxapex" in cat and "vgc-toxapex" not in m["main"]
    assert "vgc-toxapex" in capsys.readouterr().out            # the fitter names the cards it left out
    assert fit.model_cards(fit.load_rows(str(data))) == used
    # an unfitted card then stays "unseen" for the drafter (prior strength, not a known 0.0)
    assert DraftModel(m).key(cat["vgc-toxapex"]) is None and DraftModel(m).key(cat[used[0]]) == used[0]


# ---------------------------------------------------------------- population generator helpers
def test_bst_drafter_takes_the_highest_base_stat_total():
    pop = _load_script("draft_population")
    cards = [{"card_id": "a", "species": "Sableye"}, {"card_id": "b", "species": "Garchomp"},
             {"card_id": "c", "species": "Amoonguss"}]
    assert pop.bst_pick(cards, random.Random(0))["card_id"] == "b"
    assert set(pop.FAMILIES) >= {"v3", "cs3", "v2", "v1", "v0", "bst", "rand"}


def test_draft_panel_resumes_and_restarts_a_dead_bridge(tmp_path, monkeypatch, capsys):
    panel = _load_script("draft_panel")
    import local.pokemon_env as env
    import local.showdown as sdmod

    class FakeSD:
        made = 0

        def __init__(self):
            FakeSD.made += 1

        def close(self):
            pass

    calls = []

    def fake_play(sd, a, b, seed):
        calls.append(seed)
        if len(calls) == 2:                          # the second game kills the bridge once
            raise RuntimeError("bridge died")
        return env.MatchResult(winner=0 if seed % 2 == 0 else 1, turns=3, rosters=[["x"], ["y"]])   # ours wins
    monkeypatch.setattr(sdmod, "Showdown", FakeSD)
    monkeypatch.setattr(env, "play_match", fake_play)
    monkeypatch.setattr(panel, "make_opponent", lambda name, rng: object())
    import agent.agent
    monkeypatch.setattr(agent.agent, "create_agent", lambda: None)
    out = tmp_path / "p.jsonl"
    out.write_text(json.dumps({"opp": "v1", "seed": 9 * 100000 + 0, "won": True}) + "\n")   # game 0 already played
    args = type("A", (), {"model": "", "seed": 9, "opps": "v1", "games": 3, "out": str(out)})()
    panel.run(args)
    got = [json.loads(line) for line in out.read_text().splitlines()]
    assert [r["seed"] for r in got] == [900000, 900001, 900002]
    assert calls == [900001, 900002, 900002] and FakeSD.made == 2            # game 2 retried on a fresh bridge
    assert "v1                 3/3" in capsys.readouterr().out


def test_draft_panel_reseeds_the_opponent_per_game_so_runs_stay_paired(tmp_path, monkeypatch):
    """Two runs (e.g. new vs old model) whose picks differ make the random drafter draw a different number of times;
    game g must still start from the same opponent stream in both, and in a resumed run."""
    panel = _load_script("draft_panel")
    import local.pokemon_env as env
    import local.showdown as sdmod

    class FakeSD:
        def close(self):
            pass

    class Opp:
        rng = random.Random(0)

    monkeypatch.setattr(sdmod, "Showdown", FakeSD)
    monkeypatch.setattr(panel, "make_opponent", lambda name, rng: Opp())
    import agent.agent
    monkeypatch.setattr(agent.agent, "create_agent", lambda: None)

    def run_once(name, burn, done=()):
        starts = {}

        def fake_play(sd, a, b, seed):
            o = a if isinstance(a, Opp) else b
            starts[seed] = o.rng.random()
            for _ in range(burn(seed)):              # this run's picks: the opponent draws this many more times
                o.rng.random()
            return env.MatchResult(winner=0, turns=3, rosters=[["x"], ["y"]])
        monkeypatch.setattr(env, "play_match", fake_play)
        out = tmp_path / f"{name}.jsonl"
        out.write_text("".join(json.dumps({"opp": "rand", "seed": s, "won": True}) + "\n" for s in done))
        panel.run(type("A", (), {"model": "", "seed": 57, "opps": "rand", "games": 4, "out": str(out)})())
        return starts

    new = run_once("new", lambda s: s % 3)
    old = run_once("old", lambda s: 7)
    resumed = run_once("resumed", lambda s: 1, done=[5700000])
    assert new == old == {s: panel.opponent_rng(s).random() for s in range(5700000, 5700004)}
    assert resumed == {s: v for s, v in new.items() if s != 5700000}


class _FakeShowdown:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


def _dead_end_result(seed):
    """Game 1 of every seed block has a draft dead end (seat 0 got the clause waived); the others none."""
    from local.pokemon_env import MatchResult
    return MatchResult(winner=0, turns=5, rosters=[["x"], ["y"]],
                       clause_waived=[1, 0] if seed % 100000 == 1 else [0, 0])


def test_ladder_and_benchmark_report_draft_dead_ends(monkeypatch, capsys):
    """The dead-end waiver count reaches the gate outputs (it used to stay inside Match)."""
    import sys
    from types import SimpleNamespace
    from local import benchmark, ladder
    for mod in (ladder, benchmark):
        monkeypatch.setattr(mod, "Showdown", _FakeShowdown)
        monkeypatch.setattr(mod, "play_match", lambda sd, a, b, seed: _dead_end_result(seed))
        monkeypatch.setattr(mod, "ContestantPlayer", lambda factory, name: name)
        monkeypatch.setattr(mod, "_which_calc", lambda: None)
    monkeypatch.setattr(sys, "argv", ["ladder", "--games", "4", "--seed", "7", "--json"])
    with pytest.raises(SystemExit):
        ladder.main()
    out = capsys.readouterr().out
    assert "draft dead ends (Item Clause waived) 1 games" in out
    assert json.loads(out.strip().splitlines()[-1])["draft_dead_end_games"] == 1
    monkeypatch.setattr(benchmark, "opponents", lambda: [SimpleNamespace(name="o1"), SimpleNamespace(name="o2")])
    monkeypatch.setattr(sys, "argv", ["benchmark", "--games", "3"])
    benchmark.main()
    out = capsys.readouterr().out
    assert out.count("draft dead ends (Item Clause waived) 1 games") == 2          # on each opponent's row
    assert "draft dead ends (Item Clause waived): 2 of 6 games" in out             # and always in the total


def test_draft_panel_reports_draft_dead_ends(tmp_path, monkeypatch, capsys):
    panel = _load_script("draft_panel")
    import local.pokemon_env as env
    import local.showdown as sdmod
    monkeypatch.setattr(sdmod, "Showdown", _FakeShowdown)
    monkeypatch.setattr(env, "play_match", lambda sd, a, b, seed: _dead_end_result(seed))
    monkeypatch.setattr(panel, "make_opponent", lambda name, rng: object())
    import agent.agent
    monkeypatch.setattr(agent.agent, "create_agent", lambda: None)
    new = tmp_path / "new.jsonl"
    new.write_text(json.dumps({"opp": "v1", "seed": 900003, "won": True, "invalid": [0, 0], "waived": [0, 2]}) + "\n")
    panel.run(type("A", (), {"model": "", "seed": 9, "opps": "v1", "games": 4, "out": str(new)})())
    assert "draft dead ends 2" in capsys.readouterr().out        # game 1 played here + game 3 resumed from the file
    rows = {r["seed"]: r for r in map(json.loads, new.read_text().splitlines())}
    assert rows[900001]["waived"] == [0, 1] and rows[900000]["waived"] == [0, 0]     # seat 1 = ours: [ours, theirs]
    old = tmp_path / "old.jsonl"
    old.write_text("".join(json.dumps({"opp": "v1", "seed": s, "won": True, "invalid": [0, 0]}) + "\n"
                           for s in rows))
    panel.compare(str(new), str(old))
    assert "draft dead ends (Item Clause waived): new 2, old 0 of 4 paired games" in capsys.readouterr().out


def test_shipped_model_knows_every_catalog_card_but_the_documented_new_ones():
    d = json.loads((ROOT / "agent" / "pokemon" / "draft_model.json").read_text())
    cat = dex.card_catalog()
    assert lpool.NOT_IN_DRAFT_MODEL <= set(cat)
    assert set(d["fingerprints"]) == set(cat) - lpool.NOT_IN_DRAFT_MODEL
    assert all(d["fingerprints"][c] == card_fingerprint(cat[c]) for c in d["fingerprints"])
    # the unfitted cards get the unseen-card prior, never an error
    m = DraftModel(d)
    for cid in lpool.NOT_IN_DRAFT_MODEL:
        assert m.key(cat[cid]) is None
