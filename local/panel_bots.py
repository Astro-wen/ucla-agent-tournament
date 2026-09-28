"""Scripted sparring bots for the promotion panel (local/panel.py).

Both draft and pick their four exactly like versions/v1_exact_damage, so the panel measures how a candidate
fights them rather than team building, and both use v1's exact damage table (one calc batch per turn). They
differ only in the battle:

* greedy        each slot on its own takes the move + target with the highest immediate expected damage:
                accuracy x mean over the damage rolls of min(roll, target's current HP), as a fraction of the
                target's max HP; a spread move sums its foes and subtracts what it does to our own ally. Damaging
                moves are never aimed at the ally; status moves score 0 (used only when no attack does anything).
                No voluntary switches: only a forced switch sends someone in (v1's resist-their-STABs pick).
* protect_spam  greedy, except that a threatened slot uses Protect / Detect (or another Protect-like move it
                has) whenever it is available: offered by the template and not used by that Pokemon on the
                previous turn (a repeat Protect only works 1/3 of the time, so every other turn at most).
                Threatened = some live foe's known damaging move is expected to take >= THREAT_FRACTION of the
                slot's current HP, or KOs it on at least one roll.
Fake Out / First Impression count as 0 damage after the user's first turn out.

Every decision still goes through v1's Contestant wrapper: template validation, then v1's first-legal-option
fallback. Each match's counters (decisions, fallbacks, protects) are on `BotContestant.stats`.
"""
from __future__ import annotations

import logging

from versions.v1_exact_damage.agent import agent as _A
from versions.v1_exact_damage.agent.pokemon import dex
from versions.v1_exact_damage.agent.pokemon import policy as _P
from versions.v1_exact_damage.agent.pokemon.calc import calc_field
from versions.v1_exact_damage.agent.pokemon.dex import to_id

log = logging.getLogger("local.panel_bots")

PROTECT_LIKE = {"protect", "detect", "spikyshield", "banefulbunker", "silktrap", "burningbulwark", "kingsshield",
                "obstruct"}
THREAT_FRACTION = 0.4
BOTS = ("greedy", "protect_spam")


class GreedyPolicy(_P.PokemonPolicy):
    """v1's draft / team preview; battle = per-slot max immediate expected damage (see module docstring)."""
    protect_when_threatened = False

    def __init__(self):
        super().__init__()
        self.stats = {"decisions": 0, "slots": 0, "protects": 0, "no_calc": 0}
        self.protected: dict[str, int] = {}     # species -> turn it last used a Protect-like move

    def _doubles(self, st: dict, obs: dict) -> dict:
        tmpl = st["legal_actions"]["actions"][0]["input"]["action"]
        actives = list(obs.get("active_pokemon") or []) + [None, None]
        opps = list(obs.get("opponent_active_pokemon") or []) + [None, None]
        table = self._exact_table(obs, tmpl, actives[:2], opps[:2])
        self.stats["decisions"] += 1
        self.stats["no_calc"] += table is None
        pick, used = {}, set()
        for slot in tmpl.get("slots", []):
            c = self._slot_choice(slot, obs, actives, opps, table, used)
            if c.get("type") == "switch":
                used.add(to_id(c.get("species")))
            pick[f"slot_{slot['slot']}"] = c
        self.prev_active = [to_id((a or {}).get("species", "")) or None for a in actives[:2]]
        return {"action": {"type": "doubles_turn", **pick}}

    # ---- one slot ------------------------------------------------------------------------------------
    def _slot_choice(self, slot, obs, actives, opps, table, used) -> dict:
        opts = slot.get("options") or []
        i = slot.get("slot", 0)
        me = self._slot_mon(slot, actives)
        moves = [o for o in opts if o.get("type") == "move"]
        if moves:
            self.stats["slots"] += 1
            if self.protect_when_threatened:
                sp = to_id((me or {}).get("species", ""))
                turn = obs.get("turn", 0)
                prot = next((o for o in moves if to_id(o.get("move_id", "")) in PROTECT_LIKE), None)
                if prot is not None and self.protected.get(sp) != turn - 1 and self._threatened(i, me, opps, table):
                    self.stats["protects"] += 1
                    self.protected[sp] = turn
                    return {"type": "move", "move_id": prot["move_id"], "target": (prot.get("targets") or [0])[0]}
            best, best_v = None, None
            for o in moves:
                for t in o.get("targets") or [0]:
                    v = self._damage_value(o, t, i, me, actives, opps, obs, table)
                    if v is not None and (best_v is None or v > best_v):
                        best, best_v = {"type": "move", "move_id": o["move_id"], "target": t}, v
            if best is not None:
                return best
            return _P._concrete(moves[0])
        switches = [o for o in opts if o.get("type") == "switch" and to_id(o.get("species")) not in used]
        if switches:                                     # only offered on a forced switch here
            return _P._concrete(max(switches, key=lambda o: self._switch_value(o, opps)))
        if any(o.get("type") == "pass" for o in opts):
            return {"type": "pass"}
        return _P._concrete(opts[0]) if opts else {"type": "pass"}

    def _damage_value(self, opt, t, i, me, actives, opps, obs, table) -> float | None:
        """Immediate expected damage (fractions of max HP) of one move + target; None = never pick it."""
        mid = to_id(opt.get("move_id", ""))
        m = dex.move(mid)
        if not _P._damaging(m, opt):
            return 0.0
        if isinstance(t, int) and t < 0:
            return None                                  # no attacks into our own ally
        my_sp = to_id((me or {}).get("species", ""))
        if mid in _P.FIRST_TURN_ONLY and i in (0, 1) and self.prev_active[i] == my_sp:
            return 0.0                                   # Fake Out & co. fail after the first turn out
        alive = {j: o for j, o in enumerate(opps[:2]) if o and not o.get("fainted")}
        acc = _P._acc_of(m, opt)
        card = self._card(self.cards, me)
        sun = calc_field(obs, True).get("weather") == "Sun"

        def expected(side, j, mon):
            r = (table or {}).get(("me", i, mid, side, j))
            if r and r.get("rolls") and r.get("defender_hp"):
                rolls = _P._adjust_rolls(r["rolls"], m, mid, me, card, sun)
                cur = _P._cur_hp(mon, r)
                return acc * sum(min(x, cur) for x in rolls) / len(rolls) / r["defender_hp"]
            frac = (mon or {}).get("current_hp_fraction")
            frac = 1.0 if frac is None else float(frac)
            pts = _P.move_value(mid, my_sp, _P._types(mon)) * _P.HEURISTIC_TO_POINTS
            return min(1.0, max(0.0, pts) / 100) * frac

        if t in (1, 2):
            j = t - 1
            return expected("opp", j, alive[j]) if j in alive else None
        if not alive:
            return 0.0
        if m.get("target") in _P.SPREAD_TARGETS:
            v = sum(expected("opp", j, o) for j, o in alive.items())
            ally = actives[1 - i] if i in (0, 1) else None
            if m.get("target") == "allAdjacent" and ally and not ally.get("fainted"):
                v -= expected("me", 1 - i, ally)
            return v
        return sum(expected("opp", j, o) for j, o in alive.items()) / len(alive)   # locked / auto-targeted

    def _threatened(self, i, me, opps, table) -> bool:
        if not me or me.get("fainted"):
            return False
        if table is None:                                # no calc: the base-stat heuristic, same threshold
            frac = me.get("current_hp_fraction") or 1.0
            for o in opps[:2]:
                if o and not o.get("fainted"):
                    card = self.opponent_set(o.get("species", "")) or {}
                    for mv in (card.get("moves") or [])[:4]:
                        pts = _P.move_value(to_id(mv), to_id(o.get("species", "")), _P._types(me))
                        if pts * _P.HEURISTIC_TO_POINTS / 100 >= THREAT_FRACTION * frac:
                            return True
            return False
        for k, r in table.items():
            if k[0] == "opp" and k[3] == "me" and k[4] == i and r and r.get("rolls"):
                useful, pko = _P._hit_stats(r["rolls"], _P._cur_hp(me, r))
                if _P._acc_of(dex.move(k[2])) * useful >= THREAT_FRACTION or pko > 0:
                    return True
        return False


class ProtectSpamPolicy(GreedyPolicy):
    protect_when_threatened = True


class BotContestant(_A.Contestant):
    """v1's Contestant (validation + fallback) around a bot policy; never records decisions."""

    def __init__(self, policy_cls):
        super().__init__()
        self.pk = policy_cls()
        self.stats = self.pk.stats
        self.stats["fallbacks"] = 0

    def choose_action(self, state, context):             # v1's wrapper would append to logs/<session>.jsonl
        return self._choose_action(state, context)

    def _structured(self, state, kw):
        action = kw.get("action") or {}
        if action.get("type") == "doubles_turn" and not _A.doubles_ok(state.raw, action):
            self.stats["fallbacks"] += 1
        return super()._structured(state, kw)

    def _safe_fallback(self, state, game):
        self.stats["fallbacks"] += 1
        return super()._safe_fallback(state, game)


def create_agent(kind: str = "greedy") -> BotContestant:
    if kind not in BOTS:
        raise ValueError(f"unknown bot {kind!r}; known: {BOTS}")
    return BotContestant(ProtectSpamPolicy if kind == "protect_spam" else GreedyPolicy)
