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
* An attack aimed at our own ally plays out for real: it lands on the partner (Protect blocks it, it
  fails when the partner is already down, no redirection / retargeting / Armor Tail), its damage and
  KO count against us, and a Pollen Puff heals the partner by half its max HP instead (nothing at
  full HP or into Bulletproof). The exact table carries our single-target attacks into the partner for
  this. (LOOKAHEAD_ALLY_FIX=0 restores the old free pass, where such a slot simply did nothing.)
* Position value (HP fractions, our view): damage we deal x the foe's danger weight (0.7..1.3, as
  in the pair scorer) - damage we take (net of heals) + KO_VALUE x (their KO credits - ours) +
  SPEED_BONUS per surviving Pokémon of ours that outspeeds every surviving foe + static value of
  support moves (the exact scorer's points / 100, e.g. ally-targeted Decorate / Coaching keep theirs)
  - PROTECT_TEMPO per Protect.
* Decision rule: score(row) = ALPHA x min over columns + (1 - ALPHA) x mean over columns (0.4 / 0.6):
  robust against the reply that hurts most without assuming the opponent always finds it. Ties ->
  the exact pair score, then enumeration order (deterministic, no randomness anywhere).
* Unmodelled picks: when the exact pick holds an action the lookahead does not build (a voluntary
  switch, anything else missing from the rows) it has nothing to compare against, so it keeps the exact
  pick. An attack the exact pick aims at our own ally (a Pollen Puff heal) is kept the same way: the
  exact scorer only picks one deliberately. The one deliberate exception is a repeat Protect, which
  the rows leave out on purpose: alone it is replaced by the lookahead as usual; next to a kept slot,
  that slot stays and the Protect slot is re-picked by the exact pair score without the Protect chain
  (LOOKAHEAD_SWITCH_FIX=0 restores the old override for A/B runs).
* Knobs (env, read per decision; unset / invalid = the defaults above): LOOKAHEAD_ALPHA (weight of the
  min, 0..1, default 0.4; v2 used 0.6), LOOKAHEAD_FOE_WEIGHT = equal (default) | softmax: the mean over replies
  weighted by softmax(threat / LOOKAHEAD_FOE_TEMP) (default 1.0), where a reply's threat is the
  column-ranking score (damage + KO odds into our board, summed over both foes); LOOKAHEAD_K_OURS
  (default 5) / LOOKAHEAD_K_THEIRS (default 4): candidates per slot before the MAX_CELLS shrink.
* Optional Nash mixing (env LOOKAHEAD_MIX = off | sample | argmax, default off = the rule above): the
  same payoff matrix, cut to the MIX_K best rows (the exact pick always kept) and the MIX_K replies
  that hurt them most, is solved as a zero-sum game by regret matching+ (pure Python, deterministic).
  argmax plays the most likely row of our equilibrium strategy; sample draws a row from it with an RNG
  seeded from the decision (session, state version, turn, board), so replays reproduce. Any failure
  (exception, non-finite values, empty matrix, time) keeps the rule above.

Safety: `joint_lookahead` returns None whenever anything is missing (the caller keeps the exact pair
pick), K shrinks until rows x columns <= MAX_CELLS, and the cell loop gives up at TIME_CAP seconds of
lookahead work: the clock (t0) starts when the caller hands over, after the exact table, so a slow
calculator start-up or round trip never eats the budget (the policy also warms the calculator before
the first battle decision).
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
import time
import zlib
from operator import mul

from . import dex
from .calc import calc_field, view_item
from .dex import to_id
from .stats import effective_speed, move_priority

log = logging.getLogger("agent.lookahead")

TIME_CAP = 2.5            # seconds of lookahead work, from the t0 the caller passes (after the calc round trip)
MAX_CELLS = 3000          # rows x columns before K shrinks
K_OURS = 5                # defaults of LOOKAHEAD_K_OURS / LOOKAHEAD_K_THEIRS
K_THEIRS = 4
KO_VALUE = 0.5            # = policy.KO_BONUS / 100: a KO on top of the HP it removes
SPEED_BONUS = 0.05
PROTECT_TEMPO = 0.05      # a Protect gives up a turn of offence; depth 1 cannot see what it buys later
STATUS_SCALE = 0.01       # exact-scorer points -> HP fractions (100 points = a foe's whole HP)
# Default of LOOKAHEAD_ALPHA (= W_WORST; W_MEAN = 1 - W_WORST). 0.4 since v3-harden: on the 8-opponent panel (250
# paired games each) alpha 0.4 scored 70.0% vs 67.65% for v2's 0.6 (+2.35 pp, permutation p = 0.0006).
W_WORST, W_MEAN = 0.4, 0.6
FOE_WEIGHT_MODES = ("equal", "softmax")
FOE_TEMP = 1.0            # default of LOOKAHEAD_FOE_TEMP
ALLY_HEAL = {"pollenpuff": 0.5}   # attack -> fraction of max HP it heals when aimed at our own ally
MARGIN = 0.0              # the lookahead must beat the exact pair pick's row by this much to override it

MIX_MODES = ("off", "sample", "argmax")
MIX_K = 16                # rows / columns of the subgame the mixed strategy is solved on
MIX_ITERS = 300           # regret-matching+ iterations
MIX_MIN_P = 0.05          # sample: rows below this probability are solver residue, not support
MIX_TIME = 0.25           # seconds the solver may use (and never past TIME_CAP)

PROTECT_MOVES = {"protect", "detect", "spikyshield", "banefulbunker", "silktrap", "burningbulwark",
                 "kingsshield", "obstruct"}
REDIRECT_MOVES = {"followme", "ragepowder"}
SUPPORT_SIMULATED = PROTECT_MOVES | REDIRECT_MOVES | {"helpinghand", "wideguard"}
PRIORITY_BLOCKERS = {"armortail", "queenlymajesty", "dazzling"}
FLINCH_IMMUNE = {"innerfocus", "shielddust"}
SPREAD = {"allAdjacentFoes", "allAdjacent"}
SINGLE = {"normal", "any", "adjacentFoe"}
REDIRECT_IMMUNE_ABILITIES = {"stalwart", "propellertail"}

STATS = {"n": 0, "used": 0, "changed": 0, "total_ms": 0.0, "max_ms": 0.0, "timeouts": 0,
         "kept_unmodelled": 0, "kept_repicked": 0, "mix_used": 0, "mix_changed": 0, "mix_fallback": 0, "mix_pure": 0,
         "ally_rows": 0, "ally_out": 0}
# ally_rows: scored decisions whose rows hold an attack on our own ally; ally_out: decisions the lookahead answered
# with such an attack that the exact pick did not hold (with LOOKAHEAD_ALLY_FIX=0: the free-pass bug in action)


def mix_mode() -> str:
    """LOOKAHEAD_MIX: off (default) | sample | argmax; anything else reads as off."""
    m = str(os.environ.get("LOOKAHEAD_MIX") or "off").strip().lower()
    return m if m in MIX_MODES else "off"


def _flag(name: str) -> bool:
    return str(os.environ.get(name) or "1").strip().lower() not in ("0", "off", "false", "no")


def switch_fix() -> bool:
    """LOOKAHEAD_SWITCH_FIX (default on): keep an exact pick the lookahead cannot evaluate."""
    return _flag("LOOKAHEAD_SWITCH_FIX")


def ally_fix() -> bool:
    """LOOKAHEAD_ALLY_FIX (default on): an attack on our own ally plays out for real (off = the old free pass)."""
    return _flag("LOOKAHEAD_ALLY_FIX")


def _env_num(name: str, default, cast, lo, hi):
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        v = cast(float(str(raw).strip()))
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(v) or v < lo or v > hi:
        return default
    return v


def alpha() -> float:
    """LOOKAHEAD_ALPHA in [0, 1] (default W_WORST = 0.4): row score = alpha x min + (1 - alpha) x mean."""
    return _env_num("LOOKAHEAD_ALPHA", W_WORST, float, 0.0, 1.0)


def foe_weight_mode() -> str:
    """LOOKAHEAD_FOE_WEIGHT: equal (default) | softmax; anything else reads as equal."""
    m = str(os.environ.get("LOOKAHEAD_FOE_WEIGHT") or "equal").strip().lower()
    return m if m in FOE_WEIGHT_MODES else "equal"


def foe_temp() -> float:
    """LOOKAHEAD_FOE_TEMP > 0 (default 1.0): softmax temperature on the replies' threat (HP-fraction scale)."""
    t = _env_num("LOOKAHEAD_FOE_TEMP", FOE_TEMP, float, 0.0, 1e9)
    return t if t > 0 else FOE_TEMP


def k_limits() -> tuple[int, int]:
    """(LOOKAHEAD_K_OURS, LOOKAHEAD_K_THEIRS), each >= 1 (defaults K_OURS = 5, K_THEIRS = 4): our candidates per slot
    and each foe's replies before the MAX_CELLS shrink."""
    return (_env_num("LOOKAHEAD_K_OURS", K_OURS, int, 1, 1000), _env_num("LOOKAHEAD_K_THEIRS", K_THEIRS, int, 1, 1000))


def row_score(vals: list[float], w_worst: float = W_WORST, w_mean: float = W_MEAN, col_w=None) -> float:
    """w_worst x min + w_mean x mean of a row's cells; col_w (summing to 1) weighs the mean, None = equal weights."""
    if col_w is None:
        return w_worst * min(vals) + w_mean * sum(vals) / len(vals)
    return w_worst * min(vals) + w_mean * sum(map(mul, vals, col_w))


def softmax_weights(threats: list[float], temp: float) -> list[float] | None:
    """softmax(threat / temp), max-shifted; None when anything is non-finite (the caller then weighs equally)."""
    if not threats or not temp > 0 or not all(math.isfinite(x) for x in threats):
        return None
    top = max(threats)
    w = [math.exp((x - top) / temp) for x in threats]
    tot = sum(w)
    if not math.isfinite(tot) or tot <= 0:
        return None
    return [v / tot for v in w]


def _normalized(w: list[float]) -> list[float]:
    tot = sum(w)
    return [v / tot for v in w] if tot > 0 else [1.0 / len(w)] * len(w)


def solve_rm_plus(A, iters: int = MIX_ITERS, deadline: float | None = None):
    """Zero-sum matrix game A (rows = us, maximising; columns = them, minimising), solved by alternating
    regret matching+ with linearly weighted strategy averages. Pure Python and deterministic.

    Returns (row strategy, column strategy, exploitability gap = max_i (A y)_i - min_j (x A)_j), or None
    for an empty / ragged / non-finite matrix or when `deadline` (time.monotonic()) passes."""
    m = len(A)
    if not m:
        return None
    n = len(A[0])
    if not n or any(len(r) != n for r in A):
        return None
    A = [[float(v) for v in r] for r in A]
    if not all(math.isfinite(v) for r in A for v in r):
        return None
    AT = [list(c) for c in zip(*A)]
    rx, ry = [0.0] * m, [0.0] * n
    x, y = [1.0 / m] * m, [1.0 / n] * n
    sx, sy = [0.0] * m, [0.0] * n
    for t in range(1, iters + 1):
        if deadline is not None and t % 32 == 0 and time.monotonic() > deadline:
            return None
        u = [sum(map(mul, r, y)) for r in A]              # our payoff per row against their current mix
        ev = sum(map(mul, x, u))
        rx = [max(0.0, r + ui - ev) for r, ui in zip(rx, u)]
        x = _normalized(rx)
        sx = [s + t * xi for s, xi in zip(sx, x)]
        w = [sum(map(mul, c, x)) for c in AT]             # their loss per column against our updated mix
        ev = sum(map(mul, y, w))
        ry = [max(0.0, r + ev - wj) for r, wj in zip(ry, w)]
        y = _normalized(ry)
        sy = [s + t * yj for s, yj in zip(sy, y)]
    xs, ys = _normalized(sx), _normalized(sy)
    gap = max(sum(map(mul, r, ys)) for r in A) - min(sum(map(mul, c, xs)) for c in AT)
    if not math.isfinite(gap) or not all(math.isfinite(v) for v in xs):
        return None
    return xs, ys, gap


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
    """One slot's action in a cell. kind: attack | protect | redirect | helpinghand | wideguard | status | none.
    at_ally: an attack deliberately aimed at the actor's own partner (a single-target move on target -1 / -2)."""
    __slots__ = ("actor", "side", "mid", "kind", "tgt", "prio", "order", "static", "src", "move", "spread",
                 "single", "pre", "ally_hit", "at_ally")

    def __init__(self, actor, side, mid, tgt, static=0.0, src=None):
        self.actor, self.side, self.mid, self.tgt, self.static, self.src = actor, side, mid, tgt, static, src
        self.move = dex.move(mid) if mid else {}
        target = self.move.get("target")
        self.spread = target in SPREAD
        self.single = target in SINGLE
        self.ally_hit = target == "allAdjacent"
        self.at_ally = False
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
    # Hidden item -> the drafted card's; an item the view shows as gone (used Sash, knocked-off Scarf) stays gone,
    # exactly as in the calculator's input (calc.view_item), so the Sash rule here matches the calc's rolls.
    v["item"] = view_item(mon, card)
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
def joint_lookahead(pol, obs, tmpl, actives, opps, turn, table, ctx, pick, t0, seed_key=None,
                    time_cap: float | None = None) -> dict | None:
    """The lookahead's joint pick ({"slot_0": .., "slot_1": ..}) or None to keep the exact pair pick.
    t0: time.monotonic() when the caller hands over (after its calc round trip); TIME_CAP counts from it.
    seed_key (e.g. (session id, state version, turn)) seeds LOOKAHEAD_MIX=sample's draw.
    time_cap: a tighter cap than TIME_CAP for this call (the decision's deadline, agent/timecontrol.py)."""
    start = time.monotonic()
    try:
        out = _Search(pol, obs, tmpl, actives, opps, turn, table, ctx, pick, t0, seed_key, time_cap).run()
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
    def __init__(self, pol, obs, tmpl, actives, opps, turn, table, ctx, pick, t0, seed_key=None, time_cap=None):
        from . import policy as P                    # late import: policy imports this module
        self.P, self.pol, self.obs, self.tmpl, self.table, self.ctx, self.pick, self.t0 = \
            P, pol, obs, tmpl, table, ctx, pick, t0
        self.seed_key = seed_key
        self.time_cap = time_cap
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
        k_ours, k_theirs = k_limits()
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
        pick_row = self._pick_row(rows)
        if switch_fix():
            kinds = self._pick_kinds(slots, rows_per_slot)
            if "keep" in kinds:                           # nothing to compare those slots against: keep them
                if "repeat" not in kinds:
                    STATS["kept_unmodelled"] += 1
                    return None
                out = self._repick_repeat(slots, kinds)   # ... but never keep a Protect chain next to them
                if out is not None:
                    STATS["kept_unmodelled"] += 1
                    STATS["kept_repicked"] += 1
                    return out
        pair_v = {}
        for r in rows:
            pv = self.pol._pair_value(self.ctx, r[0].src, r[1].src) if r[0].src and r[1].src else None
            pair_v[id(r)] = -1e18 if pv is None else pv
        if any(a.at_ally for r in rows for a in r):
            STATS["ally_rows"] += 1
        w_worst = alpha()
        w_mean = W_MEAN if w_worst == W_WORST else 1.0 - w_worst
        fw_mode = foe_weight_mode()
        col_w = softmax_weights([c[0].pre + c[1].pre for c in cols], foe_temp()) if fw_mode == "softmax" else None
        best, best_key, pick_key, scored = None, None, None, []
        matrix, keys = [], []
        cap = self._cap()
        for n, row in enumerate(rows):
            if time.monotonic() - self.t0 > cap:
                STATS["timeouts"] += 1
                log.warning("joint lookahead hit the %.2fs cap after %d/%d rows", cap, n, len(rows))
                return None
            vals = [self._simulate(row, col) for col in cols]
            score = row_score(vals, w_worst, w_mean, col_w)
            key = (round(score, 9), pair_v[id(row)], -n)
            scored.append((score, row))
            matrix.append(vals)
            keys.append(key)
            if row is pick_row:
                pick_key = key
            if best_key is None or key > best_key:
                best, best_key = row, key
        if best is None:
            return None
        if pick_key is not None and best is not pick_row and best_key[0] < pick_key[0] + MARGIN:
            best, best_key = pick_row, pick_key
        mode, mix = mix_mode(), None
        if mode != "off":
            try:
                mix = self._mix(mode, rows, cols, matrix, keys, pick_row)
            except Exception:
                log.warning("lookahead mixing crashed; keeping the robust row", exc_info=True)
                mix = None
            if mix is None:
                STATS["mix_fallback"] += 1
            else:
                STATS["mix_used"] += 1
                n, mix = mix
                if rows[n] is not best:
                    STATS["mix_changed"] += 1
                best, best_key = rows[n], keys[n]
        out = {f"slot_{slots[s]['slot']}": best[s].src["cand"] for s in range(2)}
        if best is not pick_row and any(a.at_ally and a.src["cand"] != self.pick.get(f"slot_{slots[s]['slot']}")
                                        for s, a in enumerate(best)):
            STATS["ally_out"] += 1
        self.pol.last_lookahead = {"rows": len(rows), "cols": len(cols), "k": (k_ours, k_theirs),
                                   "score": best_key[0], "pick_score": None if pick_key is None else pick_key[0],
                                   "ms": round(1000 * (time.monotonic() - self.t0), 1), "choice": out,
                                   "mix_mode": mode, "mix": mix, "alpha": w_worst, "foe_weight": fw_mode}
        return out

    def _cap(self) -> float:
        """TIME_CAP (read per call: tests shrink it), or the tighter cap the caller passed (decision deadline)."""
        cap = getattr(self, "time_cap", None)          # (tests build bare _Search objects)
        return TIME_CAP if cap is None else min(TIME_CAP, cap)

    def _pick_row(self, rows):
        want = [self.pick.get(f"slot_{s['slot']}") for s in self.tmpl.get("slots", [])]
        return next((r for r in rows if [r[0].src["cand"], r[1].src["cand"]] == want), None)

    def _repeat_protect(self, i, cand) -> bool:
        """cand is a Protect by our slot-i mon that Protected last turn (the rows never build it)."""
        me = self.mons.get(f"me{i}")
        return (me is not None and (cand or {}).get("type") == "move"
                and to_id(cand.get("move_id", "")) in PROTECT_MOVES
                and self.pol.last_protect.get(me.species) == self.turn - 1)

    def _pick_kinds(self, slots, rows_per_slot) -> list[str]:
        """Per slot of the exact pick: "ok" (a row action stands for it), "repeat" (a repeat Protect, which the
        rows drop on purpose so the lookahead may replace it) or "keep" (an action the rows cannot stand for: a
        voluntary switch, or anything else missing from the rows; and, as before, an attack aimed at our own
        ally: the exact scorer only picks one deliberately, e.g. a Pollen Puff heal)."""
        kinds = []
        for s, slot in enumerate(slots):
            want = self.pick.get(f"slot_{slot['slot']}") or {}
            a = next((a for a in rows_per_slot[s] if a.src is not None and a.src.get("cand") == want), None)
            if a is not None:
                kinds.append("keep" if a.at_ally or (a.mid and a.kind == "none") else "ok")
            else:
                kinds.append("repeat" if self._repeat_protect(slot["slot"], want) else "keep")
        return kinds

    def _repick_repeat(self, slots, kinds) -> dict | None:
        """The exact pick with its kept slot fixed and its repeat-Protect slot re-picked by the exact pair score
        (the rule that made the pick, minus the Protect chain); None when no pair is left."""
        per_slot = self.ctx.get("per_slot") or []
        choice = []
        for s, slot in enumerate(slots):
            i, cands = per_slot[s]
            if kinds[s] == "repeat":
                choice.append([c for c in cands if not self._repeat_protect(i, c.get("cand"))])
            else:
                want = self.pick.get(f"slot_{slot['slot']}")
                choice.append([c for c in cands if c.get("cand") == want][:1])
        best, best_v = None, None
        for c0 in choice[0]:
            for c1 in choice[1]:
                v = self.pol._pair_value(self.ctx, c0, c1)
                if v is not None and (best_v is None or v > best_v):
                    best, best_v = (c0, c1), v
        if best is None:
            return None
        return {f"slot_{slots[s]['slot']}": best[s]["cand"] for s in range(2)}

    # ---- optional Nash mixing -------------------------------------------------------------------------
    def _mix(self, mode, rows, cols, matrix, keys, pick_row):
        """(row index, diagnostics) from our equilibrium strategy on the top-MIX_K subgame, or None to keep
        the robust row. Only rows already built from the policy's legal candidates can come out."""
        deadline = min(self.t0 + self._cap(), time.monotonic() + MIX_TIME)
        if time.monotonic() >= deadline or not rows or not cols:
            return None
        ok = [n for n in range(len(rows)) if keys[n][1] > -1e17 and all(math.isfinite(v) for v in matrix[n])
              and not any(a.at_ally or (a.mid and a.kind == "none") for a in rows[n])]   # no attack on our own ally
        ri = sorted(ok, key=lambda n: keys[n], reverse=True)[:MIX_K]
        p = next((n for n, r in enumerate(rows) if r is pick_row), None)
        if p is not None and p in ok and p not in ri:
            ri = ri[:MIX_K - 1] + [p]
        if not ri:
            return None
        # columns: each kept row's worst reply (best rows first), then the replies that hurt the kept rows most
        ci: list[int] = []
        for n in ri:
            j = min(range(len(cols)), key=lambda j: (matrix[n][j], j))
            if j not in ci:
                ci.append(j)
        ci = ci[:MIX_K]
        rest = sorted((j for j in range(len(cols)) if j not in ci),
                      key=lambda j: (sum(matrix[n][j] for n in ri), j))
        ci += rest[:MIX_K - len(ci)]
        sub = [[matrix[n][j] for j in ci] for n in ri]
        lo = max(min(r) for r in sub)                      # pure maximin
        hi = min(max(sub[k][c] for k in range(len(ri))) for c in range(len(ci)))   # pure minimax
        if hi - lo <= 1e-9:            # saddle point: every maximin row is optimal; the robust key picks one
            kbest = max((k for k in range(len(ri)) if min(sub[k]) >= lo - 1e-9), key=lambda k: keys[ri[k]])
            x = [1.0 if k == kbest else 0.0 for k in range(len(ri))]
            gap, pure = 0.0, True
        else:
            res = solve_rm_plus(sub, MIX_ITERS, deadline)
            if res is None:
                return None
            x, _, gap = res
            pure = False
        if len(x) != len(ri) or not all(math.isfinite(v) and v >= 0 for v in x) or sum(x) <= 0:
            return None
        if mode == "argmax":
            k = max(range(len(ri)), key=lambda k: (round(x[k], 3), keys[ri[k]]))
        else:
            sup = [k for k in range(len(ri)) if x[k] >= MIX_MIN_P] or [max(range(len(ri)), key=lambda k: x[k])]
            draw = random.Random(self._seed()).random() * sum(x[k] for k in sup)
            k, acc = sup[-1], 0.0
            for kk in sup:
                acc += x[kk]
                if draw < acc:
                    k = kk
                    break
        if pure:
            STATS["mix_pure"] += 1
        info = {"rows": len(ri), "cols": len(ci), "gap": round(gap, 6), "pure": pure,
                "p": sorted(((round(x[kk], 3), ri[kk]) for kk in range(len(ri)) if x[kk] >= 0.01), reverse=True),
                "row": ri[k]}
        return ri[k], info

    def _seed(self) -> int:
        board = sorted((k, m.species, round(m.hp, 3)) for k, m in (getattr(self, "mons", None) or {}).items() if m)
        blob = json.dumps([getattr(self, "seed_key", None), getattr(self, "turn", 0), board], default=str)
        return zlib.crc32(blob.encode())

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
        real_ally_hits = ally_fix()
        out = []
        for c in cands:
            cand = c.get("cand") or {}
            if cand.get("type") == "pass":
                out.append(_Act(None, "me", None, None, 0.0, c))
                continue
            if cand.get("type") != "move" or me is None:
                continue                                   # voluntary switches stay with the exact scorer
            mid = to_id(cand.get("move_id", ""))
            if self._repeat_protect(i, cand):
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
                a.at_ally = True                           # _simulate plays it into the partner (damage / heal)
                if not real_ally_hits or tgt[1] == i:      # old free pass (A/B) / a self-target the rows can't play
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
                # our own partner as the target: no redirection, and no retargeting when it is gone (the move fails)
                t = self._retarget(t, a.tgt[0], down) if a.tgt[0] == foe_side else \
                    (t if t in hp and t not in down else None)
                targets = [t] if t else []
            else:
                targets = []
            if not targets:
                continue
            if a.prio > 0 and any(t.startswith(foe_side) for t in targets) and self._priority_blocked(foe_side, down):
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
                if a.at_ally and a.mid in ALLY_HEAL:         # Pollen Puff on the partner: a heal, no damage
                    h = hp[t]
                    if h > 0 and not (d.ability == "bulletproof" and a.move.get("flags", {}).get("bullet")):
                        gain = min(ALLY_HEAL[a.mid], 1.0 - h)
                        hp[t] = h + gain
                        dealt[t] -= gain
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
