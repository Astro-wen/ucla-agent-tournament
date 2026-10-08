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
    "max_infantry": 14,
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
