"""Safe legal actions for any game, phase or action template: the answer of last resort.

Used when a policy crashed, ran out of time, or meets a game / phase / payload it does not know. It reads nothing but
the state's own legal actions, never raises, and is deterministic (same state -> same answer):
  * a discrete action (OpenSpiel-style ids, Pokémon singles `move:0`, Werewolf seats): the first legal action whose
    id / label does not give the game up (resign, forfeit, surrender, concede, quit);
  * a structured template the server cannot take back verbatim is filled in with its first legal choices:
    `doubles_turn` (per-slot `options`: first option of each slot, never the same switch-in twice, a switch-in
    instead of `pass` while one is still free), `select_lineup` (the first bring_count of the roster, the first
    lead_count of those as leads), `submit_team` (the platform's own known-legal fallback team,
    agent/pokemon/policy.DEFAULT_OU_TEAM);
  * any other structured action is sent as the concrete action the server listed.

Doubles turns are also checked as a pair (`doubles_problems`, Showdown's rule, which the platform enforces): the two
slots never switch in the same Pokemon, and a slot passes only when no switch-in its options offer is left after the
other slot's choice (both actives fainted with one Pokemon left: it comes in on one slot, the other passes; both
passing is rejected, and the runner then dies on the INVALID_ACTION). `repair_doubles` fixes a pair that breaks it.
"""
from __future__ import annotations

import itertools
import logging
import re

log = logging.getLogger("agent.fallback")

_GIVE_UP = re.compile(r"resign|forfeit|surrender|concede|\bquit\b", re.I)
# keys that mark an action template (something to fill in) rather than a concrete action
_TEMPLATE_KEYS = ("slots", "roster", "instructions", "bring_count", "lead_count", "options", "choices")


def _sid(x) -> str:
    return "".join(ch for ch in str(x or "").lower() if ch.isalnum())


def _tid(x) -> str:
    """Showdown ids, as agent/pokemon/dex.to_id."""
    return re.sub(r"[^a-z0-9]", "", str(x if x is not None else "").lower())


def legal_list(state) -> list:
    """The state's legal actions as a list of LegalAction-like objects (never raises)."""
    try:
        la = list(getattr(state, "legal_actions", None) or [])
    except Exception:
        la = []
    return la


def _input(a) -> dict:
    inp = getattr(a, "input", None)
    if inp is None and isinstance(a, dict):
        inp = a.get("input")
    return inp if isinstance(inp, dict) else {}


def _aid(a) -> str:
    aid = getattr(a, "action_id", None)
    if aid is None and isinstance(a, dict):
        aid = a.get("action_id")
    return str(aid if aid is not None else "")


def _label(a) -> str:
    lab = getattr(a, "label", None)
    if lab is None and isinstance(a, dict):
        lab = a.get("label")
    return str(lab or "")


def is_template(action) -> bool:
    return isinstance(action, dict) and any(k in action for k in _TEMPLATE_KEYS)


def _move_target(opt: dict):
    """A foe (1 / 2) when the move may target one: the first listed target can be our own partner."""
    targets = opt.get("targets") if isinstance(opt.get("targets"), list) and opt["targets"] else [0]
    return next((x for x in targets if isinstance(x, int) and not isinstance(x, bool) and x > 0), targets[0])


def doubles_from_template(tmpl: dict) -> dict:
    """A concrete doubles_turn from a per-slot options template: each slot's first option (a second slot never
    switches in the Pokémon the first one already chose), `pass` when a slot has nothing usable; a `pass` that the
    server would reject together with the other slot's choice becomes a free switch-in (`repair_doubles`)."""
    pick, used = {}, set()
    for n, slot in enumerate(tmpl.get("slots") or []):
        if not isinstance(slot, dict):
            continue
        key = f"slot_{slot.get('slot', n)}"
        pick[key] = {"type": "pass"}
        for opt in slot.get("options") or []:
            if not isinstance(opt, dict):
                continue
            t = opt.get("type")
            if t == "move" and opt.get("move_id") is not None:
                pick[key] = {"type": "move", "move_id": opt["move_id"], "target": _move_target(opt)}
                break
            if t == "switch" and opt.get("species") is not None:
                if _sid(opt["species"]) in used:
                    continue
                used.add(_sid(opt["species"]))
                pick[key] = {"type": "switch", "species": opt["species"]}
                break
            if t == "pass":
                pick[key] = {"type": "pass"}
                break
    out = {"type": tmpl.get("type") or "doubles_turn", **pick}
    fixed, notes = repair_doubles(out, tmpl)
    if notes:
        log.warning("template fill repaired (%s): %s -> %s", "; ".join(notes), out, fixed)
    return fixed


# ---- doubles turns as a pair (Showdown's rule, enforced by the platform) ----------------------------------------
def _slots(tmpl) -> list[tuple[str, list[dict]]]:
    """(answer key, options) of every slot of a doubles_turn template."""
    out = []
    slots = tmpl.get("slots") if isinstance(tmpl, dict) else None
    for n, slot in enumerate(slots if isinstance(slots, list) else []):
        if isinstance(slot, dict):
            out.append((f"slot_{slot.get('slot', n)}", [o for o in slot.get("options") or [] if isinstance(o, dict)]))
    return out


def _switch_ins(opts: list[dict]) -> list[str]:
    return [_tid(o["species"]) for o in opts if o.get("type") == "switch" and o.get("species") is not None]


def _is(choice, kind: str) -> bool:
    return isinstance(choice, dict) and choice.get("type") == kind


def _slot_ok(choice, opts: list[dict]) -> bool:
    """One slot's choice is one of its options (a move with one of that option's targets); agent.doubles_ok's rule."""
    if not isinstance(choice, dict):
        return False
    t = choice.get("type")
    if t == "move":                  # an option without a move_id matches no move (not even one without a move_id)
        return any(o.get("type") == "move" and o.get("move_id") is not None
                   and _tid(o.get("move_id")) == _tid(choice.get("move_id"))
                   and choice.get("target") in (o.get("targets") or [0]) for o in opts)
    if t == "switch":
        return _tid(choice.get("species")) in _switch_ins(opts)
    if t == "pass":
        return any(o.get("type") == "pass" for o in opts)
    return False


def joint_problems(tmpl: dict, action: dict) -> list[str]:
    """Why two slot choices, each fine on its own, are rejected together (empty: they are not). Showdown's doubles
    rule: never the same switch-in on both slots, and a slot may pass only when no switch-in its options offer is left
    after the other slot's choice (the number of passes is the minimum the bench allows). Slots that only offer `pass`
    (nothing left, a Commander Tatsugiri) are never a problem; a move slot is unaffected. Never raises."""
    try:
        slots = _slots(tmpl)
        picks = [action.get(k) if isinstance(action, dict) else None for k, _ in slots]
        ins = [_tid(c.get("species")) if _is(c, "switch") else None for c in picks]
        out = []
        taken = [s for s in ins if s]
        if len(taken) != len(set(taken)):
            out.append("both slots switch in the same Pokemon")
        for n, ((key, opts), c) in enumerate(zip(slots, picks)):
            if not _is(c, "pass"):
                continue
            others = {s for m, s in enumerate(ins) if m != n and s}
            free = [s for s in _switch_ins(opts) if s not in others]
            if free:
                out.append(f"{key} passes while {free[0]} is free to switch in")
        return out
    except Exception as e:                                   # noqa: BLE001
        return [f"joint check failed: {e!r}"]


def doubles_problems(tmpl: dict, action) -> list[str]:
    """Why `action` is not a legal answer to the doubles_turn template `tmpl`: its `type`, per slot and as a pair
    (empty: legal). Never raises."""
    try:
        if not isinstance(action, dict):
            return [f"not a dict: {type(action).__name__}"]
        slots = _slots(tmpl)
        if not slots:
            return ["no slots in the template"]
        want = tmpl.get("type") or "doubles_turn"
        out = [] if action.get("type") == want else [f"type {action.get('type')!r} is not {want!r}"]
        out += [f"{k}: {action.get(k)} is not one of its options" for k, opts in slots
                if not _slot_ok(action.get(k), opts)]
        return out + joint_problems(tmpl, action)
    except Exception as e:                                   # noqa: BLE001
        return [f"check failed: {e!r}"]


def repair_doubles(action, tmpl: dict, prefer=None) -> tuple[dict, list[str]]:
    """The smallest change that makes `action` a legal answer to the doubles_turn template `tmpl`, and what was changed
    (empty list: it was legal and comes back as it is). Pure (never mutates its inputs) and never raises.

    Every legal part is kept. A slot is changed only when its choice is not one of its options (a legal move with a
    bad target keeps the move, at a foe), repeats the other slot's switch-in (the later slot gives way), or passes while
    a switch-in is still free for it. A slot that must change gets the free switch-in ranked first in `prefer` (species,
    best first: the policy's own ranking) if any, else the first free one in its options, else `pass` when offered,
    else its first move, else `pass` (never the switch-in the other slot takes). When the template allows no legal
    pair at all the result is that best effort, with a "still illegal" note. A repaired answer carries exactly what the
    platform asks for, `type` and one choice per slot: nothing else of the input rides along (a template-shaped answer's
    `slots` / `instructions`, a stale select_lineup's `bring`)."""
    try:
        slots = _slots(tmpl)
        if not slots or not doubles_problems(tmpl, action):
            return action, []
        out = dict(action) if isinstance(action, dict) else {}
        notes = []
        want = (tmpl.get("type") if isinstance(tmpl, dict) else None) or "doubles_turn"
        if out.get("type") != want:
            notes.append(f"type {out.get('type')!r} -> {want!r}")
            out["type"] = want
        rank = {}
        for n, p in enumerate(prefer or []):
            rank.setdefault(_tid(p), n)

        def free_ins(key: str, opts: list[dict]) -> list[dict]:
            others = {_tid(out[k]["species"]) for k, _ in slots if k != key and _is(out.get(k), "switch")}
            free = [o for o in opts if o.get("type") == "switch" and o.get("species") is not None
                    and _tid(o["species"]) not in others]
            return sorted(free, key=lambda o: rank.get(_tid(o["species"]), len(rank)))   # stable: option order

        def refill(key: str, opts: list[dict]) -> dict:
            free = free_ins(key, opts)
            if free:
                return {"type": "switch", "species": free[0]["species"]}
            if any(o.get("type") == "pass" for o in opts):
                return {"type": "pass"}
            mv = next((o for o in opts if o.get("type") == "move" and o.get("move_id") is not None), None)
            if mv is not None:
                return {"type": "move", "move_id": mv["move_id"], "target": _move_target(mv)}
            return {"type": "pass"}          # nothing usable (never a switch-in the other slot takes), as the fill does

        # 1. choices that are not one of their slot's options, and the second of two identical switch-ins
        seen = set()
        for key, opts in slots:
            c = out.get(key)
            if _slot_ok(c, opts):
                if _is(c, "switch"):
                    sp = _tid(c.get("species"))
                    if sp in seen:
                        out[key] = None
                        notes.append(f"{key}: {sp} already switches in on the other slot")
                    seen.add(sp)
                continue
            mv = next((o for o in opts if _is(c, "move") and o.get("type") == "move" and o.get("move_id") is not None
                       and _tid(o.get("move_id")) == _tid(c.get("move_id"))), None)
            if mv is not None:                               # the move is offered, its target is not
                out[key] = {**c, "move_id": mv["move_id"], "target": _move_target(mv)}
            else:
                out[key] = None
            notes.append(f"{key}: {c} is not one of its options")
        for key, opts in slots:
            if out.get(key) is None:
                out[key] = refill(key, opts)
        # 2. a pass while a switch-in is still free for that slot (slots in order: the first one takes it)
        for key, opts in slots:
            if _is(out.get(key), "pass"):
                free = free_ins(key, opts)
                if free:
                    out[key] = {"type": "switch", "species": free[0]["species"]}
                    notes.append(f"{key}: pass -> switch {_tid(free[0]['species'])} (a switch-in was still free)")
        # 3. slot by slot is enough for the platform's templates (every forced slot lists the same bench); for an odd
        # template (slots offering different switch-ins) the nearest legal pair when it keeps more of the answer
        left = doubles_problems(tmpl, out)
        orig = [(action.get(k) if isinstance(action, dict) else None) for k, _ in slots]
        credit = sum(1 for (_, opts), o in zip(slots, orig) if _slot_ok(o, opts) or (_is(o, "move") and any(
            x.get("type") == "move" and _tid(x.get("move_id")) == _tid(o.get("move_id")) for x in opts)))
        if all(_slot_ok(o, opts) for (_, opts), o in zip(slots, orig)):
            credit -= 1                                      # only the pair was wrong: one choice has to change
        if not left and _kept_score(orig, out, slots) >= credit:
            return _only_pair(out, slots), notes             # nothing legal keeps more
        best = _nearest_legal(orig, slots, tmpl, rank)
        if best is not None and (left or _kept_score(orig, best, slots) > _kept_score(orig, out, slots)):
            out.update(best)
            notes.append("nearest legal pair")
        elif left:
            notes.append("still illegal: " + "; ".join(left))
        return _only_pair(out, slots), notes
    except Exception as e:                                   # noqa: BLE001
        return action, [f"repair failed: {e!r}"]


def _only_pair(out: dict, slots) -> dict:
    """A repaired doubles_turn as the platform asks for it: `type` and one choice per slot, nothing else."""
    return {"type": out.get("type"), **{k: out.get(k) for k, _ in slots}}


def _key(c) -> tuple:
    """A slot choice, comparable across spellings."""
    if not isinstance(c, dict):
        return (repr(c),)
    t = c.get("type")
    if t == "move":
        return ("move", _tid(c.get("move_id")), c.get("target"))
    if t == "switch":
        return ("switch", _tid(c.get("species")))
    return (str(t),)


def _choices(opts: list[dict]) -> list[dict]:
    """Every concrete choice a slot's options allow (a move at each of its targets)."""
    out = []
    for o in opts:
        t = o.get("type")
        if t == "move" and o.get("move_id") is not None:
            targets = o.get("targets") if isinstance(o.get("targets"), list) and o["targets"] else [0]
            out += [{"type": "move", "move_id": o["move_id"], "target": x} for x in targets]
        elif t == "switch" and o.get("species") is not None:
            out.append({"type": "switch", "species": o["species"]})
        elif t == "pass":
            out.append({"type": "pass"})
    return out


def _kept_score(orig: list, cand: dict, slots) -> int:
    """How much of the original slot choices `cand` keeps: a legal choice as it is, an offered move (at a bad target)
    as the same move."""
    n = 0
    for (k, opts), o in zip(slots, orig):
        c = cand.get(k)
        if _slot_ok(o, opts):
            n += _key(c) == _key(o)
        elif _is(o, "move") and _is(c, "move"):
            n += _tid(c.get("move_id")) == _tid(o.get("move_id"))
    return n


def _nearest_legal(orig: list, slots, tmpl, rank: dict) -> dict | None:
    """The legal pair that keeps most of the original slot choices (`_kept_score`; then fewer passes, better-ranked
    switch-ins, option order), as {slot key: choice}; None when the template allows no legal pair at all. Pairs that
    keep one legal choice as it is are tried first, all pairs only when none of those is legal."""
    per = [list(enumerate(_choices(opts))) for _, opts in slots]

    def pick(combos):
        best, best_key = None, None
        for combo in combos:
            cand = {k: c for (k, _), (_, c) in zip(slots, combo)}
            if joint_problems(tmpl, cand) or not all(_slot_ok(c, opts) for (_, opts), (_, c) in zip(slots, combo)):
                continue
            passes = sum(1 for _, c in combo if _is(c, "pass"))
            ranks = sum(rank.get(_tid(c.get("species")), len(rank)) for _, c in combo if _is(c, "switch"))
            key = (-_kept_score(orig, cand, slots), passes, ranks, [n for n, _ in combo])
            if best_key is None or key < best_key:
                best, best_key = cand, key
        return best

    keep_one = [itertools.product(*[p if m != k else [(-1, o)] for m, p in enumerate(per)])
                for k, ((_, opts), o) in enumerate(zip(slots, orig)) if _slot_ok(o, opts)]
    return pick(itertools.chain(*keep_one)) or pick(itertools.product(*per))


def lineup_from_template(tmpl: dict, raw: dict | None) -> dict:
    """A concrete select_lineup: the first bring_count Pokémon of the roster, the first lead_count of them lead."""
    roster = [r for r in (tmpl.get("roster") or []) if r]
    if not roster:
        obs = (raw or {}).get("observation") if isinstance(raw, dict) else None
        mine = obs.get("your_roster") if isinstance(obs, dict) else None
        roster = [p.get("species") for p in (mine or []) if isinstance(p, dict) and p.get("species")]
    roster = [r if isinstance(r, str) else (r.get("species") if isinstance(r, dict) else str(r)) for r in roster]
    roster = list(dict.fromkeys(r for r in roster if r))
    try:
        n_bring = int(tmpl.get("bring_count") or 4)
        n_lead = int(tmpl.get("lead_count") or 2)
    except (TypeError, ValueError):
        n_bring, n_lead = 4, 2
    bring = roster[:max(1, n_bring)]
    return {"type": tmpl.get("type") or "select_lineup", "bring": bring, "leads": bring[:max(1, n_lead)]}


def concrete(a, raw: dict | None = None):
    """What to send for legal action `a`: the LegalAction itself when the server can resolve it by id, a filled-in
    dict when its input is a template. None when `a` is a template we cannot fill."""
    act = _input(a).get("action")
    if not isinstance(act, dict):
        return a
    t = str(act.get("type") or "")
    if t == "doubles_turn" or isinstance(act.get("slots"), list):
        return doubles_from_template(act)
    if t == "select_lineup" or "roster" in act or "bring_count" in act:
        return lineup_from_template(act, raw)
    if t == "submit_team":
        from .pokemon.policy import DEFAULT_OU_TEAM
        return {"type": "submit_team", "team": DEFAULT_OU_TEAM}
    if is_template(act):
        return None
    return a


def safe_action(state, raw: dict | None = None):
    """A legal answer for any state (see the module docstring). Never raises."""
    try:
        raw = raw if isinstance(raw, dict) else (getattr(state, "raw", None) or {})
        la = legal_list(state)
        if not la and isinstance(raw, dict):
            acts = (raw.get("legal_actions") or {}).get("actions") if isinstance(raw.get("legal_actions"), dict) else None
            la = [x for x in (acts or []) if isinstance(x, dict)]
        keep = [a for a in la if not (_GIVE_UP.search(_aid(a)) or _GIVE_UP.search(_label(a)))] or la
        for a in keep:
            out = concrete(a, raw)
            if out is None:
                continue
            if isinstance(out, dict) and "action_id" in out and "input" in out:     # a raw legal-action dict
                act = _input(out).get("action")
                out = act if isinstance(act, dict) else _aid(out)
            return out
        if keep:
            return keep[0] if not isinstance(keep[0], dict) else _aid(keep[0])
    except Exception:
        log.exception("safe_action failed")
        try:
            la = legal_list(state)
            if la:
                return la[0]
        except Exception:
            pass
    log.error("no legal action to answer with; sending a pass")
    return {"type": "pass"}
