"""Singles battles: every Pokémon game type except the VGC doubles draft.

    pokemon_gen9same          both seats get the SAME team (so the foe's sets are ours); team preview; level 100
    pokemon_gen9random        Showdown random-battle sets; per-Pokémon levels from the protocol; no team preview
    pokemon_gen9ou_teambuild  we submit a team (policy.DEFAULT_OU_TEAM); the foe's sets are unknown; level 100
    pokemon_gen9ou_draft      snake draft like VGC: both rosters' full sets are public

What v3 did here: the best stand-alone exact-damage move, never a voluntary switch, and on a forced switch the FIRST
Pokémon in the list; every set it fed the calculator was a level-50 generic 252/252 spread (the singles formats are
level 100 / random-battle levels), and the foe was assumed to have no moves in formats without a draft.

This module (one DamageCalc batch per decision, ~100 calcs):
  * sets: ours from the battle views + the format's spread (gen9same: no EVs -> Showdown fills 252 everywhere, fitted
    on the recorded real match; random: 84s at the protocol's level; teambuild / draft: the submitted / drafted set);
    the foe's from the draft, from our own team (gen9same), from data/singles_sets.json (random-battle movepools, a
    typical Battle Factory set) plus everything the view has revealed;
  * a one-turn payoff table: each of our options (moves, switches, pivots) x each likely foe reply, with move order
    (priority, exact speeds, Trick Room), accuracy, P(KO) over the damage rolls, Focus Sash / Sturdy, Fake Out;
    the value is HP removed - HP lost + KO terms + LAMBDA x the `duel` of the Pokémon left facing each other (who
    needs fewer hits, speed breaks ties; valued in HP bars, so switching one doomed Pokémon into another is no
    escape); status moves act through that duel (setup boosts, burn / paralysis / sleep / Toxic, recovery, screens)
    or a small table (hazards, removal, phazing); never a voluntary switch into a possible trap (Shadow Tag, Arena
    Trap, Magnet Pull: Showdown would reject it);
  * the foe's reply: softmax over its own damage (weighted by how likely it has the move) blended with the worst case;
  * forced switches (faint, U-turn) by the same duel; team-preview lead by the average KO race vs their roster.
Every failure returns None; the caller then keeps the old v3 stand-alone scorer (with a type-matchup forced switch).
Measured on local/singles_env.py (python -m local.singles_eval) and checked against the recorded real match
(python -m local.singles_replay): see docs/local_sim.md.
"""
from __future__ import annotations

import json
import logging
import math
import re
from functools import lru_cache

from . import dex
from .calc import DamageCalc, calc_field, calc_pokemon, move_name
from .dex import to_id
from .stats import STAGE, acts_first, effective_speed, move_priority
from .typechart import effectiveness

log = logging.getLogger("agent.singles")

# ---- knobs (units: 1.0 = one Pokémon's full HP bar) -------------------------------------------------------------
def _knob(name: str, default: float) -> float:
    """Experiment override (local ladders only): ARENA_SINGLES_<NAME>=value. Deployed runs never set these."""
    import os
    try:
        v = os.environ.get(f"ARENA_SINGLES_{name}")
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default


C_KO = _knob("C_KO", 0.3)               # a KO is worth this on top of the HP it removes (a threat gone / tempo)
LAMBDA = _knob("LAMBDA", 0.8)           # weight of the duel left on the field after this turn (HP units)
ALPHA_WORST = _knob("ALPHA", 0.3)       # value = ALPHA x worst likely foe reply + (1 - ALPHA) x softmax mean
TAU = _knob("TAU", 0.12)                # softmax temperature of the foe's reply over its own expected damage
SWITCH_COST = _knob("SWITCH_COST", 0.04)      # tempo lost by a voluntary switch
RESWITCH_COST = _knob("RESWITCH_COST", 0.04)  # ... more if the Pokémon only just came in (no switch loops)
LIKELY = 0.5            # foe moves at least this likely to be on its set count for the worst case
SETUP_BONUS = _knob("SETUP_BONUS", 0.12)      # x (attack multiplier - 1): a boost keeps paying later
PROTECT_COST = 0.06     # Protect only pays with residual damage on the foe (never on a mere tie with an attack)
NO_BENCH_KO = _knob("NO_BENCH_KO", -2.0)      # continuation when our last Pokémon is KO'd (the game is lost)
RACE_SLOPE = 0.9

ALL252 = {s: 252 for s in ("hp", "atk", "def", "spa", "spd", "spe")}
RB_EVS = {s: 84 for s in ("hp", "atk", "def", "spa", "spd", "spe")}
GENERIC_EVS = {"hp": 4, "atk": 252, "spa": 252, "spe": 252}

SETUP = {"swordsdance": {"atk": 2}, "nastyplot": {"spa": 2}, "dragondance": {"atk": 1, "spe": 1},
         "calmmind": {"spa": 1, "spd": 1}, "quiverdance": {"spa": 1, "spd": 1, "spe": 1}, "bulkup": {"atk": 1, "def": 1},
         "shellsmash": {"atk": 2, "spa": 2, "spe": 2, "def": -1, "spd": -1}, "coil": {"atk": 1, "def": 1},
         "agility": {"spe": 2}, "rockpolish": {"spe": 2}, "autotomize": {"spe": 2}, "shiftgear": {"atk": 1, "spe": 2},
         "tidyup": {"atk": 1, "spe": 1}, "victorydance": {"atk": 1, "def": 1, "spe": 1}, "irondefense": {"def": 2},
         "acidarmor": {"def": 2}, "amnesia": {"spd": 2}, "cosmicpower": {"def": 1, "spd": 1}, "workup": {"atk": 1, "spa": 1},
         "growth": {"atk": 1, "spa": 1}, "honeclaws": {"atk": 1}, "howl": {"atk": 1}, "tailglow": {"spa": 3},
         "curse": {"atk": 1, "def": 1, "spe": -1}, "bellydrum": {"atk": 6}, "filletaway": {"atk": 2, "spa": 2, "spe": 2},
         "clangoroussoul": {"atk": 1, "def": 1, "spa": 1, "spd": 1, "spe": 1}, "noretreat": {"atk": 1, "def": 1, "spa": 1,
         "spd": 1, "spe": 1}, "trailblaze": {}, "flamecharge": {}}
SETUP_COST = {"bellydrum": 0.5, "filletaway": 0.5, "clangoroussoul": 0.33}
HEAL = {"recover": 0.5, "roost": 0.5, "softboiled": 0.5, "slackoff": 0.5, "milkdrink": 0.5, "healorder": 0.5,
        "shoreup": 0.5, "moonlight": 0.5, "morningsun": 0.5, "synthesis": 0.5, "lunarblessing": 0.25,
        "junglehealing": 0.25, "lifedew": 0.25, "strengthsap": 0.35, "wish": 0.35, "rest": 1.0}
SLEEP = {"spore": 1.0, "sleeppowder": 0.75, "hypnosis": 0.6, "sing": 0.55, "lovelykiss": 0.75, "darkvoid": 0.5,
         "grasswhistle": 0.55, "yawn": 0.6}
POWDER = {"spore", "sleeppowder", "stunspore", "poisonpowder"}
HAZARDS = {"stealthrock": ("stealthrock", 0.07), "spikes": ("spikes", 0.05), "toxicspikes": ("toxicspikes", 0.03),
           "stickyweb": ("stickyweb", 0.04), "stoneaxe": ("stealthrock", 0.07), "ceaselessedge": ("spikes", 0.05)}
REMOVAL = {"rapidspin", "defog", "mortalspin", "tidyup", "courtchange"}
SCREENS = {"reflect": "physical", "lightscreen": "special", "auroraveil": "both"}
PIVOTS = {"uturn", "voltswitch", "flipturn", "partingshot", "teleport", "chillyreception", "shedtail"}
PHAZE = {"roar", "whirlwind", "dragontail", "circlethrow"}
PROTECT = {"protect", "detect", "spikyshield", "kingsshield", "banefulbunker", "silktrap", "burningbulwark", "obstruct"}
FIRST_TURN = {"fakeout", "firstimpression"}
MISC_STATUS = {"taunt": 0.03, "encore": 0.04, "disable": 0.02, "trick": 0.04, "switcheroo": 0.04, "substitute": 0.03,
               "haze": 0.0, "clearsmog": 0.0, "destinybond": 0.02, "painsplit": 0.0}
BURN_IMMUNE = {"guts", "waterveil", "waterbubble", "flareboost", "thermalexchange", "comatose", "purifyingsalt"}
SPREAD_OR_SELF = {"self", "allySide", "foeSide", "all", "allyTeam"}


# ---------------------------------------------------------------- data
@lru_cache(maxsize=1)
def set_data() -> dict:
    try:
        return json.loads((dex.DATA / "singles_sets.json").read_text())
    except Exception:
        log.warning("data/singles_sets.json unreadable; guessing unseen sets from types", exc_info=True)
        return {}


@lru_cache(maxsize=None)
def canon(species: str) -> str:
    """A species id the dex (and so the calculator) knows: cosmetic formes the battle shows ("sawsbucksummer",
    "minioryellow", "vivillonpokeball") fall back to their base species; without this every calc of such a Pokémon
    failed and it looked immune to everything (and a free switch-in)."""
    sid = to_id(species or "")
    entry = dex.species(sid)
    if entry:
        base = entry.get("baseSpecies")
        if base and entry.get("name") in (dex.species(base).get("cosmeticFormes") or []):
            return to_id(base)                      # listed cosmetic forme (Minior-Yellow): the calc lacks it
        return sid
    if not sid:
        return sid
    for k in range(len(sid) - 1, 2, -1):
        if dex.species(sid[:k]):
            return sid[:k]
    return sid


def _cv(view: dict | None) -> dict:
    """A view with its species canonicalised (calculator input, base stats, speed)."""
    v = dict(view or {})
    if v.get("species"):
        v["species"] = canon(v["species"])
    return v


def _rand(sp: str) -> dict:
    r = set_data().get("random", {})
    return r.get(sp) or r.get(canon(sp)) or {}


def _ou(sp: str) -> dict | None:
    r = set_data().get("ou", {})
    return r.get(sp) or r.get(canon(sp))


def mode_of(st: dict, obs: dict) -> str:
    """same / random / teambuild / draft / ou (unknown singles format: level 100, generic sets)."""
    game = to_id(st.get("game_type") or "")
    fmt = to_id(obs.get("format") or obs.get("battle_format") or "")
    if "random" in game or "random" in fmt:
        return "random"
    if "same" in game:
        return "same"
    if "teambuild" in game:
        return "teambuild"
    if "draft" in game:
        return "draft"
    return "ou"


_LEVEL = re.compile(r",\s*L(\d+)")


def my_side_id(obs: dict) -> str | None:
    return next((str(k).split(":")[0].strip() for k in (obs.get("team") or {})), None)


def log_levels(obs: dict) -> dict:
    """{("me"|"opp", species id): level} from the protocol's switch / poke details ("Garchomp, L84, F"; no L = 100)."""
    out: dict = {}
    mine = my_side_id(obs)
    lines = obs.get("protocol_log")
    if not isinstance(lines, list) or mine not in ("p1", "p2"):
        return out
    for line in lines:
        if not isinstance(line, str):
            continue
        if line.startswith(("|switch|", "|drag|", "|replace|")):
            parts = line.split("|")
            if len(parts) < 4:
                continue
            side, details = parts[2][:2], parts[3]
        elif line.startswith("|poke|"):
            parts = line.split("|")
            if len(parts) < 4:
                continue
            side, details = parts[2][:2], parts[3]
        else:
            continue
        m = _LEVEL.search(details)
        out[("me" if side == mine else "opp", to_id(details.split(",")[0]))] = int(m.group(1)) if m else 100
    return out


def foe_last_move(obs: dict) -> str | None:
    """The foe active's last move since it last switched in (Choice lock), from the protocol."""
    mine = my_side_id(obs)
    lines = obs.get("protocol_log")
    if not isinstance(lines, list) or mine not in ("p1", "p2"):
        return None
    foe = "p2" if mine == "p1" else "p1"
    last = None
    for line in lines:
        if not isinstance(line, str):
            continue
        if line.startswith(("|switch|", "|drag|")) and line.split("|")[2][:2] == foe:
            last = None
        elif line.startswith("|move|"):
            parts = line.split("|")
            if len(parts) > 3 and parts[2][:2] == foe and "[from]" not in line:
                last = to_id(parts[3])
    return last


# ---------------------------------------------------------------- sets
def _level(levels: dict, side: str, sp: str, default: int) -> int:
    lv = levels.get((side, sp)) or levels.get((side, canon(sp)))
    return int(lv or default)


def own_set(pol, mode: str, view: dict, levels: dict) -> dict:
    sp = to_id(view.get("species", ""))
    card = pol.cards.get(sp) if mode in ("teambuild", "draft", "ou") else None
    if card and card.get("evs") is not None:
        s = {k: v for k, v in card.items()}
        s["level"] = _level(levels, "me", sp, int(card.get("level") or 100) if mode != "teambuild" else 100)
    elif mode == "same":
        s = {"evs": ALL252, "nature": "Serious", "level": _level(levels, "me", sp, 100)}
    elif mode == "random":
        rd = _rand(sp)
        s = {"evs": RB_EVS, "nature": "Serious", "level": _level(levels, "me", sp, int(rd.get("level") or 80))}
    else:
        s = {"evs": GENERIC_EVS, "nature": "Serious", "level": _level(levels, "me", sp, 100)}
    s = dict(s)
    s["species"] = view.get("species") or s.get("species")
    if view.get("item"):
        s["item"] = view["item"]
    if view.get("ability"):
        s["ability"] = view["ability"]
    if view.get("moves"):
        s["moves"] = [to_id(m) for m in view["moves"]]
    s.setdefault("ivs", {})
    return s


def foe_set(pol, mode: str, view: dict, levels: dict, obs: dict) -> tuple[dict, list[tuple[str, float]]]:
    """(calculator set, [(move id, P(the foe has it))]) of an opposing Pokémon."""
    sp = to_id(view.get("species", ""))
    revealed = [to_id(m) for m in (view.get("moves") or []) if m]
    exact = None
    if mode == "same":
        for v in (obs.get("team") or {}).values():
            if isinstance(v, dict) and to_id(v.get("species", "")) == sp:
                exact = {"evs": ALL252, "nature": "Serious", "level": _level(levels, "opp", sp, 100),
                         "item": v.get("item"), "ability": v.get("ability"), "moves": [to_id(m) for m in v.get("moves") or []]}
                break
    card = pol.opp_cards.get(sp) if exact is None else None
    if card and card.get("evs") is not None and card.get("moves"):
        exact = dict(card)
        exact["level"] = _level(levels, "opp", sp, int(card.get("level") or 100))
        exact["moves"] = [to_id(m) for m in card.get("moves") or []]
    if exact is not None:
        s = exact
        moves = [(m, 1.0) for m in dict.fromkeys(list(s.get("moves") or []) + revealed)]
    elif mode == "random":
        rd = _rand(sp)
        abil = rd.get("abilities") or []
        s = {"evs": RB_EVS, "nature": "Serious", "level": _level(levels, "opp", sp, int(rd.get("level") or 80))}
        if len(abil) == 1:
            s["ability"] = abil[0]
        pool = [m for m in rd.get("moves") or [] if m not in revealed]
        p = max(0.0, min(1.0, (4 - len(revealed)) / len(pool))) if pool else 0.0
        moves = [(m, 1.0) for m in revealed] + [(m, p) for m in pool]
    else:
        ou = _ou(sp)
        if ou:
            s = {"evs": ou.get("evs") or GENERIC_EVS, "nature": ou.get("nature") or "Serious", "ivs": ou.get("ivs") or {},
                 "item": ou.get("item"), "ability": ou.get("ability"), "level": _level(levels, "opp", sp, 100)}
            guess = [m for m in ou.get("moves") or [] if m not in revealed]
            p = 0.6 if len(revealed) < 4 else 0.0
        else:
            from .policy import _guess_moves
            s = {"evs": GENERIC_EVS, "nature": "Serious", "level": _level(levels, "opp", sp, 100)}
            guess = [m for m in _guess_moves(canon(sp)) if m not in revealed]
            p = 0.5 if len(revealed) < 4 else 0.0
        moves = [(m, 1.0) for m in revealed] + [(m, p) for m in guess]
    s = dict(s)
    s["species"] = view.get("species")
    s.setdefault("ivs", {})
    item = view.get("item")
    if item and item != "unknown_item":
        s["item"] = item
    if view.get("ability"):
        s["ability"] = view["ability"]
    # Choice lock: a Choice-item foe that used a move since it came in keeps using it.
    it = to_id(str(view.get("item") if view.get("item") not in (None, "unknown_item") else s.get("item") or ""))
    if it.startswith("choice"):
        last = foe_last_move(obs)
        if last and any(m == last for m, _ in moves):
            moves = [(m, 1.0 if m == last else 0.0) for m, _ in moves]
    return s, [(m, p) for m, p in moves if p > 0]


# ---------------------------------------------------------------- helpers
def _hp(view: dict | None) -> float:
    try:
        return max(0.0, min(1.0, float((view or {}).get("current_hp_fraction") or 0.0)))
    except (TypeError, ValueError):
        return 1.0


def _types(view: dict | None) -> list[str]:
    view = view or {}
    return [str(t).upper() for t in (view.get("types") or dex.types_of(view.get("species", "")))]


def _status(view: dict | None) -> str:
    s = to_id(str((view or {}).get("status") or ""))
    return "" if s in ("", "none", "fnt") else s


def race(hx: float, hy: float, dx: float, dy: float, x_first: float) -> float:
    """KO race of X vs Y from HP fractions hx / hy, expected damage per turn dx (to Y) / dy (to X): +1 X wins, -1 loses."""
    if hx <= 0:
        return -1.0
    if hy <= 0:
        return 1.0
    tx = math.inf if dx <= 0.005 else math.ceil(hy / dx - 1e-9)
    ty = math.inf if dy <= 0.005 else math.ceil(hx / dy - 1e-9)
    if tx == math.inf and ty == math.inf:
        return 0.0
    if tx == math.inf:
        return -1.0
    if ty == math.inf:
        return 1.0
    diff = ty - tx + (x_first - 0.5)
    return math.tanh(RACE_SLOPE * diff)


def duel(hx: float, hy: float, dx: float, dy: float, x_first: float) -> float:
    """Value (HP bars) of X and Y fighting it out from HP fractions hx / hy with expected damage per turn dx (to Y) /
    dy (to X): the winner removes the loser's HP (+ C_KO) and loses what the loser landed before falling. This is the
    continuation after this turn, in the same units as the turn itself (a mere +-1 race score let a doomed Pokémon
    "save" itself by switching into another doomed one, turn after turn)."""
    if hx <= 0 or hy <= 0:
        return 0.0
    tx = math.inf if dx <= 0.005 else math.ceil(hy / dx - 1e-9)
    ty = math.inf if dy <= 0.005 else math.ceil(hx / dy - 1e-9)
    if tx == math.inf and ty == math.inf:
        return 0.0

    def outcome(first: bool) -> float:
        if tx < ty or (tx == ty and first):                 # X lands its KO first
            n_y = tx - 1 if first else tx                    # Y's hits before that
            return hy + C_KO - (min(hx, dy * n_y) if n_y > 0 and dy > 0 else 0.0)
        n_x = ty if first else ty - 1                        # X's hits before Y's KO lands
        return (min(hy, dx * n_x) if n_x > 0 and dx > 0 else 0.0) - hx - C_KO

    p = max(0.0, min(1.0, x_first))
    return p * outcome(True) + (1 - p) * outcome(False)


def _grounded(view: dict, s: dict) -> bool:
    ab = to_id(str(view.get("ability") or s.get("ability") or ""))
    it = to_id(str(view.get("item") or s.get("item") or ""))
    return "FLYING" not in _types(view) and ab != "levitate" and it != "airballoon"


def entry_hazard(obs: dict, view: dict, s: dict) -> float:
    """HP fraction a Pokémon of ours loses switching in (Stealth Rock, Spikes; Heavy-Duty Boots / Magic Guard: 0)."""
    conds = {to_id(str(k)): v for k, v in (obs.get("side_conditions") or {}).items()} \
        if isinstance(obs.get("side_conditions"), dict) else {to_id(str(k)): 1 for k in obs.get("side_conditions") or []}
    it = to_id(str(view.get("item") or s.get("item") or ""))
    ab = to_id(str(view.get("ability") or s.get("ability") or ""))
    if it == "heavydutyboots" or ab == "magicguard":
        return 0.0
    out = 0.0
    if "stealthrock" in conds:
        out += 0.125 * effectiveness("ROCK", _types(view))
    if "spikes" in conds and _grounded(view, s):
        try:
            layers = max(1, min(3, int(conds["spikes"] or 1)))
        except (TypeError, ValueError):
            layers = 1
        out += {1: 0.125, 2: 1 / 6, 3: 0.25}[layers]
    return min(out, 0.5)


class Hit:
    """One attack: e = expected fraction of the defender's MAX HP removed this hit (accuracy in, capped at its current
    HP), ko = P(KO), d = expected damage per turn uncapped (for the race), prio = priority bracket."""
    __slots__ = ("e", "ko", "d", "prio", "mid", "cat")

    def __init__(self, e=0.0, ko=0.0, d=0.0, prio=0, mid="", cat=""):
        self.e, self.ko, self.d, self.prio, self.mid, self.cat = e, ko, d, prio, mid, cat


# ---------------------------------------------------------------- the decision
class Ctx:
    pass


def _hit_from(res, dview, dset, mid, acc, aview, aset, sun, prio) -> Hit:
    from .policy import _adjust_rolls, _cur_hp, _hit_stats, _sashed
    m = dex.move(mid)
    if not res or not res.get("rolls"):
        return Hit(prio=prio, mid=mid, cat=str(m.get("category", "")).lower())
    rolls = _adjust_rolls(res["rolls"], m, mid, aview, aset, sun)
    mx = max(1, int(res.get("defender_hp") or 1))
    cur = _cur_hp(dview, res)
    sash = not m.get("multihit") and _sashed(dview, dset)
    useful, pko = _hit_stats(rolls, cur, sash)
    e = acc * useful * cur / mx
    d = acc * (sum(rolls) / len(rolls)) / mx
    return Hit(e, acc * pko, d, prio, mid, str(m.get("category", "")).lower())


def _acc(mid: str, opt: dict | None = None) -> float:
    from .policy import _acc_of
    a = _acc_of(dex.move(mid), opt)
    if mid == "suckerpunch" or mid == "thunderclap":
        a *= 0.8
    return a


def build(pol, st: dict, obs: dict) -> Ctx | None:
    """Sets, speeds and every damage number of this decision (one DamageCalc batch). None = calculator unavailable."""
    from .policy import _cond_ids, _damaging, _speed_view
    dc = DamageCalc.get()
    if not dc.enabled:
        return None
    c = Ctx()
    c.mode = mode_of(st, obs)
    c.obs = obs
    c.la = (st.get("legal_actions") or {}).get("actions") or []
    c.levels = log_levels(obs)
    A = obs.get("active_pokemon") if isinstance(obs.get("active_pokemon"), dict) else None
    O = obs.get("opponent_active_pokemon") if isinstance(obs.get("opponent_active_pokemon"), dict) else None
    if O is not None and (O.get("fainted") or not O.get("species")):
        O = None
    c.A, c.O = A, O
    c.force = bool(obs.get("force_switch"))
    c.moves_la, c.switch_la = [], []
    for a in c.la:
        act = (a.get("input") or {}).get("action") or {}
        if act.get("type") == "move":
            c.moves_la.append((a, act))
        elif act.get("type") == "switch":
            c.switch_la.append((a, act))
    views = {to_id(v.get("species", "")): v for v in (obs.get("available_switches") or []) if isinstance(v, dict)}
    team = {to_id(v.get("species", "")): v for v in (obs.get("team") or {}).values() if isinstance(v, dict)}
    c.bench = []
    for a, act in c.switch_la:
        sp = to_id(act.get("species", ""))
        v = views.get(sp) or team.get(sp) or {"species": sp, "current_hp_fraction": 1.0}
        c.bench.append((a, v, own_set(pol, c.mode, v, c.levels)))
    c.setA = own_set(pol, c.mode, A, c.levels) if A and A.get("species") else None
    if O is not None:
        c.setO, c.foe_moves = foe_set(pol, c.mode, O, c.levels, obs)
    else:
        c.setO, c.foe_moves = None, []
    f_me, f_opp = calc_field(obs, True), calc_field(obs, False)
    c.terrain = f_me.get("terrain")
    sun = f_me.get("weather") == "Sun"
    c.tr = "trickroom" in _cond_ids(obs.get("fields"))
    my_side = {k: 1 for k in _cond_ids(obs.get("side_conditions"))}
    opp_side = {k: 1 for k in _cond_ids(obs.get("opponent_side_conditions"))}
    fallen = pol._fallen_counts(obs, [A] if A else [], [O] if O else [])
    calcs, keys = [], []

    def add(key, atk, dfn, mid, field):
        keys.append(key)
        calcs.append({"attacker": atk, "defender": dfn, "move": move_name(mid), "field": field})

    inA = calc_pokemon(_cv(A), c.setA, allies_fainted=fallen.get(("me", 0))) if c.setA else None
    inO = calc_pokemon(_cv(O), c.setO, allies_fainted=fallen.get(("opp", 0))) if O is not None else None
    inB = [calc_pokemon(_cv(v), s) for _, v, s in c.bench]
    if inO is not None:
        if inA is not None and A and not A.get("fainted"):
            for a, act in c.moves_la:
                mid = to_id(act.get("move_id", ""))
                if _damaging(dex.move(mid), act):
                    add(("A", mid), inA, inO, mid, f_me)
        for mid, _p in c.foe_moves:
            if not _damaging(dex.move(mid)):
                continue
            if inA is not None and A and not A.get("fainted"):
                add(("O", mid, "A"), inO, inA, mid, f_opp)
            for k, b in enumerate(inB):
                add(("O", mid, k), inO, b, mid, f_opp)
        for k, (_, v, s) in enumerate(c.bench):
            for mid in dict.fromkeys(s.get("moves") or []):
                if _damaging(dex.move(mid)):
                    add(("B", k, mid), inB[k], inO, mid, f_me)
    res = dc.batch(calcs) if calcs else []
    if res is None:
        return None
    res = _retry_as_base(dc, calcs, res)
    tab = dict(zip(keys, res))
    # speeds
    c.sA = effective_speed(_speed_view(_cv(A)), c.setA, my_side, f_me) if c.setA else 0.0
    c.sO = effective_speed(_speed_view(_cv(O)), c.setO, opp_side, f_me) if O is not None else 0.0
    c.sB = [effective_speed(_speed_view(_cv(v)), s, my_side, f_me) for _, v, s in c.bench]
    # our active's hits on the foe
    c.hitA = {}
    for a, act in c.moves_la:
        mid = to_id(act.get("move_id", ""))
        pr = move_priority(mid, _speed_view(A), c.setA, c.terrain) if A else 0
        c.hitA[mid] = _hit_from(tab.get(("A", mid)), O, c.setO, mid, _acc(mid, act), A, c.setA, sun, pr) \
            if O is not None else Hit(prio=pr, mid=mid)
    # the foe's hits on our active / bench
    c.hitO_A, c.hitO_B = {}, [dict() for _ in c.bench]
    for mid, p in c.foe_moves:
        pr = move_priority(mid, _speed_view(O), c.setO, c.terrain) if O is not None else 0
        c.hitO_A[mid] = _hit_from(tab.get(("O", mid, "A")), A, c.setA, mid, _acc(mid), O, c.setO, sun, pr)
        for k, (_, v, s) in enumerate(c.bench):
            c.hitO_B[k][mid] = _hit_from(tab.get(("O", mid, k)), v, s, mid, _acc(mid), O, c.setO, sun, pr)
    # our bench's best damage per turn on the foe
    c.dB = []
    for k, (_, v, s) in enumerate(c.bench):
        best = 0.0
        for mid in dict.fromkeys(s.get("moves") or []):
            r = tab.get(("B", k, mid))
            if r:
                best = max(best, _hit_from(r, O, c.setO, mid, _acc(mid), v, s, sun, 0).d)
        c.dB.append(best)
    c.presence = dict(c.foe_moves)
    return c


def _retry_as_base(dc, calcs: list, res: list) -> list:
    """Calcs the calculator rejected (a forme it does not know) once more with the base species' name."""
    def base(name):
        e = dex.species(name)
        b = e.get("baseSpecies")
        return b if b and b != e.get("name") else None
    retry = []
    for i, (cc, r) in enumerate(zip(calcs, res)):
        if r is None:
            a, d = cc["attacker"], cc["defender"]
            ab, db = base(a.get("species", "")), base(d.get("species", ""))
            if ab or db:
                retry.append((i, {**cc, "attacker": {**a, "species": ab or a["species"]},
                                  "defender": {**d, "species": db or d["species"]}}))
    if not retry:
        return res
    again = dc.batch([x for _, x in retry])
    if again is None:
        return res
    res = list(res)
    for (i, _), r in zip(retry, again):
        res[i] = r
    return res


def _is_attack(mid: str) -> bool:
    from .policy import _damaging
    return bool(mid) and _damaging(dex.move(mid))


def foe_best_d(hits: dict, presence: dict) -> float:
    """The foe's expected damage per turn against one of ours: its best likely move (unlikely ones discounted)."""
    best = 0.0
    for mid, h in hits.items():
        p = presence.get(mid, 0.0)
        best = max(best, h.d * (1.0 if p >= LIKELY else p))
    return best


def foe_replies(c: Ctx) -> list[tuple[str, float]]:
    """(foe move, weight) over its damaging moves: likelihood x softmax of its own expected damage to our active."""
    reps = [(mid, h) for mid, h in c.hitO_A.items() if h.d > 0 or h.e > 0]
    if not reps:
        # nothing hurts our active (immune, or its calcs failed): the foe still attacks with SOMETHING, and a switch-in
        # must face those moves -- weight its damaging moves by likelihood alone (not a free "no reply")
        reps = [(mid, h) for mid, h in c.hitO_A.items() if _is_attack(mid)]
        if not reps:
            return [("", 1.0)]
        tot = sum(c.presence.get(mid, 0.0) for mid, _ in reps) or 1.0
        return [(mid, c.presence.get(mid, 0.0) / tot) for mid, _ in reps]
    ws = [c.presence.get(mid, 0.0) * math.exp((h.e + C_KO * h.ko) / TAU) for mid, h in reps]
    tot = sum(ws) or 1.0
    return [(mid, w / tot) for (mid, _), w in zip(reps, ws)]


def _first_p(a_speed, a_prio, b_speed, b_prio, tr) -> float:
    r = acts_first(a_speed, a_prio, b_speed, b_prio, tr)
    return 1.0 if r is True else 0.0 if r is False else 0.5


def _stage_mult(cur: int, delta: int) -> float:
    return STAGE[max(-6, min(6, cur + delta))] / STAGE[max(-6, min(6, cur))]


def free_best(c: Ctx, hO: float, exclude: int | None = None) -> float:
    """Race of the best Pokémon we could bring in for free (after a KO) against the foe at hO."""
    vals = []
    for k, (_, v, s) in enumerate(c.bench):
        if k == exclude:
            continue
        hb = _hp(v) - entry_hazard(c.obs, v, s)
        dO = foe_best_d(c.hitO_B[k], c.presence)
        vals.append(duel(hb, hO, c.dB[k], dO, _first_p(c.sB[k], 0, c.sO, 0, c.tr)))
    return max(vals) if vals else NO_BENCH_KO


def evaluate(pol, c: Ctx) -> list[tuple[float, dict, str]]:
    """[(value, legal action, why)] for every legal option of a normal (not forced) turn."""
    from .policy import _damaging
    A, O = c.A, c.O
    hA, hO = _hp(A), _hp(O)
    replies = foe_replies(c)
    likely = [mid for mid, _ in replies if c.presence.get(mid, 1.0) >= LIKELY] or [mid for mid, _ in replies]
    dA_best = max((h.d for h in c.hitA.values()), default=0.0)
    dO_A = foe_best_d(c.hitO_A, c.presence)
    boostsA = (A or {}).get("boosts") or {}
    boostsO = (O or {}).get("boosts") or {}
    my_sp = to_id((A or {}).get("species", ""))
    fresh = is_fresh(pol, my_sp)
    protected_before = protected_last(pol, my_sp)
    foe_status = _status(O)
    foe_types = _types(O)
    foe_ab = to_id(str((O or {}).get("ability") or (c.setO or {}).get("ability") or ""))
    foe_phys = sum(h.d for h in c.hitO_A.values() if h.cat == "physical")
    foe_spec = sum(h.d for h in c.hitO_A.values() if h.cat == "special")
    phys_share = foe_phys / (foe_phys + foe_spec) if foe_phys + foe_spec > 0 else 0.5
    foe_left = _foe_remaining(c.obs)
    my_left = len(c.bench)
    out = []

    def reply_hit(mid) -> Hit:
        return c.hitO_A.get(mid) or Hit()

    def agg(vals: dict) -> float:
        mean = sum(w * vals[mid] for mid, w in replies)
        worst = min(vals[mid] for mid in likely) if likely else mean
        return ALPHA_WORST * worst + (1 - ALPHA_WORST) * mean

    stay_best = -1e9
    for a, act in c.moves_la:
        mid = to_id(act.get("move_id", ""))
        m = dex.move(mid)
        h = c.hitA.get(mid) or Hit()
        dmg = _damaging(m, act) and O is not None
        pr = h.prio
        vals = {}
        why = mid
        for rmid, _w in replies:
            rh = reply_hit(rmid)
            pf = _first_p(c.sA, pr, c.sO, rh.prio, c.tr)
            kA = rh.ko
            extra = 0.0
            dA_race, dO_race, first_race, hA_add = dA_best, dO_A, _first_p(c.sA, 0, c.sO, 0, c.tr), 0.0
            if mid in PROTECT and not dmg:
                if protected_before:
                    vals[rmid] = -1.0
                    continue
                passive = 0.1 if foe_status in ("tox", "psn", "brn") else 0.0
                vals[rmid] = None            # filled below from the best stay value
                continue
            if dmg:
                e, kO = h.e, h.ko
                if mid in FIRST_TURN and not fresh:
                    e, kO = 0.0, 0.0
                flinch = mid == "fakeout" and fresh and pf >= 1.0 and foe_ab not in ("innerfocus", "shielddust")
                dealt = pf * e + (1 - pf) * (1 - kA) * e
                koO = pf * kO + (1 - pf) * (1 - kA) * kO
                if flinch:
                    taken, koA = 0.0, 0.0
                else:
                    taken = pf * (1 - kO) * rh.e + (1 - pf) * rh.e
                    koA = pf * (1 - kO) * kA + (1 - pf) * kA
                if m.get("selfdestruct"):
                    koA, taken = 1.0, hA
                if mid in PIVOTS and c.bench:
                    # hit, then switch to our best bench Pokémon; if we moved first the foe's reply hits it instead
                    best_piv = -1e9
                    for k, (_, v, s) in enumerate(c.bench):
                        hb = _hp(v) - entry_hazard(c.obs, v, s)
                        hk = c.hitO_B[k].get(rmid) or Hit()
                        tb = pf * (1 - kO) * hk.e
                        kb = pf * (1 - kO) * hk.ko
                        r = duel(hb - tb, hO - dealt, c.dB[k], foe_best_d(c.hitO_B[k], c.presence),
                                 _first_p(c.sB[k], 0, c.sO, 0, c.tr))
                        best_piv = max(best_piv, -tb - C_KO * kb + LAMBDA * ((1 - kb) * r))
                    stay_taken = (1 - pf) * rh.e
                    stay_ko = (1 - pf) * kA
                    v_piv = dealt + C_KO * koO - stay_taken - C_KO * stay_ko + (1 - koO) * best_piv
                    vals[rmid] = v_piv
                    why = f"{mid} (pivot)"
                    continue
                hA2, hO2 = max(0.0, hA - taken), max(0.0, hO - dealt)
                cont = (1 - koA) * (1 - koO) * duel(hA2, hO2, dA_best, dO_A, first_race) \
                    + koA * (1 - koO) * free_best(c, hO2)
                v = dealt - taken + C_KO * (koO - koA) + LAMBDA * cont
                if mid == "knockoff" and to_id(str((O or {}).get("item") or "")) not in ("", "none"):
                    v += 0.03 * (1 - kO)
                if mid in ("rapidspin", "mortalspin", "tidyup"):
                    v += _removal_value(c.obs, my_left)
                if mid in HAZARDS and O is not None:
                    v += _hazard_value(c.obs, mid, foe_left) * (pf + (1 - pf) * (1 - kA))
                if mid in PHAZE:
                    v += 0.06 * sum(max(0, int(x or 0)) for x in boostsO.values()) * (1 - kO)
                vals[rmid] = v
                continue
            # ---- status / non-damaging moves: they act only if we move first or survive
            acts = pf + (1 - pf) * (1 - kA)
            taken, koA = rh.e, kA
            if mid in SETUP:
                boost = SETUP[mid]
                cost = SETUP_COST.get(mid, 0.0)
                if cost and hA <= cost + 0.01:
                    vals[rmid] = -0.5
                    continue
                if cost:                                  # Belly Drum & co.: the foe's hit lands on the reduced HP
                    left = hA - cost
                    k_after = 1.0 if rh.d >= left else min(1.0, kA + 0.5 * rh.d / max(left, 0.01))
                    koA = pf * k_after + (1 - pf) * kA
                    taken = pf * min(left, rh.d) + (1 - pf) * rh.e
                    acts = pf + (1 - pf) * (1 - kA)
                cat_best = max(c.hitA.values(), key=lambda x: x.d, default=Hit())
                stat = "atk" if cat_best.cat == "physical" else "spa"
                mult = _stage_mult(int(boostsA.get(stat, 0) or 0), boost.get(stat, 0))
                dA_race = dA_best * (1 + (mult - 1) * acts)
                if boost.get("spe"):
                    sp_mult = _stage_mult(int(boostsA.get("spe", 0) or 0), boost["spe"])
                    first_race = acts * _first_p(c.sA * sp_mult, 0, c.sO, 0, c.tr) + (1 - acts) * first_race
                dfm = _stage_mult(int(boostsA.get("def", 0) or 0), boost.get("def", 0))
                sdm = _stage_mult(int(boostsA.get("spd", 0) or 0), boost.get("spd", 0))
                dO_race = dO_A * (phys_share / (1 + (dfm - 1) * acts) + (1 - phys_share) / (1 + (sdm - 1) * acts))
                hA_add = -cost * acts
                extra = SETUP_BONUS * max(0.0, mult - 1) * acts if dA_best > 0.02 else 0.0
                why = f"{mid} (setup)"
            elif mid in HEAL:
                heal = HEAL[mid]
                if mid == "painsplit":
                    heal = 0.0
                w = to_id(str(c.obs.get("weather") and next(iter(c.obs.get("weather")), "") or ""))
                if mid in ("moonlight", "morningsun", "synthesis") and w:
                    heal = 0.667 if "sun" in w else 0.25
                if mid == "rest":
                    extra -= 0.25 if to_id(str((A or {}).get("item") or "")) not in ("chestoberry", "lumberry") else 0.05
                    if hA > 0.6:
                        extra -= 0.3
                if mid == "strengthsap":
                    dO_race *= 1 - 0.33 * phys_share
                hA_add = heal * acts
                why = f"{mid} (heal)"
            elif mid == "painsplit" and O is not None:
                maxA = max(1, int((A or {}).get("max_hp") or 100))
                estO = hO * _est_maxhp(O, c.setO)
                hA_add = acts * max(0.0, ((hA * maxA + estO) / 2 - hA * maxA) / maxA)
                extra += 0.0
                why = "painsplit"
            elif mid in SLEEP and O is not None:
                ok = not foe_status and not (mid in POWDER and ("GRASS" in foe_types or foe_ab == "overcoat")) \
                    and not _foe_side_sleeping(c.obs) and foe_ab not in ("insomnia", "vitalspirit", "comatose", "sweetveil")
                if ok:
                    p = SLEEP[mid] * acts * (0.5 if mid == "yawn" else 1.0)
                    dO_race *= 1 - 0.6 * p
                    extra += 0.08 * p
                why = f"{mid} (sleep)"
            elif mid in ("willowisp",) and O is not None:
                if not foe_status and "FIRE" not in foe_types and foe_ab not in BURN_IMMUNE:
                    p = 0.85 * acts
                    dO_race *= 1 - 0.5 * phys_share * p
                    dA_race += 0.0625 * p
                    extra += 0.02 * p
                why = "willowisp (burn)"
            elif mid in ("thunderwave", "glare", "stunspore") and O is not None:
                immune = foe_status or (mid == "thunderwave" and ("ELECTRIC" in foe_types or "GROUND" in foe_types)) \
                    or (mid == "stunspore" and ("GRASS" in foe_types or foe_ab == "overcoat")) \
                    or foe_ab in ("limber", "comatose")
                if not immune:
                    p = (0.9 if mid == "thunderwave" else 1.0) * acts
                    first_par = _first_p(c.sA, 0, c.sO * 0.5, 0, c.tr)
                    first_race = p * first_par + (1 - p) * first_race
                    dO_race *= 1 - 0.25 * p
                    extra += 0.02 * p
                why = f"{mid} (paralysis)"
            elif mid in ("toxic", "poisonpowder") and O is not None:
                immune = foe_status or "POISON" in foe_types or "STEEL" in foe_types or \
                    foe_ab in ("immunity", "poisonheal", "magicguard", "comatose") or \
                    (mid == "poisonpowder" and "GRASS" in foe_types)
                if not immune:
                    p = (0.9 if mid == "toxic" else 0.75) * acts
                    dA_race += (0.12 if mid == "toxic" else 0.0625) * p
                    extra += 0.03 * p
                why = f"{mid} (poison)"
            elif mid == "leechseed" and O is not None:
                if "GRASS" not in foe_types and "leechseed" not in _foe_volatiles(c.obs):
                    p = 0.9 * acts
                    dA_race += 0.1 * p
                    hA_add = 0.0
                    extra += 0.03 * p
                why = "leechseed"
            elif mid in HAZARDS:
                extra += _hazard_value(c.obs, mid, foe_left) * acts
                why = f"{mid} (hazard)"
            elif mid in REMOVAL:
                extra += _removal_value(c.obs, my_left) * acts
                why = f"{mid} (hazard removal)"
            elif mid in SCREENS:
                side = {to_id(str(k)) for k in (c.obs.get("side_conditions") or {})}
                snow = "snow" in to_id(str(next(iter(c.obs.get("weather") or {}), "") if isinstance(
                    c.obs.get("weather"), dict) else c.obs.get("weather") or ""))
                if mid not in side and (mid != "auroraveil" or snow):
                    kind = SCREENS[mid]
                    share = 1.0 if kind == "both" else phys_share if kind == "physical" else 1 - phys_share
                    dO_race *= 1 - 0.5 * share * acts
                    extra += 0.05 * share * acts
                why = f"{mid} (screen)"
            elif mid in PHAZE or mid in ("haze",):
                extra += 0.06 * sum(max(0, int(x or 0)) for x in boostsO.values()) * acts
                if mid in PHAZE:
                    extra += 0.5 * _hazard_value_present(c.obs) * acts
                why = mid
            elif mid in PIVOTS and c.bench:              # Parting Shot / Teleport / Chilly Reception: out safely
                best_piv = max(LAMBDA * duel(_hp(v) - entry_hazard(c.obs, v, s), hO, c.dB[k],
                                              foe_best_d(c.hitO_B[k], c.presence) * (0.67 if mid == "partingshot" else 1),
                                              _first_p(c.sB[k], 0, c.sO, 0, c.tr))
                               for k, (_, v, s) in enumerate(c.bench))
                vals[rmid] = -(1 - pf) * rh.e - C_KO * (1 - pf) * kA + acts * best_piv - (1 - acts) * 0.0 \
                    - pf * 0.0 - SWITCH_COST
                why = f"{mid} (pivot)"
                continue
            else:
                extra += MISC_STATUS.get(mid, 0.0) * acts
            hA2 = max(0.0, min(1.0, hA + hA_add) - taken) if pf >= 0.5 else max(0.0, min(1.0, hA - taken + hA_add))
            cont = (1 - koA) * duel(hA2, hO, dA_race, dO_race, first_race) + koA * free_best(c, hO)
            vals[rmid] = -taken - C_KO * koA + extra + LAMBDA * cont
        out.append([vals, a, why])

    # stay values (Protect is scored from the best other stay option)
    scored = []
    for vals, a, why in out:
        if any(v is None for v in vals.values()):
            scored.append((None, a, why))
            continue
        v = agg(vals)
        stay_best = max(stay_best, v)
        scored.append((v, a, why))
    res = []
    for v, a, why in scored:
        if v is None:
            passive = 0.08 if foe_status in ("tox", "psn", "brn") or "leechseed" in _foe_volatiles(c.obs) else 0.0
            leftovers = 0.02 if to_id(str((A or {}).get("item") or "")) == "leftovers" else 0.0
            v = (stay_best if stay_best > -1e8 else 0.0) - PROTECT_COST + passive + leftovers
            why = "protect"
        res.append((v, a, why))
    # voluntary switches (never into a possible trap: Showdown offers the switch, then rejects it -> INVALID_ACTION)
    if may_be_trapped(c):
        return res
    for k, (a, v, s) in enumerate(c.bench):
        hz = entry_hazard(c.obs, v, s)
        hb = _hp(v)
        vals = {}
        for rmid, _w in replies:
            hk = c.hitO_B[k].get(rmid) or Hit()
            taken, kb = hz + hk.e, hk.ko
            r = duel(max(0.0, hb - taken), hO, c.dB[k], foe_best_d(c.hitO_B[k], c.presence),
                     _first_p(c.sB[k], 0, c.sO, 0, c.tr))
            cont = (1 - kb) * r + kb * free_best(c, hO, exclude=k)
            vals[rmid] = -taken - C_KO * kb + LAMBDA * cont - SWITCH_COST - (RESWITCH_COST if fresh else 0.0)
        res.append((agg(vals), a, f"switch {to_id(v.get('species', ''))}"))
    return res


TRAP_ABILITIES = {"shadowtag", "arenatrap", "magnetpull"}


def may_be_trapped(c: Ctx) -> bool:
    """Could the foe's (possibly unrevealed) ability trap our active? Showdown then marks the request `maybeTrapped`
    and still lets the switch be chosen, but rejects it if the trap is real. Ghosts and Shed Shell always escape."""
    A, O = c.A, c.O
    if not A or not O:
        return False
    if "GHOST" in _types(A) or to_id(str(A.get("item") or "")) == "shedshell":
        return False
    sp = to_id(O.get("species", ""))
    if O.get("ability"):
        cands = {to_id(str(O["ability"]))}
    elif c.mode in ("same", "draft") and (c.setO or {}).get("ability"):
        cands = {to_id(str(c.setO["ability"]))}
    else:
        cands = {to_id(str(x)) for x in (dex.species(sp).get("abilities") or {}).values()}
        cands |= set(set_data().get("random", {}).get(sp, {}).get("abilities") or [])
        if (c.setO or {}).get("ability"):
            cands.add(to_id(str(c.setO["ability"])))
    if "shadowtag" in cands and to_id(str(A.get("ability") or "")) != "shadowtag":
        return True
    if "arenatrap" in cands and _grounded(A, c.setA or {}):
        return True
    return "magnetpull" in cands and "STEEL" in _types(A)


def _est_maxhp(view: dict, s: dict | None) -> float:
    from .stats import calc_stats
    try:
        st = calc_stats(dex.base_stats(view.get("species", "")), (s or {}).get("evs"), (s or {}).get("ivs"),
                        (s or {}).get("nature", "serious"), int((s or {}).get("level") or 100))
        return float(st["hp"])
    except Exception:
        return 300.0


def _foe_remaining(obs: dict) -> int:
    """Foe Pokémon still to come in (not active, not fainted): 6 minus fainted minus the active, as far as seen."""
    team = obs.get("opponent_team") or {}
    fainted = sum(1 for v in team.values() if isinstance(v, dict) and v.get("fainted"))
    return max(0, 6 - fainted - 1)


def _foe_side_sleeping(obs: dict) -> bool:
    team = obs.get("opponent_team") or {}
    return any(isinstance(v, dict) and to_id(str(v.get("status") or "")) == "slp" for v in team.values())


def _foe_volatiles(obs: dict) -> set:
    """Volatile effects on the foe active that we can read from the protocol (Leech Seed)."""
    mine = my_side_id(obs)
    lines = obs.get("protocol_log")
    if not isinstance(lines, list) or mine not in ("p1", "p2"):
        return set()
    foe = "p2" if mine == "p1" else "p1"
    vol: set = set()
    for line in lines:
        if not isinstance(line, str):
            continue
        parts = line.split("|")
        if len(parts) < 3 or parts[2][:2] != foe:
            continue
        if parts[1] in ("switch", "drag"):
            vol.clear()
        elif parts[1] == "-start" and len(parts) > 3:
            vol.add(to_id(parts[3].replace("move: ", "")))
        elif parts[1] == "-end" and len(parts) > 3:
            vol.discard(to_id(parts[3].replace("move: ", "")))
    return vol


def _side(obs: dict, key: str) -> dict:
    x = obs.get(key)
    if isinstance(x, dict):
        return {to_id(str(k)): v for k, v in x.items()}
    return {to_id(str(k)): 1 for k in (x or [])}


MAX_LAYERS = {"spikes": 3, "toxicspikes": 2}


def _layers(v) -> int:
    """Layers of a stackable hazard from a side-condition value (poke-env style: the layer count; 1 if unreadable)."""
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return 1


def _hazard_value(obs: dict, mid: str, foe_left: int) -> float:
    """Value of laying a hazard: per switch-in chip x the foe Pokémon still to come in; nothing once it is maxed out
    (Stealth Rock / Sticky Web up, Spikes x3, Toxic Spikes x2: a wasted turn), half for another layer."""
    cond, per = HAZARDS[mid]
    theirs = _side(obs, "opponent_side_conditions")
    if cond in theirs:
        if cond not in MAX_LAYERS or _layers(theirs[cond]) >= MAX_LAYERS[cond]:
            return 0.0
        per *= 0.5
    return per * foe_left


def _hazard_value_present(obs: dict) -> float:
    theirs = _side(obs, "opponent_side_conditions")
    return 0.06 * sum(1 for k in ("stealthrock", "spikes", "toxicspikes") if k in theirs)


def _removal_value(obs: dict, my_left: int) -> float:
    ours = _side(obs, "side_conditions")
    per = sum(HAZARDS[k][1] for k in ("stealthrock", "spikes", "toxicspikes", "stickyweb") if k in ours)
    return per * my_left


def forced_switch(pol, c: Ctx) -> tuple[dict, str] | None:
    """Best Pokémon to bring in (after a faint: a free switch; after our own U-turn the foe may still hit it)."""
    if not c.bench:
        return None
    hO = _hp(c.O)
    alive_out = c.A is not None and not c.A.get("fainted") and _hp(c.A) > 0
    best, best_v = None, -1e9
    for k, (a, v, s) in enumerate(c.bench):
        hb = _hp(v) - entry_hazard(c.obs, v, s)
        if c.O is None:
            val = hb + 0.001 * sum(dex.base_stats(v.get("species", "")).values()) / 100
        else:
            dO = foe_best_d(c.hitO_B[k], c.presence)
            taken = 0.0
            if alive_out:                              # pivot mid-turn: the foe may not have moved yet
                reps = foe_replies(c)
                taken = 0.5 * sum(w * (c.hitO_B[k].get(m) or Hit()).e for m, w in reps)
            val = duel(hb - taken, hO, c.dB[k], dO, _first_p(c.sB[k], 0, c.sO, 0, c.tr)) + 0.1 * hb
        if val > best_v:
            best, best_v = a, val
    return best, f"switch-in by KO race ({best_v:.2f})"


def mon_key(species: str) -> str:
    """Who a Pokémon of our team is, across in-battle forme changes (Mimikyu -> Mimikyu-Busted, Aegislash-Shield /
    -Blade, Morpeko-Hangry, Minior-Meteor, Palafin-Hero): its base species (one per team under Species Clause)."""
    sid = to_id(species or "")
    base = (dex.species(sid) or {}).get("baseSpecies")
    return to_id(base) if base else sid


def is_fresh(pol, my_sp: str) -> bool:
    """Is our active on its first turn on the field (Fake Out / First Impression work; switching it straight back out
    is a switch loop)? True when it was not the Pokémon out at our previous normal decision (also turn 1)."""
    return pol.singles_prev.get("active") != mon_key(my_sp)


def protected_last(pol, my_sp: str) -> bool:
    return pol.singles_prev.get("protect") == mon_key(my_sp)


def remember(pol, c: Ctx, act: dict) -> None:
    """Memory for the next decision. A normal decision is a start-of-turn state: remember who is out NOW (whatever we
    chose: a Pokémon we switch in, or one dragged in, differs from it next turn and is fresh there). A forced-switch
    state is mid-turn (after a faint or our own pivot): its replacement's first turn is still to come, so nothing is
    remembered (the doubles convention, policy.py prev_active). Protect chains: only a normal decision's Protect."""
    my_sp = mon_key((c.A or {}).get("species", ""))
    if not c.force:
        pol.singles_prev["protect"] = my_sp if act.get("type") == "move" and to_id(act.get("move_id", "")) in PROTECT \
            else None
        pol.singles_prev["active"] = my_sp
    else:
        pol.singles_prev["protect"] = None


def decide(pol, st: dict, obs: dict) -> dict | None:
    c = build(pol, st, obs)
    if c is None or not c.la:
        return None
    pick = why = None
    if c.force or not c.moves_la:
        fs = forced_switch(pol, c)
        if fs is not None:
            pick, why = fs
    elif c.O is not None and c.setA is not None:
        scored = evaluate(pol, c)
        if scored:
            scored.sort(key=lambda x: -x[0])
            v, pick, why = scored[0]
            pol.singles_last = [(round(x[0], 3), x[1].get("action_id"), x[2]) for x in scored]
    remember(pol, c, ((pick or {}).get("input") or {}).get("action") or {})   # also when the v3 scorer answers
    if pick is None:
        return None
    pol.singles_why = why
    return {k: v for k, v in (pick.get("input") or {}).items() if k not in ("session_id", "state_version")} \
        or {"action_id": pick.get("action_id")}


# ---------------------------------------------------------------- draft (pokemon_gen9ou_draft)
SINGLES_ABILITIES = {"regenerator": 1.0, "magicguard": 1.0, "intimidate": 0.8, "goodasgold": 1.0, "multiscale": 0.8,
                     "unaware": 0.8, "supremeoverlord": 0.8, "protosynthesis": 0.5, "quarkdrive": 0.5, "levitate": 0.5,
                     "speedboost": 1.0, "hugepower": 1.5, "purepower": 1.5, "sheerforce": 0.5, "technician": 0.5,
                     "adaptability": 0.8, "download": 0.4, "moxie": 0.6, "prankster": 0.4, "sturdy": 0.3,
                     "toxicdebris": 0.4, "roughskin": 0.2, "ironbarbs": 0.2, "poisonheal": 0.8, "thickfat": 0.4}
SINGLES_DRAFT_MODEL = {"beta": 0.232, "prior": {"a": 0.0, "b": 0.02, "c": 0.0}}


def card_value(card: dict) -> float:
    """Singles draft prior on the scale of policy.card_score (which rewards VGC tools: Fake Out, Follow Me, spread
    moves, Tailwind): stats and speed, setup, recovery, hazards, pivots, priority, a few strong abilities."""
    sp = to_id(card.get("species", ""))
    bs = dex.base_stats(sp)
    bst = sum(bs.values()) or 500
    s = (bst - 500) / 25 + (0.8 if bs.get("spe", 0) >= 100 else 0.0)
    moves = [to_id(m) for m in card.get("moves") or [] if m]
    s += 1.5 * any(m in SETUP for m in moves)
    s += 1.0 * any(m in HEAL for m in moves)
    s += 0.8 * any(m in HAZARDS for m in moves)
    s += 0.6 * any(m in PIVOTS for m in moves)
    s += 0.5 * any(m in REMOVAL for m in moves)
    s += 0.6 * any(int(dex.move(m).get("priority", 0) or 0) > 0 and dex.move(m).get("category") != "Status"
                   for m in moves)
    s += SINGLES_ABILITIES.get(to_id(card.get("ability", "")), 0.0)
    item = to_id(card.get("item", ""))
    s += 0.4 if item in ("choicescarf", "choiceband", "choicespecs", "lifeorb", "heavydutyboots", "leftovers",
                         "boosterenergy", "assaultvest", "rockyhelmet") else 0.0
    return s


# ---------------------------------------------------------------- team preview (singles: all six battle, lead = first)
def preview(pol, st: dict, obs: dict) -> dict | None:
    """Lead with the Pokémon that wins the most KO races against the foe's roster (full HP, 1-hit calcs both ways);
    `bring` lists our four best by the same score (the platform orders the team by it: leads, rest of bring, others)."""
    from .policy import _damaging
    mine = [p for p in (obs.get("your_roster") or []) if isinstance(p, dict) and p.get("species")]
    opp = [p for p in (obs.get("opponent_roster") or []) if isinstance(p, dict) and p.get("species")]
    if len(mine) < 4 or not opp:
        return None
    dc = DamageCalc.get()
    if not dc.enabled:
        return None
    mode = mode_of(st, obs)
    levels = log_levels(obs)
    fake_obs = {"team": {f"p1: {p.get('name') or p['species']}": p for p in mine}, "protocol_log": []}
    my_sets = [own_set(pol, mode, dict(p, current_hp_fraction=1.0), levels) for p in mine]
    same_items = {to_id(p["species"]): p.get("item") for p in mine}
    foe_sets = [foe_set(pol, mode, dict(p, current_hp_fraction=1.0, item=(same_items.get(to_id(p["species"]))
                                                                          if mode == "same" else None) or "unknown_item"),
                        levels, fake_obs) for p in opp]
    field = {"gameType": "Singles", "attackerSide": {}, "defenderSide": {}}
    calcs, keys = [], []
    full = lambda p: dict(_cv(p), current_hp_fraction=1.0, boosts={})       # noqa: E731
    for i, (p, s) in enumerate(zip(mine, my_sets)):
        for j, (q, (fs, fm)) in enumerate(zip(opp, foe_sets)):
            a_in, d_in = calc_pokemon(full(p), s), calc_pokemon(full(q), fs)
            for mid in dict.fromkeys(s.get("moves") or []):
                if _damaging(dex.move(mid)):
                    keys.append(("me", i, j, mid))
                    calcs.append({"attacker": a_in, "defender": d_in, "move": move_name(mid), "field": field})
            for mid, pr in fm:
                if pr >= LIKELY and _damaging(dex.move(mid)):
                    keys.append(("opp", j, i, mid))
                    calcs.append({"attacker": d_in, "defender": a_in, "move": move_name(mid), "field": field})
    res = dc.batch(calcs)
    if res is None:
        return None
    res = _retry_as_base(dc, calcs, res)
    best_d: dict = {}
    for k, r in zip(keys, res):
        if not r or not r.get("rolls"):
            continue
        d = _acc(k[3]) * (sum(r["rolls"]) / len(r["rolls"])) / max(1, r.get("defender_hp") or 1)
        best_d[k[:3]] = max(best_d.get(k[:3], 0.0), d)
    from .policy import _speed_view
    spd_me = [effective_speed(_speed_view(full(p)), s, {}, {}) for p, s in zip(mine, my_sets)]
    spd_op = [effective_speed(_speed_view(dict(full(q), item=fs.get("item"))), fs, {}, {})
              for q, (fs, _) in zip(opp, foe_sets)]
    score = []
    for i in range(len(mine)):
        tot = 0.0
        for j in range(len(opp)):
            tot += race(1.0, 1.0, best_d.get(("me", i, j), 0.0), best_d.get(("opp", j, i), 0.0),
                        _first_p(spd_me[i], 0, spd_op[j], 0, False))
        score.append(tot / len(opp))
    order = sorted(range(len(mine)), key=lambda i: -score[i])
    bring = [mine[i]["species"] for i in order[:4]]
    leads = bring[:2]
    return {"action": {"type": "select_lineup", "bring": bring, "leads": leads},
            "reasoning_summary": f"lead {leads[0]}: best average matchup"}
