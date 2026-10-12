"""Army comparison by unit-type counters (v4.5 `counter_value`): instead of comparing raw cost, two forces are
compared with Lanchester's square law on damage rate x hit points, where each unit's damage rate depends on the
armour it shoots at (rifles barely scratch tanks, rockets barely scratch infantry, flame towers burn infantry).

Numbers: damage per second per 1000 credits against each armour class, from the RA mod's weapon tables at the
engine commit the platform names (damage x versus % / reload, the same tables local/ra_rules.py takes for the
simulator; the platform's get_game_config gives damage and range but no versus table or reload). Hit points come
from the reference (`hp`), else a default per kind. Crushing and splash (which favour tanks against clumped
infantry in the real engine) are not in these numbers.

    force: list of (type, count, hp_fraction): our units are (type, 1, hp/100); a remembered enemy mix is
    (type, value / cost, 1.0).
"""
from __future__ import annotations

import math

from .world import is_infantry

# dps per 1000 credits vs armour: None (infantry), Light, Heavy (tanks, APC, harvester, towers), Wood (buildings)
EFF = {
    "e1": (18750, 5000, 1250, 3750), "e2": (10000, 4167, 4167, 16667), "e3": (833, 2833, 8333, 6167),
    "e4": (13672, 7812, 3906, 15625), "shok": (45918, 4592, 2755, 3352), "dog": (8000, 0, 0, 0),
    "1tnk": (1361, 4932, 2041, 2211), "2tnk": (706, 1765, 2706, 1765), "3tnk": (696, 1739, 2667, 1739),
    "4tnk": (474, 1184, 1816, 1184), "jeep": (7500, 1500, 500, 500), "apc": (4412, 882, 294, 294),
    "ftrk": (3333, 5000, 833, 833), "arty": (4775, 4775, 1990, 3183), "v2rl": (523, 407, 233, 436),
    "pbox": (14286, 3571, 1429, 3571), "hbox": (11429, 2857, 1143, 2857), "gun": (1250, 4688, 6250, 3125),
    "ftur": (12500, 6944, 3472, 6944), "tsla": (34722, 3472, 3472, 2083),
}
DEFAULT_EFF = (2000, 2000, 2000, 2000)
ARMOR_INDEX = {"None": 0, "Light": 1, "Heavy": 2, "Wood": 3}
LIGHT = frozenset({"jeep", "ftrk", "arty", "v2rl", "mcv", "truk", "dtrk", "mgg", "mrj", "heli", "mh60", "tran", "yak",
                   "mig", "pt"})
TOWERS = frozenset({"pbox", "hbox", "gun", "ftur", "tsla", "agun", "sam", "gap"})
DEFAULT_HP = {"None": 5000, "Light": 15000, "Heavy": 40000, "Wood": 60000}


def armor(t: str, rules) -> str:
    if is_infantry(t, rules):
        return "None"
    if t in LIGHT:
        return "Light"
    if t in TOWERS or not rules.is_building(t):
        return "Heavy"
    return "Wood"


def hp(t: str, rules) -> float:
    v = rules.item(t).get("hp")
    try:
        f = float(v)
        if f > 0:
            return f
    except (TypeError, ValueError):
        pass
    return float(DEFAULT_HP[armor(t, rules)])


def _parts(force, rules):
    """(damage rate vs each armour, total hp, hp by armour) of a force."""
    dmg = [0.0, 0.0, 0.0, 0.0]
    tot_hp = 0.0
    by_armor = [0.0, 0.0, 0.0, 0.0]
    for t, n, frac in force:
        if n <= 0:
            continue
        h = n * frac * hp(t, rules)
        tot_hp += h
        by_armor[ARMOR_INDEX[armor(t, rules)]] += h
        e = EFF.get(t, DEFAULT_EFF)
        c = n * frac * rules.cost(t) / 1000.0
        for k in range(4):
            dmg[k] += c * e[k]
    return dmg, tot_hp, by_armor


def ratio(ours, theirs, rules) -> float:
    """Force ratio (1.0 = even; the same as the cost ratio for identical compositions): sqrt of our damage rate
    against their armour mix x our hit points over theirs. inf when they cannot hurt us, 0 when we cannot hurt them."""
    d_a, h_a, arm_a = _parts(ours, rules)
    d_b, h_b, arm_b = _parts(theirs, rules)
    if h_b <= 0:
        return math.inf
    if h_a <= 0:
        return 0.0
    rate_a = sum(d_a[k] * arm_b[k] / h_b for k in range(4))       # our damage rate against their mix
    rate_b = sum(d_b[k] * arm_a[k] / h_a for k in range(4))
    if rate_b <= 0:
        return math.inf
    if rate_a <= 0:
        return 0.0
    return math.sqrt((rate_a * h_a) / (rate_b * h_b))
