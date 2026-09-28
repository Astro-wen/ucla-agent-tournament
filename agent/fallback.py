"""Safe legal actions for any game, phase or action template: the answer of last resort.

Used when a policy crashed, ran out of time, or meets a game / phase / payload it does not know. It reads nothing but
the state's own legal actions, never raises, and is deterministic (same state -> same answer):
  * a discrete action (OpenSpiel-style ids, Pokémon singles `move:0`, Werewolf seats): the first legal action whose
    id / label does not give the game up (resign, forfeit, surrender, concede, quit);
  * a structured template the server cannot take back verbatim is filled in with its first legal choices:
    `doubles_turn` (per-slot `options`: first option of each slot, never the same switch-in twice), `select_lineup`
    (the first bring_count of the roster, the first lead_count of those as leads), `submit_team` (the platform's own
    known-legal fallback team, agent/pokemon/policy.DEFAULT_OU_TEAM);
  * any other structured action is sent as the concrete action the server listed.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("agent.fallback")

_GIVE_UP = re.compile(r"resign|forfeit|surrender|concede|\bquit\b", re.I)
# keys that mark an action template (something to fill in) rather than a concrete action
_TEMPLATE_KEYS = ("slots", "roster", "instructions", "bring_count", "lead_count", "options", "choices")


def _sid(x) -> str:
    return "".join(ch for ch in str(x or "").lower() if ch.isalnum())


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


def doubles_from_template(tmpl: dict) -> dict:
    """A concrete doubles_turn from a per-slot options template: each slot's first option (a second slot never
    switches in the Pokémon the first one already chose), `pass` when a slot has nothing usable."""
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
                targets = opt.get("targets") if isinstance(opt.get("targets"), list) and opt["targets"] else [0]
                # a foe (1 / 2) when the move may target one: the first listed target can be our own partner
                target = next((x for x in targets if isinstance(x, int) and not isinstance(x, bool) and x > 0),
                              targets[0])
                pick[key] = {"type": "move", "move_id": opt["move_id"], "target": target}
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
    return {"type": tmpl.get("type") or "doubles_turn", **pick}


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
