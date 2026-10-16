import copy

import pytest

from agent.hok import policy as p


def state(hp=70, camp=1, **updates):
    s = {"my_hero": {"camp": camp, "hp": hp, "max_hp": 100, "x": 0, "z": 0},
         "structures": [{"type": "ACTOR_TOWER", "camp": 3 - camp, "hp": 4000, "x": 12000, "z": 0}]}
    s.update(updates)
    return s


MACROS = {"macro_advance": "Move toward nearest tower", "macro_retreat": "Move home",
          "macro_attack": "Basic attack (nearest: enemy hero 169, 13732 away)",
          "macro_skill3": "Cast skill 3 toward enemy hero 169 (13732 away)"}


@pytest.mark.parametrize("camp", [1, 2])
@pytest.mark.parametrize("kind", ["ACTOR_TOWER", "ACTOR_CRYSTAL"])
def test_healthy_objective_push_both_camps(camp, kind):
    s = state(camp=camp)
    s["structures"][0]["type"] = kind
    before = copy.deepcopy(s)
    assert p.rule_macro(s, MACROS) == "macro_advance"
    assert s == before


@pytest.mark.parametrize("hp,expected", [(49.9, "macro_attack"), (50, "macro_advance"), (100, "macro_advance")])
def test_health_boundary(hp, expected):
    assert p.rule_macro(state(hp=hp), MACROS) == expected


def test_radius_boundary():
    s = state()
    s["structures"][0]["x"] = 12000.01
    assert p.rule_macro(s, MACROS) == "macro_attack"


def test_off_switch_restores_old_choice(monkeypatch):
    monkeypatch.setattr(p, "OBJECTIVE_PUSH", False)
    assert p.rule_macro(state(), MACROS) == "macro_attack"


def test_building_attack_stays_first():
    macros = dict(MACROS, macro_attack="Basic attack (nearest: enemy tower, 12000 away)")
    assert p.rule_macro(state(), macros) == "macro_attack"


def test_safety_retreat_stays_first():
    class Danger:
        def outnumbered(self, position):
            return True

        def under_tower(self, position):
            return True

    assert p.rule_macro(state(hp=100), MACROS, Danger()) == "macro_retreat"
    assert p.rule_macro(state(hp=70), MACROS, retreating=True) == "macro_retreat"
    assert p.rule_macro(state(hp=29), MACROS) == "macro_retreat"


def test_missing_advance_keeps_hero_attack():
    macros = {k: v for k, v in MACROS.items() if k != "macro_advance"}
    assert p.rule_macro(state(), macros) == "macro_attack"


@pytest.mark.parametrize("update", [{"type": "ACTOR_TOWER_SPRING"}, {"type": None}, {"camp": 1},
                                     {"camp": None}, {"hp": 0}, {"hp": None}, {"visible": False},
                                     {"x": None}, {"z": None}, {"x": float("nan")}])
def test_invalid_or_nonobjective_structure_falls_back(update):
    s = state()
    s["structures"][0].update(update)
    assert p.rule_macro(s, MACROS) == "macro_attack"


@pytest.mark.parametrize("field", ["camp", "hp", "max_hp", "x", "z"])
def test_missing_hero_metadata_falls_back(field):
    s = state()
    del s["my_hero"][field]
    assert p.rule_macro(s, MACROS) == "macro_attack"


def test_empty_structures_falls_back():
    assert p.rule_macro(state(structures=[]), MACROS) == "macro_attack"


@pytest.mark.parametrize("tower_distance,hero_id,hero_distance,hp", [(9064, 175, 3075, 99.4),
                                                                  (8134, 123, 13732, 100)])
def test_observed_objective_distances_with_hero_attack(tower_distance, hero_id, hero_distance, hp):
    # Minimal geometry/offer representation of the two recorded distances, not a full raw replay.
    summary = state(hp=hp, camp=2)
    summary["structures"][0]["x"] = tower_distance
    macros = {k: v for k, v in MACROS.items() if not k.startswith("macro_skill")}
    macros["macro_attack"] = f"Basic attack (nearest: enemy hero {hero_id}, {hero_distance} away)"
    expected = "macro_attack" if hero_distance <= p.SKILL_RANGE else "macro_advance"
    assert p.rule_macro(summary, macros) == expected


class TowerOnly:
    def outnumbered(self, position):
        return False

    def under_tower(self, position):
        return True


@pytest.mark.parametrize("hp,expected", [(49.9, "macro_retreat"), (50, "macro_advance"),
                                        (55, "macro_advance")])
def test_half_health_near_tower_commitment(hp, expected):
    assert p.rule_macro(state(hp=hp), MACROS, TowerOnly()) == expected


@pytest.mark.parametrize("hp", [50, 55])
def test_half_health_outnumbered_still_retreats(hp):
    class Outnumbered(TowerOnly):
        def outnumbered(self, position):
            return True

    assert p.rule_macro(state(hp=hp), MACROS, Outnumbered()) == "macro_retreat"


@pytest.mark.parametrize("hp", [50, 55])
def test_half_health_existing_recovery_still_retreats(hp):
    assert p.rule_macro(state(hp=hp), MACROS, TowerOnly(), retreating=True) == "macro_retreat"


def test_half_health_switch_off_restores_tower_retreat(monkeypatch):
    monkeypatch.setattr(p, "OBJECTIVE_PUSH", False)
    assert p.rule_macro(state(hp=55), MACROS, TowerOnly()) == "macro_retreat"


def test_half_health_structure_attack_before_advance():
    macros = dict(MACROS, macro_attack="Basic attack (nearest: enemy tower, 12000 away)")
    assert p.rule_macro(state(hp=70), macros, TowerOnly()) == "macro_attack"


def test_half_health_far_or_no_advance_keeps_tower_retreat():
    s = state(hp=55)
    s["structures"][0]["x"] = 12000.01
    assert p.rule_macro(s, MACROS, TowerOnly()) == "macro_retreat"
    macros = {k: v for k, v in MACROS.items() if k != "macro_advance"}
    assert p.rule_macro(state(hp=55), macros, TowerOnly()) == "macro_retreat"


@pytest.mark.parametrize("distance,expected", [(8000, "macro_attack"), (8000.01, "macro_advance")])
def test_near_hero_attack_blocks_objective_override(distance, expected):
    macros = {k: v for k, v in MACROS.items() if not k.startswith("macro_skill")}
    macros["macro_attack"] = f"Basic attack (nearest: enemy hero 169, {distance} away)"
    summary = state()
    summary["structures"][0]["x"] = 7000
    assert p.rule_macro(summary, macros) == expected


def test_unknown_hero_attack_distance_blocks_override():
    macros = {k: v for k, v in MACROS.items() if not k.startswith("macro_skill")}
    macros["macro_attack"] = "Basic attack (nearest: enemy hero 169)"
    assert p.rule_macro(state(), macros) == "macro_attack"


@pytest.mark.parametrize("label", ["Cast skill 1 toward enemy hero 169 (8000 away)",
                                  "Cast skill 1 toward enemy hero 169"])
def test_near_or_unknown_hero_skill_blocks_override(label):
    macros = dict(MACROS, macro_skill1=label)
    assert p.rule_macro(state(), macros) == "macro_skill1"


def test_no_hero_combat_offers_preserves_tower_retreat():
    macros = {k: v for k, v in MACROS.items() if k in ("macro_advance", "macro_retreat")}
    assert p.rule_macro(state(hp=50), macros, TowerOnly()) == "macro_retreat"


def test_near_hero_keeps_original_tower_safety():
    macros = dict(MACROS, macro_attack="Basic attack (nearest: enemy hero 169, 1000 away)")
    assert p.rule_macro(state(hp=55), macros, TowerOnly()) == "macro_retreat"


def test_no_attack_preserves_original_structure_skill():
    macros = {k: v for k, v in MACROS.items() if k != "macro_attack"}
    macros["macro_skill1"] = "Cast skill 1 toward enemy tower (5000 away)"
    assert p.rule_macro(state(), macros) == "macro_skill1"


def test_structure_attack_does_not_gain_new_tower_safety_exception():
    macros = dict(MACROS, macro_attack="Basic attack (nearest: enemy tower, 5000 away)")
    assert p.rule_macro(state(hp=55), macros, TowerOnly()) == "macro_retreat"


def test_unknown_attack_target_preserves_original_skill():
    macros = dict(MACROS, macro_attack="Basic attack (nearest: unknown target, 13732 away)",
                  macro_skill1="Cast skill 1 toward enemy tower (5000 away)")
    assert p.rule_macro(state(), macros) == "macro_skill1"


@pytest.mark.parametrize("hero_distance", [9000, 12000])
def test_hero_not_farther_than_building_is_not_overridden(hero_distance):
    macros = {k: v for k, v in MACROS.items() if not k.startswith("macro_skill")}
    macros["macro_attack"] = f"Basic attack (nearest: enemy hero 169, {hero_distance} away)"
    assert p.rule_macro(state(), macros) == "macro_attack"
