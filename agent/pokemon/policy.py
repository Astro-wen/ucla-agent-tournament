"""Pokémon policy: draft, team preview, singles + VGC doubles turns.

Battle moves are scored with Smogon's exact damage calculator (agent/pokemon/calc.py): ONE batched node
round trip per decision covers every (our active, move, legal target) plus the foes' known moves against
our actives. A hit is worth its expected damage as a fraction of the defender's *current* HP (capped, so
overkill earns nothing) plus a KO bonus weighted by P(KO) over the 16 rolls and the move's accuracy;
foes are weighted by how dangerous they are to us. In doubles both slots are chosen jointly, so two
attacks are not wasted on one low-HP foe and both slots never Fake Out the same target.

When the calculator is unavailable (no node / node_modules, bridge error) every decision falls back to
the transparent base-stat heuristic `move_value()` with the same VGC bonuses (Fake Out, Intimidate,
Tailwind, spread moves), exactly as v0 played. See docs/strategy.md for the upgrade plan.
"""
from __future__ import annotations

import logging

from . import dex
from .calc import DamageCalc, calc_field, calc_pokemon, move_name
from .dex import to_id
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
PROTECT_MOVES = {"protect", "detect", "spikyshield", "silktrap", "banefulbunker", "burningbulwark", "kingsshield",
                 "obstruct"}
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
STALL_BONUS = 30.0        # Protect through the last turns of an opposing Tailwind / Trick Room
FAKE_OUT_LIKELY = 0.9     # a fresh foe that carries Fake Out uses it
SUCKER_PUNCH_ACC = 0.8    # fails into status moves / Protect
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


def _hit_stats(rolls, cur: int) -> tuple[float, float]:
    """(expected fraction of the defender's current HP removed, P(KO)) given that the move hits."""
    n = len(rolls) or 1
    return sum(min(r, cur) for r in rolls) / n / cur, sum(r >= cur for r in rolls) / n


def _points(rolls, cur: int) -> float:
    useful, pko = _hit_stats(rolls, cur)
    return 100.0 * useful + KO_BONUS * pko


def _pair_points(a: tuple, b: tuple, cur: int) -> float:
    """Value of two attacks (rolls, acc) landing on the same defender this turn: joint KO odds, no double counting."""
    ra, pa = a
    rb, pb = b
    both = _points([x + y for x in ra for y in rb], cur)
    return pa * pb * both + pa * (1 - pb) * _points(ra, cur) + (1 - pa) * pb * _points(rb, cur)


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
    """A battle view safe for stats.effective_speed / move_priority: hidden foe item dropped, boosts a dict."""
    v = dict(mon or {})
    if not v.get("item") or to_id(str(v.get("item"))) in ("unknownitem", "unknown", "none"):
        v["item"] = None
    if not isinstance(v.get("boosts"), dict):
        v["boosts"] = {}
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
    item = mon.get("item")
    if not item or item == "unknown_item":
        item = (card or {}).get("item")
    ability = mon.get("ability") or (card or {}).get("ability")
    return to_id(item or "") in SASH_ITEMS or to_id(ability or "") in SASH_ABILITIES


def _pko(rolls, acc: float, cur: int, sash: bool = False) -> float:
    """P(one hit KOs a defender with `cur` HP left), accuracy included."""
    if sash or not rolls:
        return 0.0
    return acc * sum(r >= cur for r in rolls) / len(rolls)


def _pair_ko(a: tuple, b: tuple, cur: int, sash: bool = False) -> float:
    """P(two hits on the same defender KO it this turn), accuracy included (a Sash only stops a lone hit)."""
    ra, pa = a
    rb, pb = b
    n = (len(ra) * len(rb)) or 1
    both = sum(x + y >= cur for x in ra for y in rb) / n
    return pa * pb * both + pa * (1 - pb) * _pko(ra, 1.0, cur, sash) + (1 - pa) * pb * _pko(rb, 1.0, cur, sash)


class PokemonPolicy:
    def __init__(self):
        self.cards: dict[str, dict] = {}        # our drafted sets by species id
        self.opp_cards: dict[str, dict] = {}    # opponent's drafted sets by species id
        self.seen: dict[str, dict] = dex.card_catalog()   # card_id -> full set (real catalog + this draft)
        self.prev_active: list[str | None] = [None, None]
        self.me: str | None = None
        self.last_exact: bool = False           # did the last battle decision use the exact calculator?
        self.prev_opp: list[str | None] = [None, None]
        self.last_move: dict[int, tuple[str, str | None]] = {}   # slot -> (species, move id) of our last action
        self.cond_first: dict[str, int] = {}    # "opp_tailwind" / "my_tailwind" / "trickroom" -> first turn seen
        self.last_speed: dict | None = None     # speed model of the last doubles decision (debugging / tests)

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
        phase = st.get("phase")
        if phase == "draft":
            return self._draft(st, obs)
        if phase == "teambuild":
            for s in DEFAULT_OU_TEAM:
                self.cards.setdefault(to_id(s["species"]), s)
            return {"action": {"type": "submit_team", "team": DEFAULT_OU_TEAM}}
        if phase == "team_preview":
            return self._preview(obs)
        if obs.get("is_doubles"):
            return self._doubles(st, obs)
        return self._singles(st, obs)

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
                    if opt.get("type") == "switch":
                        used.add(to_id(opt.get("species")))
                    break
            return {"action": {"type": "doubles_turn", **pick}}
        if st.get("phase") == "team_preview":
            roster = [to_id(p.get("species", "")) for p in obs.get("your_roster", [])]
            return {"action": {"type": "select_lineup", "bring": roster[:4], "leads": roster[:2]}}
        return {k: v for k, v in la[0]["input"].items() if k not in ("session_id", "state_version")}

    # ---- draft ---------------------------------------------------------
    def _draft(self, st: dict, obs: dict) -> dict:
        me = (st.get("current_actor") or {}).get("agent_id") or obs.get("current_seat")
        self.me = me
        self._track_draft(obs, me)
        taken = {to_id(c.get("species", "")) for c in (obs.get("rosters") or {}).get(me, [])}
        roster = [self.cards[s] for s in taken if s in self.cards] or [
            {"species": c.get("species", "")} for c in (obs.get("rosters") or {}).get(me, [])]
        legal_ids = {a["input"].get("action", {}).get("card_id") or a["action_id"].split(":", 1)[-1]
                     for a in (st.get("legal_actions") or {}).get("actions", [])}
        cards = [c for c in obs.get("available_cards", []) if c["card_id"] in legal_ids] \
            or obs.get("available_cards", [])
        best = max(cards, key=lambda c: card_score(c, roster))
        self.cards[to_id(best["species"])] = best
        return {"action": {"type": "draft_pick", "card_id": best["card_id"]},
                "reasoning_summary": f"{best['species']}: best value + synergy with current roster"}

    # ---- team preview --------------------------------------------------
    def _preview(self, obs: dict) -> dict:
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
            my_in = {i: calc_pokemon(a, self._card(self.cards, a)) for i, a in mine.items()}
            foe_in = {j: calc_pokemon(o, self._card(self.opp_cards, o)) for j, o in foes.items()}
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
            for j, o in foes.items():
                card = self.opponent_set(o.get("species", "")) or {}
                for mv in (card.get("moves") or o.get("moves") or [])[:4]:
                    mid = to_id(mv)
                    m = dex.move(mid)
                    if not _damaging(m):
                        continue
                    tgt = m.get("target")
                    hits = len(mine) + (1 if tgt == "allAdjacent" and (1 - j) in foes else 0)
                    field = f_opp if tgt in SPREAD_TARGETS and hits > 1 else {**f_opp, "gameType": "Singles"}
                    for i in mine:
                        add(("opp", j, mid, "me", i), foe_in[j], my_in[i], mid, field)
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
                if k[0] == "opp" and k[1] == j and r:
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
            if k[0] != "opp" or not r or k[1] not in foes or k[4] not in mine:
                continue
            j, mid, i = k[1], k[2], k[4]
            if mid in FIRST_TURN_ONLY:                   # Fake Out: only its flinch matters (fake_out below)
                continue
            m = dex.move(mid)
            acc = _acc_of(m)
            cur = _cur_hp(mine[i], r)
            useful, _ = _hit_stats(r["rolls"], cur)
            pko = _pko(r["rolls"], acc, cur, my_sash[i])
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
            c["pf"][j] = _first(acts_first(mine_spe, prio, sm["foe_spe"][j], sm["foe_prio"].get(j, 0), sm["tr"]))
            c["ko"][j] = _pko(rolls, acc, cur, sm["foe_sash"].get(j, False))
            c["kob"][j] = c["pf"][j] * c["ko"][j]
            c["use"][j] = acc * _hit_stats(rolls, cur)[0]
        for j in sm["foes"]:
            flinch = 0.0
            if c.get("flinch") == j:
                flinch = _first(acts_first(mine_spe, 3, sm["foe_spe"][j], sm["foe_prio"].get(j, 0), sm["tr"]))
            n = min(1.0, c["kob"].get(j, 0.0) + flinch)
            if n:
                c["nb"][j] = pa * n

    def _protect_value(self, c: dict, other: dict, sm: dict | None, obs: dict, turn: int) -> float:
        """Protect: worth it when this slot is likely KO'd this turn and the partner removes / cripples the threat,
        or to stall the last turns of an opposing Tailwind / Trick Room. Never twice in a row, never idly."""
        i = c["slot"]
        if self._protected_last(i, c.get("species", "")) or c.get("choice"):
            return PROTECT_AGAIN                         # a Choice item would lock this mon into Protect
        if sm is None or i not in sm["mine"]:
            return _status_value(c.get("mid", "protect"), c.get("hp", 1.0), turn, obs)
        keep, v = 1.0, 0.0
        for j in sm["foes"]:
            t = sm["threat"].get((j, i))
            if not t:
                continue
            d = sm["focus"].get((j, i), 0.5) * t[0] * (1.0 - other.get("nb", {}).get(j, 0.0))
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
        v *= 0.3 + 0.7 * hp                            # saving a mon on its last few HP is worth less
        danger = 1.0 - keep
        stall = sm["stall"].get(i, False)
        if danger < PROTECT_MIN_DANGER:
            v = PROTECT_IDLE
        if stall:
            v = max(v, 0.0) + STALL_BONUS
        return v

    # ---- battle --------------------------------------------------------
    def _singles(self, st: dict, obs: dict) -> dict:
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
                    v = _acc_of(m, act) * _points(_adjust_rolls(r["rolls"], m, mid, me_mon, card, sun), _cur_hp(opp, r))
                    if m.get("selfdestruct"):
                        v -= SELF_KO_PENALTY
                else:
                    v = move_value(mid, me, opp_types, extra) * HEURISTIC_TO_POINTS
            else:
                v = 5.0 if obs.get("force_switch") else 0.0
            if v > best_v:
                best, best_v = a, v
        best = best or la[0]
        return {k: v for k, v in best["input"].items() if k not in ("session_id", "state_version")}

    def _singles_table(self, obs: dict, la: list, me_mon: dict, opp: dict) -> dict | None:
        dc = DamageCalc.get()
        if not dc.enabled or not me_mon.get("species") or not opp.get("species") or opp.get("fainted"):
            return None
        try:
            atk = calc_pokemon(me_mon, self._card(self.cards, me_mon))
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
        tmpl = st["legal_actions"]["actions"][0]["input"]["action"]
        actives = obs.get("active_pokemon") or [None, None]
        opps = obs.get("opponent_active_pokemon") or [None, None]
        turn = obs.get("turn", 0)
        slots = tmpl.get("slots", [])
        self._track_conditions(obs)
        table = self._exact_table(obs, tmpl, actives, opps)
        self.last_exact = table is not None
        pick = None
        if table is not None:
            try:
                pick = self._doubles_exact(obs, slots, actives, opps, turn, table)
            except Exception:
                log.warning("exact doubles scoring failed; using the heuristic", exc_info=True)
                pick = None
        if pick is None:
            pick = self._doubles_heuristic(obs, slots, actives, opps, turn)
        self._remember(obs, pick, actives)
        self.prev_active = [to_id((a or {}).get("species", "")) or None for a in (list(actives) + [None, None])[:2]]
        self.prev_opp = [to_id((o or {}).get("species", "")) or None for o in (list(opps) + [None, None])[:2]]
        return {"action": {"type": "doubles_turn", **pick}}

    # ---- memory across turns (the policy instance lives for the whole match) ----
    def _track_conditions(self, obs: dict) -> None:
        """First turn we saw each speed-control condition, to know how many turns of it are left."""
        try:
            turn = int(obs.get("turn") or 0)
            now = {"opp_tailwind": "tailwind" in _cond_ids(obs.get("opponent_side_conditions")),
                   "my_tailwind": "tailwind" in _cond_ids(obs.get("side_conditions")),
                   "trickroom": "trickroom" in _cond_ids(obs.get("fields"))}
            for k, on in now.items():
                if not on:
                    self.cond_first.pop(k, None)
                elif k not in self.cond_first or self.cond_first[k] > turn:
                    self.cond_first[k] = turn
        except Exception:
            log.warning("condition tracking failed", exc_info=True)

    def _cond_left(self, key: str, turn: int) -> int | None:
        """Turns of a condition left *including* this one (it was set the turn before we first saw it)."""
        if key not in self.cond_first:
            return None
        total = TRICK_ROOM_TURNS if key == "trickroom" else TAILWIND_TURNS
        return max(1, total - 1 - (turn - self.cond_first[key]))

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
        return bool(last) and last[0] == to_id(species or "") and last[1] in PROTECT_MOVES

    def _slot_mon(self, slot: dict, actives: list) -> dict | None:
        i = slot.get("slot", 0)
        me = actives[i] if isinstance(i, int) and i < len(actives) else None
        return me or slot.get("active") or ({"species": slot["species"]} if slot.get("species") else None)

    def _doubles_exact(self, obs, slots, actives, opps, turn, table) -> dict | None:
        curs, danger, weights = self._foe_weights(table, actives, opps)
        try:
            sm = self._speed_model(obs, actives, opps, table)
        except Exception:
            log.warning("speed model failed; scoring without move order", exc_info=True)
            sm = None
        self.last_speed = sm
        terrain = (sm or {}).get("terrain")
        per_slot = []
        for slot in slots:
            i = slot["slot"]
            me = self._slot_mon(slot, actives)
            my_sp = to_id((me or {}).get("species", ""))
            fresh = self.prev_active[i] != my_sp if i < 2 else True
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
            return c["base"] + sum(weights.get(j, 1.0) * acc * _points(rolls, curs.get(j, 1))
                                   for j, (rolls, acc) in c["foe"].items())

        def remove_w(j):
            return REMOVE_BASE + REMOVE_DANGER * danger.get(j, 0.5)

        def p_act(c, other):
            if not c["thr"]:
                return c["pa1"]
            p = 1.0
            for j, t in c["thr"].items():
                p *= 1.0 - t * (1.0 - other["nb"].get(j, 0.0))
            return p

        dummy = {"cand": None, "base": 0.0, "foe": {}, "flinch": None, "slot": -1, "acts": False, "kind": "none",
                 "pa1": 1.0, "thr": {}, "pf": {}, "ko": {}, "kob": {}, "use": {}, "nb": {}, "mid": ""}
        lists = [cands for _, cands in per_slot][:2]
        while len(lists) < 2:
            lists.append([dummy])
        solos = [[solo(c) for c in lst] for lst in lists]
        cache: dict = {}
        best, best_v = None, -1e18
        for a, c0 in enumerate(lists[0]):
            for b, c1 in enumerate(lists[1]):
                if c0["kind"] == "switch" and c1["kind"] == "switch" \
                        and to_id(c0["cand"].get("species")) == to_id(c1["cand"].get("species")):
                    continue
                pa0, pa1 = p_act(c0, c1), p_act(c1, c0)
                v = pa0 * solos[0][a] + pa1 * solos[1][b]
                if c0["kind"] == "protect":
                    v += self._protect_value(c0, c1, sm, obs, turn)
                if c1["kind"] == "protect":
                    v += self._protect_value(c1, c0, sm, obs, turn)
                if c0["flinch"] is not None and c0["flinch"] == c1["flinch"]:
                    v -= FAKE_OUT_BONUS
                for j in c0["foe"].keys() & c1["foe"].keys():      # both hit foe j: joint value replaces the sum
                    da, db = c0["foe"][j], c1["foe"][j]
                    key = (id(da), id(db), j)
                    if key not in cache:
                        cur = curs.get(j, 1)
                        cache[key] = (_pair_points(da, db, cur) - da[1] * _points(da[0], cur)
                                      - db[1] * _points(db[0], cur),
                                      _pair_ko(da, db, cur, bool(sm and sm["foe_sash"].get(j))))
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
                        v += remove_w(j) * min(1.0, rem)
                if v > best_v:
                    best, best_v = (c0, c1), v
        if best is None:
            return None
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
        item = (me or {}).get("item")
        if not item or item == "unknown_item":
            item = card.get("item")
        choice = to_id(item or "").startswith("choice")
        try:
            prio = move_priority(mid, _speed_view(me), self.cards.get(my_sp), terrain)
        except Exception:
            prio = int(m.get("priority", 0) or 0)
        out = []

        def dist(j):
            r = table.get(("me", i, mid, "opp", j))
            return (_adjust_rolls(r["rolls"], m, mid, me, card, sun), acc) if r else None

        def heuristic(j):
            return move_value(mid, my_sp, _types(alive[j])) * HEURISTIC_TO_POINTS

        for t in targets:
            base, foe, flinch = 0.0, {}, None
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
                            base -= ALLY_HIT_WEIGHT * acc * _points(r["rolls"], _cur_hp(ally, r))
                        else:
                            base -= ALLY_HIT_WEIGHT * move_value(mid, my_sp, _types(ally)) * HEURISTIC_TO_POINTS
                elif damaging:                                   # locked / auto-targeted single-target move
                    vals = [dist(j) for j in alive]
                    pts = [acc * _points(d[0], _cur_hp(alive[j], table[("me", i, mid, "opp", j)])) if d else heuristic(j)
                           for j, d in zip(alive, vals)]
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
                        "acts": not protect, "prio": prio, "mid": mid, "target": t, "choice": choice})
        if not out:
            out.append({"cand": {"type": "move", "move_id": opt["move_id"], "target": targets[0]}, "base": -1.0,
                        "foe": {}, "flinch": None, "acts": not protect, "prio": prio, "mid": mid, "target": targets[0]})
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


def _threat(o: dict) -> float:
    sp = o.get("species", "")
    bs = o.get("base_stats") or dex.base_stats(sp)
    return max(bs.get("atk", 90), bs.get("spa", 90)) + bs.get("spe", 80) / 2


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
