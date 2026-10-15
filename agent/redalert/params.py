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
    # v4.6 switches (ARENA_RA_HARV_FIRST=0 ARENA_RA_IDLE_HARV_FIX=0: the v4.5 behaviour exactly); see
    # docs/redalert_design.md section 17
    "harv_first": 1,                # harvesters first (agent/redalert/harvest.py): a harvester target over time,
    "harv_t4": 180,                 # refineries for it, spending serialised behind it. Target: 4 harvesters by harv_t4
    "harv_t6": 270,                 # (game s), 6 by harv_t6, 8 by harv_t8, at most harv_cap (fewer with little ore:
    "harv_t8": 390,                 # one per harv_cells_per ore cells of the fields reaching 15 cells of our spawn)
    "harv_cap": 10,
    "harv_cells_per": 12,
    "harv_min_path": 70,            # only where the spawn-to-spawn ground path is this long (pool: shadowfiend-ii)
    "harv_per_proc": 2.5,           # a refinery more when the target exceeds this many harvesters per refinery
    "harv_proc_first": 1,           # the third refinery before the depot (0: after it)
    "serial_infantry": 6,           # below the target and nothing attacking: at most this much infantry, no tower
    "serial_threat": 300,           # beyond threat towers; a threat at home worth serial_threat (or a harvester /
    "serial_half": 600,             # building hit, an enemy army worth serial_half on our half) ends it
    "harv_weap2": 0,                # harv_first: a 2nd war factory once 5 harvesters work and this much floats (0 off;
                                    # 1500 tried: dev seeds 611-616 26 / 10 vs 25 / 9 without it)
    "idle_harv_fix": 1,             # idle harvesters go back to work even without a construction yard (v4.5 bug)
    "deploy_pending_ticks": 375,    # an MCV deploy the platform accepted is not resent for this long (0: v4.5, 100)
    "harv_spread": 0,               # idle harvesters to the field with the most ore per harvester near our refineries;
    "harv_share": 3,                # one moves on when more than harv_share share a refinery's field (off: no gain)
    # v4.7 switches (ARENA_RA_ATTACK_GATE=0 ARENA_RA_TRADE_RETREAT=0 ARENA_RA_RECOVER=0, attack_comp off by default:
    # the v4.6 behaviour exactly); see docs/redalert_design.md section 18
    "attack_gate": 1,               # the attack decision: a larger margin while no enemy building was ever seen
    "gate_unseen_ratio": 1.6,       # (instead of attack_ratio), the blind prior keeps growing after prior_cap up to
    "prior_late_cap": 13000,        # prior_late_cap, and seen towers / artillery weigh gate_def_w (they shoot first)
    "gate_def_w": 1.5,
    "trade_retreat": 1,             # the attack group falls back when the running trade is clearly losing: over the
    "trade_window_s": 12,           # last trade_window_s our losses >= trade_loss_ratio x our kills, >= trade_min_loss
    "trade_loss_ratio": 1.5,        # and >= trade_min_frac of the wave's value (0.2 tried: it stopped charges into
    "trade_min_loss": 1500,         # nashball's post that won, kills lag behind the first deaths; the two real losses
    "trade_min_frac": 0.33,         # lost 40% of the wave in 10-11 s)
    "trade_cum_ratio": 1.0,         # ... and only while the wave's losses since it set out >= this x its kills (0: no
                                    # such check; real wins fffc7ced / 971ec5dc: a bad 12 s after a good exchange)
    "attack_comp": 0,               # attack waves take vehicles, rocket / grenade infantry and at most
    "comp_rifles_per_veh": 0.5,     # comp_rifles_per_veh rifles per vehicle; the other rifles stay home as guards
    "comp_min_veh": 5,              # (only once the wave has comp_min_veh vehicles and from comp_from_s: a small early
    "comp_from_s": 360,             # wave without its rifles is too weak, dev seeds 1404002 / 1405014). Off: the sim
                                    # has no crush / splash, so it cannot judge it, and human RA armies keep rifles
    "recover": 1,                   # after a failed wave: recover_s of defend-and-rebuild (next wave needs
    "recover_s": 240,               # recover_mult x the failed wave; recover_towers towers at home while their army
    "recover_mult": 1.6,            # is the bigger one, 0 = none: dev seed 1405014, 3 towers ate the money before
    "recover_towers": 0,            # riffo's push)
    # v4.8 switches (ARENA_RA_AT_TOWERS=0 ARENA_RA_AT_COMP=0 ARENA_RA_HF_KEEP=0: the v4.7 behaviour exactly); see
    # docs/redalert_design.md section 19 (real losses to an early tank army, 10-14 / 10-15)
    "at_towers": 2,                 # anti-tank towers (gun / tesla coil) toward the threat once an enemy tank army
    "at_tank_min": 1700,            # was seen (tanks worth at_tank_min at once, about 2) and their army is worth at
    "at_ratio": 0.6,                # least at_ratio x ours (0: off)
    "at_comp": 1,                   # infantry once an enemy tank army was seen: rockets at least at_e3_share of the
    "at_e3_share": 0.6,             # rifles + rockets, rifles at most at_e1_cap
    "at_e1_cap": 12,
    "hf_keep": 1,                   # harv_first goes on through early-army signs from inside their base (a dead
                                    # scout, units seen at their home); a rush or two barracks still end it
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
