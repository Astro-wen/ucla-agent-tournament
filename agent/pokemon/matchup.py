"""Pairwise threat table among the draft pool's cards (exact damage calc) + matchup-based draft scoring.

`table()` is a process-wide cache. `ThreatTable.ensure(cards)` adds any card not seen yet and runs ONE
DamageCalc.batch() for all new ordered pairs (every damaging move of the attacker against the defender,
doubles field, full HP). When the calculator is unavailable a plain Gen 9 damage formula is used instead,
so the draft never depends on node being present. Each pair keeps the attacker's best expected damage
(fraction of the defender's max HP, accuracy and charge/recharge included), the best priority-move damage,
and each card's speed; `beats(a, b)` turns that into a 1v1 duel estimate in [0, 1].

`Drafter` scores the available cards for one pick: threat to the opponent's roster, coverage of what
threatens our roster, role synergy (Fake Out / Intimidate / redirection / Tailwind or Trick Room matched
to the roster's speed), and deny value, with a one-pick look-ahead along the snake order (we often pick
twice in a row). Nothing in here is allowed to raise into the policy: `PokemonPolicy._draft` wraps it and
falls back to `card_score`.
"""
from __future__ import annotations

import json
import logging
import math

from . import dex
from .calc import DamageCalc, calc_pokemon, move_name
from .dex import to_id
from .stats import effective_speed, stats_for
from .typechart import effectiveness

log = logging.getLogger("agent.matchup")

SNAKE = "ABBAABBAABBA"
LEVEL = 50
SPREAD_TARGETS = {"allAdjacentFoes", "allAdjacent"}
MAX_HITS = 6
SPREAD_VALUE = 1.25            # a spread move's damage counts a bit more: it pressures both foes

FAKE_OUT = {"fakeout"}
REDIRECT = {"followme", "ragepowder"}
TAILWIND = {"tailwind"}
TRICK_ROOM = {"trickroom"}
SPEED_CONTROL = {"icywind", "electroweb", "bulldoze", "thunderwave"}
SCREENS = {"reflect", "lightscreen", "auroraveil"}
SLEEP = {"spore", "sleeppowder", "yawn"}
INTIMIDATE = {"intimidate"}

_TYPE_ITEMS = {"blackglasses": "DARK", "sharpbeak": "FLYING", "mysticwater": "WATER", "charcoal": "FIRE",
               "magnet": "ELECTRIC", "miracleseed": "GRASS", "nevermeltice": "ICE", "softsand": "GROUND",
               "silkscarf": "NORMAL", "spelltag": "GHOST", "dragonfang": "DRAGON", "metalcoat": "STEEL",
               "poisonbarb": "POISON", "twistedspoon": "PSYCHIC", "hardstone": "ROCK", "blackbelt": "FIGHTING",
               "silverpowder": "BUG", "fairyfeather": "FAIRY"}


def card_key(card: dict) -> str:
    return str(card.get("card_id") or ("species:" + to_id(card.get("species", ""))))


def _moves(card: dict) -> list[str]:
    return [to_id(m) for m in (card.get("moves") or []) if m]


def _is_damaging(mid: str) -> bool:
    m = dex.move(mid)
    return m.get("category") in ("Physical", "Special") and bool(m.get("basePower") or m.get("damage")
                                                                 or mid in ("foulplay", "bodypress", "ruination"))


def _is_known(card: dict) -> bool:
    """A full platform card (moves + evs) rather than a species-only roster entry."""
    return bool(card.get("moves")) and card.get("evs") is not None


def _acc(m: dict) -> float:
    a = m.get("accuracy", 100)
    return 1.0 if a is True or a is None else (a / 100 if a > 1 else float(a))


def _move_factor(card: dict, mid: str) -> float:
    """Per-turn factor for moves that cost a turn (charge without Power Herb, recharge)."""
    m = dex.move(mid)
    flags = m.get("flags") or {}
    f = _acc(m)
    if flags.get("charge") and to_id(card.get("item") or "") != "powerherb":
        f *= 0.5
    if flags.get("recharge"):
        f *= 0.6
    if mid in ("overheat", "dracometeor", "leafstorm", "makeitrain", "glaiverush"):
        f *= 0.85                        # stat drop after the first use
    return f


def _hits(card: dict, mid: str) -> int | None:
    """Expected hit count of a 2-5 hit move (the calculator assumes 3): Loaded Dice 4-5, Skill Link 5."""
    mh = dex.move(mid).get("multihit")
    if not isinstance(mh, list):
        return None
    if to_id(card.get("ability") or "") == "skilllink":
        return mh[1]
    if to_id(card.get("item") or "") == "loadeddice":
        return 4
    return None


class ThreatTable:
    def __init__(self):
        self.cards: dict[str, dict] = {}
        self.sig: dict[str, str] = {}
        self.dmg: dict[tuple[str, str], float] = {}        # best expected damage fraction of max HP per turn
        self.prio: dict[tuple[str, str], float] = {}       # same, priority moves only
        self.spread: dict[tuple[str, str], bool] = {}      # is the best move a spread move?
        self.best: dict[tuple[str, str], str] = {}         # best move id
        self.spe: dict[str, float] = {}
        self.exact: set[tuple[str, str]] = set()
        self.calc_used = False

    # ---- building ------------------------------------------------------------------------------
    def ensure(self, cards) -> None:
        fresh: dict[str, dict] = {}
        for c in cards or []:
            if not isinstance(c, dict) or not c.get("species"):
                continue
            k = card_key(c)
            try:
                sig = json.dumps(c, sort_keys=True, default=str)
            except Exception:
                sig = str(sorted(c.items(), key=str))
            if self.sig.get(k) == sig or k in fresh:
                continue
            if k in self.cards and _is_known(self.cards[k]) and not _is_known(c):
                continue                                   # never downgrade a full set to a species stub
            fresh[k] = c
            self.sig[k] = sig
        if not fresh:
            return
        self.cards.update(fresh)
        for k, c in fresh.items():
            sp = to_id(c.get("species", ""))
            card = c if _is_known(c) else None
            try:
                self.spe[k] = float(effective_speed({"species": sp, "item": c.get("item"), "ability": c.get("ability")},
                                                    card))
            except Exception:
                self.spe[k] = 80.0
        pairs = [(a, b) for a in fresh for b in self.cards if a != b]
        pairs += [(a, b) for a in self.cards if a not in fresh for b in fresh]
        self._compute(pairs)

    def _compute(self, pairs: list[tuple[str, str]]) -> None:
        calcs, index = [], []
        inputs: dict[str, dict] = {}

        def inp(k):
            if k not in inputs:
                inputs[k] = calc_pokemon(None, self.cards[k])
            return inputs[k]

        dc = DamageCalc.get()
        if dc.enabled:
            for a, b in pairs:
                ca, cb = self.cards[a], self.cards[b]
                if not (_is_known(ca) and _is_known(cb)):
                    continue
                for mv in _moves(ca):
                    if _is_damaging(mv):
                        q = {"attacker": inp(a), "defender": inp(b), "move": move_name(mv),
                             "field": {"gameType": "Doubles"}}
                        h = _hits(ca, mv)
                        if h:
                            q["hits"] = h
                        calcs.append(q)
                        index.append((a, b, mv))
        results = None
        if calcs:
            try:
                results = dc.batch(calcs)
            except Exception:
                log.warning("threat table: calc failed, using the formula", exc_info=True)
                results = None
        got: dict[tuple[str, str], dict[str, float]] = {}
        if results is not None:
            self.calc_used = True
            for (a, b, mv), r in zip(index, results):
                if r and r.get("defender_hp"):
                    rolls = r.get("rolls") or [r.get("max", 0)]
                    mean = sum(rolls) / max(1, len(rolls))
                    got.setdefault((a, b), {})[mv] = mean / float(r["defender_hp"]) * _move_factor(self.cards[a], mv)
        for a, b in pairs:
            per = got.get((a, b))
            if per is None:
                per = self._formula(self.cards[a], self.cards[b])
            else:
                self.exact.add((a, b))
            self._store(a, b, per)

    def _store(self, a: str, b: str, per: dict[str, float]) -> None:
        best, best_v, pbest, best_eff = "", 0.0, 0.0, 0.0
        for mv, d in per.items():
            m = dex.move(mv)
            eff = d * (SPREAD_VALUE if m.get("target") in SPREAD_TARGETS else 1.0)
            if eff > best_eff:
                best, best_v, best_eff = mv, d, eff
            if m.get("priority", 0) > 0 and d > pbest:
                pbest = d
            if to_id(self.cards[a].get("ability") or "") == "galewings" and m.get("type") == "Flying" and d > pbest:
                pbest = d                  # Gale Wings: priority at full HP
        self.dmg[(a, b)] = best_v
        self.prio[(a, b)] = pbest
        self.spread[(a, b)] = bool(best) and dex.move(best).get("target") in SPREAD_TARGETS
        self.best[(a, b)] = best

    def _formula(self, att: dict, deff: dict) -> dict[str, float]:
        """Gen 9 damage formula without the calc's item/ability detail (calc unavailable / unknown sets)."""
        sa, sd = to_id(att.get("species", "")), to_id(deff.get("species", ""))
        st_a = stats_for(att if _is_known(att) else None, sa)
        st_d = stats_for(deff if _is_known(deff) else None, sd)
        def_types = dex.types_of(sd)
        moves = [m for m in _moves(att) if _is_damaging(m)]
        generic = {}
        if not moves:                           # species-only entry: a 90 BP STAB of its better side
            cat = "Physical" if st_a.get("atk", 0) >= st_a.get("spa", 0) else "Special"
            for t in dex.types_of(sa) or ["NORMAL"]:
                generic["generic" + t.lower()] = {"basePower": 90, "category": cat, "type": t.capitalize(),
                                                  "target": "normal"}
            moves = list(generic)
        item, ability = to_id(att.get("item") or ""), to_id(att.get("ability") or "")
        out = {}
        for mv in moves:
            m = generic.get(mv) or dex.move(mv)
            bp = m.get("basePower") or 0
            cat = m.get("category")
            if mv == "bodypress":
                A = st_a.get("def", 100)
            elif mv == "foulplay":
                A = st_d.get("atk", 100)
            else:
                A = st_a.get("atk" if cat == "Physical" else "spa", 100)
            if not bp:
                continue
            D = st_d.get("def" if cat == "Physical" else "spd", 100)
            base = math.floor(math.floor(math.floor(2 * LEVEL / 5 + 2) * bp * A / max(1, D)) / 50) + 2
            mtype = str(m.get("type") or "").upper()
            mult = 0.925                        # mean random roll
            if m.get("target") in SPREAD_TARGETS:
                mult *= 0.75
            if mtype in dex.types_of(sa):
                mult *= 2.0 if ability == "adaptability" else 1.5
            mult *= effectiveness(mtype, def_types)
            if item == "lifeorb":
                mult *= 1.3
            elif (item == "choicespecs" and cat == "Special") or (item == "choiceband" and cat == "Physical"):
                mult *= 1.5
            if _TYPE_ITEMS.get(item) == mtype:
                mult *= 1.2
            hits = m.get("multihit")
            if hits:
                mult *= 4 if item == "loadeddice" else (hits if isinstance(hits, int) else 3)
            out[mv] = base * mult / max(1, st_d.get("hp", 100)) * (_move_factor(att, mv) if not generic else 1.0)
        return out

    # ---- lookups ---------------------------------------------------------------------------------
    def damage(self, a: str, b: str) -> float:
        return self.dmg.get((a, b), 0.0)

    def hits_to_ko(self, a: str, b: str) -> float:
        d = self.dmg.get((a, b), 0.0)
        if d <= 0.02:
            return math.inf
        h = max(1, math.ceil(1.0 / d - 1e-9))
        cb = self.cards.get(b, {})
        if h == 1 and to_id(cb.get("item") or "") == "focussash":
            h = 2
        if to_id(cb.get("ability") or "") == "multiscale" and d < 1.0 and (a, b) in self.exact:
            h = 1 + math.ceil((1.0 - d) / (2 * d) - 1e-9)      # the calc halved only the first hit (full HP)
        return min(h, MAX_HITS)

    def first(self, a: str, b: str) -> float:
        """+1 when A moves first, -1 when B does, 0 on a tie."""
        sa, sb = self.spe.get(a, 0.0), self.spe.get(b, 0.0)
        return 0.0 if sa == sb else (1.0 if sa > sb else -1.0)

    def beats(self, a: str, b: str) -> float:
        """P(A wins a 1v1 slugfest against B) proxy in [0, 1]; 0.5 when nothing is known."""
        ha, hb = self.hits_to_ko(a, b), self.hits_to_ko(b, a)
        if ha == math.inf and hb == math.inf:
            return 0.5
        if ha == math.inf:
            return 0.1
        if hb == math.inf:
            return 0.9
        first = self.first(a, b)
        pa, pb = self.prio.get((a, b), 0.0), self.prio.get((b, a), 0.0)
        pa_ok = pa > 0.02 and math.ceil(1.0 / pa) <= ha
        pb_ok = pb > 0.02 and math.ceil(1.0 / pb) <= hb
        if pa_ok != pb_ok:
            first = 1.0 if pa_ok else -1.0
        margin = (hb - ha) + 0.5 * first
        return 1.0 / (1.0 + math.exp(-1.6 * margin))
