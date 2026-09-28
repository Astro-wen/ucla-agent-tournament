"""One-step joint-action lookahead (a payoff matrix) for VGC doubles turns.

Rows are our joint actions, columns the opponent's plausible joint replies; every cell abstractly plays
out one turn and scores the resulting position.

* Rows: the top-K_OURS choices per slot from the exact pair scorer (policy._exact_context), plus
  Protect where legal (never twice in a row) and the exact scorer's own pick, so the lookahead can
  always reproduce it. Forced switches and voluntary switches are left to the exact scorer.
* Columns: per live foe, its known moves (drafted set / revealed / guessed STAB) x legal targets,
  the top-K_THEIRS by exact damage into our board, plus Protect, Fake Out (fresh foes only) and
  Follow Me / Rage Powder when it has them.
* A cell: move order (priority incl. Prankster / Gale Wings / Grassy Glide, effective speed, Trick
  Room; exact speed ties go to the opponent), Protect / Wide Guard blocks, Follow Me / Rage Powder
  redirection, Helping Hand, Fake Out flinch on fresh Pokémon, Sucker Punch failing on non-attackers,
  priority blocked by Armor Tail / Queenly Majesty / Dazzling / Psychic Terrain, retargeting when
  the intended foe already fainted, Focus Sash at full HP, recoil and self-KO moves; KOs remove the
  victim's later action. Damage comes from the ONE DamageCalc.batch() of the decision (the exact
  table: spread moves carry the calc's 0.75x through the Doubles gameType, rescaled by 4/3 when
  only one target is left at execution). A hit KOs when P(hit) x P(roll >= HP) >= 0.5; otherwise it
  removes its expected damage. The score keeps the expectation (HP removed and a KO credit of P(KO)),
  so accuracy and roll luck are not rounded away.
* Position value (HP fractions, our view): damage we deal x the foe's danger weight (0.7..1.3, as
  in the pair scorer) - damage we take + KO_VALUE x (their KO credits - ours) + SPEED_BONUS per
  surviving Pokémon of ours that outspeeds every surviving foe + static value of support moves (the
  exact scorer's points / 100) - PROTECT_TEMPO per Protect.
* Decision rule: score(row) = W_WORST x min over columns + W_MEAN x mean over columns (0.6 / 0.4):
  robust against the reply that hurts most without assuming the opponent always finds it. Ties ->
  the exact pair score, then enumeration order (deterministic, no randomness anywhere).

Safety: `joint_lookahead` returns None whenever anything is missing (the caller keeps the exact pair
pick), K shrinks until rows x columns <= MAX_CELLS, and the cell loop gives up at TIME_CAP seconds
measured from the start of the decision.
"""
from __future__ import annotations

import logging
import time

from . import dex
from .calc import calc_field
from .dex import to_id
from .stats import effective_speed, move_priority

log = logging.getLogger("agent.lookahead")

TIME_CAP = 2.5            # seconds from the start of the decision (calc round trip included)
MAX_CELLS = 3000          # rows x columns before K shrinks
K_OURS = 5
K_THEIRS = 4
KO_VALUE = 0.5            # = policy.KO_BONUS / 100: a KO on top of the HP it removes
SPEED_BONUS = 0.05
PROTECT_TEMPO = 0.05      # a Protect gives up a turn of offence; depth 1 cannot see what it buys later
STATUS_SCALE = 0.01       # exact-scorer points -> HP fractions (100 points = a foe's whole HP)
W_WORST, W_MEAN = 0.6, 0.4
MARGIN = 0.0              # the lookahead must beat the exact pair pick's row by this much to override it

PROTECT_MOVES = {"protect", "detect", "spikyshield", "banefulbunker", "silktrap", "burningbulwark",
                 "kingsshield", "obstruct"}
REDIRECT_MOVES = {"followme", "ragepowder"}
SUPPORT_SIMULATED = PROTECT_MOVES | REDIRECT_MOVES | {"helpinghand", "wideguard"}
PRIORITY_BLOCKERS = {"armortail", "queenlymajesty", "dazzling"}
FLINCH_IMMUNE = {"innerfocus", "shielddust"}
SPREAD = {"allAdjacentFoes", "allAdjacent"}
SINGLE = {"normal", "any", "adjacentFoe"}
REDIRECT_IMMUNE_ABILITIES = {"stalwart", "propellertail"}

STATS = {"n": 0, "used": 0, "changed": 0, "total_ms": 0.0, "max_ms": 0.0, "timeouts": 0}


class _Mon:
    __slots__ = ("key", "side", "idx", "species", "hp", "view", "card", "types", "item", "ability", "speed",
                 "fresh", "maxhp", "grounded", "flinch_immune", "sash")

    def __init__(self, key, side, idx, species, hp, view, card, types, speed, fresh):
        self.key, self.side, self.idx, self.species = key, side, idx, species
        self.hp, self.view, self.card, self.types, self.speed, self.fresh = hp, view, card, types, speed, fresh
        self.item = to_id(view.get("item") or "")
        self.ability = to_id(view.get("ability") or "")
        self.maxhp = None
        self.grounded = "FLYING" not in types and self.ability != "levitate" and self.item != "airballoon"
        self.flinch_immune = self.ability in FLINCH_IMMUNE or self.item == "covertcloak"
        self.sash = self.item == "focussash" and hp >= 0.999


class _Act:
    """One slot's action in a cell. kind: attack | protect | redirect | helpinghand | wideguard | status | none."""
    __slots__ = ("actor", "side", "mid", "kind", "tgt", "prio", "order", "static", "src", "move", "spread",
                 "single", "pre", "ally_hit")

    def __init__(self, actor, side, mid, tgt, static=0.0, src=None):
        self.actor, self.side, self.mid, self.tgt, self.static, self.src = actor, side, mid, tgt, static, src
        self.move = dex.move(mid) if mid else {}
        target = self.move.get("target")
        self.spread = target in SPREAD
        self.single = target in SINGLE
        self.ally_hit = target == "allAdjacent"
        self.prio, self.order, self.pre = 0, (0, 0.0, 0), 0.0
        if not mid:
            self.kind = "none"
        elif mid in PROTECT_MOVES:
            self.kind = "protect"
        elif mid in REDIRECT_MOVES:
            self.kind = "redirect"
        elif mid == "helpinghand":
            self.kind = "helpinghand"
        elif mid == "wideguard":
            self.kind = "wideguard"
        elif _is_damaging(self.move):
            self.kind = "attack"
        else:
            self.kind = "status"


def _is_damaging(m: dict) -> bool:
    return bool(m.get("basePower")) and (m.get("category") or "").lower() != "status"


def _norm_view(mon: dict | None, card: dict | None) -> dict:
    v = dict(mon or {})
    v["status"] = to_id(v.get("status") or "") or None
    if not v.get("item") or v.get("item") == "unknown_item":
        v["item"] = (card or {}).get("item")
    if not v.get("ability"):
        v["ability"] = (card or {}).get("ability")
    return v


def _hp_frac(mon: dict | None) -> float:
    f = (mon or {}).get("current_hp_fraction")
    try:
        return max(0.0, min(1.0, float(1.0 if f is None else f)))
    except (TypeError, ValueError):
        return 1.0


def _only_switches(slot: dict) -> bool:
    opts = slot.get("options") or []
    return any(o.get("type") == "switch" for o in opts) and not any(o.get("type") == "move" for o in opts)


# ---------------------------------------------------------------------------------------------- entry point
def joint_lookahead(pol, obs, tmpl, actives, opps, turn, table, ctx, pick, t0) -> dict | None:
    """The lookahead's joint pick ({"slot_0": .., "slot_1": ..}) or None to keep the exact pair pick."""
    start = time.monotonic()
    try:
        out = _Search(pol, obs, tmpl, actives, opps, turn, table, ctx, pick, t0).run()
    except Exception:
        log.warning("joint lookahead crashed; keeping the exact pair pick", exc_info=True)
        out = None
    ms = 1000 * (time.monotonic() - start)
    STATS["n"] += 1
    STATS["total_ms"] += ms
    STATS["max_ms"] = max(STATS["max_ms"], ms)
    if out is not None:
        STATS["used"] += 1
        if out != pick:
            STATS["changed"] += 1
    return out


class _Search:
    def __init__(self, pol, obs, tmpl, actives, opps, turn, table, ctx, pick, t0):
        from . import policy as P                    # late import: policy imports this module
        self.P, self.pol, self.obs, self.tmpl, self.table, self.ctx, self.pick, self.t0 = \
            P, pol, obs, tmpl, table, ctx, pick, t0
        self.actives = (list(actives or []) + [None, None])[:2]
        self.opps = (list(opps or []) + [None, None])[:2]
        self.turn = turn if isinstance(turn, int) else 0
        self._dmg: dict = {}

    # ---- setup ---------------------------------------------------------------------------------------
    def run(self) -> dict | None:
        slots = self.tmpl.get("slots") or []
        per_slot = self.ctx.get("per_slot") or []
        if len(slots) != 2 or len(per_slot) != 2 or any(_only_switches(s) for s in slots):
            return None                                   # forced switches: the exact scorer decides
        fs = self.obs.get("force_switch")
        if (isinstance(fs, list) and any(fs)) or fs is True:
            return None
        if not self._board(slots):
            return None
        rows_per_slot = [self._our_acts(i, cands) for i, cands in per_slot]
        if any(not r for r in rows_per_slot):
            return None
        cols_per_slot = [self._their_acts(j) for j in range(2)]
        k_ours, k_theirs = K_OURS, K_THEIRS
        while True:
            rows = self._product(self._top(rows_per_slot, k_ours, ours=True))
            cols = self._product(self._top(cols_per_slot, k_theirs, ours=False))
            if len(rows) * len(cols) <= MAX_CELLS or (k_ours <= 2 and k_theirs <= 2):
                break
            if k_theirs >= k_ours:
                k_theirs -= 1
            else:
                k_ours -= 1
        if not rows or not cols:
            return None
        pair_v = {}
        for r in rows:
            pv = self.pol._pair_value(self.ctx, r[0].src, r[1].src) if r[0].src and r[1].src else None
            pair_v[id(r)] = -1e18 if pv is None else pv
        best, best_key, pick_key, scored = None, None, None, []
        pick_row = self._pick_row(rows)
        for n, row in enumerate(rows):
            if time.monotonic() - self.t0 > TIME_CAP:
                STATS["timeouts"] += 1
                log.warning("joint lookahead hit the %.1fs cap after %d/%d rows", TIME_CAP, n, len(rows))
                return None
            vals = [self._simulate(row, col) for col in cols]
            score = W_WORST * min(vals) + W_MEAN * sum(vals) / len(vals)
            key = (round(score, 9), pair_v[id(row)], -n)
            scored.append((score, row))
            if row is pick_row:
                pick_key = key
            if best_key is None or key > best_key:
                best, best_key = row, key
        if best is None:
            return None
        if pick_key is not None and best is not pick_row and best_key[0] < pick_key[0] + MARGIN:
            best, best_key = pick_row, pick_key
        out = {f"slot_{slots[s]['slot']}": best[s].src["cand"] for s in range(2)}
        self.pol.last_lookahead = {"rows": len(rows), "cols": len(cols), "k": (k_ours, k_theirs),
                                   "score": best_key[0], "pick_score": None if pick_key is None else pick_key[0],
                                   "ms": round(1000 * (time.monotonic() - self.t0), 1), "choice": out}
        return out

    def _pick_row(self, rows):
        want = [self.pick.get(f"slot_{s['slot']}") for s in self.tmpl.get("slots", [])]
        return next((r for r in rows if [r[0].src["cand"], r[1].src["cand"]] == want), None)

    def _board(self, slots) -> bool:
        P, pol, obs = self.P, self.pol, self.obs
        f = calc_field(obs, True)
        self.sun = f.get("weather") == "Sun"
        self.terrain = f.get("terrain")
        fields = obs.get("fields")
        fields = list(fields) if isinstance(fields, dict) else (fields if isinstance(fields, list) else [])
        self.trick_room = any("trickroom" in to_id(str(x)) for x in fields)
        speed_field = {"weather": f.get("weather"), "terrain": f.get("terrain")}
        sides = {"me": obs.get("side_conditions") or {}, "opp": obs.get("opponent_side_conditions") or {}}
        self.mons: dict[str, _Mon | None] = {}
        fresh_me = self.ctx.get("fresh") or {}
        # A foe is fresh (Fake Out works) when it was not on the field at our previous decision. Without any
        # history (first decision we see is past turn 1, e.g. after a restart) only turn 1 counts as fresh.
        history = any(pol.opp_prev_active)
        for s in slots:
            i = s.get("slot")
            if i not in (0, 1):
                return False
            raw = pol._slot_mon(s, self.actives)
            m = None
            if raw and not raw.get("fainted") and raw.get("species"):
                card = pol._card(pol.cards, raw)
                view = _norm_view(raw, card)
                sp = to_id(raw.get("species"))
                m = _Mon(f"me{i}", "me", i, sp, _hp_frac(raw), view, card, P._types(raw),
                         effective_speed(view, card, sides["me"], speed_field), bool(fresh_me.get(i, False)))
            self.mons[f"me{i}"] = m
        for j in range(2):
            o = self.opps[j]
            m = None
            if o and not o.get("fainted") and o.get("species"):
                card = pol._card(pol.opp_cards, o)
                view = _norm_view(o, card)
                sp = to_id(o.get("species"))
                fresh = (list(pol.opp_prev_active) + [None, None])[j] != sp if history else self.turn <= 1
                m = _Mon(f"opp{j}", "opp", j, sp, _hp_frac(o), view, card, P._types(o),
                         effective_speed(view, card, sides["opp"], speed_field), fresh)
                if m.hp <= 0:
                    m = None
            self.mons[f"opp{j}"] = m
        if not any(self.mons.get(f"opp{j}") for j in range(2)):
            return False
        if not any(self.mons.get(f"me{i}") for i in range(2)):
            return False
        for k, r in self.table.items():                    # max HP on the calc's scale, for recoil
            if r and r.get("defender_hp"):
                m = self.mons.get(("me" if k[3] == "me" else "opp") + str(k[4]))
                if m is not None and m.maxhp is None:
                    m.maxhp = r["defender_hp"]
        self.hp0 = {k: m.hp for k, m in self.mons.items() if m}
        self.weight = {f"opp{j}": self.ctx.get("weights", {}).get(j, 1.0) for j in range(2)}
        return True

    # ---- damage lookup ------------------------------------------------------------------------------
    def _dist(self, a: _Mon, mid: str, d: _Mon):
        """(damage rolls as fractions of the defender's max HP, accuracy, calc used the spread cut) or None."""
        key = (a.key, mid, d.key)
        if key in self._dmg:
            return self._dmg[key]
        P = self.P
        m = dex.move(mid)
        r = self.table.get((a.side, a.idx, mid, d.side, d.idx))
        out = None
        if r and r.get("defender_hp") and r.get("rolls"):
            rolls = P._adjust_rolls(r["rolls"], m, mid, a.view, a.card, self.sun)
            out = ([x / r["defender_hp"] for x in rolls], P._acc_of(m), self._spread_count(a, m) > 1)
        elif _is_damaging(m):                              # calc entry missing: the base-stat heuristic
            pts = P.move_value(mid, a.species, d.types) * P.HEURISTIC_TO_POINTS
            out = ([max(0.0, pts) / 100 * max(d.hp, 0.05)], 1.0, self._spread_count(a, m) > 1)
        self._dmg[key] = out
        return out

    def _spread_count(self, a: _Mon, m: dict) -> int:
        """Targets the table's calc saw for a spread move (it used the Doubles cut only when > 1)."""
        tgt = m.get("target")
        if tgt not in SPREAD:
            return 1
        foe_side = "opp" if a.side == "me" else "me"
        n = sum(1 for j in range(2) if self.mons.get(f"{foe_side}{j}"))
        if tgt == "allAdjacent" and self.mons.get(f"{a.side}{1 - a.idx}"):
            n += 1
        return n

    # ---- candidates ---------------------------------------------------------------------------------
    def _our_acts(self, i, cands) -> list[_Act]:
        me = self.mons.get(f"me{i}")
        protected_last = me is not None and self.pol.last_protect.get(me.species) == self.turn - 1
        out = []
        for c in cands:
            cand = c.get("cand") or {}
            if cand.get("type") == "pass":
                out.append(_Act(None, "me", None, None, 0.0, c))
                continue
            if cand.get("type") != "move" or me is None:
                continue                                   # voluntary switches stay with the exact scorer
            mid = to_id(cand.get("move_id", ""))
            if mid in PROTECT_MOVES and protected_last:
                continue
            t = cand.get("target", 0)
            tgt = None
            if t in (1, 2):
                tgt = ("opp", t - 1)
            elif t in (-1, -2):
                tgt = ("me", -t - 1)
            a = _Act(me.key, "me", mid, tgt, 0.0, c)
            if a.kind == "status":
                a.static = STATUS_SCALE * c.get("base", 0.0)
            elif a.kind == "attack" and tgt is not None and tgt[0] == "me":
                a.kind, a.static = "none", STATUS_SCALE * min(0.0, c.get("base", 0.0))
            elif a.kind == "attack" and tgt is None and a.single:
                live = [j for j in range(2) if self.mons.get(f"opp{j}")]
                a.tgt = ("opp", live[0]) if live else None     # locked single-target move: first live foe
            a.pre = c.get("solo", 0.0)
            out.append(self._finish(a, me))
        return out

    def _their_acts(self, j) -> list[_Act]:
        foe = self.mons.get(f"opp{j}")
        if foe is None:
            return [_Act(None, "opp", None, None)]
        out = []
        for mid in dict.fromkeys(self.pol.foe_moves(self.opps[j])):
            probe = _Act(foe.key, "opp", mid, None)
            if probe.kind in ("protect", "redirect"):
                out.append(self._finish(probe, foe))
            elif probe.kind != "attack":
                continue
            elif mid == "fakeout" and not foe.fresh:
                continue
            elif probe.single:
                for i in range(2):
                    if self.mons.get(f"me{i}"):
                        out.append(self._finish(_Act(foe.key, "opp", mid, ("me", i)), foe))
            elif probe.spread:
                out.append(self._finish(probe, foe))
        for a in out:
            a.pre = self._threat(a, foe)
        return out or [_Act(None, "opp", None, None)]

    def _threat(self, a: _Act, foe: _Mon) -> float:
        """Their action's damage into our board (HP fractions + KO odds) — ranks the replies."""
        if a.kind != "attack":
            return 0.0
        targets = [self.mons.get(f"me{i}") for i in range(2)] if a.spread else \
            [self.mons.get(f"me{a.tgt[1]}")] if a.tgt else []
        v = 0.0
        for d in targets:
            if d is None:
                continue
            dist = self._dist(foe, a.mid, d)
            if dist:
                fr, acc, _ = dist
                n = len(fr)
                v += acc * (sum(min(x, d.hp) for x in fr) / n + KO_VALUE * sum(x >= d.hp for x in fr) / n)
        if a.mid == "fakeout":
            v += 0.3
        return v

    def _finish(self, a: _Act, m: _Mon) -> _Act:
        prio = move_priority(a.mid, m.view, m.card)
        if a.mid == "grassyglide" and self.terrain == "Grassy" and m.grounded:
            prio += 1
        a.prio = prio
        spd = m.speed if self.trick_room else -m.speed
        a.order = (-prio, spd, 0 if a.side == "opp" else 1)       # exact speed ties go to the opponent
        return a

    def _top(self, per_slot, k, ours: bool):
        res = []
        for n, acts in enumerate(per_slot):
            ranked = sorted(acts, key=lambda a: -a.pre)             # stable: enumeration order breaks ties
            keep = ranked[:k]
            for a in ranked[k:]:
                must = a.kind == "protect" and not any(x.kind == "protect" for x in keep)
                if not ours:
                    must = must or a.mid == "fakeout" or (a.kind == "redirect" and not any(x.kind == "redirect" for x in keep))
                else:
                    want = self.pick.get(f"slot_{(self.tmpl.get('slots') or [{}, {}])[n].get('slot')}")
                    must = must or (a.src is not None and a.src.get("cand") == want)
                if must:
                    keep.append(a)
            res.append(keep)
        return res

    @staticmethod
    def _product(per_slot):
        return [(a, b) for a in per_slot[0] for b in per_slot[1]]

    # ---- one cell -----------------------------------------------------------------------------------
    def _simulate(self, row, col) -> float:
        mons = self.mons
        hp = dict(self.hp0)                  # state HP (drives KOs and later rolls)
        dealt = dict.fromkeys(hp, 0.0)       # expected HP removed (drives the score)
        credit = dict.fromkeys(hp, 0.0)      # expected KO credit, max over hits
        down = set()
        protected, flinched, acted, hh, wide = set(), set(), set(), set(), set()
        redirect: dict[str, str] = {}
        bonus = 0.0
        acts = [a for a in (row[0], row[1], col[0], col[1]) if a.actor is not None and a.kind != "none"]
        chosen = {a.actor: a for a in acts}
        acts.sort(key=lambda a: a.order)
        for a in acts:
            k = a.actor
            if k in down or k in flinched:
                continue
            acted.add(k)
            m = mons[k]
            kind = a.kind
            if kind == "protect":
                protected.add(k)
                if a.side == "me":
                    bonus -= PROTECT_TEMPO
                continue
            if kind == "redirect":
                redirect[a.side] = k
                continue
            if kind == "helpinghand":
                p = f"{a.side}{1 - m.idx}"
                if p in hp and p not in down:
                    hh.add(p)
                continue
            if kind == "wideguard":
                wide.add(a.side)
                continue
            foe_side = "opp" if a.side == "me" else "me"
            if kind == "status":
                if a.side == "me":
                    if a.tgt and a.tgt[0] == "opp":
                        t = self._retarget(f"opp{a.tgt[1]}", "opp", down)
                        if t is None or (t in protected and a.move.get("flags", {}).get("protect")):
                            continue
                    bonus += a.static
                continue
            # ---- an attack
            if a.mid == "fakeout" and not m.fresh:
                continue
            if a.spread:
                targets = [f"{foe_side}{j}" for j in range(2) if f"{foe_side}{j}" in hp and f"{foe_side}{j}" not in down]
                if a.ally_hit:
                    p = f"{a.side}{1 - m.idx}"
                    if p in hp and p not in down:
                        targets.append(p)
            elif a.tgt:
                t = f"{a.tgt[0]}{a.tgt[1]}"
                r = redirect.get(foe_side)
                if a.tgt[0] == foe_side and r and r not in down and r != t and self._redirectable(a, m, r):
                    t = r
                t = self._retarget(t, a.tgt[0], down) if a.tgt[0] == foe_side else (t if t not in down else None)
                targets = [t] if t else []
            else:
                targets = []
            if not targets:
                continue
            if a.prio > 0 and self._priority_blocked(foe_side, down):
                continue
            n_targets = len(targets)
            mult = 1.5 if k in hh else 1.0
            for t in targets:
                d = mons[t]
                if t in protected and a.move.get("flags", {}).get("protect") \
                        and not (m.ability == "unseenfist" and a.move.get("flags", {}).get("contact")):
                    continue
                if a.spread and d.side != a.side and d.side in wide:
                    continue
                if a.spread and d.side == a.side and a.side in wide:
                    continue
                if a.mid == "suckerpunch":
                    ta = chosen.get(t)
                    if t in acted or ta is None or ta.kind != "attack":
                        continue
                if a.prio > 0 and self.terrain == "Psychic" and d.grounded and d.side != a.side:
                    continue
                dist = self._dist(m, a.mid, d)
                if not dist:
                    continue
                fr, acc, spread_cut = dist
                f = mult * (4 / 3 if spread_cut and n_targets == 1 else 1.0)
                h = hp[t]
                if h <= 0:
                    continue
                sash = d.sash and h >= 0.999 and not a.move.get("multihit")
                cap = h - 0.01 if sash else h
                n = len(fr)
                useful = acc * sum(min(x * f, cap) for x in fr) / n
                pko = 0.0 if sash else acc * sum(x * f >= h for x in fr) / n
                dealt[t] += useful
                credit[t] = max(credit[t], pko)
                if pko >= 0.5:
                    hp[t] = 0.0
                    down.add(t)
                else:
                    hp[t] = max(0.01, h - useful)
                    if a.mid == "fakeout" and t not in acted and not d.flinch_immune:
                        flinched.add(t)
                rec = a.move.get("recoil")
                if rec and m.maxhp and d.maxhp and k not in down:
                    loss = useful * d.maxhp * rec[0] / rec[1] / m.maxhp
                    dealt[k] += min(loss, hp[k])
                    hp[k] = hp[k] - loss
                    if hp[k] <= 0:
                        hp[k] = 0.0
                        down.add(k)
                        credit[k] = 1.0
            if a.move.get("selfdestruct") and k not in down:
                dealt[k] += hp[k]
                hp[k] = 0.0
                down.add(k)
                credit[k] = 1.0
        v = bonus
        for k, h0 in self.hp0.items():
            loss = min(dealt[k], h0) + KO_VALUE * credit[k]
            if k[0] == "m":
                v -= loss
            else:
                v += self.weight.get(k, 1.0) * loss
        foes_alive = [mons[k].speed for k in ("opp0", "opp1") if k in hp and k not in down]
        if foes_alive:
            for k in ("me0", "me1"):
                if k in hp and k not in down:
                    s = mons[k].speed
                    if all((s < x) if self.trick_room else (s > x) for x in foes_alive):
                        v += SPEED_BONUS
        return v

    def _retarget(self, t: str, side: str, down: set) -> str | None:
        """A single-target move whose target already fainted hits the other live foe (Gen 5+)."""
        if t in self.hp0 and t not in down:
            return t
        other = f"{side}{1 - int(t[-1])}"
        return other if other in self.hp0 and other not in down else None

    def _redirectable(self, a: _Act, m: _Mon, r: str) -> bool:
        if m.ability in REDIRECT_IMMUNE_ABILITIES or a.mid == "snipeshot":
            return False
        rm = self.mons[r]
        ra = next((x for x in (self._redirect_move(rm),) if x), "followme")
        if ra == "ragepowder" and ("GRASS" in m.types or m.ability == "overcoat" or m.item == "safetygoggles"):
            return False
        return True

    def _redirect_move(self, rm: _Mon) -> str | None:
        moves = [to_id(x) for x in (rm.card or {}).get("moves") or []]
        return "ragepowder" if "ragepowder" in moves else ("followme" if "followme" in moves else None)

    def _priority_blocked(self, foe_side: str, down: set) -> bool:
        for j in range(2):
            k = f"{foe_side}{j}"
            if k in self.hp0 and k not in down and self.mons[k].ability in PRIORITY_BLOCKERS:
                return True
        return False
