"""Stat formulas, speed ordering and the damage-calc bridge (bridge tests skip without node_modules)."""
import pytest

from agent.pokemon import dex
from agent.pokemon.calc import DamageCalc, _js_dir, calc_field, calc_pokemon
from agent.pokemon.stats import acts_first, calc_stats, effective_speed, move_priority, stats_for

CHIYU = {"species": "Chi-Yu", "item": "Choice Specs", "ability": "Beads of Ruin", "nature": "Timid", "level": 50,
         "evs": {"hp": 4, "spa": 252, "spe": 252}, "ivs": {}, "moves": ["Heat Wave", "Dark Pulse", "Overheat", "Snarl"]}
INC = {"species": "Incineroar", "item": "Safety Goggles", "ability": "Intimidate", "nature": "Careful", "level": 50,
       "evs": {"hp": 252, "atk": 4, "def": 100, "spd": 148, "spe": 4}, "ivs": {}, "moves": ["Fake Out"]}


def test_stat_formula_matches_known_values():
    # Chi-Yu base 55/80/80/135/120/100; Timid 252 SpA / 252 Spe / 4 HP at level 50 (verified against @smogon/calc)
    st = calc_stats(dex.base_stats("chiyu"), CHIYU["evs"], {}, "Timid", 50)
    assert st == {"hp": 131, "atk": 90, "def": 100, "spa": 187, "spd": 140, "spe": 167}


def test_speed_modifiers_and_priority():
    base = effective_speed({"species": "chiyu"}, CHIYU)
    assert base == 167
    assert effective_speed({"species": "chiyu", "boosts": {"spe": 1}}, CHIYU) == 167 * 1.5
    assert effective_speed({"species": "chiyu", "status": "par"}, CHIYU) == 167 * 0.5
    assert effective_speed({"species": "chiyu"}, CHIYU, side={"tailwind": 1}) == 167 * 2
    assert effective_speed({"species": "chiyu", "item": "choicescarf"}, CHIYU) == 167 * 1.5
    assert acts_first(167, 0, 80, 0) is True and acts_first(167, 0, 80, 0, trick_room=True) is False
    assert acts_first(60, 3, 200, 0) is True                     # Fake Out priority beats speed
    assert acts_first(100, 0, 100, 0) is None                    # speed tie
    assert move_priority("fakeout") == 3 and move_priority("tailwind", {"ability": "prankster"}) == 1


def test_stats_for_falls_back_when_card_unknown():
    st = stats_for(None, "incineroar")
    assert st["hp"] > 0 and st["spe"] > 0


@pytest.mark.skipif(_js_dir() is None, reason="agent/js has no node_modules")
def test_damage_calc_bridge_doubles_spread_and_cache():
    dc = DamageCalc.get()
    obs = {"is_doubles": True, "side_conditions": {}, "opponent_side_conditions": {}, "weather": {}, "fields": {}}
    atk = calc_pokemon({"species": "chiyu"}, CHIYU)
    d = calc_pokemon({"species": "incineroar"}, INC)
    r = dc.batch([{"attacker": atk, "defender": d, "move": "Heat Wave", "field": calc_field(obs, True)},
                  {"attacker": atk, "defender": d, "move": "Heat Wave", "field": {**calc_field(obs, True), "gameType": "Singles"}},
                  {"attacker": atk, "defender": d, "move": "Not A Move", "field": calc_field(obs, True)}])
    assert r is not None and r[0] and r[1] and r[2]["max"] == 0        # unknown move -> 0 damage, not a crash
    assert len(r[0]["rolls"]) == 16 and r[0]["defender_hp"] == 202                    # Incineroar 252 HP at L50
    assert r[0]["max"] < r[1]["max"]                                                  # spread 0.75x in doubles
    assert r[0]["max"] < r[0]["defender_hp"]                                          # Heat Wave doesn't OHKO a Careful Incineroar
    again = dc.batch([{"attacker": atk, "defender": d, "move": "Heat Wave", "field": calc_field(obs, True)}])
    assert again[0]["rolls"] == r[0]["rolls"]


def test_calc_inputs_use_display_names_and_unknown_item_is_dropped():
    p = calc_pokemon({"species": "ironhands", "item": "unknown_item", "ability": "quarkdrive"}, None)
    assert p["species"] == "Iron Hands" and "item" not in p and p["ability"] == "Quark Drive"
    f = calc_field({"is_doubles": True, "side_conditions": {"tailwind": 1}, "weather": {"SunnyDay": 3}, "fields": {}}, True)
    assert f["gameType"] == "Doubles" and f["attackerSide"]["isTailwind"] and f["weather"] == "Sun"
