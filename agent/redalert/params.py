"""Tunable numbers of the Red Alert bot (docs/redalert_design.md). Each one can be overridden for an A/B run with an
environment variable ARENA_RA_<NAME> (upper case), e.g. ARENA_RA_ATTACK_MIN_VALUE=5000. Bad values are ignored."""
from __future__ import annotations

import os
from types import SimpleNamespace

DEFAULTS: dict[str, float] = {
    # cadence (ticks: 25 a second)
    "throttle_ticks": 9,            # non-urgent batches at most every 0.36 s
    "budget_ms": 50.0,              # a frame slower than this skips the army step
    # economy
    "rich_cash_opening": 8000,      # starting_cash >= this: the fast-factory opening (B), else double refinery (A)
    "max_procs": 4,
    "max_weaps": 2,
    "max_harvesters": 8,
    "repair_below": 70,             # building hp %
    "repair_min_cash": 500,
    "repair_stop_cash": 300,
    "late_defenses": 4,             # towers built at home once the economy is up and cash floats
    # army
    "home_infantry": 6,             # infantry kept at home; the rest joins the attack groups
    "max_infantry": 20,
    "early_infantry": 36,           # infantry cap before 4:00 when our scout was killed early (an army at home)
    "rush_infantry": 22,            # infantry cap while an early rush is on
    "attack_min_units": 5,
    "attack_min_value": 4000,
    "attack_late_s": 420,           # from 7:00 a smaller first wave may go
    "attack_late_units": 3,
    "attack_ratio": 1.3,            # our attack group vs the enemy army we have seen
    "opportunistic_s": 240,
    "quiet_s": 60,
    "retreat_ratio": 0.7,
    "reengage_ratio": 1.2,
    "reinforce_units": 3,
    "reinforce_value": 2000,
    "threat_radius": 15,
    "engage_radius": 10,
    "endgame_s": 150,
    "allin_s": 90,
    "hold_margin": 1500,
    # v4.5 switches (1 on, 0 the v4.3.1 behaviour); see docs/redalert_design.md section 16
    "econ_first": 1,                # a scout killed or a light harass does not derail the opening
    "pre_weap_infantry": 6,         # econ_first: infantry cap before the first war factory, unless a real attack
    "early_army_infantry": 6,       # econ_first: that many more rifles and early_army_towers towers after signs of
    "early_army_towers": 2,         # an early enemy army (scout killed in their base, 2+ barracks, 700+ seen)
    "real_threat": 800,             # econ_first: enemy value at home that counts as a real attack (not a harass)
    "harv_guard": 1,                # harvesters under attack: pull back, idle units go help
    "counter_value": 1,             # compare armies by unit-type counters (rifles barely scratch tanks), not cost
    "blind_s": 150,                 # econ_first: no look at the enemy army / base for this long = blind: then an
    "prior_rate": 25,               # attack must beat an assumed army of prior_rate x (t - 90 s), at most
    "prior_cap": 9000,              # prior_cap, and a cheap unit goes to look again (re-scout)
    "prior_until_s": 900,
    "attack_3x_min": 2000,          # econ_first: "3x the enemy we saw" attacks only with this much (0: any size)
    "safety_tower_s": 150,          # econ_first: one tower at the front from here (war factory up), safety_towers
    "safety_towers": 2,             # a minute later
    # v4.6 switches (1 on, 0 the v4.5 behaviour exactly); see docs/redalert_design.md section 17
    "harv_first": 0,                # harvesters first: a harvester target over time, refineries for it, and spending
    "harv_t4": 180,                 # serialised behind it. Target: 4 harvesters by harv_t4 (game s), 6 by harv_t6,
    "harv_t6": 270,                 # 8 by harv_t8, never more than harv_cap (the map's ore near home lowers it:
    "harv_t8": 390,                 # about one harvester per harv_cells_per ore cells within 15 cells of our spawn)
    "harv_cap": 10,
    "harv_min_path": 70,            # only on maps where the ground path between the spawns is at least this long
    "harv_cells_per": 12,
    "harv_per_proc": 2.5,           # a refinery more when the target exceeds this many harvesters per refinery
    "harv_proc_first": 1,           # the third refinery before the depot (0: after it)
    "serial_infantry": 6,           # below the target and not really attacked: at most this much infantry, no new
    "serial_threat": 300,           # tower beyond threat towers; a threat at home worth serial_threat (or a harvester /
    "serial_half": 600,             # building hit, enemy army worth serial_half on our half, early-army signs) ends it
    "harv_weap2": 1500,             # harv_first: a 2nd war factory once 5 harvesters work and this much floats (0 off)
    "idle_harv_fix": 0,             # idle harvesters go back to work even without a construction yard (v4.5 bug)
    "harv_spread": 0,           # idle harvesters to the field with the most ore per harvester near our refineries;
    "harv_share": 3,                # one harvester moves on when more than harv_share share a refinery's field
}


def load(env=None) -> SimpleNamespace:
    env = os.environ if env is None else env
    out = dict(DEFAULTS)
    for k, v in DEFAULTS.items():
        raw = env.get("ARENA_RA_" + k.upper())
        if raw is None or str(raw).strip() == "":
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if val == val and abs(val) < 1e9:            # not NaN, not absurd
            out[k] = type(v)(val) if isinstance(v, int) and not isinstance(v, bool) else val
    return SimpleNamespace(**out)
