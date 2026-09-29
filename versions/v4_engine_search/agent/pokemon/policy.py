"""Pokémon policy: draft, team preview, singles + VGC doubles turns.

Battle moves are scored with Smogon's exact damage calculator (agent/pokemon/calc.py): ONE batched node
round trip per decision covers every (our active, move, legal target) plus the foes' known moves against
our actives. A hit is worth its expected damage as a fraction of the defender's *current* HP (capped, so
overkill earns nothing) plus a KO bonus weighted by P(KO) over the rolls and the move's accuracy;
foes are weighted by how dangerous they are to us. In doubles both slots are chosen jointly, so two
attacks are not wasted on one low-HP foe and both slots never Fake Out the same target.
calc.js already folds in Focus Sash / Sturdy (a single hit on a full-HP holder leaves 1 HP, both sides),
multi-hit totals and hit-count odds; Supreme Overlord's fainted-ally count comes from `_fallen_counts()`.

Doubles also use move order (`_speed_model`, exact speeds from the drafted sets, priority brackets, Tailwind,
Trick Room, paralysis, Scarf): an action is worth P(we act before a foe KOs us) x its points, a KO that lands
before the target acts earns REMOVE_* on top, Protect is valued by `_protect_value`, Tailwind / Trick Room by
how many speed matchups they flip, and Focus Sash / Sturdy at full HP stop a lone single-hit KO.

When the calculator is unavailable (no node / node_modules, bridge error) every decision falls back to
the transparent base-stat heuristic `move_value()` with the same VGC bonuses (Fake Out, Intimidate,
Tailwind, spread moves), exactly as v0 played. See docs/strategy.md for the upgrade plan.
"""
from __future__ import annotations

import logging
import os
import time
import zlib

from .. import timecontrol
from . import dex
from . import drafter as drafter_mod
from . import lookahead as lookahead_mod
from . import preview as preview_matrix
from .calc import DamageCalc, calc_field, calc_pokemon, move_name, view_item
from .dex import to_id
from .lookahead import PROTECT_MOVES, joint_lookahead
from .stats import acts_first, effective_speed, move_priority
from .typechart import effectiveness

log = logging.getLogger("agent.policy")

# Rough VGC Reg-I priors (restricteds + common support). Tune from match logs.
STAPLES = {
    "calyrexshadow": 10, "miraidon": 10, "zaciancrowned": 10, "koraidon": 9, "calyrexice": 9,
    "kyogre": 9, "terapagos": 9, "groudon": 8, "lunala": 8, "zamazentacrowned": 8,
    "incineroar": 9, "rillaboom": 8, "urshifurapidstrike": 8, "urshifu": 7, "fluttermane": 8,
    "amoonguss": 7, "tornadus": 7, "landorustherian": 7, "ogerponhearthflame": 7,
    "ogerponwellspring": 7, "ogerponcornerstone": 6, "chienpao": 7, "chiyu": 7, "ragingbolt": 7,
    "farigiraf": 6, "kingambit": 6, "gholdengo": 6, "ironhands": 6, "archaludon": 6,
    "grimmsnarl": 5, "indeedeef": 5, "whimsicott": 5, "tatsugiri": 5, "ursalunabloodmoon": 6,
}
SUPPORT_MOVES = {"fakeout": 2.0, "followme": 1.5, "ragepowder": 1.5, "tailwind": 1.5, "trickroom": 1.0,
                 "protect": 0.5, "detect": 0.5, "spore": 1.0, "icywind": 0.7, "electroweb": 0.5,
                 "snarl": 0.5, "partingshot": 0.7, "helpinghand": 0.3, "wideguard": 0.5}
ABILITIES = {"intimidate": 2.0, "prankster": 0.8, "drought": 1.0, "drizzle": 1.0,
             "electricsurge": 0.8, "psychicsurge": 0.8, "unseenfist": 0.8, "asoneglastrier": 0.5,
             "asonespectrier": 0.5, "innerfocus": 0.3}
SPREAD_TARGETS = {"allAdjacentFoes", "allAdjacent"}
USE_DRAFTER = True               # draft with agent/pokemon/drafter.py (search over the rest of the draft)
USE_SINGLES = True               # singles battles / preview with agent/pokemon/singles.py (False = the v3 scorer)
SINGLES_DRAFTER = os.environ.get("ARENA_SINGLES_DRAFTER", "1") != "0"   # OU draft: singles prior (A/B knob, local only)
# ---- time control (agent/timecontrol.py): below these the bounded step is skipped, not started
MIN_LOOKAHEAD_S = 0.05           # joint lookahead (normally ~30 ms, TIME_CAP 2.5 s)
MIN_DRAFTER_S = 0.02             # drafter search wall (normally < 0.3 s, SAFETY_WALL_S 2 s)
MIN_PREVIEW_S = 0.25             # preview matrix game (normally 0.5-2.7 s incl. its calc batch, wall cap 6 s)
DRAFTER_SHARE = 0.5              # the drafter's search may use this share of the draft pick's time left
PREVIEW_SHARE = 0.8              # the preview matrix game (calc batch + search) may use this share
BATTLE_PHASES = ("moving",)      # the platform's battle phase (docs/real_payloads.md)


# ---- engine-search battle policy (ARENA_BATTLE=search; agent/pokemon/engine.py + agent/js/search.js) -------------
def _env_num(name: str, default: float, cast=float):
    try:
        v = os.environ.get(name)
        return default if v in (None, "") else cast(v)
    except (TypeError, ValueError):
        return default


def battle_mode() -> str:
    """ARENA_BATTLE: `search` (default: the engine search, with the classic pick as its fallback and as one of its
    candidate rows) or `classic` (the rollback: the exact-damage scorer + joint lookahead alone, = champion v3.2).
    Anything else also means classic."""
    v = str(os.environ.get("ARENA_BATTLE") or "search").strip().lower()
    return "search" if v == "search" else "classic"


# The deployed search configuration (ARENA_SEARCH_* env knobs below override any of it):
#   mode blend: our best reply to 50% the opponent's equilibrium mix + 50% its greedy (max-damage) joint action, not a
#     sample of our own mix (picked on dev seed 62 as the best of three variants vs the champion: blend 96, nash
#     sampling 86, delta 85 of 150, classic mirror 78; clean recheck on seed 63: +15.3 [+5.1, +25.6], p = 0.006; gates
#     on fresh seeds: ladder 7 / 11 +9.8 / +10.8 over the champion, VGC-Bench sampling / argmax +8.5 / +15.5, simbot
#     -9.0 n.s.);
#   fixed 1: a fixed amount of work per decision (agent/js/search.js header), so the answer does not depend on the
#     machine's speed or load (the platform server's CPU is unknown) unless the safety net below cuts a search.
#     ARENA_SEARCH_FIXED=0 restores the time budget (SEARCH_MS) it was tuned with.
SEARCH_CONFIG = {"mode": "blend", "fixed": 1}
SEARCH_MS = _env_num("ARENA_SEARCH_MS", 2500, int)       # the time budget (ARENA_SEARCH_FIXED=0 only)
# The safety net (hard_ms): min(SEARCH_CAP_MS, 80% of the search's limit), the limit being SEARCH_SHARE of the decision's
# time left minus its reserve, at most SEARCH_HARD_S. A battle decision gets at most timecontrol.BATTLE_S = 10 s, on
# the platform too (every recorded real state: 10 s; the timer line only ever lowers it), so the limit is ~4.9 s and
# the net ~3.9 s: SEARCH_CAP_MS does not bind unless ARENA_TC_BATTLE_S raises the budget. search.js also takes 1
# sample instead of 2 once pruning used half the net (~2 s): at load ~14 (the gates ran at 6-14) 15 of 50 searches did
# and 3 hit the net (review measurement on 3 real matches). Gate latency: 1.8 s per search on average, 4.8 s at most;
# the platform's turn timer is 55 s.
SEARCH_CAP_MS = _env_num("ARENA_SEARCH_CAP_MS", 6000, int)
SEARCH_HARD_S = _env_num("ARENA_SEARCH_HARD_S", 8.0)     # a search answer later than this is dropped (classic plays)
SEARCH_MIN_S = 0.4                                        # less time than this left: classic only
SEARCH_SHARE = 0.6                                        # the search may use this share of the decision's time left
TRACE_KEYS = ("choice", "rows", "cols", "n_my", "n_opp", "samples", "sims", "errors", "stageA_ms", "build_ms", "ms",
              "value", "pick_value", "classic_value", "classic_prob", "is_classic", "kept_classic", "forced", "cut",
              "prune_cut", "guard")
SEARCH_KNOBS = {"samples": ("ARENA_SEARCH_SAMPLES", int), "rollout": ("ARENA_SEARCH_ROLLOUT", int),
                "keep": ("ARENA_SEARCH_KEEP", int), "slot_keep": ("ARENA_SEARCH_SLOT_KEEP", int),
                "probes": ("ARENA_SEARCH_PROBES", int), "mode": ("ARENA_SEARCH_MODE", str),
                "min_prob": ("ARENA_SEARCH_MIN_PROB", float), "iters": ("ARENA_SEARCH_ITERS", int),
                "margin": ("ARENA_SEARCH_MARGIN", float), "delta": ("ARENA_SEARCH_DELTA", float),
                "blend_w": ("ARENA_SEARCH_BLEND_W", float), "guard": ("ARENA_SEARCH_GUARD", int),
                "guard_delta": ("ARENA_SEARCH_GUARD_DELTA", float), "free_delta": ("ARENA_SEARCH_FREE_DELTA", float),
                "fixed": ("ARENA_SEARCH_FIXED", int)}


def search_params() -> dict:
    """SEARCH_CONFIG with the ARENA_SEARCH_* overrides (an unparsable value is ignored)."""
    out = dict(SEARCH_CONFIG)
    for k, (var, cast) in SEARCH_KNOBS.items():
        v = os.environ.get(var)
        if v not in (None, ""):
            try:
                out[k] = cast(v)
            except (TypeError, ValueError):
                pass
    return out


def _template_ok(tmpl: dict, action: dict) -> bool:
    """Is a doubles_turn `action` one of the template's options (agent.doubles_ok's rules; never raises)?"""
    try:
        used = set()
        for slot in tmpl.get("slots", []):
            c = action.get(f"slot_{slot['slot']}")
            if not isinstance(c, dict):
                return False
            opts = slot.get("options") or []
            if c.get("type") == "move":
                if not any(o.get("type") == "move" and to_id(o.get("move_id")) == to_id(c.get("move_id"))
                           and c.get("target") in (o.get("targets") or [0]) for o in opts):
                    return False
            elif c.get("type") == "switch":
                sp = to_id(c.get("species"))
                if sp in used or not any(o.get("type") == "switch" and to_id(o.get("species")) == sp for o in opts):
                    return False
                used.add(sp)
            elif c.get("type") == "pass":
                if not any(o.get("type") == "pass" for o in opts):
                    return False
            else:
                return False
        return True
    except Exception:
        return False

# ---- exact-damage scoring scale ("points"): removing all of a foe's current HP = 100, a KO adds KO_BONUS.
KO_BONUS = 50.0
FAKE_OUT_BONUS = 1000.0          # Fake Out on a fresh mon stays dominant (v0 behaviour)
ALLY_HIT_WEIGHT = 0.9            # allAdjacent moves: our partner's damage counts against the move
SELF_KO_PENALTY = 150.0          # Explosion & co.
HEURISTIC_TO_POINTS = 1 / 3.5    # move_value() (~400 for a STAB super-effective 120 BP hit) -> points, when one calc fails
GENERIC_CARD = {"evs": {"hp": 4, "atk": 252, "spa": 252, "spe": 252}, "nature": "Hardy", "level": 50}
FIRST_TURN_ONLY = {"fakeout", "firstimpression"}
FLINCH_IMMUNE_ABILITIES = {"innerfocus", "shielddust"}
ALLY_SUPPORT_MOVES = {"helpinghand", "pollenpuff", "decorate", "coaching"}

# ---- speed-aware KO races and Protect (same points scale) -------------------------------------------------------
# PROTECT_MOVES (protect, detect, spikyshield, silktrap, banefulbunker, burningbulwark, kingsshield, obstruct) is
# imported from lookahead.py: the lookahead and speed-protect defined the same set, one definition now serves both.
# Any of these used last turn makes the next Protect succeed only 1/3 of the time (Gen 9 stall counter).
STALL_MOVES = PROTECT_MOVES | {"wideguard", "quickguard", "endure", "maxguard"}
# Partner moves that leave a threat unable to hurt a Protecting mon next turn (slept, burned, slowed, outsped...).
CRIPPLE_MOVES = {"spore", "sleeppowder", "hypnosis", "yawn", "willowisp", "thunderwave", "nuzzle", "icywind",
                 "electroweb", "tailwind", "trickroom", "encore", "taunt", "snarl", "partingshot"}
REMOVE_BASE = 15.0        # KO a foe *before it acts*: + this ...
REMOVE_DANGER = 60.0      # ... + this x its danger (fraction of our current HP its best move removes)
PROTECT_SAVE = 90.0       # our mon surviving a KO this turn while the partner removes / cripples the threat
PROTECT_ALONE = 0.2       # share of PROTECT_SAVE when nothing deals with the threat (a plain one-turn stall)
PROTECT_MIN_DANGER = 0.2  # below this P(KO'd this turn) Protect is pointless ...
PROTECT_IDLE = -40.0      # ... and scores this
PROTECT_AGAIN = -300.0    # Protect twice in a row (succeeds 1/3 in Gen 9): never
PROTECT_ABSORB = 40.0     # x expected non-lethal fraction of our HP the foes' attacks on this slot would remove
STALL_BONUS = 30.0        # Protect through the last turns of an opposing Tailwind / Trick Room
FAKE_OUT_LIKELY = 0.9     # a fresh foe that carries Fake Out uses it
SUCKER_PUNCH_ACC = 0.8    # fails into status moves / Protect
TIE_BREAK = 1e-6          # x stand-alone points: only decides exact ties in the joint scoring
TAILWIND_TURNS, TRICK_ROOM_TURNS = 4, 5
SASH_ITEMS = {"focussash"}
SASH_ABILITIES = {"sturdy"}


def _stat(sp: str, key: str, default: int = 90) -> int:
    return dex.base_stats(sp).get(key, default)


def move_value(move_id: str, attacker: str, defender_types: list[str], extra: dict | None = None) -> float:
    """Expected single-target damage proxy of one move against one defender (base-stat heuristic, the fallback)."""
    m = {**dex.move(move_id), **{k: v for k, v in (extra or {}).items() if v is not None}}
    bp = m.get("basePower") or m.get("base_power") or 0
    if not bp:
        return 0.0
    mtype = (m.get("type") or m.get("move_type") or "").upper()
    cat = (m.get("category") or "").lower()
    hits = m.get("multihit", 1)
    hits = sum(hits) / 2 if isinstance(hits, list) else hits
    acc = m.get("accuracy", 100)
    acc = 1.0 if acc is True else (acc / 100 if acc > 1 else acc)
    stab = 1.5 if mtype in dex.types_of(attacker) else 1.0
    atk = _stat(attacker, "atk" if cat == "physical" else "spa") / 100
    return bp * hits * stab * effectiveness(mtype, defender_types) * acc * atk


def card_score(card: dict, roster: list[dict]) -> float:
    sp = to_id(card.get("species", ""))
    s = float(STAPLES.get(sp, 0))
    bst = sum(dex.base_stats(sp).values()) or 500
    s += (bst - 500) / 40
    moves = [to_id(m) for m in card.get("moves", [])]
    s += sum(SUPPORT_MOVES.get(m, 0) for m in moves)
    s += min(1.4, 0.7 * sum(dex.move(m).get("target") in SPREAD_TARGETS for m in moves))
    s += ABILITIES.get(to_id(card.get("ability", "")), 0)
    # Synergy: penalize stacking weaknesses and duplicate types.
    mine = dex.types_of(sp)
    team_types = [dex.types_of(c.get("species", "")) for c in roster]
    for t in mine:
        s -= 0.6 * sum(t in tt for tt in team_types)
    for atk_type in ("FIRE", "WATER", "GRASS", "ELECTRIC", "GROUND", "ICE", "FIGHTING", "FAIRY", "DARK",
                     "GHOST", "DRAGON", "STEEL", "FLYING", "ROCK", "BUG", "POISON", "PSYCHIC"):
        if effectiveness(atk_type, mine) >= 2:
            weak = sum(effectiveness(atk_type, tt) >= 2 for tt in team_types)
            if weak >= 2:
                s -= 0.8
    return s


# ---- exact-damage helpers ---------------------------------------------------------------------------
def _types(mon: dict | None) -> list[str]:
    mon = mon or {}
    return [str(x).upper() for x in (mon.get("types") or dex.types_of(mon.get("species", "")))]


def _damaging(m: dict, opt: dict | None = None) -> bool:
    cat = (m.get("category") or (opt or {}).get("category") or "").lower()
    if cat:
        return cat != "status"
    return bool(m.get("basePower") or (opt or {}).get("base_power"))


def _acc_of(m: dict, opt: dict | None = None) -> float:
    """Accuracy as a probability; the platform sends 0-1 floats, the dex 0-100 ints or True."""
    a = (opt or {}).get("accuracy")
    if a is None:
        a = m.get("accuracy", 100)
    if a is True or a is None:
        return 1.0
    try:
        a = float(a)
    except (TypeError, ValueError):
        return 1.0
    return a / 100 if a > 1 else a


def _cur_hp(mon: dict | None, res: dict) -> int:
    """Defender's current HP on the calculator's own scale (the platform reports foe HP as a percentage)."""
    frac = (mon or {}).get("current_hp_fraction")
    try:
        frac = 1.0 if frac is None else float(frac)
    except (TypeError, ValueError):
        frac = 1.0
    return max(1, int(round(min(1.0, max(0.0, frac)) * (res.get("defender_hp") or 1))))


def _hit_stats(rolls, cur: int, sash: bool = False) -> tuple[float, float]:
    """(expected fraction of the defender's current HP removed, P(KO)) given that the move hits.
    `sash`: Focus Sash / Sturdy at full HP, so a lone hit leaves the defender on 1 HP (never a KO)."""
    n = len(rolls) or 1
    if sash and cur > 1:
        return sum(min(r, cur - 1) for r in rolls) / n / cur, 0.0
    return sum(min(r, cur) for r in rolls) / n / cur, sum(r >= cur for r in rolls) / n


def _points(rolls, cur: int, sash: bool = False) -> float:
    useful, pko = _hit_stats(rolls, cur, sash)
    return 100.0 * useful + KO_BONUS * pko


def _pair_rolls(ra, rb, cur: int, sash_a: bool = False, sash_b: bool = False) -> list:
    """Total damage of two hits on the same defender. Behind a Sash the first damaging hit leaves 1 HP and the
    second finishes it; only a lone damaging hit (the other did 0) is stopped."""
    if not (sash_a or sash_b):
        return [x + y for x in ra for y in rb]
    cap = cur - 1
    return [x + y if x > 0 and y > 0 else (min(x, cap) if sash_a else x) if y <= 0 else (min(y, cap) if sash_b else y)
            for x in ra for y in rb]


def _pair_points(a: tuple, b: tuple, cur: int, sash_a: bool = False, sash_b: bool = False) -> float:
    """Value of two attacks (rolls, acc) landing on the same defender this turn: joint KO odds, no double counting.
    sash_a / sash_b: the defender's Focus Sash / Sturdy stops a lone hit of a / b (False for multi-hit moves)."""
    ra, pa = a
    rb, pb = b
    both = _points(_pair_rolls(ra, rb, cur, sash_a, sash_b), cur)
    return pa * pb * both + pa * (1 - pb) * _points(ra, cur, sash_a) + (1 - pa) * pb * _points(rb, cur, sash_b)


def _adjust_rolls(rolls, m: dict, mid: str, mon: dict | None, card: dict | None, sun: bool) -> list:
    """Per-turn expected damage of charge / recharge moves (a two-turn move is worth about half)."""
    f = 1.0
    flags = m.get("flags") or {}
    item = to_id((mon or {}).get("item") or (card or {}).get("item") or "")
    if flags.get("charge") and item != "powerherb" and not (sun and mid in ("solarbeam", "solarblade")):
        f *= 0.45
    if flags.get("recharge"):
        f *= 0.65
    return [r * f for r in rolls] if f != 1.0 else list(rolls)


def _flinch_blocked(o: dict, card: dict | None, obs: dict) -> bool:
    ability = to_id(o.get("ability") or (card or {}).get("ability") or "")
    item = to_id((card or {}).get("item") or "")
    if o.get("item") and o.get("item") != "unknown_item":
        item = to_id(o["item"])
    if ability in FLINCH_IMMUNE_ABILITIES or item == "covertcloak":
        return True
    if calc_field(obs, True).get("terrain") == "Psychic" and "FLYING" not in _types(o) and ability != "levitate":
        return True
    return False


def _side_fainted(obs: dict, key: str) -> int:
    """Fainted Pokémon on one side, from the `team` / `opponent_team` views (dicts keyed "p1: Name")."""
    team = obs.get(key)
    if isinstance(team, dict):
        return sum(1 for v in team.values() if isinstance(v, dict) and v.get("fainted"))
    return 0


def _log_fallen(obs: dict) -> dict | None:
    """Supreme Overlord's locked-in counts from the protocol log: {(side, species id): fallen} for every Pokémon
    whose latest switch-in is in the log (0 unless a `|-start|<pos>|fallenN` line followed it; Showdown fixes the
    count on entry, later faints don't raise it). None when there is no log or our side id is unknown."""
    lines = obs.get("protocol_log")
    if not isinstance(lines, list) or not lines:
        return None
    mine = next((str(k).split(":")[0].strip() for k in (obs.get("team") or {})), None)
    if mine not in ("p1", "p2"):
        return None
    pos_sp: dict[str, str] = {}
    out: dict[tuple, int] = {}
    for line in lines:
        if not isinstance(line, str) or not line.startswith("|"):
            continue
        parts = line.split("|")
        if len(parts) < 4:
            continue
        tag, pos = parts[1], parts[2].split(":")[0].strip()
        if tag in ("switch", "drag", "replace"):
            sp = to_id(parts[3].split(",")[0])
            pos_sp[pos] = sp
            out[("me" if pos[:2] == mine else "opp", sp)] = 0
        elif tag == "-start" and parts[3].startswith("fallen"):
            sp = pos_sp.get(pos) or to_id(parts[2].split(":", 1)[-1])
            digits = "".join(ch for ch in parts[3] if ch.isdigit())
            out[("me" if pos[:2] == mine else "opp", sp)] = min(5, int(digits or 0))
    return out


def _target_status_value(mid: str, o: dict, card: dict | None) -> float:
    """Status moves aimed at a foe (the v0 heuristic gave these 0)."""
    types, status = _types(o), o.get("status")
    ability = to_id(o.get("ability") or (card or {}).get("ability") or "")
    if mid in ("spore", "sleeppowder", "stunspore"):
        if "GRASS" in types or ability == "overcoat" or status:
            return 0.0
        return 55.0 if mid == "spore" else 25.0
    if mid == "willowisp":
        bs = o.get("base_stats") or dex.base_stats(o.get("species", ""))
        return 0.0 if "FIRE" in types or status else (35.0 if bs.get("atk", 0) >= bs.get("spa", 0) else 15.0)
    if mid == "thunderwave":
        return 0.0 if "ELECTRIC" in types or "GROUND" in types or status else 20.0
    if mid in ("taunt", "encore", "disable"):
        return 10.0
    return 8.0


def _first(p) -> float:
    """acts_first() result as a probability (speed tie = coin flip)."""
    return 1.0 if p is True else 0.0 if p is False else 0.5


def _cond_ids(x) -> set[str]:
    """Side conditions / fields as ids; the platform sends dicts ({"TAILWIND": turn}), the local sim lists too."""
    if isinstance(x, (dict, list, tuple, set)):
        return {to_id(str(k)) for k in x}
    return set()


def _speed_view(mon: dict | None) -> dict:
    """A battle view safe for stats.effective_speed / move_priority: hidden foe item dropped, boosts a dict, status an
    id ("PAR" on the platform, "par" in the local sim)."""
    v = dict(mon or {})
    if not v.get("item") or to_id(str(v.get("item"))) in ("unknownitem", "unknown", "none"):
        v["item"] = None
    if not isinstance(v.get("boosts"), dict):
        v["boosts"] = {}
    v["status"] = to_id(str(v.get("status") or "")) or None
    return v


def _sashed(mon: dict | None, card: dict | None) -> bool:
    """Focus Sash / Sturdy at full HP: no single hit KOs it."""
    mon = mon or {}
    try:
        frac = mon.get("current_hp_fraction")
        full = frac is None or float(frac) >= 1.0
    except (TypeError, ValueError):
        full = True
    if not full:
        return False
    # Hidden item -> the drafted card's; a slot the view shows as empty (used Sash) stays empty: the same rule as the
    # calculator's input (calc.view_item), whose calc.js caps a full-HP Sash / Sturdy hit at max HP - 1 itself.
    item = view_item(mon, card)
    ability = mon.get("ability") or (card or {}).get("ability")
    return to_id(item or "") in SASH_ITEMS or to_id(ability or "") in SASH_ABILITIES


def _pko(rolls, acc: float, cur: int, sash: bool = False) -> float:
    """P(one hit KOs a defender with `cur` HP left), accuracy included."""
    if sash or not rolls:
        return 0.0
    return acc * sum(r >= cur for r in rolls) / len(rolls)


def _pair_ko(a: tuple, b: tuple, cur: int, sash_a: bool = False, sash_b: bool = False) -> float:
    """P(two hits on the same defender KO it this turn), accuracy included (a Sash only stops a lone hit)."""
    ra, pa = a
    rb, pb = b
    both = _pair_rolls(ra, rb, cur, sash_a, sash_b)
    both = sum(t >= cur for t in both) / (len(both) or 1)
    return pa * pb * both + pa * (1 - pb) * _pko(ra, 1.0, cur, sash_a) + (1 - pa) * pb * _pko(rb, 1.0, cur, sash_b)


def _breaks_sash(mid: str) -> bool:
    """Multi-hit moves: the first hit leaves a Sash / Sturdy mon on 1 HP, the next one KOs it."""
    return bool(dex.move(mid).get("multihit"))


def _sash_for(foe_sash: dict, c: dict, j: int) -> bool:
    """Does foe j's full-HP Focus Sash / Sturdy stop candidate c's lone hit (multi-hit moves break it)?"""
    return bool(foe_sash.get(j)) and not c.get("mh")


def _p_act(c: dict, other: dict) -> float:
    """P(candidate c's slot acts this turn): no foe KOs / Fake-Outs it first, unless the partner (`other`) removes or
    flinches that foe before it moves."""
    if not c["thr"]:
        return c["pa1"]
    p = 1.0
    for j, t in c["thr"].items():
        p *= 1.0 - t * (1.0 - other["nb"].get(j, 0.0))
    return p


# The partner of a lone slot in the joint scoring (acts never, scores 0).
_DUMMY_CAND = {"cand": None, "base": 0.0, "solo": 0.0, "foe": {}, "flinch": None, "slot": -1, "acts": False,
               "kind": "none", "pa1": 1.0, "thr": {}, "pf": {}, "ko": {}, "kob": {}, "use": {}, "nb": {}, "mid": ""}


class PokemonPolicy:
    def __init__(self):
        self.cards: dict[str, dict] = {}        # our drafted sets by species id
        self.opp_cards: dict[str, dict] = {}    # opponent's drafted sets by species id
        self.seen: dict[str, dict] = dex.card_catalog()   # card_id -> full set (real catalog + this draft)
        self.prev_active: list[str | None] = [None, None]
        self.me: str | None = None
        self.last_exact: bool = False           # did the last battle decision use the exact calculator?
        self.opp_prev_active: list[str | None] = [None, None]   # foes on the field at our previous decision
        self.last_protect: dict[str, int] = {}  # our species -> turn it last chose Protect (no Protect chains)
        self.last_lookahead: dict | None = None  # diagnostics of the last joint lookahead (None = not used)
        # Supreme Overlord without a protocol log: the side's fainted count when each Pokémon was first seen active.
        self._active_sets: dict[str, set] = {}
        self._entry_fallen: dict[tuple, int] = {}
        self.prev_opp: list[str | None] = [None, None]
        self.last_move: dict[int, tuple[str, str | None]] = {}   # slot -> (species, move id) of our last action
        self.cond_set: dict[str, int] = {}      # "opp_tailwind" / "my_tailwind" / "trickroom" -> turn it was set
        self.last_speed: dict | None = None     # speed model of the last doubles decision (debugging / tests)
        self.debug_pairs: list | None = None    # set to [] to collect (value, slot-0 choice, slot-1 choice) per pair
        self.draft_pool: dict[str, dict] = {}   # card_id -> full set of every card offered in THIS match's draft
        self.draft_info: dict = {}              # last drafter search summary (depth, nodes, ms, top picks)
        self.drafter_kwargs: dict = {}          # Drafter(...) overrides (node budget / max_depth / top_k), experiments only
        self.last_timing: dict | None = None     # last doubles decision: exact table / lookahead / total ms
        self.warmed = False                      # did this policy already warm the damage calculator?
        # Singles (agent/pokemon/singles.py): game type of the current state, per-battle memory, last scores / reason.
        self.game_type: str | None = None
        self.singles_prev: dict = {}
        self.singles_last: list | None = None
        self.singles_why: str | None = None
        self.lookahead_skipped = 0               # doubles decisions whose deadline left no time for the lookahead
        self.degraded: list[str] = []            # time-control degradations of the current match (diagnostics)
        self._reported: set = set()              # unknown phases already reported on stderr
        # Engine search (ARENA_BATTLE=search): mode, the foe's six and the preview game's prior over their lineups,
        # foes seen on the field this battle, per-match counters and the last search's diagnostics.
        self.battle_mode = battle_mode()
        self.opp_roster: list[str] = []
        self.lineup_prior: list | None = None
        self.seen_foes: list[str] = []
        self.last_search: dict | None = None
        self.search_stats = {"decisions": 0, "searched": 0, "used": 0, "same_as_classic": 0, "fallback": 0,
                             "skipped_time": 0, "reasons": {}}
        self.search_ms: list[float] = []         # wall time of every search round trip (latency diagnostics)
        self.search_trace: list[dict] = []       # the last 400 searches' summaries (diagnostics)
        self.oracle = None                       # local ablation only: callable -> the true battle as JSON

    def _warm_calc(self) -> None:
        """Pay the node + @smogon/calc start-up before the first battle decision (the draft, teambuild and team
        preview phases have time to spare), once per policy. Never raises."""
        if self.warmed:
            return
        self.warmed = True
        try:
            DamageCalc.get().warm()
        except Exception:
            log.warning("damage calc warm-up failed", exc_info=True)

    def _warm_search(self, wait: bool) -> None:
        """ARENA_BATTLE=search: spawn the engine's node process during the draft (it loads in the background) and, at
        team preview, wait a little for it, so the first battle decision does not pay the ~1 s load. VGC doubles
        only. Never raises."""
        try:
            game = to_id(self.game_type or "")
            if game and "vgc" not in game and "doubles" not in game:
                return
            from .engine import SearchEngine
            eng = SearchEngine.get()
            if not wait:
                eng.start_background()
                return
            limit = min(5.0, timecontrol.cap(5.0, 0.3)) if timecontrol.current() is not None else 5.0
            if limit > 0.05:
                eng.warm(limit)
        except Exception:
            log.warning("engine search warm-up failed", exc_info=True)

    def _track_draft(self, obs: dict, me: str | None) -> None:
        """Full sets are only visible while a card is still in `available_cards`; rosters/picks carry just
        card_id + species and team preview hides items/moves. Remember every set we ever see."""
        for c in obs.get("available_cards") or []:
            self.seen[c["card_id"]] = c
        for seat, roster in (obs.get("rosters") or {}).items():
            target = self.cards if seat == me else self.opp_cards
            for r in roster:
                full = self.seen.get(r.get("card_id"), {"species": r.get("species", "")})
                target[to_id(full.get("species", r.get("species", "")))] = full

    def opponent_set(self, species: str) -> dict | None:
        return self.opp_cards.get(to_id(species))

    def foe_moves(self, o: dict | None) -> list[str]:
        """A foe's moves: its drafted set, else what it revealed, else one guessed STAB attack per type."""
        o = o or {}
        card = self.opponent_set(o.get("species", "")) or {}
        moves = [to_id(m) for m in (card.get("moves") or o.get("moves") or [])[:4] if m]
        return moves or _guess_moves(to_id(o.get("species", "")))

    def _fallen_counts(self, obs: dict, actives: list, opps: list) -> dict:
        """{("me"|"opp", slot): fainted allies Supreme Overlord counts for that active}. Exact from the protocol log
        (the platform sends it); otherwise the side's fainted count when we first saw the Pokémon active (can be one
        too high if an ally fainted in the same turn it switched in). {} on any error: the calc then assumes 0."""
        try:
            sides = (("me", list(actives or []), "team"), ("opp", list(opps or []), "opponent_team"))
            for side, mons, key in sides:
                now = {to_id(m.get("species", "")) for m in mons
                       if isinstance(m, dict) and m.get("species") and not m.get("fainted")}
                n = _side_fainted(obs, key)
                for sp in now - self._active_sets.get(side, set()):
                    self._entry_fallen[(side, sp)] = n
                self._active_sets[side] = now
            from_log = _log_fallen(obs)
            out = {}
            for side, mons, _ in sides:
                for i, m in enumerate(mons[:2]):
                    if not isinstance(m, dict) or not m.get("species"):
                        continue
                    k = (side, to_id(m["species"]))
                    n = from_log[k] if from_log is not None and k in from_log else self._entry_fallen.get(k, 0)
                    if n:
                        out[(side, i)] = n
            return out
        except Exception:
            log.warning("fainted-ally tracking failed; Supreme Overlord assumed off", exc_info=True)
            return {}

    @staticmethod
    def _card(store: dict, mon: dict | None) -> dict:
        """The drafted set of a Pokémon for the calculator, or a generic 252/252 spread when unknown."""
        card = store.get(to_id((mon or {}).get("species", ""))) or {}
        out = dict(GENERIC_CARD)
        out.update({k: v for k, v in card.items() if v not in (None, {}, [])})
        return out

    # ---- dispatch ------------------------------------------------------
    def act(self, st: dict) -> dict:
        obs = st.get("observation") or {}
        if not isinstance(obs, dict):
            raise TypeError(f"observation is a {type(obs).__name__}, not a dict")
        phase = st.get("phase")
        if st.get("game_type"):
            self.game_type = str(st.get("game_type"))
        if phase in ("draft", "teambuild", "team_preview"):
            self._warm_calc()
            if self.battle_mode == "search" and phase in ("draft", "team_preview"):
                self._warm_search(wait=phase == "team_preview")
        if phase == "draft":
            return self._draft(st, obs)
        if phase == "teambuild":
            return self._teambuild(obs)
        if phase == "team_preview":
            return self._preview(obs, str(st.get("session_id") or ""))
        if phase not in BATTLE_PHASES:
            kind = _battle_kind(st, obs)
            if kind is None:                    # an unknown phase that is not a battle turn: the safe fallback
                self._report(("phase", phase), "unknown Pokémon phase %r: answering with the safe fallback", phase)
                return self.fallback(st)
            self._report(("phase", phase), "unknown Pokémon phase %r looks like a %s turn: playing it", phase, kind)
            return self._doubles(st, obs) if kind == "doubles" else self._singles(st, obs)
        if obs.get("is_doubles"):
            return self._doubles(st, obs)
        return self._singles(st, obs)

    def _report(self, key, msg: str, *args) -> None:
        if key not in self._reported:
            self._reported.add(key)
            log.error(msg, *args)

    def fallback(self, st: dict) -> dict:
        la = (st.get("legal_actions") or {}).get("actions") or []
        obs = st.get("observation") or {}
        if st.get("phase") == "moving" and obs.get("is_doubles") and la:
            tmpl = la[0]["input"]["action"]
            pick, used = {}, set()
            for slot in tmpl.get("slots", []):
                pick[f"slot_{slot['slot']}"] = {"type": "pass"}
                for opt in slot.get("options", []):
                    if opt.get("type") == "switch" and to_id(opt.get("species")) in used:
                        continue
                    pick[f"slot_{slot['slot']}"] = _concrete(opt)
                    foes = [t for t in (opt.get("targets") or []) if isinstance(t, int) and t > 0]
                    if opt.get("type") == "move" and foes:      # never the first listed target: our own partner
                        pick[f"slot_{slot['slot']}"]["target"] = foes[0]
                    if opt.get("type") == "switch":
                        used.add(to_id(opt.get("species")))
                    break
            return {"action": {"type": "doubles_turn", **pick}}
        if st.get("phase") == "team_preview":
            roster = [to_id(p.get("species", "")) for p in obs.get("your_roster", [])]
            return {"action": {"type": "select_lineup", "bring": roster[:4], "leads": roster[:2]}}
        return {k: v for k, v in la[0]["input"].items() if k not in ("session_id", "state_version")}

    # ---- teambuild (pokemon_gen9ou_teambuild) ---------------------------
    def _teambuild(self, obs: dict) -> dict:
        """DEFAULT_OU_TEAM first; after a rejected team (the runner dies on INVALID_ACTION, the next worker sees
        `submit_attempts_failed` > 0) the next of OU_TEAMS, so a ban or catalog quirk costs one attempt, not all eight
        (after 8 the platform assigns its fallback team). Every set is remembered at level 100 for the calculator."""
        try:
            failed = max(0, int(obs.get("submit_attempts_failed") or 0))
        except (TypeError, ValueError):
            failed = 0
        team = OU_TEAMS[failed % len(OU_TEAMS)]
        for s in team:
            self.cards.setdefault(to_id(s["species"]), {**s, "level": 100})
        return {"action": {"type": "submit_team", "team": team}}

    # ---- draft ---------------------------------------------------------
    def _draft(self, st: dict, obs: dict) -> dict:
        me = (st.get("current_actor") or {}).get("agent_id") or obs.get("current_seat")
        self.me = me
        self._track_draft(obs, me)
        for c in obs.get("available_cards") or []:           # this match's pool (item-blocked cards included)
            if isinstance(c, dict) and c.get("card_id"):
                self.draft_pool[c["card_id"]] = c
        try:                    # boot the calculator's node process now, so team preview doesn't pay its start-up
            DamageCalc.get()._start()
        except Exception:
            log.debug("calc warm-up failed", exc_info=True)
        taken = {to_id(c.get("species", "")) for c in (obs.get("rosters") or {}).get(me, [])}
        roster = [self.cards[s] for s in taken if s in self.cards] or [
            {"species": c.get("species", "")} for c in (obs.get("rosters") or {}).get(me, [])]
        legal_ids = {a["input"].get("action", {}).get("card_id") or a["action_id"].split(":", 1)[-1]
                     for a in (st.get("legal_actions") or {}).get("actions", [])}
        cards = [c for c in obs.get("available_cards", []) if c["card_id"] in legal_ids] \
            or obs.get("available_cards", [])
        best = max(cards, key=lambda c: card_score(c, roster))
        why = f"{best['species']}: best value + synergy with current roster"
        drafter_time = timecontrol.cap(drafter_mod.SAFETY_WALL_S, DRAFTER_SHARE)
        if USE_DRAFTER and timecontrol.current() is not None and drafter_time < MIN_DRAFTER_S:
            log.warning("no time left for the drafter (%.2fs); keeping the card_score pick", drafter_time)
            self.degraded.append("draft:card_score")
        elif USE_DRAFTER:
            try:
                cid = self._drafter_pick(obs, me, legal_ids)
                hit = next((c for c in cards if c["card_id"] == cid), None)
                if hit is not None:
                    best = hit
                    why = f"{best['species']}: drafter {self.draft_info}"
            except Exception:
                log.warning("drafter failed; keeping the card_score pick", exc_info=True)
        self.cards[to_id(best["species"])] = best
        return {"action": {"type": "draft_pick", "card_id": best["card_id"]},
                "reasoning_summary": why[:300]}

    def _drafter_pick(self, obs: dict, me: str | None, legal_ids: set) -> str:
        """Search the rest of the draft (agent/pokemon/drafter.py). Raises on anything unexpected."""
        from .drafter import Drafter
        rosters = {seat: list(ro or []) for seat, ro in (obs.get("rosters") or {}).items()}
        for p in obs.get("picks") or []:                     # `picks` backs up `rosters` if one is incomplete
            seat, cid = p.get("seat_id"), p.get("card_id")
            if seat is not None and cid and all(r.get("card_id") != cid for r in rosters.get(seat, [])):
                rosters.setdefault(seat, []).append({"card_id": cid, "species": p.get("species", "")})
        taken_ids = {r.get("card_id") for ro in rosters.values() for r in ro}

        def full(r: dict) -> dict:
            cid = r.get("card_id")
            c = self.draft_pool.get(cid) or self.seen.get(cid)
            return c if c else {"card_id": cid, "species": r.get("species", "")}

        mine = [full(r) for r in rosters.get(me) or []]
        opp = [full(r) for seat, ro in rosters.items() if seat != me for r in ro]
        pool = [c for cid, c in self.draft_pool.items() if cid not in taken_ids]
        n_taken = len(mine) + len(opp)
        pick_no = obs.get("pick_number")
        if isinstance(pick_no, int) and 1 <= pick_no <= 12:
            n_taken = pick_no - 1                             # the platform's own count decides whose turn it is
        if USE_SINGLES and SINGLES_DRAFTER and self._singles_format(obs):
            # OU singles draft: the fitted model knows VGC cards only; drive the search by the calc duels (counters)
            # and a singles prior instead of the VGC card_score.
            from . import singles
            from .drafter import DraftModel
            d = Drafter(model=DraftModel(singles.SINGLES_DRAFT_MODEL), prior_score=singles.card_value, singles=True,
                        **self.drafter_kwargs)
        else:
            d = Drafter(prior_score=lambda c: card_score(c, []), **self.drafter_kwargs)
        cid = d.pick(pool, mine, opp, legal_ids, n_taken=n_taken)
        self.draft_info = d.info
        return cid

    # ---- team preview --------------------------------------------------
    def _preview(self, obs: dict, session_id: str = "") -> dict:
        """Matrix-game lineup (agent/pokemon/preview.py); the v0/v1 heuristic on any failure or timeout."""
        self._new_battle()               # whichever path answers: team preview starts a new battle
        try:
            # Picks made after our last draft turn never reached us as a draft state: resolve by species (the
            # battle's opponent_set() needs these whichever preview path answers).
            by_species = {to_id(c.get("species", "")): c for c in self.seen.values()}
            for p in obs.get("opponent_roster") or []:
                sid = to_id(p.get("species", ""))
                if sid not in self.opp_cards and sid in by_species:
                    self.opp_cards[sid] = by_species[sid]
                elif sid not in self.opp_cards and not preview_matrix.resolve_card(sid, self.opp_cards):
                    # masked forme at preview ("urshifu" for Urshifu-Rapid-Strike): battle views use the full id
                    card = preview_matrix.resolve_card(sid, {}, by_species)
                    if card:
                        self.opp_cards[to_id(card.get("species", ""))] = card
        except Exception:
            log.debug("opponent set recovery failed", exc_info=True)
        try:
            self.opp_roster = []
            for p in obs.get("opponent_roster") or []:
                sid = to_id(p.get("species", ""))
                card = preview_matrix.resolve_card(sid, self.opp_cards)
                self.opp_roster.append(to_id((card or {}).get("species") or sid))
        except Exception:
            log.debug("opponent roster capture failed", exc_info=True)
        try:
            out = self._preview_matrix(obs, session_id)
            if out is not None:
                return out
        except Exception as e:
            log.warning("preview matrix failed; using the heuristic preview", exc_info=True)
            self.degraded.append(f"preview:{type(e).__name__}")
        if USE_SINGLES and self._singles_format(obs):
            try:
                from . import singles
                out = singles.preview(self, {"game_type": self.game_type}, obs)
                if out is not None:
                    return out
            except Exception:
                log.warning("singles preview failed; using the heuristic preview", exc_info=True)
        return self._preview_heuristic(obs)

    def _singles_format(self, obs: dict) -> bool:
        """A singles game type (every Pokémon type but the VGC doubles draft): the game type if known, else the
        preview's battle format (the same test `_preview_matrix` uses to skip singles)."""
        game = to_id(self.game_type or "")
        if game:
            return "vgc" not in game and "doubles" not in game
        fmt = to_id(obs.get("battle_format") or "")
        return bool(fmt) and "vgc" not in fmt and "doubles" not in fmt

    def _new_battle(self) -> None:
        """Forget per-battle memory at team preview. calc-fixes' Supreme Overlord entry counts used to be cleared at
        the top of the old `_preview`, which preview-matrix renamed to `_preview_heuristic`: there the reset only ran
        when the matrix path failed, so the counts leaked into the next battle. The battle trackers of the lookahead
        (Protect chains, foes seen last turn) and of speed-protect (last action per slot, Tailwind / Trick Room start
        turns, who was on the field) are per battle too and are reset with them. Draft knowledge (our / their sets)
        is kept: it belongs to the match."""
        self._active_sets.clear()
        self._entry_fallen.clear()
        self.prev_active = [None, None]
        self.prev_opp = [None, None]
        self.opp_prev_active = [None, None]
        self.last_protect.clear()
        self.last_move.clear()
        self.cond_set.clear()
        self.seen_foes = []
        self.lineup_prior = None

    def _preview_matrix(self, obs: dict, session_id: str) -> dict | None:
        fmt = to_id(obs.get("battle_format") or "")
        if (fmt and "vgc" not in fmt and "doubles" not in fmt) or self._singles_format(obs):
            return None                     # the race models VGC doubles; a singles game type never uses it, even
                                            # when its preview state carries no battle_format
        mine = [p for p in (obs.get("your_roster") or []) if isinstance(p, dict) and p.get("species")]
        opp = [p for p in (obs.get("opponent_roster") or []) if isinstance(p, dict) and p.get("species")]
        if not (4 <= len(mine) <= 6 and 4 <= len(opp) <= 6):
            return None
        sids = [to_id(p["species"]) for p in mine]
        if len(set(sids)) != len(sids):
            return None
        by_species = {to_id(c.get("species", "")): c for c in self.seen.values() if isinstance(c, dict)}
        my_cards = [preview_matrix.resolve_card(s, self.cards, by_species) for s in sids]
        opp_cards = [preview_matrix.resolve_card(p["species"], self.opp_cards, by_species) for p in opp]
        seed = f"{session_id}|{','.join(sids)}|{','.join(to_id(p['species']) for p in opp)}"
        knobs = None
        if timecontrol.current() is not None:        # the matrix game never runs past the preview's deadline
            wall = timecontrol.cap(float(preview_matrix.KNOBS.get("wall_cap_s", 6.0)), PREVIEW_SHARE)
            if wall < MIN_PREVIEW_S:
                log.warning("no time left for the preview matrix game (%.2fs); heuristic preview", wall)
                self.degraded.append("preview:heuristic")
                return None
            if wall < float(preview_matrix.KNOBS.get("wall_cap_s", 6.0)):
                knobs = {"wall_cap_s": wall}
        guess = None
        if float(preview_matrix.KNOBS.get("prior_w", 0) or 0) > 0:
            try:
                guess = self._guess_opponent_lineup(mine, opp, opp_cards)
            except Exception:
                log.debug("opponent lineup guess failed", exc_info=True)
        res = preview_matrix.choose(mine, my_cards, opp, opp_cards, seed_text=seed, opp_guess=guess, knobs=knobs)
        if res is None:
            return None
        try:        # the foe's equilibrium lineup mix: the engine search's prior for the four they brought
            lus = preview_matrix.lineups(len(opp))
            ids = [to_id((c or {}).get("species") or p["species"]) for p, c in zip(opp, opp_cards)]
            self.lineup_prior = [(tuple(ids[k] for k in lus[c]), float(q))
                                 for c, q in zip(res.get("cols") or [], res.get("pc") or []) if q > 1e-3]
        except Exception:
            self.lineup_prior = None
        # Echo the roster's own species strings (the platform validates bring/leads against them).
        bring = [mine[i]["species"] for i in res["bring"]]
        leads = [mine[i]["species"] for i in res["leads"]]
        if len(set(bring)) != 4 or len(set(leads)) != 2 or not set(leads) <= set(bring):
            return None
        info = res.get("info") or {}
        return {"action": {"type": "select_lineup", "bring": bring, "leads": leads},
                "reasoning_summary": f"preview matrix game: bring {bring}, lead {leads} "
                                     f"(value {info.get('value')}, p {info.get('p')}, {info.get('t_total')}s)"}

    @staticmethod
    def _guess_opponent_lineup(mine: list[dict], opp: list[dict], opp_cards: list) -> tuple | None:
        """The lineup the opponent would pick with our own heuristic preview (a prior for a typical agent), as
        opponent-roster indices (lead, lead, bench, bench). They see our masked formes, as we see theirs."""
        other = PokemonPolicy()
        other.seen = {}
        sids = []
        for p, c in zip(opp, opp_cards):
            sid = to_id((c or {}).get("species") or p.get("species", ""))
            sids.append(sid)
            if c:
                other.cards[sid] = c
        seen_by_them = []
        for p in mine:
            sid = to_id(p.get("species", ""))
            base = preview_matrix.base_species(sid)
            seen_by_them.append({"species": base, "types": dex.types_of(base)} if base != sid
                                else {"species": sid, "types": p.get("types") or dex.types_of(sid)})
        out = other._preview_heuristic({"your_roster": [{"species": s} for s in sids],
                                        "opponent_roster": seen_by_them})["action"]
        idx = {s: k for k, s in enumerate(sids)}
        leads = [idx[to_id(s)] for s in out["leads"]]
        rest = [idx[to_id(s)] for s in out["bring"] if idx[to_id(s)] not in leads]
        return tuple(leads + rest) if len(leads) == 2 and len(rest) == 2 else None

    def _preview_heuristic(self, obs: dict) -> dict:
        """v0/v1 preview (card value + base-stat offense into their types): the fallback."""
        mine = obs.get("your_roster", [])
        opp = obs.get("opponent_roster", [])
        # Picks made after our last draft turn never reached us as a draft state: resolve by species.
        by_species = {to_id(c.get("species", "")): c for c in self.seen.values()}
        for p in opp:
            sid = to_id(p.get("species", ""))
            if sid not in self.opp_cards and sid in by_species:
                self.opp_cards[sid] = by_species[sid]
        opp_types = [[t.upper() for t in (p.get("types") or dex.types_of(p.get("species", "")))] for p in opp]

        def value(p: dict) -> float:
            sp = to_id(p.get("species", ""))
            card = self.cards.get(sp, {"species": sp, "moves": p.get("moves", [])})
            base = card_score(card, [])
            moves = [to_id(m) for m in card.get("moves", [])]
            offense = sum(max((move_value(m, sp, ot) for m in moves), default=0) for ot in opp_types)
            return base + offense / max(1, len(opp_types)) / 40

        ranked = sorted(mine, key=value, reverse=True)
        bring = [to_id(p["species"]) for p in ranked[:4]]

        def lead_value(sid: str) -> float:
            card = self.cards.get(sid, {})
            moves = [to_id(m) for m in card.get("moves", [])]
            return (3 if "fakeout" in moves else 0) + (2 if to_id(card.get("ability", "")) == "intimidate" else 0) \
                + (1.5 if "tailwind" in moves else 0) + value({"species": sid}) / 5

        leads = sorted(bring, key=lead_value, reverse=True)[:2]
        return {"action": {"type": "select_lineup", "bring": bring, "leads": leads},
                "reasoning_summary": f"bring {bring}, lead {leads}"}

    # ---- battle: exact damage table ------------------------------------
    def _exact_table(self, obs: dict, tmpl: dict, actives: list, opps: list) -> dict | None:
        """Every damage calc of this decision in ONE DamageCalc.batch(): each (our slot, move) against each live foe
        (and our partner for allAdjacent moves), plus each foe's known moves against our actives (threat weights).
        Keys: (attacker side, slot, move id, defender side, slot) -> calc result or None. None = calc unavailable."""
        dc = DamageCalc.get()
        if not dc.enabled:
            return None
        try:
            f_me, f_opp = calc_field(obs, True), calc_field(obs, False)
            mine = {i: a for i, a in enumerate(list(actives)[:2]) if a and not a.get("fainted")}
            foes = {j: o for j, o in enumerate(list(opps)[:2]) if o and not o.get("fainted")}
            fallen = self._fallen_counts(obs, actives, opps)
            my_in = {i: calc_pokemon(a, self._card(self.cards, a), allies_fainted=fallen.get(("me", i)))
                     for i, a in mine.items()}
            foe_in = {j: calc_pokemon(o, self._card(self.opp_cards, o), allies_fainted=fallen.get(("opp", j)))
                      for j, o in foes.items()}
            calcs, keys = [], []

            def add(key, atk, dfn, mid, field):
                keys.append(key)
                calcs.append({"attacker": atk, "defender": dfn, "move": move_name(mid), "field": field})

            for slot in tmpl.get("slots", []):
                i = slot.get("slot")
                if i not in mine:
                    continue
                for opt in slot.get("options", []):
                    if opt.get("type") != "move":
                        continue
                    mid = to_id(opt.get("move_id", ""))
                    m = dex.move(mid)
                    if not _damaging(m, opt):
                        continue
                    tgt = m.get("target")
                    if tgt in SPREAD_TARGETS:
                        hits = len(foes) + (1 if tgt == "allAdjacent" and (1 - i) in mine else 0)
                        field = f_me if hits > 1 else {**f_me, "gameType": "Singles"}   # lone target: no spread cut
                        for j in foes:
                            add(("me", i, mid, "opp", j), my_in[i], foe_in[j], mid, field)
                        if tgt == "allAdjacent" and (1 - i) in mine:
                            add(("me", i, mid, "me", 1 - i), my_in[i], my_in[1 - i], mid, field)
                    else:
                        for j in foes:
                            add(("me", i, mid, "opp", j), my_in[i], foe_in[j], mid, f_me)
                        if (1 - i) in mine and any(isinstance(t, int) and t < 0 for t in opt.get("targets") or []):
                            # aimed at our own partner (the lookahead scores that row with its real damage);
                            # our side's screens are the defender's
                            add(("me", i, mid, "me", 1 - i), my_in[i], my_in[1 - i], mid,
                                {**f_me, "defenderSide": f_me["attackerSide"]})
            for j, o in foes.items():
                for mid in self.foe_moves(o):
                    m = dex.move(mid)
                    if not _damaging(m):
                        continue
                    tgt = m.get("target")
                    hits = len(mine) + (1 if tgt == "allAdjacent" and (1 - j) in foes else 0)
                    field = f_opp if tgt in SPREAD_TARGETS and hits > 1 else {**f_opp, "gameType": "Singles"}
                    for i in mine:
                        add(("opp", j, mid, "me", i), foe_in[j], my_in[i], mid, field)
                    if tgt == "allAdjacent" and (1 - j) in foes:            # Earthquake & co. hit their partner too
                        add(("opp", j, mid, "opp", 1 - j), foe_in[j], foe_in[1 - j], mid, field)
            res = dc.batch(calcs)
            if res is None:
                return None
            return dict(zip(keys, res))
        except Exception:
            log.warning("exact damage table failed; using the heuristic", exc_info=True)
            return None

    def _foe_weights(self, table: dict, actives: list, opps: list) -> tuple[dict, dict, dict]:
        """Per live foe j: current HP on the calc scale, danger (fraction of our current HP its best move removes)
        and the target weight 0.7..1.3 derived from it."""
        curs, danger, weights = {}, {}, {}
        for j, o in enumerate(list(opps)[:2]):
            if not o or o.get("fainted"):
                continue
            r = next((r for k, r in table.items() if k[3] == "opp" and k[4] == j and r), None)
            curs[j] = _cur_hp(o, r) if r else 1
            d = None
            for k, r in table.items():
                if k[0] == "opp" and k[1] == j and k[3] == "me" and r:
                    i = k[4]
                    me = actives[i] if i < len(actives) else None
                    useful, _ = _hit_stats(r["rolls"], _cur_hp(me, r))
                    d = max(d or 0.0, _acc_of(dex.move(k[2])) * useful)
            if d is None:
                d = min(1.0, _threat(o) / 300)
            danger[j] = d
            weights[j] = 0.7 + 0.6 * d
        return curs, danger, weights

    def _speed_model(self, obs: dict, actives: list, opps: list, table: dict) -> dict:
        """Who moves first and who KOs whom this turn, from exact stats + the damage table.

        threat[(j, i)] = (P(foe j's best move KOs our slot i), expected fraction of i's HP it removes, its priority,
        spread?) ; focus[(j, i)] = P(foe j aims at our slot i) ; fake_out[(j, i)] = P(a fresh foe j flinches i)."""
        f_me = calc_field(obs, True)
        terrain = f_me.get("terrain")
        tr = "trickroom" in _cond_ids(obs.get("fields"))
        my_side = {k: 1 for k in _cond_ids(obs.get("side_conditions"))}
        opp_side = {k: 1 for k in _cond_ids(obs.get("opponent_side_conditions"))}
        mine = {i: a for i, a in enumerate(list(actives)[:2]) if a and not a.get("fainted")}
        foes = {j: o for j, o in enumerate(list(opps)[:2]) if o and not o.get("fainted")}
        my_card = {i: self.cards.get(to_id(a.get("species", ""))) for i, a in mine.items()}
        foe_card = {j: self.opponent_set(o.get("species", "")) for j, o in foes.items()}
        my_view = {i: _speed_view(a) for i, a in mine.items()}
        foe_view = {j: _speed_view(o) for j, o in foes.items()}
        my_spe = {i: effective_speed(my_view[i], my_card[i], my_side, f_me) for i in mine}
        foe_spe = {j: effective_speed(foe_view[j], foe_card[j], opp_side, f_me) for j in foes}
        my_sash = {i: _sashed(a, my_card[i]) for i, a in mine.items()}

        threat: dict = {}
        for k, r in table.items():
            # k[3] == "me" only: the lookahead's table also holds a foe's Earthquake into ITS OWN partner
            # (("opp", j, mid, "opp", 1 - j)), which must not count as a threat to our slot 1 - j.
            if k[0] != "opp" or k[3] != "me" or not r or k[1] not in foes or k[4] not in mine:
                continue
            j, mid, i = k[1], k[2], k[4]
            if mid in FIRST_TURN_ONLY:                   # Fake Out: only its flinch matters (fake_out below)
                continue
            m = dex.move(mid)
            acc = _acc_of(m)
            cur = _cur_hp(mine[i], r)
            sash = my_sash[i] and not m.get("multihit")
            useful, _ = _hit_stats(r["rolls"], cur, sash)
            pko = _pko(r["rolls"], acc, cur, sash)
            pts = 100.0 * acc * useful + KO_BONUS * pko
            prio = move_priority(mid, foe_view[j], foe_card[j], terrain)
            by_prio = dict(threat[(j, i)][5]) if (j, i) in threat else {}
            by_prio[prio] = max(by_prio.get(prio, 0.0), pko)          # a priority move may KO us first
            if (j, i) not in threat or pts > threat[(j, i)][4]:
                threat[(j, i)] = (pko, acc * useful, prio, m.get("target") in SPREAD_TARGETS, pts, by_prio)
            else:
                threat[(j, i)] = threat[(j, i)][:5] + (by_prio,)

        focus: dict = {}
        foe_prio: dict = {}
        for j in foes:
            pts = {i: threat[(j, i)][4] if (j, i) in threat else 0.0 for i in mine}
            for i in mine:
                t = threat.get((j, i))
                if len(mine) == 1 or (t and t[3]):
                    focus[(j, i)] = 1.0
                    continue
                o = pts[1 - i] if (1 - i) in pts else 0.0
                tot = pts[i] ** 2 + o ** 2
                focus[(j, i)] = 0.5 if tot <= 0 else min(0.9, max(0.15, pts[i] ** 2 / tot))
            aim = max(mine, key=lambda i: pts[i], default=None)
            foe_prio[j] = threat[(j, aim)][2] if aim is not None and (j, aim) in threat else 0

        fake_out: dict = {}
        for j, o in foes.items():
            sp = to_id(o.get("species", ""))
            moves = {to_id(x) for x in ((foe_card[j] or {}).get("moves") or o.get("moves") or [])}
            if "fakeout" not in moves or (j < len(self.prev_opp) and self.prev_opp[j] == sp):
                continue
            open_ = [i for i, a in mine.items() if not _flinch_blocked(a, my_card[i], obs)]
            for i in open_:
                fake_out[(j, i)] = FAKE_OUT_LIKELY / len(open_)

        # Speed control running out: which foes outspeed us only because of it (a Protect stalls those turns).
        turn = int(obs.get("turn") or 0)
        stall: dict = {}
        tw_left = self._cond_left("opp_tailwind", turn) if "tailwind" in opp_side else None
        tr_left = self._cond_left("trickroom", turn) if tr else None
        for (j, i), t in threat.items():
            if t[0] < 0.15 and t[1] < 0.4:
                continue
            now = _first(acts_first(foe_spe[j], t[2], my_spe[i], 0, tr))
            if now <= 0.5:
                continue
            if tw_left is not None and tw_left <= 2 and \
                    _first(acts_first(foe_spe[j] / 2, t[2], my_spe[i], 0, tr)) < now:
                stall[i] = True
            if tr_left is not None and tr_left <= 2 and \
                    _first(acts_first(foe_spe[j], t[2], my_spe[i], 0, False)) < now:
                stall[i] = True
        return {"tr": tr, "terrain": terrain, "my_spe": my_spe, "foe_spe": foe_spe, "threat": threat,
                "focus": focus, "foe_prio": foe_prio, "fake_out": fake_out, "stall": stall,
                "foe_sash": {j: _sashed(o, foe_card[j]) for j, o in foes.items()},
                "mine": set(mine), "foes": set(foes)}

    def _prep_cand(self, c: dict, sm: dict | None, curs: dict) -> None:
        """Per-candidate speed facts, computed once: P(it acts), P(it KOs / flinches each foe before that foe acts)."""
        c.update(pa1=1.0, thr={}, pf={}, ko={}, kob={}, use={}, nb={})
        if sm is None or not c.get("acts") or c["slot"] not in sm["mine"]:
            return
        i, prio = c["slot"], c.get("prio", 0)
        mine_spe = sm["my_spe"][i]
        pa = 1.0
        for j in sm["foes"]:
            t = sm["threat"].get((j, i))
            hit = 0.0
            if t:
                ko_first = max(pko * (1.0 - _first(acts_first(mine_spe, prio, sm["foe_spe"][j], fp, sm["tr"])))
                               for fp, pko in t[5].items())
                hit = sm["focus"].get((j, i), 0.5) * ko_first
            fo = sm["fake_out"].get((j, i), 0.0)
            if fo:
                fo *= 1.0 - _first(acts_first(mine_spe, prio, sm["foe_spe"][j], 3, sm["tr"]))
                hit = fo + (1.0 - FAKE_OUT_LIKELY) * hit
            c["thr"][j] = hit
            pa *= 1.0 - hit
        c["pa1"] = pa
        for j, (rolls, acc) in c["foe"].items():
            if j not in sm["foes"]:
                continue
            cur = curs.get(j, 1)
            sash = sm["foe_sash"].get(j, False) and not c.get("mh")
            c["pf"][j] = _first(acts_first(mine_spe, prio, sm["foe_spe"][j], sm["foe_prio"].get(j, 0), sm["tr"]))
            c["ko"][j] = _pko(rolls, acc, cur, sash)
            c["kob"][j] = c["pf"][j] * c["ko"][j]
            c["use"][j] = acc * _hit_stats(rolls, cur, sash)[0]
        for j in sm["foes"]:
            flinch = 0.0
            if c.get("flinch") == j:
                flinch = _first(acts_first(mine_spe, 3, sm["foe_spe"][j], sm["foe_prio"].get(j, 0), sm["tr"]))
            n = min(1.0, c["kob"].get(j, 0.0) + flinch)
            if n:
                c["nb"][j] = pa * n

    def _protect_value(self, c: dict, other: dict, sm: dict | None, obs: dict, turn: int) -> float:
        """Protect: worth it when this slot is likely KO'd this turn and the partner removes / cripples the threat,
        a little for the chip damage it wastes while the partner attacks, or to stall the last turns of an opposing
        Tailwind / Trick Room. Never twice in a row (any stall move last turn), never with a Choice item, never idly.
        The partner's allAdjacent self-hit (Earthquake next to us) is refunded in the joint loop."""
        i = c["slot"]
        if self._protected_last(i, c.get("species", "")) or c.get("choice"):
            return PROTECT_AGAIN                         # a Choice item would lock this mon into Protect
        if sm is None or i not in sm["mine"]:
            return _status_value(c.get("mid", "protect"), c.get("hp", 1.0), turn, obs)
        keep, v, absorb = 1.0, 0.0, 0.0
        for j in sm["foes"]:
            t = sm["threat"].get((j, i))
            if not t:
                continue
            aim = sm["focus"].get((j, i), 0.5) * (1.0 - other.get("nb", {}).get(j, 0.0))
            if other.get("acts"):                       # chip damage wasted while the partner gets a free hit
                absorb += aim * t[1] * (1.0 - t[0])     # (lethal hits are the KO-save part below)
            d = aim * t[0]
            if d <= 0:
                continue
            keep *= 1.0 - d
            helps = max(other.get("pa1", 1.0) * other.get("ko", {}).get(j, 0.0),
                        0.5 * other.get("use", {}).get(j, 0.0))
            if other.get("mid") in CRIPPLE_MOVES and other.get("target") in (0, j + 1):
                helps = max(helps, 0.5)
            v += PROTECT_SAVE * d * (PROTECT_ALONE + (1.0 - PROTECT_ALONE) * min(1.0, helps))
        try:
            hp = min(1.0, max(0.0, float(c.get("hp", 1.0))))
        except (TypeError, ValueError):
            hp = 1.0
        danger = 1.0 - keep
        if danger < PROTECT_MIN_DANGER:
            v = PROTECT_IDLE
        v += PROTECT_ABSORB * absorb
        v *= 0.3 + 0.7 * hp if v > 0 else 1.0           # saving a mon on its last few HP is worth less
        if sm["stall"].get(i, False):
            v = max(v, 0.0) + STALL_BONUS
        return v

    # ---- battle --------------------------------------------------------
    def _singles(self, st: dict, obs: dict) -> dict:
        """Singles: agent/pokemon/singles.py (sets per format, one-turn payoff table with the foe's replies, switching,
        KO races); the v3 stand-alone scorer below when it is off, fails or the calculator is unavailable."""
        if USE_SINGLES:
            try:
                from . import singles
                out = singles.decide(self, st, obs)
                if out is not None:
                    self.last_exact = True
                    return out
            except Exception:
                log.warning("singles lookahead failed; using the stand-alone scorer", exc_info=True)
        return self._singles_v3(st, obs)

    def _singles_v3(self, st: dict, obs: dict) -> dict:
        la = (st.get("legal_actions") or {}).get("actions") or []
        me_mon = obs.get("active_pokemon") or {}
        me = me_mon.get("species", "")
        opp = obs.get("opponent_active_pokemon") or {}
        opp_types = _types(opp)
        table = self._singles_table(obs, la, me_mon, opp)
        self.last_exact = table is not None
        card = self._card(self.cards, me_mon)
        sun = calc_field(obs, True).get("weather") == "Sun"
        best, best_v = None, -1.0
        for a in la:
            act = a["input"].get("action") or {}
            if act.get("type") == "move":
                mid = to_id(act.get("move_id", ""))
                m = dex.move(mid)
                extra = {"basePower": act.get("base_power"), "type": act.get("move_type"),
                         "category": act.get("category"), "accuracy": act.get("accuracy")}
                r = (table or {}).get(mid)
                if table is None:
                    v = move_value(mid, me, opp_types, extra)
                elif not _damaging(m, act):
                    v = _target_status_value(mid, opp, self.opponent_set(opp.get("species", ""))) \
                        if mid not in ("protect", "detect", "spikyshield", "silktrap", "banefulbunker") \
                        else _status_value(mid, me_mon.get("current_hp_fraction", 1.0) or 1.0, obs.get("turn", 0), obs)
                elif r:
                    sash = not m.get("multihit") and _sashed(opp, self.opponent_set(opp.get("species", "")))
                    v = _acc_of(m, act) * _points(_adjust_rolls(r["rolls"], m, mid, me_mon, card, sun), _cur_hp(opp, r),
                                                  sash)
                    if m.get("selfdestruct"):
                        v -= SELF_KO_PENALTY
                else:
                    v = move_value(mid, me, opp_types, extra) * HEURISTIC_TO_POINTS
            else:
                # v3 took the FIRST switch on a forced switch; now the one that resists the foe / hits it hardest
                v = 5.0 + _singles_switch_score(act, obs) if obs.get("force_switch") else 0.0
            if v > best_v:
                best, best_v = a, v
        best = best or la[0]
        return {k: v for k, v in best["input"].items() if k not in ("session_id", "state_version")}

    def _singles_table(self, obs: dict, la: list, me_mon: dict, opp: dict) -> dict | None:
        dc = DamageCalc.get()
        if not dc.enabled or not me_mon.get("species") or not opp.get("species") or opp.get("fainted"):
            return None
        try:
            fallen = self._fallen_counts(obs, [me_mon], [opp])
            atk = calc_pokemon(me_mon, self._card(self.cards, me_mon), allies_fainted=fallen.get(("me", 0)))
            dfn = calc_pokemon(opp, self._card(self.opp_cards, opp))
            field = calc_field(obs, True)
            mids = []
            for a in la:
                act = a["input"].get("action") or {}
                mid = to_id(act.get("move_id", ""))
                if act.get("type") == "move" and mid and _damaging(dex.move(mid), act) and mid not in mids:
                    mids.append(mid)
            res = dc.batch([{"attacker": atk, "defender": dfn, "move": move_name(mid), "field": field} for mid in mids])
            return None if res is None else dict(zip(mids, res))
        except Exception:
            log.warning("singles damage table failed; using the heuristic", exc_info=True)
            return None

    def _doubles(self, st: dict, obs: dict) -> dict:
        t0 = time.monotonic()
        tmpl = st["legal_actions"]["actions"][0]["input"]["action"]
        actives = obs.get("active_pokemon") or [None, None]
        opps = obs.get("opponent_active_pokemon") or [None, None]
        turn = obs.get("turn", 0)
        slots = tmpl.get("slots", [])
        self._track_conditions(obs)
        table = self._exact_table(obs, tmpl, actives, opps)
        t_table = time.monotonic()
        self.last_exact = table is not None
        self.last_lookahead = None
        pick = ctx = None
        la_ms = None
        if table is not None:
            try:
                ctx = self._exact_context(obs, slots, actives, opps, turn, table)
                pick = self._doubles_exact(ctx)
            except Exception:
                log.warning("exact doubles scoring failed; using the heuristic", exc_info=True)
                pick = ctx = None
        la_cap = None                          # the decision deadline's cap on the lookahead (None: no deadline)
        if timecontrol.current() is not None:
            la_cap = timecontrol.cap(lookahead_mod.TIME_CAP)
        if pick is not None and ctx is not None and la_cap is not None and la_cap < MIN_LOOKAHEAD_S:
            log.warning("no time left for the joint lookahead (%.2fs); keeping the exact pair pick", la_cap)
            self.lookahead_skipped += 1
        elif pick is not None and ctx is not None:
            # One-step joint lookahead; None = keep the exact pair pick. Its TIME_CAP clock starts here: it bounds
            # the lookahead's own work, never the calculator start-up or round trip spent above.
            la_t0 = time.monotonic()
            try:
                kw = {} if la_cap is None or la_cap >= lookahead_mod.TIME_CAP else {"time_cap": la_cap}
                better = joint_lookahead(self, obs, tmpl, actives, opps, turn, table, ctx, pick, la_t0,
                                         seed_key=(st.get("session_id"), st.get("state_version"), turn), **kw)
                if better is not None:
                    pick = better
            except Exception:
                log.warning("joint lookahead failed; keeping the exact pair pick", exc_info=True)
                self.last_lookahead = None
            la_ms = round(1000 * (time.monotonic() - la_t0), 1)
        if pick is None:
            pick = self._doubles_heuristic(obs, slots, actives, opps, turn)
        for o in list(opps)[:2]:                     # every foe seen on the field (the search's revealed set)
            sid = to_id((o or {}).get("species", ""))
            if sid and sid not in self.seen_foes:
                self.seen_foes.append(sid)
        search_ms = None
        if self.battle_mode == "search":
            t_s = time.monotonic()
            better = self._search_pick(st, obs, tmpl, pick, turn)
            search_ms = round(1000 * (time.monotonic() - t_s), 1)
            if better is not None:
                pick = better
        self.last_timing = {"table_ms": round(1000 * (t_table - t0), 1), "lookahead_ms": la_ms,
                            "search_ms": search_ms, "total_ms": round(1000 * (time.monotonic() - t0), 1)}
        self._remember_protect(pick, slots, actives, turn)       # lookahead: no Protect chains (species -> turn)
        self._remember(obs, pick, actives)                       # speed-protect: last action per slot (stall moves)
        # Who was on the field for a whole turn (Fake Out freshness, ours and theirs). A forced-switch state is
        # mid-turn: a mon that came in during this turn (U-turn, Parting Shot, Eject Button) and is visible there
        # still gets its Fake Out next turn, so only the normal start-of-turn states are remembered. The lookahead's
        # own foe memory (opp_prev_active) follows the same rule (merge: it used to update in forced states too).
        if not any(_forced(obs, i) for i in (0, 1)):
            self.prev_active = [to_id((a or {}).get("species", "")) or None for a in (list(actives) + [None, None])[:2]]
            self.prev_opp = [to_id((o or {}).get("species", "")) or None for o in (list(opps) + [None, None])[:2]]
            self.opp_prev_active = [to_id((o or {}).get("species", "")) or None
                                    if o and not o.get("fainted") else None for o in (list(opps) + [None, None])[:2]]
        return {"action": {"type": "doubles_turn", **pick}}

    # ---- engine search (ARENA_BATTLE=search) ----
    def _search_reason(self, why: str) -> None:
        r = self.search_stats["reasons"]
        r[why] = r.get(why, 0) + 1

    def _search_pick(self, st: dict, obs: dict, tmpl: dict, classic: dict, turn) -> dict | None:
        """The engine search's joint action (agent/js/search.js on the battle rebuilt from platform-legal information,
        agent/pokemon/battlestate.py), or None: the classic pick then plays. Never raises; never waits past the
        decision's deadline (the search gets SEARCH_SHARE of the time left, at most SEARCH_HARD_S)."""
        self.search_stats["decisions"] += 1
        self.last_search = None
        try:
            from . import battlestate
            from .engine import SearchEngine
            eng = SearchEngine.get()
            if not eng.enabled:
                self._search_reason("engine off")
                self.search_stats["fallback"] += 1
                return None
            limit = SEARCH_HARD_S
            if timecontrol.current() is not None:
                limit = timecontrol.cap(SEARCH_HARD_S, SEARCH_SHARE)
            if limit < SEARCH_MIN_S:
                self.search_stats["skipped_time"] += 1
                self._search_reason("no time")
                return None
            # the time budget (fixed=0) and the safety net (both), never past the time this decision may spend
            params = {"budget_ms": int(min(SEARCH_MS, 1000 * limit * 0.6)),
                      "hard_ms": int(min(SEARCH_CAP_MS, 1000 * limit * 0.8)), **search_params()}
            key = f"{st.get('session_id')}|{st.get('state_version')}|{turn}|{obs.get('turn')}"
            params["rng"] = zlib.crc32(key.encode()) % 2147483646 + 1
            has_log = isinstance(obs.get("protocol_log"), list) and bool(obs.get("protocol_log"))
            cond_left = None
            if not has_log:
                cond_left = {}
                for k in ("my_tailwind", "opp_tailwind", "trickroom"):
                    left = self._cond_left(k, int(turn or 0)) if k in self.cond_set else None
                    if left:
                        cond_left[k] = left
            state = battlestate.build(obs, tmpl, self.cards, self.opp_cards, opp_roster=self.opp_roster,
                                      lineup_prior=self.lineup_prior, seen_foes=self.seen_foes,
                                      classic=classic, seed=[params["rng"] % 65000 + 1, 17, 29, 43],
                                      cond_left=cond_left,
                                      prev_mine=None if has_log else self.prev_active,
                                      prev_foe=None if has_log else self.prev_opp)
            battle_json = None
            if self.oracle is not None:                  # local ablation only (never on the platform)
                battle_json = self.oracle()
            t0 = time.monotonic()
            ans = eng.search(state, params, limit, battle_json=battle_json)
            self.search_ms.append(round(1000 * (time.monotonic() - t0), 1))
            self.search_stats["searched"] += 1
            if ans is None:
                self._search_reason(f"engine: {(eng.last_error or 'no answer')[:60]}")
                self.search_stats["fallback"] += 1
                _debug_dump(state, eng.last_error)
                return None
            action = ans.get("action") or {}
            info = ans.get("info") or {}
            self.last_search = info
            self.search_trace.append({k: info.get(k) for k in TRACE_KEYS if k in info})
            del self.search_trace[:-400]
            if not _template_ok(tmpl, action):
                self._search_reason("answer not in the template")
                self.search_stats["fallback"] += 1
                return None
            out = {f"slot_{s['slot']}": action[f"slot_{s['slot']}"] for s in tmpl.get("slots", [])}
            self.search_stats["used"] += 1
            if all(_same_choice(out.get(k), classic.get(k)) for k in out):
                self.search_stats["same_as_classic"] += 1
            return out
        except ValueError as e:
            self._search_reason(f"state: {str(e)[:60]}")
            self.search_stats["fallback"] += 1
            return None
        except Exception:
            log.warning("engine search failed; playing the classic pick", exc_info=True)
            self._search_reason("crash")
            self.search_stats["fallback"] += 1
            return None

    # ---- memory across turns (the policy instance lives for the whole match) ----
    def _track_conditions(self, obs: dict) -> None:
        """The turn each speed-control condition was set, to know how many turns of it are left.

        We see every turn, so a condition first seen at the start of turn T was set during turn T-1; one first seen in
        our forced-switch state of turn T (a faint / U-turn later in that turn; `turn` is still T) was set during T.
        (The platform's dict value is that start turn too, e.g. {"TAILWIND": 4}; the local sim sends 1 for every side
        condition and lists for fields, so it is not relied on.)"""
        try:
            turn = int(obs.get("turn") or 0)
            set_turn = turn if any(_forced(obs, i) for i in (0, 1)) else turn - 1
            now = {"opp_tailwind": "tailwind" in _cond_ids(obs.get("opponent_side_conditions")),
                   "my_tailwind": "tailwind" in _cond_ids(obs.get("side_conditions")),
                   "trickroom": "trickroom" in _cond_ids(obs.get("fields"))}
            for k, on in now.items():
                if not on:
                    self.cond_set.pop(k, None)
                elif k not in self.cond_set or self.cond_set[k] > turn:
                    self.cond_set[k] = set_turn
        except Exception:
            log.warning("condition tracking failed", exc_info=True)

    def _cond_left(self, key: str, turn: int) -> int | None:
        """Turns of a condition left *including* this one (Tailwind: the turn it is set + 3, Trick Room: + 4)."""
        if key not in self.cond_set:
            return None
        total = TRICK_ROOM_TURNS if key == "trickroom" else TAILWIND_TURNS
        return max(1, total - (turn - self.cond_set[key]))

    def _remember(self, obs: dict, pick: dict, actives: list) -> None:
        """Last action per slot (Protect twice in a row fails). Forced-switch states only change the switching slot."""
        try:
            forced_state = any(_forced(obs, i) for i in (0, 1))
            for i in (0, 1):
                c = pick.get(f"slot_{i}") or {}
                me = actives[i] if i < len(actives) else None
                sp = to_id((me or {}).get("species", ""))
                if c.get("type") == "move":
                    if not forced_state:
                        self.last_move[i] = (sp, to_id(c.get("move_id", "")))
                elif c.get("type") == "switch":
                    self.last_move[i] = (to_id(c.get("species", "")), None)
                elif not forced_state:
                    self.last_move.pop(i, None)
        except Exception:
            log.warning("could not remember last actions", exc_info=True)

    def _protected_last(self, i: int, species: str) -> bool:
        last = self.last_move.get(i)
        return bool(last) and last[0] == to_id(species or "") and last[1] in STALL_MOVES

    def _remember_protect(self, pick: dict, slots: list, actives: list, turn) -> None:
        try:
            for slot in slots:
                i = slot.get("slot")
                c = pick.get(f"slot_{i}") or {}
                sp = to_id((self._slot_mon(slot, actives) or {}).get("species", ""))
                if sp and c.get("type") == "move" and to_id(c.get("move_id", "")) in PROTECT_MOVES:
                    self.last_protect[sp] = turn
        except Exception:
            log.debug("protect bookkeeping failed", exc_info=True)

    def _slot_mon(self, slot: dict, actives: list) -> dict | None:
        i = slot.get("slot", 0)
        me = actives[i] if isinstance(i, int) and i < len(actives) else None
        return me or slot.get("active") or ({"species": slot["species"]} if slot.get("species") else None)

    def _exact_context(self, obs, slots, actives, opps, turn, table) -> dict:
        """Per-slot candidates with exact damage distributions, shared by the pair scorer and the lookahead.

        Merge (lookahead x speed-protect): the lookahead's refactor split the exact scorer into this context + a
        pair scorer (`_pair_value`) that the lookahead reuses; speed-protect's doubles scoring lives inside both:
        the speed model, per-candidate speed facts (`_prep_cand`), Sash-aware stand-alone points here, and the
        joint P(act) / Protect / KO-before-it-acts terms in `_pair_value`."""
        curs, danger, weights = self._foe_weights(table, actives, opps)
        try:
            sm = self._speed_model(obs, actives, opps, table)
        except Exception:
            log.warning("speed model failed; scoring without move order", exc_info=True)
            sm = None
        self.last_speed = sm
        terrain = (sm or {}).get("terrain")
        # Focus Sash / Sturdy at full HP: a lone single-hit move can't KO (also without the speed model).
        foe_sash = (sm or {}).get("foe_sash") or {
            j: _sashed(o, self.opponent_set(o.get("species", ""))) for j, o in enumerate(list(opps)[:2])
            if o and not o.get("fainted")}
        per_slot, fresh_of = [], {}
        for slot in slots:
            i = slot["slot"]
            me = self._slot_mon(slot, actives)
            my_sp = to_id((me or {}).get("species", ""))
            fresh = self.prev_active[i] != my_sp if i < 2 else True
            fresh_of[i] = fresh
            cands = []
            for opt in slot.get("options", []):
                if opt.get("type") == "switch":
                    v = self._switch_value(opt, opps) if _forced(obs, i) else -50.0
                    cands.append({"cand": _concrete(opt), "base": v, "foe": {}, "flinch": None})
                elif opt.get("type") == "move":
                    cands.extend(self._move_choices(opt, i, me, actives, opps, turn, fresh, obs, table, danger,
                                                    terrain, sm))
                else:
                    cands.append({"cand": {"type": "pass"}, "base": -100.0, "foe": {}, "flinch": None})
            if not cands:
                cands.append({"cand": _concrete(slot["options"][0]) if slot.get("options") else {"type": "pass"},
                              "base": -100.0, "foe": {}, "flinch": None})
            for c in cands:
                c.setdefault("slot", i)
                c.setdefault("acts", False)
                c.setdefault("kind", c["cand"].get("type"))
                c.setdefault("species", my_sp)
                c.setdefault("hp", (me or {}).get("current_hp_fraction", 1.0) or 1.0)
                self._prep_cand(c, sm, curs)
            per_slot.append((i, cands))

        def solo(c):
            return c["base"] + sum(weights.get(j, 1.0) * acc * _points(rolls, curs.get(j, 1), _sash_for(foe_sash, c, j))
                                   for j, (rolls, acc) in c["foe"].items())

        for _, cands in per_slot:
            for c in cands:
                c["solo"] = solo(c)
        return {"per_slot": per_slot, "curs": curs, "danger": danger, "weights": weights, "fresh": fresh_of,
                "cache": {}, "sm": sm, "foe_sash": foe_sash, "obs": obs, "turn": turn}

    def _pair_value(self, ctx: dict, c0: dict, c1: dict, tie_break: bool = True) -> float | None:
        """The exact pair score of two slot choices (None = illegal together): speed-protect's joint scoring.

        Each slot's stand-alone points count x P(it acts before a foe KOs / flinches it), with the partner's KO /
        Fake Out before that foe acts raising it; Protect is valued by `_protect_value` (and refunds the partner's
        Earthquake self-hit); two hits on one foe use the joint (Sash-aware) KO odds; a KO that lands before the
        target acts earns REMOVE_* on top. `tie_break` adds TIE_BREAK x (stand-alone points): it only decides exact
        ties, e.g. a slot that is surely KO'd first (P(act) = 0) never picks the first listed target, our partner."""
        if c0["kind"] == "switch" and c1["kind"] == "switch" \
                and to_id(c0["cand"].get("species")) == to_id(c1["cand"].get("species")):
            return None
        curs, weights, cache, danger = ctx["curs"], ctx["weights"], ctx["cache"], ctx["danger"]
        sm, foe_sash, obs, turn = ctx.get("sm"), ctx.get("foe_sash") or {}, ctx.get("obs") or {}, ctx.get("turn", 0)
        pa0, pa1 = _p_act(c0, c1), _p_act(c1, c0)
        v = pa0 * c0["solo"] + pa1 * c1["solo"]
        if c0["kind"] == "protect":
            v += self._protect_value(c0, c1, sm, obs, turn) + pa1 * c1.get("ally_pen", 0.0)
        if c1["kind"] == "protect":
            v += self._protect_value(c1, c0, sm, obs, turn) + pa0 * c0.get("ally_pen", 0.0)
        if c0["flinch"] is not None and c0["flinch"] == c1["flinch"]:
            v -= FAKE_OUT_BONUS
        for j in c0["foe"].keys() & c1["foe"].keys():      # both hit foe j: joint value replaces the sum
            da, db = c0["foe"][j], c1["foe"][j]
            key = (id(da), id(db), j)
            if key not in cache:
                cur = curs.get(j, 1)
                sa, sb = _sash_for(foe_sash, c0, j), _sash_for(foe_sash, c1, j)
                cache[key] = (_pair_points(da, db, cur, sa, sb) - da[1] * _points(da[0], cur, sa)
                              - db[1] * _points(db[0], cur, sb),
                              _pair_ko(da, db, cur, sa, sb))
            v += pa0 * pa1 * weights.get(j, 1.0) * cache[key][0]
        if sm is not None:                                  # KOs that land before the target acts
            for j in c0["kob"].keys() | c1["kob"].keys():
                n0, n1 = pa0 * c0["kob"].get(j, 0.0), pa1 * c1["kob"].get(j, 0.0)
                rem = 1.0 - (1.0 - n0) * (1.0 - n1)
                if j in c0["kob"] and j in c1["kob"]:
                    k0, k1 = c0["ko"][j], c1["ko"][j]
                    extra = cache[(id(c0["foe"][j]), id(c1["foe"][j]), j)][1] - (1 - (1 - k0) * (1 - k1))
                    if extra > 0:
                        rem += extra * pa0 * pa1 * min(c0["pf"][j], c1["pf"][j])
                v += (REMOVE_BASE + REMOVE_DANGER * danger.get(j, 0.5)) * min(1.0, rem)
        if tie_break:
            v += TIE_BREAK * (c0["solo"] + c1["solo"])
        return v

    def _doubles_exact(self, ctx: dict) -> dict | None:
        """Best joint pair by `_pair_value` (a lone slot is paired with a do-nothing dummy, as in speed-protect)."""
        per_slot = ctx["per_slot"]
        lists = [cands for _, cands in per_slot][:2]
        while len(lists) < 2:
            lists.append([dict(_DUMMY_CAND)])
        best, best_v = None, -1e18
        dbg = self.debug_pairs
        if dbg is not None:
            dbg.clear()
        for c0 in lists[0]:
            for c1 in lists[1]:
                v = self._pair_value(ctx, c0, c1, tie_break=False)
                if v is None:
                    continue
                if dbg is not None:
                    dbg.append((round(v, 1), c0["cand"], c1["cand"]))
                # Exact ties go to the better stand-alone moves: a slot that is surely KO'd before it moves has
                # P(act) = 0, so all its options score the same -- without this the first listed target (-1 / -2,
                # our own partner) would win and a Close Combat would hit the ally if the model is wrong.
                v += TIE_BREAK * (c0["solo"] + c1["solo"])
                if v > best_v:
                    best, best_v = (c0, c1), v
        if best is None:
            return None
        if len(per_slot) == 2:
            ctx["best_pair"] = best
        return {f"slot_{i}": c["cand"] for (i, _), c in zip(per_slot, best)}

    def _move_choices(self, opt, i, me, actives, opps, turn, fresh, obs, table, danger,
                      terrain=None, sm=None) -> list[dict]:
        """Every legal target of one move option as a candidate: base points + per-foe damage distributions."""
        mid = to_id(opt.get("move_id", ""))
        m = dex.move(mid)
        targets = opt.get("targets") or [0]
        damaging = _damaging(m, opt)
        my_sp = to_id((me or {}).get("species", ""))
        hp = (me or {}).get("current_hp_fraction", 1.0) or 1.0
        card = self._card(self.cards, me)
        sun = calc_field(obs, True).get("weather") == "Sun"
        acc = _acc_of(m, opt)
        if mid == "suckerpunch":
            acc *= SUCKER_PUNCH_ACC
        alive = {j: o for j, o in enumerate(list(opps)[:2]) if o and not o.get("fainted")}
        ally = actives[1 - i] if i < 2 and len(actives) > 1 - i else None
        protect = mid in PROTECT_MOVES and not damaging
        choice = to_id(view_item(me, card) or "").startswith("choice")   # a knocked-off Choice item no longer locks
        try:
            prio = move_priority(mid, _speed_view(me), self.cards.get(my_sp), terrain)
        except Exception:
            prio = int(m.get("priority", 0) or 0)
        mh = _breaks_sash(mid)
        out = []

        def dist(j):
            r = table.get(("me", i, mid, "opp", j))
            return (_adjust_rolls(r["rolls"], m, mid, me, card, sun), acc) if r else None

        def heuristic(j):
            return move_value(mid, my_sp, _types(alive[j])) * HEURISTIC_TO_POINTS

        for t in targets:
            base, foe, flinch, ally_pen = 0.0, {}, None, 0.0
            if t in (1, 2):
                j = t - 1
                if j not in alive:
                    continue
                o = alive[j]
                if mid in FIRST_TURN_ONLY:
                    if not fresh:
                        base = -20.0
                    elif not _flinch_blocked(o, self.opponent_set(o.get("species", "")), obs):
                        base, flinch = FAKE_OUT_BONUS + 100.0 * danger.get(j, 0.5), j
                if damaging and not (mid in FIRST_TURN_ONLY and not fresh):   # a stale Fake Out fails: no damage
                    d = dist(j)
                    if d:
                        foe[j] = d
                    else:
                        base += heuristic(j)
                elif not damaging:
                    base += _target_status_value(mid, o, self.opponent_set(o.get("species", "")))
            elif t == 0:
                if m.get("target") in SPREAD_TARGETS and damaging:
                    for j in alive:
                        d = dist(j)
                        if d:
                            foe[j] = d
                        else:
                            base += heuristic(j)
                    if m.get("target") == "allAdjacent" and ally and not ally.get("fainted"):
                        r = table.get(("me", i, mid, "me", 1 - i))
                        if r:
                            ally_pen = ALLY_HIT_WEIGHT * acc * _points(r["rolls"], _cur_hp(ally, r))
                        else:
                            ally_pen = ALLY_HIT_WEIGHT * move_value(mid, my_sp, _types(ally)) * HEURISTIC_TO_POINTS
                        base -= ally_pen                 # refunded when the partner Protects (joint scoring)
                elif damaging:                                   # locked / auto-targeted single-target move
                    vals = [dist(j) for j in alive]
                    pts = [acc * _points(d[0], _cur_hp(alive[j], table[("me", i, mid, "opp", j)]),
                                         not mh and _sashed(alive[j], self.opponent_set(alive[j].get("species", ""))))
                           if d else heuristic(j) for j, d in zip(alive, vals)]
                    base = sum(pts) / len(pts) if pts else 0.0
                elif not protect:
                    base = _status_value(mid, hp, turn, obs)
                    if sm is not None and mid in ("tailwind", "trickroom"):
                        base = _speed_control_value(mid, sm, obs, base)
            else:                                                # ally-targeting: only support moves make sense
                ally_up = bool(ally) and not ally.get("fainted")
                base = 15.0 if mid in ALLY_SUPPORT_MOVES and ally_up else -100.0
            if m.get("selfdestruct") and damaging:
                base -= SELF_KO_PENALTY
            out.append({"cand": {"type": "move", "move_id": opt["move_id"], "target": t}, "base": base,
                        "foe": foe, "flinch": flinch, "kind": "protect" if protect and t == 0 else "move",
                        "acts": not protect, "prio": prio, "mid": mid, "target": t, "choice": choice, "mh": mh,
                        "ally_pen": ally_pen})
        if not out:
            out.append({"cand": {"type": "move", "move_id": opt["move_id"], "target": targets[0]}, "base": -1.0,
                        "foe": {}, "flinch": None, "acts": not protect, "prio": prio, "mid": mid, "target": targets[0],
                        "mh": mh})
        return out

    def _doubles_heuristic(self, obs, slots, actives, opps, turn) -> dict:
        """v0 per-slot heuristic (base-stat damage proxy); the fallback when the calculator is unavailable."""
        pick, used_switch = {}, set()
        for slot in slots:
            i = slot["slot"]
            me = actives[i] if i < len(actives) else None
            my_sp = to_id((me or {}).get("species", "") or slot.get("species", ""))
            fresh = self.prev_active[i] != my_sp if i < 2 else True
            best, best_v = None, -1e9
            for opt in slot.get("options", []):
                if opt.get("type") == "switch":
                    if to_id(opt.get("species")) in used_switch:
                        continue
                    v = self._switch_value(opt, opps) if _forced(obs, i) else -50
                    cand = _concrete(opt)
                elif opt.get("type") == "move":
                    cand, v = self._best_target(opt, my_sp, me, actives, opps, i, turn, fresh, obs)
                else:
                    cand, v = {"type": "pass"}, -100
                if v > best_v:
                    best, best_v = cand, v
            if best is None:
                best = _concrete(slot["options"][0])
            if best.get("type") == "switch":
                used_switch.add(to_id(best.get("species")))
            pick[f"slot_{i}"] = best
        return pick

    def _best_target(self, opt, my_sp, me, actives, opps, i, turn, fresh, obs):
        mid = to_id(opt.get("move_id", ""))
        m = dex.move(mid)
        targets = opt.get("targets") or [0]
        hp = (me or {}).get("current_hp_fraction", 1.0) or 1.0
        best, best_v = None, -1e9
        for t in targets:
            v = 0.0
            if t in (1, 2):
                o = opps[t - 1] if t - 1 < len(opps) else None
                if not o or o.get("fainted"):
                    continue
                ot = [x.upper() for x in (o.get("types") or dex.types_of(o.get("species", "")))]
                v = move_value(mid, my_sp, ot)
                if mid == "fakeout":
                    v = (1000 + _threat(o)) if fresh else -20
                v *= 1.0 + 0.5 * (1 - (o.get("current_hp_fraction", 1.0) or 1.0))  # prefer finishing blows
            elif t == 0:
                if m.get("target") in SPREAD_TARGETS:
                    for o in opps:
                        if o and not o.get("fainted"):
                            ot = [x.upper() for x in (o.get("types") or dex.types_of(o.get("species", "")))]
                            v += 0.75 * move_value(mid, my_sp, ot)
                    if m.get("target") == "allAdjacent":           # hits our partner too
                        ally = actives[1 - i] if len(actives) > 1 else None
                        if ally and not ally.get("fainted"):
                            at = [x.upper() for x in (ally.get("types") or dex.types_of(ally.get("species", "")))]
                            v -= 0.9 * move_value(mid, my_sp, at)
                else:
                    v = _status_value(mid, hp, turn, obs)
                    if mid in PROTECT_MOVES and (self._protected_last(i, my_sp) or to_id(
                            self.cards.get(my_sp, {}).get("item") or "").startswith("choice")):
                        v = -50.0                                   # Protect twice in a row fails 2/3; Choice locks it
            else:  # ally-targeting (negative); only support moves make sense
                v = 15.0 if mid in ALLY_SUPPORT_MOVES else -100
            if v > best_v:
                best, best_v = {"type": "move", "move_id": opt["move_id"], "target": t}, v
        if best is None:
            best, best_v = {"type": "move", "move_id": opt["move_id"], "target": targets[0]}, -1.0
        return best, best_v

    def _switch_value(self, opt, opps) -> float:
        sp = to_id(opt.get("species", ""))
        mine = dex.types_of(sp)
        v = 10.0
        for o in opps:
            if o and not o.get("fainted"):
                for t in (o.get("types") or dex.types_of(o.get("species", ""))):
                    v -= 3 * (effectiveness(t, mine) - 1)          # resist their STABs
        return v + 10 * (opt.get("current_hp_fraction") or opt.get("hp_fraction") or 1.0)


def _singles_switch_score(act: dict, obs: dict) -> float:
    """0..1 for a singles switch-in without the calculator: HP left, resisting the foe's types, hitting it super
    effectively with our own types."""
    try:
        sp = to_id(act.get("species", ""))
        view = next((v for v in obs.get("available_switches") or [] if isinstance(v, dict)
                     and to_id(v.get("species", "")) == sp), {"species": sp})
        opp = obs.get("opponent_active_pokemon") or {}
        mine = _types(view)
        s = float(view.get("current_hp_fraction", 1.0) or 0.0)
        if isinstance(opp, dict) and opp.get("species") and not opp.get("fainted"):
            theirs = _types(opp)
            s -= 0.25 * sum(effectiveness(t, mine) - 1 for t in theirs)
            s += 0.2 * (max((effectiveness(t, theirs) for t in mine), default=1.0) - 1)
        return max(0.0, min(0.99, 0.5 + 0.25 * s))
    except Exception:
        return 0.0


def _threat(o: dict) -> float:
    sp = o.get("species", "")
    bs = o.get("base_stats") or dex.base_stats(sp)
    return max(bs.get("atk", 90), bs.get("spa", 90)) + bs.get("spe", 80) / 2


PHYSICAL_STAB = {"FIRE": "flareblitz", "WATER": "liquidation", "GRASS": "woodhammer", "ELECTRIC": "wildcharge",
                 "GROUND": "highhorsepower", "FLYING": "bravebird", "ROCK": "rockslide", "STEEL": "ironhead",
                 "FIGHTING": "closecombat", "DARK": "knockoff", "GHOST": "shadowclaw", "DRAGON": "dragonclaw",
                 "FAIRY": "playrough", "ICE": "iciclecrash", "BUG": "uturn", "POISON": "gunkshot",
                 "PSYCHIC": "zenheadbutt", "NORMAL": "bodyslam"}
SPECIAL_STAB = {"FIRE": "heatwave", "WATER": "hydropump", "GRASS": "energyball", "ELECTRIC": "thunderbolt",
                "GROUND": "earthpower", "FLYING": "hurricane", "ROCK": "powergem", "STEEL": "flashcannon",
                "FIGHTING": "focusblast", "DARK": "darkpulse", "GHOST": "shadowball", "DRAGON": "dracometeor",
                "FAIRY": "moonblast", "ICE": "icebeam", "BUG": "bugbuzz", "POISON": "sludgebomb",
                "PSYCHIC": "psychic", "NORMAL": "hypervoice"}


def _guess_moves(species: str) -> list[str]:
    """Plausible attacks of a Pokémon whose set we never saw: one STAB per type on its better attacking stat."""
    if not species:
        return []
    bs = dex.base_stats(species)
    table = PHYSICAL_STAB if bs.get("atk", 0) >= bs.get("spa", 0) else SPECIAL_STAB
    return [table[t] for t in dex.types_of(species) if t in table] + ["protect"]


def _battle_kind(st: dict, obs: dict) -> str | None:
    """"doubles" / "singles" when a state of an unknown phase is shaped like a battle turn, else None."""
    try:
        la = (st.get("legal_actions") or {}).get("actions") or []
        act = ((la[0].get("input") or {}).get("action") or {}) if la else {}
        if act.get("type") == "doubles_turn" and isinstance(act.get("slots"), list):
            return "doubles"
        ids = [str(a.get("action_id", "")) for a in la]
        if ids and all(i.startswith(("move:", "switch:")) for i in ids) and isinstance(obs.get("active_pokemon"), dict):
            return "singles"
    except Exception:
        return None
    return None


def _forced(obs: dict, i: int) -> bool:
    fs = obs.get("force_switch")
    return bool(fs[i]) if isinstance(fs, list) and i < len(fs) else bool(fs)


def _status_value(mid: str, hp: float, turn: int, obs: dict) -> float:
    ours = {to_id(k) for k in (obs.get("side_conditions") or {})}
    if mid in ("protect", "detect", "spikyshield", "silktrap", "banefulbunker"):
        return 45.0 if hp < 0.35 else 5.0
    if mid == "tailwind":
        return 70.0 if "tailwind" not in ours else -10
    if mid == "trickroom":
        return 20.0 if turn <= 2 else 5.0
    if mid in ("followme", "ragepowder"):
        return 35.0 if turn <= 1 else 15.0
    if mid == "spore":
        return 55.0
    return 8.0


def _speed_control_value(mid: str, sm: dict, obs: dict, default: float) -> float:
    """Tailwind / Trick Room valued by how many (our mon, foe) speed matchups they flip for us."""
    pairs = [(i, j) for i in sm["mine"] for j in sm["foes"]]
    if not pairs:
        return default
    me, foe, tr = sm["my_spe"], sm["foe_spe"], sm["tr"]

    def first(i, j, my_mult=1.0, trick=tr):
        return _first(acts_first(me[i] * my_mult, 0, foe[j], 0, trick))

    if mid == "tailwind":
        if "tailwind" in _cond_ids(obs.get("side_conditions")) or tr:
            return -10.0
        gain = sum(first(i, j, 2.0) - first(i, j) for i, j in pairs)
        return 20.0 + 50.0 * min(1.0, gain / 2)
    # Trick Room: using it while it is up ends it
    gain = sum(first(i, j, 1.0, not tr) - first(i, j) for i, j in pairs)
    if tr:
        return 40.0 if gain > 0 else -60.0
    return max(-30.0, min(80.0, 10.0 + 25.0 * gain))


def _debug_dump(state: dict, error) -> None:
    """ARENA_SEARCH_DEBUG_DIR (local diagnostics only): keep the battle description of a failed search."""
    d = os.environ.get("ARENA_SEARCH_DEBUG_DIR")
    if not d:
        return
    try:
        import json
        os.makedirs(d, exist_ok=True)
        name = f"fail-{os.getpid()}-{int(time.time() * 1000)}.json"
        with open(os.path.join(d, name), "w") as f:
            json.dump({"error": error, "state": state}, f)
    except Exception:
        log.debug("search debug dump failed", exc_info=True)


def _same_choice(a: dict | None, b: dict | None) -> bool:
    a, b = a or {}, b or {}
    if a.get("type") != b.get("type"):
        return False
    if a.get("type") == "move":
        return to_id(a.get("move_id")) == to_id(b.get("move_id")) and a.get("target") == b.get("target")
    if a.get("type") == "switch":
        return to_id(a.get("species")) == to_id(b.get("species"))
    return True


def _concrete(opt: dict) -> dict:
    t = opt.get("type")
    if t == "move":
        return {"type": "move", "move_id": opt["move_id"], "target": (opt.get("targets") or [0])[0]}
    if t == "switch":
        return {"type": "switch", "species": opt["species"]}
    return {"type": "pass"}


# The platform's own known-legal fallback species (pokemon.md §5), with standard sets.
DEFAULT_OU_TEAM = [
    {"species": "Great Tusk", "item": "Leftovers", "ability": "Protosynthesis", "nature": "Jolly",
     "evs": {"hp": 252, "def": 4, "spe": 252}, "moves": ["Rapid Spin", "Headlong Rush", "Knock Off", "Stealth Rock"]},
    {"species": "Gholdengo", "item": "Choice Scarf", "ability": "Good as Gold", "nature": "Timid",
     "evs": {"spa": 252, "spd": 4, "spe": 252}, "moves": ["Make It Rain", "Shadow Ball", "Trick", "Focus Blast"]},
    {"species": "Kingambit", "item": "Leftovers", "ability": "Supreme Overlord", "nature": "Adamant",
     "evs": {"hp": 252, "atk": 252, "spd": 4}, "moves": ["Swords Dance", "Kowtow Cleave", "Sucker Punch", "Iron Head"]},
    {"species": "Dragapult", "item": "Choice Specs", "ability": "Infiltrator", "nature": "Timid",
     "evs": {"spa": 252, "spd": 4, "spe": 252}, "moves": ["Draco Meteor", "Shadow Ball", "Flamethrower", "U-turn"]},
    {"species": "Rillaboom", "item": "Choice Band", "ability": "Grassy Surge", "nature": "Adamant",
     "evs": {"hp": 252, "atk": 252, "spd": 4}, "moves": ["Grassy Glide", "Wood Hammer", "Knock Off", "U-turn"]},
    {"species": "Iron Valiant", "item": "Booster Energy", "ability": "Quark Drive", "nature": "Naive",
     "evs": {"atk": 4, "spa": 252, "spe": 252}, "moves": ["Moonblast", "Close Combat", "Knock Off", "Encore"]},
]
# Backups after a rejected team (a ban or catalog difference on the platform's Showdown), none of DEFAULT's species:
# long-standing OU staples. All pass the local gen9ou validator (tests/strategy/test_singles.py).
ALT_OU_TEAM = [
    {"species": "Garchomp", "item": "Rocky Helmet", "ability": "Rough Skin", "nature": "Jolly",
     "evs": {"hp": 252, "def": 4, "spe": 252}, "moves": ["Earthquake", "Dragon Tail", "Stealth Rock", "Spikes"]},
    {"species": "Corviknight", "item": "Leftovers", "ability": "Pressure", "nature": "Impish",
     "evs": {"hp": 252, "def": 168, "spd": 88}, "moves": ["Brave Bird", "Roost", "Defog", "U-turn"]},
    {"species": "Toxapex", "item": "Black Sludge", "ability": "Regenerator", "nature": "Bold",
     "evs": {"hp": 252, "def": 252, "spd": 4}, "moves": ["Surf", "Toxic", "Recover", "Haze"]},
    {"species": "Heatran", "item": "Air Balloon", "ability": "Flash Fire", "nature": "Modest",
     "evs": {"hp": 252, "spa": 252, "spe": 4}, "moves": ["Magma Storm", "Earth Power", "Flash Cannon", "Taunt"]},
    {"species": "Clefable", "item": "Life Orb", "ability": "Magic Guard", "nature": "Modest",
     "evs": {"hp": 252, "spa": 252, "spd": 4}, "moves": ["Moonblast", "Calm Mind", "Moonlight", "Flamethrower"]},
    {"species": "Rotom-Wash", "item": "Choice Scarf", "ability": "Levitate", "nature": "Timid",
     "evs": {"hp": 4, "spa": 252, "spe": 252}, "moves": ["Hydro Pump", "Volt Switch", "Trick", "Will-O-Wisp"]},
]
OU_TEAMS = [DEFAULT_OU_TEAM, ALT_OU_TEAM]
