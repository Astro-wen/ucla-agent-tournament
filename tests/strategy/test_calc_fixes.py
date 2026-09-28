"""Damage-calculator correctness: Focus Sash / Sturdy, multi-hit rolls, Supreme Overlord, champion calc isolation.

Calc-backed tests skip without agent/js/node_modules; the Python-side plumbing is tested always."""
import shutil
from collections import Counter

import pytest

import agent.pokemon.calc as calc_mod
from agent.pokemon import dex
from agent.pokemon.calc import DamageCalc, _js_dir, calc_pokemon
from agent.pokemon.policy import PokemonPolicy, _hit_stats, _log_fallen, _pair_points, _cur_hp

needs_calc = pytest.mark.skipif(_js_dir() is None, reason="agent/js has no node_modules")

# Real platform cards (data/vgc_cards.json).
GARCHOMP = {"species": "Garchomp", "item": "Focus Sash", "ability": "Rough Skin", "nature": "Jolly", "level": 50,
            "evs": {"hp": 4, "atk": 252, "spe": 252}, "ivs": {}, "moves": ["Earthquake", "Rock Slide", "Iron Head", "Protect"]}
CHIENPAO = {"species": "Chien-Pao", "item": "Focus Sash", "ability": "Sword of Ruin", "nature": "Jolly", "level": 50,
            "evs": {"hp": 4, "atk": 252, "spe": 252}, "ivs": {},
            "moves": ["Icicle Crash", "Sacred Sword", "Sucker Punch", "Protect"]}
BAXCALIBUR = {"species": "Baxcalibur", "item": "Loaded Dice", "ability": "Thermal Exchange", "nature": "Jolly",
              "level": 50, "evs": {"hp": 4, "atk": 252, "spe": 252}, "ivs": {},
              "moves": ["Icicle Spear", "Glaive Rush", "Ice Shard", "Protect"]}
KINGAMBIT = {"species": "Kingambit", "item": "Black Glasses", "ability": "Supreme Overlord", "nature": "Adamant",
             "level": 50, "evs": {"hp": 252, "atk": 252, "spe": 4}, "ivs": {},
             "moves": ["Sucker Punch", "Iron Head", "Kowtow Cleave", "Protect"]}
INC = {"species": "Incineroar", "item": "Safety Goggles", "ability": "Intimidate", "nature": "Careful", "level": 50,
       "evs": {"hp": 244, "def": 12, "spd": 252}, "ivs": {}, "moves": ["Fake Out", "Knock Off", "Parting Shot", "Flare Blitz"]}


def inp(card, **kw):
    """Calculator input straight from a card (hpFraction etc. via kw)."""
    return {**calc_pokemon({}, card), **kw}


def one(attacker, defender, move, **kw):
    r = DamageCalc.get().batch([{"attacker": attacker, "defender": defender, "move": move,
                                 "field": {"gameType": "Singles"}, **kw}])
    assert r is not None and r[0] is not None, r
    return r[0]


def sid(card):
    return dex.to_id(card["species"])


def view(card, hp=1.0, **kw):
    """A platform battle view (foe HP out of 100)."""
    return {"species": sid(card), "name": card["species"], "current_hp_fraction": hp, "current_hp": round(100 * hp),
            "max_hp": 100, "status": None, "fainted": False, "boosts": {}, **kw}


def policy(cards, opp_cards):
    pol = PokemonPolicy()
    for c in cards:
        pol.cards[sid(c)] = c
    for c in opp_cards:
        pol.opp_cards[sid(c)] = c
    return pol


def obs_of(actives, opps, **kw):
    return {"is_doubles": True, "turn": 3, "active_pokemon": actives, "opponent_active_pokemon": opps,
            "weather": {}, "fields": {}, "side_conditions": {}, "opponent_side_conditions": {}, **kw}


# ---------------------------------------------------------------- (1) Focus Sash / Sturdy
@needs_calc
def test_focus_sash_at_full_hp_caps_a_single_hit_at_max_hp_minus_one():
    r = one(inp(CHIENPAO), inp(GARCHOMP, hpFraction=1.0), "Icicle Crash")     # 4x Ice: a sure OHKO without the Sash
    hp = r["defender_hp"]
    assert r["survives"] == "Focus Sash"
    assert max(r["rolls"]) == hp - 1 and r["max"] == hp - 1 and r["min"] <= hp - 1
    assert _hit_stats(r["rolls"], hp)[1] == 0.0                                 # P(KO) = 0
    chipped = one(inp(CHIENPAO), inp(GARCHOMP, hpFraction=0.99), "Icicle Crash")  # 99/100: the Sash is off
    assert "survives" not in chipped and _hit_stats(chipped["rolls"], round(0.99 * hp))[1] == 1.0
    no_item = {k: v for k, v in inp(GARCHOMP, hpFraction=1.0).items() if k != "item"}
    assert "survives" not in one(inp(CHIENPAO), no_item, "Icicle Crash")


@needs_calc
def test_sturdy_caps_unless_mold_breaker_and_multi_hit_breaks_both():
    aggron = {"species": "Aggron", "ability": "Sturdy", "level": 50, "hpFraction": 1.0}
    drill = {"species": "Excadrill", "ability": "Sand Rush", "level": 50, "evs": {"atk": 252}, "nature": "Adamant"}
    r = one(drill, aggron, "Earthquake")                                       # 4x Ground on Steel/Rock
    assert r["survives"] == "Sturdy" and max(r["rolls"]) == r["defender_hp"] - 1
    r = one({**drill, "ability": "Mold Breaker"}, aggron, "Earthquake")
    assert "survives" not in r and min(r["rolls"]) >= r["defender_hp"]
    # A multi-hit move breaks the Sash on its first hit: never capped, still a KO.
    r = one(inp(BAXCALIBUR), inp(GARCHOMP, hpFraction=1.0), "Icicle Spear")
    assert "survives" not in r and _hit_stats(r["rolls"], r["defender_hp"])[1] == 1.0


@needs_calc
def test_policy_sees_zero_ko_chance_on_full_hp_sash_foes_and_our_own_sash_holders():
    # Both sides field a Chien-Pao (Icicle Crash) and a Sash Garchomp: 4x Ice is an OHKO without the Sash.
    pol = policy(cards=(CHIENPAO, GARCHOMP), opp_cards=(GARCHOMP, CHIENPAO))
    tmpl = {"slots": [{"slot": 0, "options": [{"type": "move", "move_id": "iciclecrash", "targets": [-2, 1, 2]}]}]}
    actives, opps = [view(CHIENPAO), view(GARCHOMP)], [view(GARCHOMP), view(CHIENPAO)]
    table = pol._exact_table(obs_of(actives, opps), tmpl, actives, opps)
    r = table[("me", 0, "iciclecrash", "opp", 0)]
    assert r["survives"] == "Focus Sash"
    assert _hit_stats(r["rolls"], _cur_hp(opps[0], r))[1] == 0.0                 # foe at 100/100 with its Sash
    both = _pair_points((r["rolls"], 1.0), (r["rolls"], 1.0), _cur_hp(opps[0], r))
    assert both == pytest.approx(150.0)                                          # two hits break it: a sure KO
    # Our own Sash holder at full HP (the foe-move calcs behind the danger weights): no KO either.
    ours = table[("opp", 1, "iciclecrash", "me", 1)]
    assert ours["survives"] == "Focus Sash" and max(ours["rolls"]) == ours["defender_hp"] - 1
    assert _hit_stats(ours["rolls"], _cur_hp(actives[1], ours))[1] == 0.0
    # Chipped foe (99/100): the KO chance is back.
    opps = [view(GARCHOMP, hp=0.99), view(CHIENPAO)]
    table = pol._exact_table(obs_of(actives, opps), tmpl, actives, opps)
    r = table[("me", 0, "iciclecrash", "opp", 0)]
    assert "survives" not in r and _hit_stats(r["rolls"], _cur_hp(opps[0], r))[1] == 1.0


def test_consumed_sash_is_not_resurrected_from_the_card():
    assert "item" not in calc_pokemon({"species": "garchomp", "item": None}, GARCHOMP)      # used (platform: None)
    assert "item" not in calc_pokemon({"species": "garchomp", "item": ""}, GARCHOMP)
    assert calc_pokemon({"species": "garchomp", "item": "unknown_item"}, GARCHOMP)["item"] == "Focus Sash"
    assert calc_pokemon({"species": "garchomp"}, GARCHOMP)["item"] == "Focus Sash"          # local sim: no item key


DRAGONITE = {"species": "Dragonite", "item": "Loaded Dice", "ability": "Multiscale", "nature": "Adamant", "level": 50,
             "evs": {"hp": 252, "atk": 252, "spe": 4}, "ivs": {}, "moves": ["Extreme Speed", "Scale Shot", "Tailwind", "Protect"]}
FLUTTER = {"species": "Flutter Mane", "item": "Booster Energy", "ability": "Protosynthesis", "nature": "Timid",
           "level": 50, "evs": {"spa": 252, "spd": 4, "spe": 252}, "ivs": {},
           "moves": ["Moonblast", "Shadow Ball", "Icy Wind", "Protect"]}
URSHIFU = {"species": "Urshifu-Rapid-Strike", "item": "Mystic Water", "ability": "Unseen Fist", "nature": "Adamant",
           "level": 50, "evs": {"hp": 4, "atk": 252, "spe": 252}, "ivs": {},
           "moves": ["Surging Strikes", "Close Combat", "Aqua Jet", "Protect"]}


def test_a_lost_item_is_not_resurrected_from_the_card_except_a_spent_booster_energy():
    # Review defect 2: a knocked-off Loaded Dice came back from the card, so the move kept its 4-5 hit odds.
    assert "item" not in calc_pokemon({"species": "dragonite", "item": None}, DRAGONITE)      # foe: None after -enditem
    assert "item" not in calc_pokemon({"species": "dragonite", "item": ""}, DRAGONITE)        # ours: ""
    assert "item" not in calc_pokemon({"species": "incineroar", "item": None}, INC)           # any item, not just a Sash
    assert calc_pokemon({"species": "dragonite", "item": "unknown_item"}, DRAGONITE)["item"] == "Loaded Dice"
    assert calc_pokemon({"species": "dragonite"}, DRAGONITE)["item"] == "Loaded Dice"
    # A spent Booster Energy keeps its boost this stint: the calculator needs the item to apply it.
    assert calc_pokemon({"species": "fluttermane", "item": None}, FLUTTER)["item"] == "Booster Energy"


@needs_calc
def test_knocked_off_loaded_dice_falls_back_to_the_2_to_5_hit_odds():
    target = inp(GARCHOMP, hpFraction=0.5)
    dice = one(inp(DRAGONITE), target, "Scale Shot")
    lost = one(calc_pokemon({"species": "dragonite", "item": None}, DRAGONITE), target, "Scale Shot")
    assert dice["hits"] == "4/5" and lost["hits"] == "2/3/4/5"
    assert sum(lost["rolls"]) < 0.8 * sum(dice["rolls"])                          # ~3.1 hits on average, not 4.5


# ---------------------------------------------------------------- (3) multi-hit rolls
def _exact_total(per_hit: list, odds: dict) -> Counter:
    """Exact distribution of a multi-hit total, every hit rolling one of `per_hit` independently: {total: prob}."""
    out = Counter()
    for h, w in odds.items():
        dist = Counter({0: 1.0})
        for _ in range(h):
            nxt = Counter()
            for v, p in dist.items():
                for x in per_hit:
                    nxt[v + x] += p / len(per_hit)
            dist = nxt
        for v, p in dist.items():
            out[v] += w * p
    return out


def _p_ko(dist: Counter, hp: int) -> float:
    return sum(p for v, p in dist.items() if v >= hp)


@needs_calc
def test_multi_hit_rolls_are_totals_over_hits_not_sums_of_one_hits_rolls():
    bax = {**inp(BAXCALIBUR), "item": "Leftovers"}
    r1 = one(bax, inp(INC), "Icicle Spear", hits=1)
    r3 = one(bax, inp(INC), "Icicle Spear", hits=3)
    assert (r3["min"], r3["max"]) == (3 * min(r1["rolls"]), 3 * max(r1["rolls"]))
    assert r3["min"] <= min(r3["rolls"]) and max(r3["rolls"]) <= r3["max"]     # main: rolls were ~5x the range
    assert sum(r3["rolls"]) / len(r3["rolls"]) == pytest.approx(3 * sum(r1["rolls"]) / 16, rel=0.01)
    s = one(inp(URSHIFU), inp(INC), "Surging Strikes")
    assert s["hits"] == 3 and s["min"] <= min(s["rolls"]) and max(s["rolls"]) <= s["max"]


@needs_calc
def test_multi_hit_ko_odds_roll_every_hit_independently():
    # Review defect 1: roll i summed over the hits (all low or all high together) got the range right but the KO odds
    # near its edges wrong. Against an exact convolution of the single-hit rolls, P(KO) must be within 1/32 (quantiles).
    bax = {**inp(BAXCALIBUR), "item": "Leftovers"}
    per = one(bax, inp(INC), "Icicle Spear", hits=1)["rolls"]
    for hits in (2, 3, 5):
        r = one(bax, inp(INC), "Icicle Spear", hits=hits)
        exact = _exact_total(per, {hits: 1.0})
        for hp in range(r["min"], r["max"] + 2):
            assert _hit_stats(r["rolls"], hp)[1] == pytest.approx(_p_ko(exact, hp), abs=1 / 32 + 1e-9), (hits, hp)
    # The reviewer's case: 3 crit hits of Surging Strikes into Kingambit at 61/100 — 0.18, not 0.44.
    s = one(inp(URSHIFU), inp(KINGAMBIT, hpFraction=0.61), "Surging Strikes")
    assert _hit_stats(s["rolls"], _cur_hp({"current_hp_fraction": 0.61}, s))[1] == pytest.approx(0.18, abs=0.05)


@needs_calc
def test_variable_hit_moves_use_showdown_hit_odds_loaded_dice_and_skill_link():
    bax = {**inp(BAXCALIBUR), "item": "Leftovers"}
    target = inp(GARCHOMP, hpFraction=1.0)                    # 4x Ice: big per-hit damage, hit counts don't overlap
    per = {h: one(bax, target, "Icicle Spear", hits=h) for h in (1, 2, 3, 4, 5)}
    r = one(bax, target, "Icicle Spear")                                        # 2-5 hits: 35/35/15/15 %
    assert r["hits"] == "2/3/4/5" and len(r["rolls"]) == 32 and "survives" not in r
    assert r["min"] == per[2]["min"] and r["max"] == per[5]["max"]
    assert sum(x >= per[4]["min"] for x in r["rolls"]) / 32 == pytest.approx(0.30, abs=0.04)
    assert sum(x <= per[2]["max"] for x in r["rolls"]) / 32 == pytest.approx(0.35, abs=0.04)
    dice = one(inp(BAXCALIBUR), target, "Icicle Spear")                         # Loaded Dice: 4 or 5 hits, 50/50
    assert dice["hits"] == "4/5" and (dice["min"], dice["max"]) == (per[4]["min"], per[5]["max"])
    assert sum(x <= per[4]["max"] for x in dice["rolls"]) == 16
    exact = _exact_total(per[1]["rolls"], {4: 0.5, 5: 0.5})
    for hp in range(dice["min"], dice["max"] + 2, 3):
        assert _hit_stats(dice["rolls"], hp)[1] == pytest.approx(_p_ko(exact, hp), abs=1 / 32 + 1e-9), hp
    link = one({**bax, "ability": "Skill Link"}, target, "Icicle Spear")
    assert link["rolls"] == per[5]["rolls"]


# ---------------------------------------------------------------- (2) Supreme Overlord
def test_calc_pokemon_passes_allies_fainted_only_to_supreme_overlord():
    assert calc_pokemon({"species": "kingambit"}, KINGAMBIT, allies_fainted=2)["alliesFainted"] == 2
    assert calc_pokemon({"species": "kingambit"}, KINGAMBIT, allies_fainted=9)["alliesFainted"] == 5
    assert "alliesFainted" not in calc_pokemon({"species": "kingambit"}, KINGAMBIT, allies_fainted=0)
    assert "alliesFainted" not in calc_pokemon({"species": "incineroar"}, INC, allies_fainted=2)


@needs_calc
def test_supreme_overlord_boosts_damage_in_the_calculator():
    base = one(inp(KINGAMBIT), inp(INC), "Kowtow Cleave")
    two = one(inp(KINGAMBIT, alliesFainted=2), inp(INC), "Kowtow Cleave")
    assert two["max"] == pytest.approx(base["max"] * 4915 / 4096, rel=0.05) and two["max"] > base["max"]
    assert one(inp(INC, alliesFainted=2), inp(KINGAMBIT), "Flare Blitz")["max"] == \
        one(inp(INC), inp(KINGAMBIT), "Flare Blitz")["max"]                        # no ability: ignored


LOG = ["|init|battle", "|switch|p1a: Incineroar|Incineroar, L50, M|201/201",
       "|switch|p2a: Rillaboom|Rillaboom, L50, M|100/100", "|switch|p2b: Tornadus|Tornadus, L50, M|100/100",
       "|faint|p2a: Rillaboom", "|faint|p2b: Tornadus",
       "|switch|p2a: Kingambit|Kingambit, L50, F|100/100", "|-activate|p2a: Kingambit|ability: Supreme Overlord",
       "|-start|p2a: Kingambit|fallen2|[silent]", "|turn|4"]


def test_protocol_log_gives_the_count_locked_in_on_entry():
    obs = {"protocol_log": LOG, "team": {"p1: Incineroar": {}}}
    got = _log_fallen(obs)
    assert got[("opp", "kingambit")] == 2 and got[("me", "incineroar")] == 0
    later = LOG + ["|faint|p1a: Incineroar", "|switch|p2a: Farigiraf|Farigiraf, L50|100/100",
                   "|switch|p2a: Kingambit|Kingambit, L50, F|100/100"]                  # re-entry, no -start line
    assert _log_fallen({**obs, "protocol_log": later})[("opp", "kingambit")] == 0
    assert _log_fallen({"protocol_log": LOG, "team": {}}) is None                          # side unknown
    assert _log_fallen({"team": {"p1: X": {}}}) is None                                    # no log (local sim)


def _capture(monkeypatch):
    dc = DamageCalc.get()
    seen = []
    monkeypatch.setattr(dc, "enabled", True)
    monkeypatch.setattr(dc, "batch", lambda calcs: seen.extend(calcs) or None)
    return seen


def test_supreme_overlord_count_reaches_the_calculator(monkeypatch):
    seen = _capture(monkeypatch)
    pol = policy(cards=(INC, CHIENPAO), opp_cards=(KINGAMBIT, GARCHOMP))
    tmpl = {"slots": [{"slot": 0, "options": [{"type": "move", "move_id": "flareblitz", "targets": [-2, 1, 2]}]}]}
    actives, opps = [view(INC), view(CHIENPAO)], [view(KINGAMBIT), view(GARCHOMP)]
    obs = obs_of(actives, opps, protocol_log=LOG, team={"p1: Incineroar": {}})
    assert pol._exact_table(obs, tmpl, actives, opps) is None                    # batch -> None: heuristic fallback
    gambit = [c["attacker"] for c in seen if c["attacker"]["species"] == "Kingambit"]
    assert gambit and all(a.get("alliesFainted") == 2 for a in gambit)
    assert all("alliesFainted" not in c["attacker"] for c in seen if c["attacker"]["species"] != "Kingambit")


def test_without_a_log_the_count_is_taken_when_it_enters_and_kept(monkeypatch):
    seen = _capture(monkeypatch)
    pol = policy(cards=(INC, CHIENPAO), opp_cards=(KINGAMBIT, GARCHOMP))
    tmpl = {"slots": []}
    actives = [view(INC), view(CHIENPAO)]

    def count(opps, fainted):
        seen.clear()
        team = {f"opp: X{k}": {"fainted": k < fainted} for k in range(4)}
        pol._exact_table(obs_of(actives, opps, opponent_team=team), tmpl, actives, opps)
        return {c["attacker"].get("alliesFainted") for c in seen if c["attacker"]["species"] == "Kingambit"}

    assert count([view(GARCHOMP), None], 0) == set()
    assert count([view(GARCHOMP), view(KINGAMBIT)], 1) == {1}        # came in after one faint
    assert count([view(GARCHOMP), view(KINGAMBIT)], 2) == {1}        # a later faint doesn't raise it
    assert count([view(GARCHOMP), None], 2) == set()                  # switched out
    assert count([view(GARCHOMP), view(KINGAMBIT)], 2) == {2}        # re-entry re-reads it
    pol._preview({"your_roster": [], "opponent_roster": []})          # next battle: forgotten
    assert pol._entry_fallen == {}


def test_fainted_tracking_never_raises_on_odd_payloads(monkeypatch):
    _capture(monkeypatch)
    pol = policy(cards=(INC,), opp_cards=(KINGAMBIT,))
    weird = {"protocol_log": ["garbage", None, "|switch|", "|-start|p2a: Kingambit|fallenX"], "team": ["not", "a dict"],
             "opponent_team": {"a": None, "b": "x"}}
    assert isinstance(pol._fallen_counts(weird, [view(INC), "junk"], [None, view(KINGAMBIT)]), dict)
    weird["team"] = {"p1: Incineroar": {}}
    assert isinstance(pol._fallen_counts(weird, [view(INC)], [None, view(KINGAMBIT)]), dict)
    assert pol._fallen_counts({"opponent_team": 5}, 7, None) == {}                   # swallowed, logged


# ---------------------------------------------------------------- the ladder's champion keeps its own calc.js
def test_champion_copy_runs_its_own_calc_js_with_borrowed_node_modules(tmp_path, monkeypatch):
    champ_js = tmp_path / "champion" / "agent" / "js"
    champ_js.mkdir(parents=True)
    (champ_js / "calc.js").write_text("// old calculator")
    repo_js = tmp_path / "agent" / "js"
    (repo_js / "node_modules").mkdir(parents=True)
    (repo_js / "calc.js").write_text("// candidate calculator")
    monkeypatch.setattr(calc_mod, "__file__", str(tmp_path / "champion" / "agent" / "pokemon" / "calc.py"))
    monkeypatch.setattr(calc_mod.dex, "repo_root", lambda: tmp_path)
    assert calc_mod._js_paths() == (champ_js, repo_js / "node_modules")        # NOT the candidate's calc.js
    shutil.rmtree(repo_js / "node_modules")
    assert calc_mod._js_paths() is None and calc_mod._js_dir() is None          # no node_modules anywhere: disabled


def test_benchmark_warns_when_an_archived_version_runs_the_candidates_calc_js(tmp_path, monkeypatch, capsys):
    import types

    import local.benchmark as bench
    cand_js = tmp_path / "agent" / "js"
    cand_js.mkdir(parents=True)
    (cand_js / "calc.js").write_text("// candidate calculator")
    old_js = tmp_path / "versions" / "v1_exact_damage" / "agent" / "js"
    old_js.mkdir(parents=True)
    (old_js / "calc.js").write_text("// old calculator")
    monkeypatch.setattr(bench, "ROOT", tmp_path)
    monkeypatch.setattr(calc_mod, "_js_dir", lambda: cand_js)
    archived = types.SimpleNamespace(_js_dir=lambda: cand_js)       # old calc.py, no node_modules: repo fallback
    real_import = bench.importlib.import_module
    monkeypatch.setattr(bench.importlib, "import_module",
                        lambda name, *a: archived if name.startswith("versions.") else real_import(name, *a))
    bench._which_calc()
    assert "WARNING: v1_exact_damage runs the candidate's calc.js" in capsys.readouterr().out
    archived._js_dir = lambda: old_js                                # node_modules linked: runs its own calc.js
    bench._which_calc()
    assert "WARNING" not in capsys.readouterr().out


@needs_calc
def test_node_path_lets_a_calc_js_without_node_modules_run(tmp_path, monkeypatch):
    real_dir, modules = calc_mod._js_paths()
    shutil.copy(real_dir / "calc.js", tmp_path / "calc.js")
    monkeypatch.setattr(calc_mod, "_js_paths", lambda: (tmp_path, modules))
    dc = DamageCalc()
    try:
        r = dc.batch([{"attacker": inp(INC), "defender": inp(KINGAMBIT), "move": "Flare Blitz"}])
        assert r and r[0] and r[0]["max"] > 0
    finally:
        if dc.proc:
            dc.proc.kill()
