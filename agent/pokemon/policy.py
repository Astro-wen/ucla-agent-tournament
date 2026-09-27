"""Baseline Pokémon policy (v0): draft, team preview, singles + VGC doubles turns.

Scoring is a transparent expected-damage heuristic (power x STAB x type x accuracy
x attacker stat) plus hand-tuned VGC bonuses (Fake Out, Intimidate, Tailwind,
spread moves). It is meant to beat random/first-option bots and give the LLM or
search-based brain a safe fallback. See docs/strategy.md for the upgrade plan.
"""
from __future__ import annotations

from . import dex
from .dex import to_id
from .typechart import effectiveness

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


def _stat(sp: str, key: str, default: int = 90) -> int:
    return dex.base_stats(sp).get(key, default)


def move_value(move_id: str, attacker: str, defender_types: list[str], extra: dict | None = None) -> float:
    """Expected single-target damage proxy of one move against one defender."""
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


class PokemonPolicy:
    def __init__(self):
        self.cards: dict[str, dict] = {}        # our drafted sets by species id
        self.opp_cards: dict[str, dict] = {}    # opponent's drafted sets by species id
        self.seen: dict[str, dict] = dex.card_catalog()   # card_id -> full set (real catalog + this draft)
        self.prev_active: list[str | None] = [None, None]
        self.me: str | None = None

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

    # ---- dispatch ------------------------------------------------------
    def act(self, st: dict) -> dict:
        obs = st.get("observation") or {}
        phase = st.get("phase")
        if phase == "draft":
            return self._draft(st, obs)
        if phase == "teambuild":
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

    # ---- battle --------------------------------------------------------
    def _singles(self, st: dict, obs: dict) -> dict:
        la = (st.get("legal_actions") or {}).get("actions") or []
        me = (obs.get("active_pokemon") or {}).get("species", "")
        opp = obs.get("opponent_active_pokemon") or {}
        opp_types = [t.upper() for t in (opp.get("types") or dex.types_of(opp.get("species", "")))]
        best, best_v = None, -1.0
        for a in la:
            act = a["input"].get("action") or {}
            if act.get("type") == "move":
                v = move_value(act.get("move_id", ""), me, opp_types,
                               {"basePower": act.get("base_power"), "type": act.get("move_type"),
                                "category": act.get("category"), "accuracy": act.get("accuracy")})
            else:
                v = 5.0 if obs.get("force_switch") else 0.0
            if v > best_v:
                best, best_v = a, v
        best = best or la[0]
        return {k: v for k, v in best["input"].items() if k not in ("session_id", "state_version")}

    def _doubles(self, st: dict, obs: dict) -> dict:
        tmpl = st["legal_actions"]["actions"][0]["input"]["action"]
        actives = obs.get("active_pokemon") or [None, None]
        opps = obs.get("opponent_active_pokemon") or [None, None]
        turn = obs.get("turn", 0)
        pick, used_switch = {}, set()
        for slot in tmpl.get("slots", []):
            i = slot["slot"]
            me = actives[i] if i < len(actives) else None
            my_sp = to_id((me or {}).get("species", "") or slot.get("species", ""))
            fresh = self.prev_active[i] != my_sp
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
        self.prev_active = [to_id((a or {}).get("species", "")) or None for a in (list(actives) + [None, None])[:2]]
        return {"action": {"type": "doubles_turn", **pick}}

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
            else:  # ally-targeting (negative); only support moves make sense
                v = 15.0 if mid in ("helpinghand", "pollenpuff", "decorate", "coaching") else -100
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
