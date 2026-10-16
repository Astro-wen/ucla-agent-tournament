"""Team preview as a 90 x 90 matrix game (bring 4 of 6 + 2 leads, for both sides), solved with regret matching+.

Why a matrix game: at team preview both players pick simultaneously and both rosters are fully known (the
opponent's six sets were public during the draft and the policy cached them). Each side has C(6,4) * C(4,2) =
15 * 6 = 90 lineups. For every pair (our lineup, their lineup) we estimate a win value in [-1, 1] and solve the
zero-sum game for its equilibrium, then play the equilibrium lineup.

The payoff of one lineup pair comes from a fast deterministic "race" simulation of the whole 4-vs-4 doubles
battle that mirrors how our own battle policy (v1, exact damage) plays:
  * damage = mean of the 16 exact-calc rolls (one DamageCalc batch per preview, every attacker x move x
    defender of both rosters), x accuracy, x stat stages / burn / paralysis / Grassy Terrain / spread 0.75;
  * each turn both sides pick moves like v1: points = % of the target's current HP removed + 50 x P(KO), foe
    weights 0.7..1.3 by danger, Fake Out on fresh mons, Tailwind 70, Follow Me 35, Spore 55, Protect at low HP,
    the two slots chosen jointly (no double-targeting overkill, never two Fake Outs on one target);
  * turn order from priority (Prankster, Gale Wings, Grassy Glide) and real speed (Scarf, Booster Energy,
    Tailwind x2, Trick Room, paralysis, Icy Wind drops); an exact speed tie between the two sides is a coin
    flip (the payoff averages both orders); fainted targets retarget like Showdown;
  * Intimidate (Defiant / Competitive / immunities / Clear Amulet), Focus Sash, Multiscale, Sitrus, Leftovers,
    Life Orb and recoil, drain, self stat drops (Draco Meteor, Close Combat), Follow Me / Rage Powder
    redirection, Helping Hand, sleep / burn / paralysis;
  * a fainted slot is refilled with v1's own switch rule (resist the foes' STAB types, prefer HP).
The payoff is tanh(k_margin * margin), margin = our surviving HP fractions minus theirs at the end of the
race (a whole Pokemon = 1.0), so a narrow expected-damage win is worth less than a sweep.

Speed: 8100 races in pure Python are affordable because a battle "segment" (the turns between two faint
events) never looks at the unrevealed bench, so segments are memoised on the active Pokemon's state and shared
by every lineup pair that reaches the same state (all 36 bench variants share the lead exchange, etc.).

Everything is pure Python (no numpy on the deployment machine) and budgeted in work (race segments, v3-harden;
it was CPU time), with a wall-clock safety cap: `choose()` returns None or raises when anything is missing, fails,
or runs past its budget, and the caller falls back to the old heuristic preview. Knobs live in KNOBS; PREVIEW_MATRIX_KNOBS='{"mode": "argmax"}' overrides them (local tuning
runs only; the deployed agent never sets it).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import time
from itertools import combinations
from operator import itemgetter, mul

from . import dex
from .calc import DamageCalc, calc_pokemon
from .dex import to_id
from .stats import STAGE, effective_speed, stats_for
from .typechart import effectiveness

log = logging.getLogger("agent.preview")

KNOBS = {
    "mode": "argmax",        # argmax (equilibrium's most likely lineup) | sample (seeded by the match id) | off
    "k_margin": 1.0,         # payoff = tanh(k_margin * surviving-HP margin)
    "max_turns": 14,         # race length cap
    "rm_iters": 120,         # regret matching+ iterations per double-oracle round (warm-started)
    "eps": 0.01,             # double oracle stops when no best response gains more than this
    "search_share": 0.8,     # share of the budget the equilibrium search may use before answering
    "prior_w": 0.0,          # >0: best-respond to prior_w * (heuristic guess of their lineup) + rest * equilibrium
    "min_prob": 0.03,        # sample mode: drop equilibrium tails below this probability, renormalise
    # Work budget of the whole preview decision, in race segments (v3-harden; it was 1.6 s of CPU time). Segments
    # explain 97% of the search's CPU time (R^2 over 78 real + synthetic previews, scripts/harden_calib_preview.py):
    # ~11k segments per CPU second here, so 17,500 ~ the old 1.6 s. The converged search never needed more than
    # 9.2k (the double oracle's 60-round cap binds first), so like the old budget it does not bind in practice, but
    # the same inputs now always do the same work: the lineup can no longer depend on machine load.
    "work_units": 17500,
    "wall_cap_s": 6.0,       # wall-clock safety cap only (pathological load); it abandons the search like the budget
    "acc_mode": "expected",  # expected: damage x accuracy | full: ignore accuracy in the race
}
try:
    KNOBS.update(json.loads(os.environ.get("PREVIEW_MATRIX_KNOBS") or "{}"))
except Exception:           # a bad tuning override must never break the deployed agent
    log.warning("ignoring malformed PREVIEW_MATRIX_KNOBS")

# Showdown's team preview masks these formes ("Urshifu-*"); the platform then shows e.g. species "urshifu".
MASKED_FORMES = ("arceus", "gourgeist", "pumpkaboo", "xerneas", "silvally", "urshifu", "dudunsparce")

GENERIC_CARD = {"evs": {"hp": 4, "atk": 252, "spa": 252, "spe": 252}, "nature": "Hardy", "level": 50}
GENERIC_PHYS = {"NORMAL": "bodyslam", "FIRE": "flareblitz", "WATER": "waterfall", "ELECTRIC": "wildcharge",
                "GRASS": "seedbomb", "ICE": "iciclecrash", "FIGHTING": "closecombat", "POISON": "poisonjab",
                "GROUND": "highhorsepower", "FLYING": "bravebird", "PSYCHIC": "zenheadbutt", "BUG": "xscissor",
                "ROCK": "stoneedge", "GHOST": "shadowclaw", "DRAGON": "dragonclaw", "DARK": "crunch",
                "STEEL": "ironhead", "FAIRY": "playrough"}
GENERIC_SPEC = {"NORMAL": "triattack", "FIRE": "flamethrower", "WATER": "scald", "ELECTRIC": "thunderbolt",
                "GRASS": "energyball", "ICE": "icebeam", "FIGHTING": "aurasphere", "POISON": "sludgebomb",
                "GROUND": "earthpower", "FLYING": "airslash", "PSYCHIC": "psychic", "BUG": "bugbuzz",
                "ROCK": "powergem", "GHOST": "shadowball", "DRAGON": "dragonpulse", "DARK": "darkpulse",
                "STEEL": "flashcannon", "FAIRY": "moonblast"}
FIELD = {"gameType": "Doubles", "attackerSide": {}, "defenderSide": {}}

HALF_HP_MOVES = {"ruination", "superfang", "naturesmadness"}
PROTECT_MOVES = {"protect", "detect", "spikyshield", "silktrap", "banefulbunker", "kingsshield", "burningbulwark"}
REDIRECT_MOVES = {"followme", "ragepowder"}
SLEEP_MOVES = {"spore": 55.0, "sleeppowder": 25.0}
FLINCH_IMMUNE_ABILITIES = {"innerfocus", "shielddust"}
INTIMIDATE_IMMUNE = {"innerfocus", "owntempo", "oblivious", "scrappy", "clearbody", "whitesmoke",
                     "fullmetalbody", "hypercutter"}
KO_BONUS = 50.0
FAKE_OUT_BONUS = 1000.0
ALLY_HIT_WEIGHT = 0.9
SLEEP_TURNS = 2
SPREAD_SINGLE = 4.0 / 3.0       # a spread move with a lone target loses the 0.75 spread cut
MIN_USABLE_CALCS = 0.5          # fewer usable damage calcs than this share -> no matrix game (old preview)

_first = itemgetter(0)
# option kinds
K_ATK, K_FAKE, K_TW, K_TR, K_REDIR, K_SLEEP, K_WISP, K_TWAVE, K_PROTECT, K_HH, K_NONE = range(11)
# per-unit dynamic state layout
HP, ATK, DEF, SPA, SPD, SPE, STATUS, SLP, FRESH, SASH, SITRUS, LUM, PROT = range(13)
# global state layout: turn, tailwind turns (side 0, side 1), trick room, grassy terrain, psychic terrain
TURN, TW0, TW1, TR, GRASSY, PSY = range(6)
ST_SLP, ST_BRN, ST_PAR = 1, 2, 3


def base_species(sid: str) -> str:
    sid = to_id(sid)
    for b in MASKED_FORMES:
        if sid.startswith(b):
            return b
    return sid


def resolve_card(sid: str, store: dict, extra: dict | None = None) -> dict | None:
    """The drafted set for a roster species: exact id first, then a masked forme ("urshifu" -> the one
    "urshifu*" card in `store`, else in `extra`). None when unknown or ambiguous."""
    sid = to_id(sid)
    if sid in store:
        return store[sid]
    base = base_species(sid)
    if base != sid and sid not in MASKED_FORMES:
        # a full forme id ("urshifurapidstrike"): only that exact card, never another forme's
        return (extra or {}).get(sid)
    for pool in (store, extra or {}):
        hits = [c for k, c in pool.items() if base_species(k) == base]
        if len(hits) == 1:
            return hits[0]
    return None


# ------------------------------------------------------------------------------------------------ units
class Unit:
    """One roster slot with everything the race needs that does not change during a battle."""

    def __init__(self, side: int, view: dict | None, card: dict | None):
        view = view or {}
        known = bool(card and card.get("evs") is not None)
        species = (card or {}).get("species") or view.get("species") or ""
        self.side = side
        self.sid = to_id(species)
        self.types = dex.types_of(species) or [str(t).upper() for t in (view.get("types") or [])] or ["NORMAL"]
        self.ability = to_id((card or {}).get("ability") or view.get("ability") or "")
        item = to_id((card or {}).get("item") or view.get("item") or "")
        self.item = "" if item == "unknownitem" else item
        self.calc_card = dict(card) if known else {**GENERIC_CARD, "species": species,
                                                   "ability": self.ability or None, "item": self.item or None}
        st = stats_for(card if known else None, self.sid)
        self.maxhp = max(1, st["hp"])
        self.spe = effective_speed({"species": self.sid, "item": self.item, "ability": self.ability},
                                   card if known else None)
        bs = dex.base_stats(self.sid)
        self.physical = bs.get("atk", 80) >= bs.get("spa", 80)
        moves = [to_id(m) for m in ((card or {}).get("moves") or view.get("moves") or [])]
        if not moves:
            table = GENERIC_PHYS if self.physical else GENERIC_SPEC
            moves = [table[t] for t in self.types if t in table]
        self.moves = moves
        self.grounded = "FLYING" not in self.types and self.ability != "levitate" and self.item != "airballoon"
        self.flinch_immune = self.ability in FLINCH_IMMUNE_ABILITIES or self.item == "covertcloak"
        self.powder_immune = "GRASS" in self.types or self.ability == "overcoat" or self.item == "safetygoggles"
        self.sash = self.item == "focussash"
        self.multiscale = self.ability in ("multiscale", "shadowshield")
        self.intimidate = self.ability == "intimidate"
        if self.item == "clearamulet" or self.ability in INTIMIDATE_IMMUNE:
            self.intim_react = 0            # immune
        elif self.ability == "defiant":
            self.intim_react = 1            # -1 atk then +2 atk
        elif self.ability == "competitive":
            self.intim_react = 2            # -1 atk, +2 spa
        elif self.ability == "guarddog":
            self.intim_react = 3            # +1 atk
        elif self.ability == "mirrorarmor":
            self.intim_react = 4            # bounced back
        else:
            self.intim_react = 5            # -1 atk
        self.drop_immune = self.item == "clearamulet" or self.ability in ("clearbody", "whitesmoke", "fullmetalbody")
        self.prankster = self.ability == "prankster"
        self.galewings = self.ability == "galewings"
        self.grassy_surge = self.ability == "grassysurge"
        self.psychic_surge = self.ability == "psychicsurge"
        self.lifeorb = self.item == "lifeorb"
        self.leftovers = self.item == "leftovers"
        self.dark = "DARK" in self.types
        self.fire = "FIRE" in self.types
        self.electric_or_ground = "ELECTRIC" in self.types or "GROUND" in self.types
        self.sleep_blocked_v1 = "GRASS" in self.types or self.ability == "overcoat"   # what v1 believes

    def fresh_state(self) -> list:
        return [1.0, 0, 0, 0, 0, 0, 0, 0, True, self.sash, self.item == "sitrusberry", self.item == "lumberry", False]


def _damaging(mid: str) -> bool:
    m = dex.move(mid)
    if not m:
        return False
    if (m.get("category") or "").lower() == "status":
        return False
    return bool(m.get("basePower")) or mid in HALF_HP_MOVES or bool(m.get("basePowerCallback"))


def _acc(m: dict) -> float:
    a = m.get("accuracy", 100)
    if a is True or a is None:
        return 1.0
    try:
        a = float(a)
    except (TypeError, ValueError):
        return 1.0
    return a / 100 if a > 1 else a


def _status_kind(mid: str):
    if mid == "tailwind":
        return K_TW, 70.0
    if mid == "trickroom":
        return K_TR, 20.0
    if mid in REDIRECT_MOVES:
        return K_REDIR, 35.0
    if mid in SLEEP_MOVES:
        return K_SLEEP, SLEEP_MOVES[mid]
    if mid == "willowisp":
        return K_WISP, 35.0
    if mid == "thunderwave":
        return K_TWAVE, 20.0
    if mid in PROTECT_MOVES:
        return K_PROTECT, 5.0
    if mid == "helpinghand":
        return K_HH, 15.0
    if mid in ("taunt", "encore", "disable"):
        return K_NONE, 10.0
    return K_NONE, 8.0


# ------------------------------------------------------------------------------------------------ tables
class Tables:
    """Static per-preview data: units of both sides and the exact damage table from ONE calc batch."""

    def __init__(self, units: list[Unit]):
        self.units = units
        n = len(units)
        self.n = n
        calcs, keys = [], []
        calc_in = [calc_pokemon(None, u.calc_card, u.sid) for u in units]
        calc_hurt = [calc_pokemon({"current_hp_fraction": 0.99}, u.calc_card, u.sid) if u.multiscale else None
                     for u in units]
        for x, a in enumerate(units):
            for mid in dict.fromkeys(a.moves):
                if not _damaging(mid) or mid in HALF_HP_MOVES:
                    continue
                m = dex.move(mid)
                name = m.get("name") or mid
                for y, d in enumerate(units):
                    if y == x or (d.side == a.side and m.get("target") != "allAdjacent"):
                        continue
                    keys.append((x, mid, y, 0))
                    calcs.append({"attacker": calc_in[x], "defender": calc_in[y], "move": name, "field": FIELD})
                    if calc_hurt[y] is not None and d.side != a.side:
                        keys.append((x, mid, y, 1))
                        calcs.append({"attacker": calc_in[x], "defender": calc_hurt[y], "move": name, "field": FIELD})
        res = DamageCalc.get().batch(calcs) if calcs else []
        if res is None:
            raise RuntimeError("damage calc unavailable")
        dmg: dict = {}
        for k, r in zip(keys, res):
            if not r or not r.get("rolls") or not r.get("defender_hp"):
                continue
            hp = float(r["defender_hp"])
            rolls = r["rolls"]
            dmg[k] = (sum(rolls) / len(rolls) / hp, min(rolls) / hp, max(rolls) / hp)
        # Normally every calc answers (0 unusable in 80 sampled previews). calc.js answering {"error"} to
        # everything, or DamageCalc's cache overflowing mid-batch (it is cleared before the results are read),
        # yields an all-zero table whose "equilibrium" is just lineup 0: let the caller use the old preview.
        if len(dmg) < MIN_USABLE_CALCS * len(calcs):
            raise RuntimeError(f"damage calc answered only {len(dmg)}/{len(calcs)} calcs")
        self.n_calcs = len(calcs)
        zero = (0.0, 0.0, 0.0)
        self.attacks: list[list[tuple]] = []
        self.status: list[list[tuple]] = []
        self.fakeout: list[tuple | None] = []
        self.threat = [[0.0] * n for _ in range(n)]      # best acc x mean damage fraction x -> y (danger weights)
        for x, a in enumerate(units):
            atks, stats_, fo = [], [], None
            for mid in dict.fromkeys(a.moves):
                m = dex.move(mid)
                if not m:
                    continue
                prio = int(m.get("priority", 0) or 0)
                acc = _acc(m)
                if _damaging(mid):
                    full = [dmg.get((x, mid, y, 0), zero) for y in range(n)]
                    hurt = [dmg.get((x, mid, y, 1)) or dmg.get((x, mid, y, 0), zero) for y in range(n)]
                    rec = self._move_rec(a, mid, m, prio, acc, full, hurt)
                    if mid == "fakeout":
                        fo = rec
                    else:
                        atks.append(rec)
                    for y in range(n):
                        if units[y].side != a.side:
                            v = 0.5 if mid in HALF_HP_MOVES else full[y][0]
                            self.threat[x][y] = max(self.threat[x][y], acc * v)
                else:
                    kind, val = _status_kind(mid)
                    if a.prankster:
                        prio += 1
                    stats_.append((kind, mid, prio, acc, val))
            self.attacks.append(atks)
            self.status.append(stats_)
            self.fakeout.append(fo)
        # v1's forced-switch rule: 10 - 3 * sum over foe STAB types of (effectiveness - 1) + 10 * hp
        self.switch_pen = [[sum(3.0 * (effectiveness(t, units[i].types) - 1) for t in units[j].types)
                            for j in range(n)] for i in range(n)]

    @staticmethod
    def _move_rec(a: Unit, mid: str, m: dict, prio: int, acc: float, full: list, hurt: list) -> tuple:
        tgt = m.get("target")
        spread = 2 if tgt == "allAdjacent" else 1 if tgt == "allAdjacentFoes" else 0
        phys = (m.get("category") or "").lower() == "physical"
        recoil = m.get("recoil")
        recoil = recoil[0] / recoil[1] if isinstance(recoil, list) and len(recoil) == 2 and recoil[1] else 0.0
        drain = m.get("drain")
        drain = drain[0] / drain[1] if isinstance(drain, list) and len(drain) == 2 and drain[1] else 0.0
        selfm = m.get("self") if isinstance(m.get("self"), dict) else {}
        sb = selfm.get("boosts") if isinstance(selfm.get("boosts"), dict) else {}
        selfdrop = (sb.get("atk", 0), sb.get("def", 0), sb.get("spa", 0), sb.get("spd", 0), sb.get("spe", 0))
        sec = m.get("secondary") or {}
        secdrop = None
        if isinstance(sec, dict) and (sec.get("chance") or 0) >= 100 and isinstance(sec.get("boosts"), dict):
            b = sec["boosts"]
            secdrop = (b.get("atk", 0), b.get("def", 0), b.get("spa", 0), b.get("spd", 0), b.get("spe", 0))
        mtype = (m.get("type") or "").upper()
        # 0 mid, 1 prio, 2 acc, 3 phys, 4 spread, 5 full, 6 hurt, 7 recoil, 8 drain, 9 selfdrop, 10 secdrop,
        # 11 multihit, 12 grass, 13 earthquake-like, 14 half-hp, 15 gale wings
        return (mid, prio, acc, phys, spread, full, hurt, recoil, drain,
                selfdrop if any(selfdrop) else None, secdrop, bool(m.get("multihit")),
                mtype == "GRASS", mid in ("earthquake", "bulldoze", "magnitude"), mid in HALF_HP_MOVES,
                a.galewings and mtype == "FLYING")


def _pts(d: float, lo: float, hi: float, h: float) -> float:
    """v1's points: 100 x fraction of the target's current HP removed + KO_BONUS x P(KO) (rolls ~ uniform lo..hi)."""
    if h <= 0:
        return 0.0
    useful = d if d < h else h
    if lo >= h:
        pko = 1.0
    elif hi < h:
        pko = 0.0
    else:
        pko = (hi - h) / (hi - lo) if hi > lo else 1.0
    return 100.0 * useful / h + KO_BONUS * pko


# ------------------------------------------------------------------------------------------------ race
class Race:
    """Deterministic expected-damage simulation of one 4-vs-4 doubles battle (see the module docstring).
    Segments between faint events are memoised on (global state, actives and their dynamic state).

    Speed ties: Showdown breaks an exact speed tie with a coin flip. `run(..., tie_side)` lets side `tie_side`
    win every cross-side tie and records in `self.tied` whether one occurred; `value()` averages the payoffs
    of the two orders when it did, so the race favours neither seat (it used to hand side 0 = us every tie)."""

    def __init__(self, tb: Tables, max_turns: int = 14, acc_mode: str = "expected"):
        self.tb = tb
        self.max_turns = max_turns
        self.use_acc = acc_mode != "full"
        self.memo: dict = {}        # segments run with side 0 winning ties (valid for both orders if untied)
        self.memo_alt: dict = {}    # tied segments run with side 1 winning ties
        self.tie_side = 0
        self.tied = False
        self.segments = 0

    def value(self, la: tuple, lb: tuple, k: float = 1.0) -> float:
        """Payoff tanh(k * margin), averaged over both winners of any cross-side speed tie (a coin flip)."""
        m = self.run(la, lb, 0)
        if not self.tied:
            return math.tanh(k * m)
        return 0.5 * (math.tanh(k * m) + math.tanh(k * self.run(la, lb, 1)))

    def run(self, la: tuple, lb: tuple, tie_side: int = 0) -> float:
        """Margin (our surviving HP fractions minus theirs; a whole Pokemon = 1.0) for unit-index lineups
        (lead, lead, bench, bench), side `tie_side` winning exact cross-side speed ties. Sets `self.tied`."""
        U = self.tb.units
        self.tie_side = tie_side
        tied = False
        alt = self.memo_alt
        P: dict = {}
        active = [[la[0], la[1]], [lb[0], lb[1]]]
        bench = [list(la[2:]), list(lb[2:])]
        g = [0, 0, 0, 0, 0, 0]
        for s in (0, 1):
            for x in active[s]:
                P[x] = U[x].fresh_state()
        for s in (0, 1):
            for x in active[s]:
                self._enter(x, s, active, P, g)
        memo = self.memo
        max_turns = self.max_turns
        while True:
            a0, a1 = active[0]
            b0, b1 = active[1]
            key = (g[0], g[1], g[2], g[3], g[4], g[5], a0, a1, b0, b1,
                   None if a0 is None else tuple(P[a0]), None if a1 is None else tuple(P[a1]),
                   None if b0 is None else tuple(P[b0]), None if b1 is None else tuple(P[b1]))
            res = memo.get(key)
            if tie_side and (res is None or res[2]):
                res = alt.get(key)
                if res is None:
                    res = self._segment(g, active, P)
                    (alt if res[2] else memo)[key] = res    # an untied segment is the same in both orders
            elif res is None:
                res = self._segment(g, active, P)
                memo[key] = res
            tied = tied or res[2]
            g = list(res[0])
            for x, st in zip((a0, a1, b0, b1), res[1]):
                if x is not None:
                    P[x] = list(st)
            for s in (0, 1):
                for k in (0, 1):
                    x = active[s][k]
                    if x is not None and P[x][HP] <= 0:
                        active[s][k] = None
            if g[TURN] >= max_turns:
                break
            entering = []
            foes_of = [[y for y in active[1] if y is not None], [y for y in active[0] if y is not None]]
            for s in (0, 1):
                foes = foes_of[s]              # both sides pick replacements simultaneously
                for k in (0, 1):
                    if active[s][k] is None and bench[s]:
                        best, best_v = None, -1e9
                        for b in bench[s]:
                            v = 10.0 - sum(self.tb.switch_pen[b][y] for y in foes) + 10.0
                            if v > best_v:
                                best, best_v = b, v
                        bench[s].remove(best)
                        active[s][k] = best
                        P[best] = U[best].fresh_state()
                        entering.append((best, s))
            for x, s in entering:
                self._enter(x, s, active, P, g)
            if (active[0][0] is None and active[0][1] is None) or (active[1][0] is None and active[1][1] is None):
                break
        ours = sum(P[x][HP] if x in P else 1.0 for x in la)
        theirs = sum(P[x][HP] if x in P else 1.0 for x in lb)
        self.tied = tied
        return ours - theirs

    # ---- pieces -----------------------------------------------------------------------------
    def _enter(self, x: int, s: int, active, P, g) -> None:
        U = self.tb.units
        u = U[x]
        if u.grassy_surge:
            g[GRASSY] = 5
        if u.psychic_surge:
            g[PSY] = 5
        if not u.intimidate:
            return
        for y in active[1 - s]:
            if y is None or P[y][HP] <= 0:
                continue
            r = U[y].intim_react
            st = P[y]
            if r == 5:
                st[ATK] = max(-6, st[ATK] - 1)
            elif r == 1 or r == 3:
                st[ATK] = min(6, st[ATK] + 1)
            elif r == 2:
                st[ATK] = max(-6, st[ATK] - 1)
                st[SPA] = min(6, st[SPA] + 2)
            elif r == 4:
                P[x][ATK] = max(-6, P[x][ATK] - 1)

    def _segment(self, g0, act0, P0):
        """Run whole turns until one ends with a faint (or the turn cap). Only the actives are touched."""
        self.segments += 1
        U = self.tb.units
        g = list(g0)
        active = [list(act0[0]), list(act0[1])]
        P = {x: list(P0[x]) for s in (0, 1) for x in active[s] if x is not None}
        max_turns = self.max_turns
        tie_side = self.tie_side
        tied = False
        while g[TURN] < max_turns:
            g[TURN] += 1
            turn = g[TURN]
            acts = []
            for s in (0, 1):
                acts.extend(self._decide(s, turn, active, P, g))
            if not acts:
                break
            tr = g[TR] > 0
            order = []
            first_side: dict = {}
            for x, kind, rec, tgt, prio in acts:
                px = P[x]
                side = U[x].side
                sp = U[x].spe * STAGE[px[SPE]]
                if g[TW0 + side]:
                    sp *= 2.0
                if px[STATUS] == ST_PAR:
                    sp *= 0.5
                spk = -sp if tr else sp
                if not tied and first_side.setdefault((prio, spk), side) != side:
                    tied = True                 # an exact speed tie across the two sides this turn
                # ties: side `tie_side` first, then the lower unit index (allies)
                order.append((prio, spk, side == tie_side, -x, x, kind, rec, tgt))
            order.sort(reverse=True)
            acted, flinch, protected = set(), set(), set()
            redirect = [None, None]
            hh = {}
            for _p, _sp, _t, _x, x, kind, rec, tgt in order:
                px = P[x]
                if px[HP] <= 0 or x in flinch:
                    continue
                acted.add(x)
                if px[STATUS] == ST_SLP:
                    if px[SLP] > 0:
                        px[SLP] -= 1
                        continue
                    px[STATUS] = 0
                s = U[x].side
                if kind == K_ATK or kind == K_FAKE:
                    self._attack(x, s, rec, tgt, kind, active, P, protected, redirect, hh, flinch, acted, g)
                elif kind == K_PROTECT:
                    if not px[PROT]:
                        protected.add(x)
                elif kind == K_TW:
                    g[TW0 + s] = 4
                elif kind == K_TR:
                    g[TR] = 0 if g[TR] > 0 else 5
                elif kind == K_REDIR:
                    redirect[s] = x
                elif kind == K_HH:
                    ally = active[s][1] if active[s][0] == x else active[s][0]
                    if ally is not None and P[ally][HP] > 0:
                        hh[ally] = 1.5
                elif kind in (K_SLEEP, K_WISP, K_TWAVE):
                    y = self._retarget(tgt, 1 - s, active, P)
                    if y is None or y in protected or (U[x].prankster and U[y].dark):
                        continue
                    py = P[y]
                    if py[STATUS]:
                        continue
                    uy = U[y]
                    if (kind == K_SLEEP and uy.powder_immune) or (kind == K_WISP and uy.fire) \
                            or (kind == K_TWAVE and uy.electric_or_ground):
                        continue
                    if py[LUM]:
                        py[LUM] = False
                        continue
                    if kind == K_SLEEP:
                        py[STATUS], py[SLP] = ST_SLP, SLEEP_TURNS
                    else:
                        py[STATUS] = ST_BRN if kind == K_WISP else ST_PAR
            # ---- end of turn
            fainted = False
            for x, px in P.items():
                px[PROT] = x in protected
                if px[HP] <= 0:
                    fainted = True
                    continue
                heal = 0.0
                if U[x].leftovers:
                    heal += 0.0625
                if g[GRASSY] > 0 and U[x].grounded:
                    heal += 0.0625
                if px[STATUS] == ST_BRN:
                    heal -= 0.0625
                if heal:
                    px[HP] = min(1.0, px[HP] + heal)
                    if px[HP] <= 1e-9:
                        px[HP] = 0.0
                        fainted = True
                px[FRESH] = False
            for k in (TW0, TW1, TR, GRASSY, PSY):
                if g[k]:
                    g[k] -= 1
            if fainted:
                break
        a0, a1 = active[0]
        b0, b1 = active[1]
        return (tuple(g), tuple(None if x is None else tuple(P[x]) for x in (a0, a1, b0, b1)), tied)

    @staticmethod
    def _retarget(tgt, o: int, active, P):
        if tgt is not None and tgt in active[o] and P[tgt][HP] > 0:
            return tgt
        for y in active[o]:
            if y is not None and P[y][HP] > 0:
                return y
        return None

    def _dmg(self, x, rec, y, P, g, n_t):
        """(mean, min, max) damage as a fraction of y's max HP, in the current state (no accuracy)."""
        if rec[14]:                                         # Ruination & co: half the target's current HP
            h = P[y][HP] * 0.5
            return h, h, h
        U = self.tb.units
        py = P[y]
        d, lo, hi = (rec[6] if (U[y].multiscale and py[HP] < 1.0) else rec[5])[y]
        if not d:
            return 0.0, 0.0, 0.0
        px = P[x]
        if rec[3]:
            f = STAGE[px[ATK]] / STAGE[py[DEF]]
            if px[STATUS] == ST_BRN:
                f *= 0.5
        else:
            f = STAGE[px[SPA]] / STAGE[py[SPD]]
        if rec[4] and n_t == 1:
            f *= SPREAD_SINGLE
        if g[GRASSY] > 0:
            if rec[12] and U[x].grounded:
                f *= 1.3
            if rec[13] and U[y].grounded:
                f *= 0.5
        return d * f, lo * f, hi * f

    def _decide(self, s: int, turn: int, active, P, g) -> list:
        """v1-like joint choice for side s: list of (unit, kind, rec, target unit, priority)."""
        tb = self.tb
        mine = [x for x in active[s] if x is not None and P[x][HP] > 0]
        foes = [y for y in active[1 - s] if y is not None and P[y][HP] > 0]
        if not mine or not foes:
            return []
        w = {}
        threat = tb.threat
        for y in foes:
            d = 0.0
            ty = threat[y]
            for x in mine:
                h = P[x][HP]
                t = ty[x]
                v = (t if t < h else h) / h
                if v > d:
                    d = v
            w[y] = 0.7 + 0.6 * d
        if len(mine) == 1:
            x = mine[0]
            c = max(self._options(x, s, turn, mine, foes, w, P, g), key=_first)
            return [(x, c[1], c[2], c[3], c[4])]
        xa, xb = mine
        oa = self._options(xa, s, turn, mine, foes, w, P, g)
        ob = self._options(xb, s, turn, mine, foes, w, P, g)
        fa = max(oa, key=_first)
        fb = max(ob, key=_first)
        # fast path: the two solo-best choices do not interact
        if not ((fa[1] == K_FAKE and fb[1] == K_FAKE and fa[3] == fb[3])
                or (fa[5] and fb[5] and not fa[5].keys().isdisjoint(fb[5].keys()))):
            return [(xa, fa[1], fa[2], fa[3], fa[4]), (xb, fb[1], fb[2], fb[3], fb[4])]
        best, best_v = None, -1e18
        overlap = self._overlap
        for first, second, a_first in ((fa, ob, True), (fb, oa, False)):
            sv, sc = -1e18, None
            fk, ft, fh = first[1], first[3], first[5]
            for c in second:
                v = c[0]
                if fk == K_FAKE and c[1] == K_FAKE and ft == c[3]:
                    v -= FAKE_OUT_BONUS
                elif fh and c[5] and not fh.keys().isdisjoint(c[5].keys()):
                    v += overlap(first, c, P)
                if v > sv:
                    sv, sc = v, c
            if first[0] + sv > best_v:
                best_v = first[0] + sv
                best = (first, sc) if a_first else (sc, first)
        ca, cb = best
        return [(xa, ca[1], ca[2], ca[3], ca[4]), (xb, cb[1], cb[2], cb[3], cb[4])]

    @staticmethod
    def _overlap(a: tuple, b: tuple, P) -> float:
        """Correction when both slots hit the same foe: joint KO odds instead of the sum; two Fake Outs on
        one target are forbidden (v1)."""
        if a[1] == K_FAKE and b[1] == K_FAKE and a[3] == b[3]:
            return -FAKE_OUT_BONUS
        ha, hb = a[5], b[5]
        if not ha or not hb:
            return 0.0
        corr = 0.0
        for y, (da, la, ua, pa, wy) in ha.items():
            hb_y = hb.get(y)
            if hb_y is None:
                continue
            db, lb, ub, pb, _ = hb_y
            h = P[y][HP]
            pa_pts = _pts(da, la, ua, h)
            pb_pts = _pts(db, lb, ub, h)
            both = _pts(da + db, la + lb, ua + ub, h)
            joint = pa * pb * both + pa * (1 - pb) * pa_pts + (1 - pa) * pb * pb_pts
            corr += wy * (joint - pa * pa_pts - pb * pb_pts)
        return corr

    def _options(self, x, s, turn, mine, foes, w, P, g) -> list:
        """(value, kind, rec, target, priority, hits{y: (d, lo, hi, acc, weight)}) for every choice of x."""
        tb = self.tb
        U = tb.units
        u = U[x]
        px = P[x]
        out = []
        ally = mine[1] if mine[0] == x and len(mine) > 1 else (mine[0] if mine[0] != x else None)
        fo = tb.fakeout[x]
        if fo is not None and px[FRESH]:
            for y in foes:
                if U[y].flinch_immune or (g[PSY] > 0 and U[y].grounded):
                    continue
                out.append((FAKE_OUT_BONUS + 100.0 * (w[y] - 0.7) / 0.6, K_FAKE, fo, y, 3, None))
        nf = len(foes)
        for rec in tb.attacks[x]:
            prio, acc, spread = rec[1], rec[2], rec[4]
            if rec[15] and px[HP] >= 1.0:
                prio += 1
            if g[GRASSY] > 0 and rec[0] == "grassyglide" and u.grounded:
                prio += 1
            if spread:
                n_t = nf + (1 if spread == 2 and ally is not None else 0)
                val, hits = 0.0, {}
                for y in foes:
                    d, lo, hi = self._dmg(x, rec, y, P, g, n_t)
                    val += w[y] * acc * _pts(d, lo, hi, P[y][HP])
                    hits[y] = (d, lo, hi, acc, w[y])
                if spread == 2 and ally is not None:
                    d, lo, hi = self._dmg(x, rec, ally, P, g, n_t)
                    val -= ALLY_HIT_WEIGHT * acc * _pts(d, lo, hi, P[ally][HP])
                out.append((val, K_ATK, rec, None, prio, hits))
            else:
                for y in foes:
                    d, lo, hi = self._dmg(x, rec, y, P, g, 1)
                    out.append((w[y] * acc * _pts(d, lo, hi, P[y][HP]), K_ATK, rec, y, prio,
                                {y: (d, lo, hi, acc, w[y])}))
        for kind, mid, prio, acc, val in tb.status[x]:
            if kind == K_TW:
                out.append((70.0 if not g[TW0 + s] else -10.0, kind, None, None, prio, None))
            elif kind == K_TR:
                out.append((20.0 if turn <= 2 else 5.0, kind, None, None, prio, None))
            elif kind == K_REDIR:
                out.append((35.0 if turn <= 1 else 15.0, kind, None, None, prio, None))
            elif kind == K_PROTECT:
                out.append((45.0 if px[HP] < 0.35 else 5.0, kind, None, None, prio, None))
            elif kind == K_HH:
                if ally is not None:
                    out.append((15.0, kind, None, None, prio, None))
            elif kind in (K_SLEEP, K_WISP, K_TWAVE):
                for y in foes:
                    uy = U[y]
                    if P[y][STATUS]:
                        v = 0.0
                    elif kind == K_SLEEP:
                        v = 0.0 if uy.sleep_blocked_v1 else val
                    elif kind == K_WISP:
                        v = 0.0 if uy.fire else (35.0 if uy.physical else 15.0)
                    else:
                        v = 0.0 if uy.electric_or_ground else 20.0
                    out.append((v, kind, None, y, prio, None))
            else:
                out.append((val, K_NONE, None, None, prio, None))
        if not out:
            out.append((0.0, K_NONE, None, None, 0, None))
        return out

    def _attack(self, x, s, rec, tgt, kind, active, P, protected, redirect, hh, flinch, acted, g) -> None:
        U = self.tb.units
        o = 1 - s
        spread = rec[4]
        if spread:
            targets = [y for y in active[o] if y is not None and P[y][HP] > 0]
            if spread == 2:
                targets += [a for a in active[s] if a is not None and a != x and P[a][HP] > 0]
        else:
            r = redirect[o]
            y = r if (r is not None and P[r][HP] > 0) else self._retarget(tgt, o, active, P)
            targets = [y] if y is not None else []
        if not targets:
            return
        n_t = len(targets)
        px = P[x]
        mult = (rec[2] if self.use_acc else 1.0) * (0.75 if px[STATUS] == ST_PAR else 1.0) * hh.get(x, 1.0)
        dealt = 0.0
        for y in targets:
            if y in protected:
                continue
            d, _lo, _hi = self._dmg(x, rec, y, P, g, n_t)
            d *= mult
            if d <= 0:
                continue
            py = P[y]
            if py[SASH] and py[HP] >= 1.0 and d >= 1.0 and not rec[11]:
                d = 0.99
                py[SASH] = False
            before = py[HP]
            py[HP] = before - d
            dealt += (d if d < before else before) * U[y].maxhp
            if py[HP] <= 1e-9:
                py[HP] = 0.0
                continue
            if py[SITRUS] and py[HP] < 0.5:
                py[SITRUS] = False
                py[HP] = min(1.0, py[HP] + 0.25)
            if kind == K_FAKE and y not in acted and not U[y].flinch_immune:
                flinch.add(y)
            sec = rec[10]
            if sec is not None and not U[y].drop_immune:
                for k in range(5):
                    if sec[k]:
                        py[ATK + k] = max(-6, min(6, py[ATK + k] + sec[k]))
        if dealt > 0:
            ux = U[x]
            loss = 0.0
            if rec[7]:
                loss += rec[7] * dealt / ux.maxhp
            if ux.lifeorb:
                loss += 0.1
            if rec[8]:
                loss -= rec[8] * dealt / ux.maxhp
            if loss:
                px[HP] = min(1.0, px[HP] - loss)
                if px[HP] <= 1e-9:
                    px[HP] = 0.0
            sd = rec[9]
            if sd is not None:
                for k in range(5):
                    if sd[k]:
                        px[ATK + k] = max(-6, min(6, px[ATK + k] + sd[k]))


# ------------------------------------------------------------------------------------------------ game
def lineups(n: int = 6) -> list[tuple]:
    """All (lead, lead, bench, bench) index tuples: C(n,4) * C(4,2) = 90 for n = 6."""
    out = []
    for bring in combinations(range(n), 4):
        for leads in combinations(bring, 2):
            rest = tuple(i for i in bring if i not in leads)
            out.append(leads + rest)
    return out


class Deadline:
    """Work budget (v3-harden): `units` of work read from `counter()` (the preview's race segments computed so far),
    plus a wall-clock safety cap. Counting work instead of CPU time makes the search do exactly the same work, hence
    give the same answer, whatever the machine load or speed; the wall cap only bounds pathological cases and is
    reported (`wall_hit`). Without a counter only the wall cap applies."""

    def __init__(self, units: float, wall_s: float, counter=None):
        self.units = float(units)
        self.wall = time.perf_counter() + wall_s
        self.counter = counter or (lambda: 0)
        self.wall_hit = False

    def passed(self) -> bool:
        if self.counter() > self.units:
            return True
        if time.perf_counter() > self.wall:
            self.wall_hit = True
            return True
        return False


def payoff_matrix(tb: Tables, ours: list[int], theirs: list[int], knobs: dict, deadline: Deadline):
    race = Race(tb, int(knobs.get("max_turns", 14)), str(knobs.get("acc_mode", "expected")))
    k = float(knobs.get("k_margin", 1.0))
    la = [tuple(ours[i] for i in t) for t in lineups(len(ours))]
    lb = [tuple(theirs[i] for i in t) for t in lineups(len(theirs))]
    rows = []
    for a in la:
        if deadline.passed():
            raise TimeoutError("payoff matrix over budget")
        rows.append([race.value(a, b, k) for b in lb])
    return rows, race


def solve_rm_plus(A: list[list[float]], iters: int = 400, deadline: Deadline | None = None,
                  state: dict | None = None):
    """Zero-sum matrix game (row maximises A). Alternating regret matching+ with linear averaging.
    Returns (row strategy, column strategy, value of the averaged profile). Deterministic.
    `state` (a dict, updated in place) warm-starts from a previous solve of a smaller game whose rows/columns
    are a prefix of this one's (double oracle only ever appends)."""
    n, m = len(A), len(A[0])
    AT = [list(c) for c in zip(*A)]
    if state and state.get("qr"):
        qr = state["qr"] + [0.0] * (n - len(state["qr"]))
        qc = state["qc"] + [0.0] * (m - len(state["qc"]))
        sr = state["sr"] + [0.0] * (n - len(state["sr"]))
        sc = state["sc"] + [0.0] * (m - len(state["sc"]))
        t0 = state["t"]
    else:
        qr, qc, sr, sc, t0 = [0.0] * n, [0.0] * m, [0.0] * n, [0.0] * m, 0
    tot = sum(qr)
    xr = [q / tot for q in qr] if tot > 0 else [1.0 / n] * n
    tot = sum(qc)
    xc = [q / tot for q in qc] if tot > 0 else [1.0 / m] * m
    t = t0
    for t in range(t0 + 1, t0 + iters + 1):
        u = [sum(map(mul, row, xc)) for row in A]
        ev = sum(map(mul, u, xr))
        qr = [q + ui - ev if q + ui - ev > 0 else 0.0 for q, ui in zip(qr, u)]
        tot = sum(qr)
        xr = [q / tot for q in qr] if tot > 0 else [1.0 / n] * n
        sr = [a + t * b for a, b in zip(sr, xr)]
        v = [sum(map(mul, col, xr)) for col in AT]
        ev = sum(map(mul, v, xc))
        qc = [q - vj + ev if q - vj + ev > 0 else 0.0 for q, vj in zip(qc, v)]
        tot = sum(qc)
        xc = [q / tot for q in qc] if tot > 0 else [1.0 / m] * m
        sc = [a + t * b for a, b in zip(sc, xc)]
        if deadline is not None and (t & 31) == 0 and deadline.passed():
            break
    if state is not None:
        state.update(qr=qr, qc=qc, sr=sr, sc=sc, t=t)
    zr, zc = sum(sr) or 1.0, sum(sc) or 1.0
    pr, pc = [a / zr for a in sr], [a / zc for a in sc]
    value = sum(p * sum(map(mul, row, pc)) for p, row in zip(pr, A))
    return pr, pc, value


def exploitability(A, pr, pc) -> float:
    """Best-response gap of a profile (0 at an exact equilibrium)."""
    best_row = max(sum(map(mul, row, pc)) for row in A)
    best_col = min(sum(map(mul, col, pr)) for col in zip(*A))
    return best_row - best_col


def pick(pr: list[float], A, mode: str, seed_text: str, min_prob: float = 0.03) -> int:
    """Row to play: argmax of the equilibrium, pure maximin, or a sample from the (tail-trimmed)
    equilibrium seeded by `seed_text` (the match id), so a replay reproduces the same lineup."""
    if mode == "maximin" and A is not None:
        return max(range(len(A)), key=lambda i: (min(A[i]), -i))
    if mode in ("argmax", "maximin"):
        return max(range(len(pr)), key=lambda i: (pr[i], -i))
    keep = [(i, p) for i, p in enumerate(pr) if p >= min_prob]
    if not keep:
        return max(range(len(pr)), key=lambda i: (pr[i], -i))
    rng = random.Random(int(hashlib.sha256(seed_text.encode()).hexdigest()[:16], 16))
    r = rng.random() * sum(p for _, p in keep)
    for i, p in keep:
        r -= p
        if r <= 0:
            return i
    return keep[-1][0]


class LazyPayoff:
    """A[r][c] = tanh(k * race margin) of our lineup r vs their lineup c, computed on first use."""

    def __init__(self, tb: Tables, ours: list[int], theirs: list[int], knobs: dict):
        self.race = Race(tb, int(knobs.get("max_turns", 14)), str(knobs.get("acc_mode", "expected")))
        self.k = float(knobs.get("k_margin", 1.0))
        self.la = [tuple(ours[i] for i in t) for t in lineups(len(ours))]
        self.lb = [tuple(theirs[i] for i in t) for t in lineups(len(theirs))]
        self.cache: dict = {}

    def __call__(self, r: int, c: int) -> float:
        v = self.cache.get((r, c))
        if v is None:
            v = self.cache[(r, c)] = self.race.value(self.la[r], self.lb[c], self.k)
        return v


def double_oracle(A: LazyPayoff, deadline: Deadline, eps: float = 0.01, iters: int = 120,
                  start: tuple[int, int] = (0, 0), max_rounds: int = 60, final_iters: int = 600):
    """Equilibrium of the full 90 x 90 game without filling it: solve the restricted game R x C with RM+
    (warm-started across rounds), add each side's best response to the other's restricted equilibrium, stop
    when neither improves by more than eps. Anytime: past the deadline it returns the current restricted
    equilibrium. Returns (rows R, row strategy over R, cols C, col strategy over C, value, gap, rounds)."""
    n, m = len(A.la), len(A.lb)
    R, C = [start[0]], [start[1]]
    pr, pc, value, gap = [1.0], [1.0], A(start[0], start[1]), float("inf")
    rounds = 0
    state: dict = {}
    while rounds < max_rounds:
        rounds += 1
        sub = [[A(r, c) for c in C] for r in R]
        pr, pc, value = solve_rm_plus(sub, iters, state=state)
        if deadline.passed():
            break
        supp_c = [(C[j], p) for j, p in enumerate(pc) if p > 1e-3]
        supp_r = [(R[i], p) for i, p in enumerate(pr) if p > 1e-3]
        row_vals = [sum(p * A(r, c) for c, p in supp_c) for r in range(n)]
        if deadline.passed():
            break
        col_vals = [sum(p * A(r, c) for r, p in supp_r) for c in range(m)]
        br_r = max(range(n), key=lambda r: (row_vals[r], -r))
        br_c = min(range(m), key=lambda c: (col_vals[c], c))
        gap = row_vals[br_r] - col_vals[br_c]
        grew = False
        if row_vals[br_r] > value + eps and br_r not in R:
            R.append(br_r)
            grew = True
        if col_vals[br_c] < value - eps and br_c not in C:
            C.append(br_c)
            grew = True
        if (not grew and gap <= 2 * eps) or deadline.passed():
            break
        # no new strategy but a large gap: the restricted solve is still imprecise, keep iterating it
    if len(pr) == len(R) and len(pc) == len(C) and not deadline.passed():
        sub = [[A(r, c) for c in C] for r in R]
        pr, pc, value = solve_rm_plus(sub, final_iters, deadline, state=state)
    elif len(pr) != len(R) or len(pc) != len(C):           # stopped right after growing: re-solve what we have
        sub = [[A(r, c) for c in C] for r in R]
        pr, pc, value = solve_rm_plus(sub, iters, state=state)
    return R, pr, C, pc, value, gap, rounds


def choose(my_views: list[dict], my_cards: list[dict | None], opp_views: list[dict], opp_cards: list[dict | None],
           seed_text: str = "", knobs: dict | None = None, opp_guess: tuple | None = None) -> dict | None:
    """Full pipeline. Returns {"bring": [our roster indices, leads first], "leads": [...], "info": {...}} or
    None when the game cannot be built (the caller falls back). May raise on errors / a blown budget.
    opp_guess: a predicted opponent lineup (lead, lead, bench, bench) as opponent-roster indices; with
    knobs["prior_w"] > 0 we best-respond to prior_w * guess + (1 - prior_w) * the equilibrium opponent."""
    kn = dict(KNOBS)
    kn.update(knobs or {})
    if kn.get("mode") == "off":
        return None
    t0, c0 = time.perf_counter(), time.process_time()
    work_units, wall = float(kn.get("work_units", 17500)), float(kn.get("wall_cap_s", 6.0))
    share = float(kn.get("search_share", 0.8))
    race_ref: dict = {}                                        # the race of this decision, once built

    def work() -> int:
        r = race_ref.get("race")
        return r.segments if r is not None else 0

    deadline = Deadline(work_units, wall, work)
    search_deadline = Deadline(work_units * share, wall * share, work)   # always leave room for the answer
    if not (4 <= len(my_views) <= 6 and 4 <= len(opp_views) <= 6):
        return None
    if len(my_cards) != len(my_views) or len(opp_cards) != len(opp_views):
        return None
    if not DamageCalc.get().enabled:
        return None
    units = [Unit(0, v, c) for v, c in zip(my_views, my_cards)] + [Unit(1, v, c) for v, c in zip(opp_views, opp_cards)]
    if any(not u.sid for u in units):
        return None
    tb = Tables(units)
    t_calc = time.perf_counter() - t0
    if deadline.passed():
        raise TimeoutError("damage table over budget")
    ours = list(range(len(my_views)))
    theirs = list(range(len(my_views), len(units)))
    A = LazyPayoff(tb, ours, theirs, kn)
    race_ref["race"] = A.race
    R, pr, C, pc, value, gap, rounds = double_oracle(A, search_deadline, float(kn.get("eps", 0.01)),
                                                     int(kn.get("rm_iters", 120)))
    if deadline.passed():
        raise TimeoutError("preview over budget")
    j = pick(pr, None, str(kn.get("mode", "sample")), seed_text, float(kn.get("min_prob", 0.03)))
    i = R[j]
    w = float(kn.get("prior_w", 0.0))
    guess_col = None
    if w > 0 and opp_guess is not None:
        lus_b = lineups(len(theirs))
        g = tuple(opp_guess)
        guess_col = next((c for c, t in enumerate(lus_b) if frozenset(t[:2]) == frozenset(g[:2])
                          and frozenset(t) == frozenset(g)), None)
    if guess_col is not None and not deadline.passed():
        # best response to a mixture of the predicted lineup and the equilibrium opponent
        supp_c = [(C[k], p * (1 - w)) for k, p in enumerate(pc) if p > 1e-3]
        supp_c.append((guess_col, w))
        vals = [sum(p * A(r, c) for c, p in supp_c) for r in range(len(A.la))]
        i = max(range(len(vals)), key=lambda r: (vals[r], -r))
        j = R.index(i) if i in R else None
    keep = kn.get("keep")
    if keep is not None and keep not in lineups(len(ours))[i] and not deadline.passed():
        # ARENA_PREVIEW_KEEP_FIRST: best response, among the lineups that bring roster index `keep`, to the column
        # mix used above; rows already in the restricted game first (cached), anytime (stops at the deadline)
        mix = supp_c if guess_col is not None else [(C[k], p) for k, p in enumerate(pc) if p > 1e-3]
        cand = [r for r, t in enumerate(lineups(len(ours))) if keep in t]
        cand.sort(key=lambda r: r not in R)
        best = None
        for r in cand:
            if deadline.passed():
                break
            v = sum(p * A(r, c) for c, p in mix)
            if best is None or v > best[0]:
                best = (v, r)
        if best is not None:
            i = best[1]
            j = R.index(i) if i in R else None
    lu = lineups(len(ours))[i]
    info = {"value": round(value, 3), "p": round(pr[j], 3) if j is not None else 0.0, "gap": round(gap, 3),
            "rounds": rounds, "guess": guess_col,
            "support": sum(p >= 0.03 for p in pr), "entries": len(A.cache), "segments": A.race.segments,
            "work_units": work_units, "wall_hit": deadline.wall_hit or search_deadline.wall_hit,
            "calcs": tb.n_calcs, "t_calc": round(t_calc, 3), "t_total": round(time.perf_counter() - t0, 3),
            "cpu_total": round(time.process_time() - c0, 3)}
    return {"bring": list(lu), "leads": list(lu[:2]), "info": info, "row": i,
            "rows": R, "pr": pr, "cols": C, "pc": pc}
