"""Public move reasoning: the `reasoning_summary` the platform shows spectators next to each move.

Everything written here must be treated as read by the opponent, live: the spectate feed is public, needs no login
and refreshes every second (docs/competition_brief.md 4.4), and a draft pick's reason is copied straight into the
other seat's own observation (`picks[].public_reason`, docs/platform/pokemon.md). So the texts are built from public
information only, from fixed templates, and never say anything about a choice the other side has not seen yet:

* Werewolf: nothing at night (only wolves and the seer act at night, so even a bland line would say "I am one of
  them"); for a day vote, the same line for every role and every target (votes are hidden until the tally).
* Pokemon draft: picks are sequential and public the moment they are made, so the line names the card just taken
  and states a type-chart fact about it against the opponent's already-public picks. Never the card's item,
  ability or moves (hidden once the battle starts) and never a card still in the pool.
* Pokemon team preview and battle turns are simultaneous. In the default "safe" mode the line is the same whatever
  we chose (lineup, move, target, Protect, switch): the turn number and the public knockout count. "detail" mode
  (ARENA_REASONING=detail) names the action just chosen (leads at preview, move/target/switch in battle), never
  anything else; only use it once the organizers confirm spectators see a move after both sides have chosen.

ARENA_REASONING=off (or 0/false/no/none) is the kill switch: no reasoning is attached at all, and the agent returns
exactly the decisions it returned before this module existed. Every text goes through `sanitize` (printable ASCII
from a small whitelist, whitespace collapsed, at most REASONING_CAP characters, under the ~280 the platform's
dashboard shows) and `leaks` (role words, seat names and future-intent phrases); a text that fails is dropped, never
sent. Building a line never raises: any error means the move goes out without reasoning.
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata

from altruagent import LegalAction, WithReasoning

from .pokemon import dex
from .pokemon.dex import to_id
from .pokemon.typechart import effectiveness

log = logging.getLogger("agent.reasoning")

ENV = "ARENA_REASONING"
OFF, SAFE, DETAIL = "off", "safe", "detail"
REASONING_CAP = 200

_OFF_VALUES = {"0", "off", "false", "no", "none", "disable", "disabled"}
_DETAIL_VALUES = {"detail", "detailed", "verbose", "full"}
_DISALLOWED = re.compile(r"[^A-Za-z0-9 .,:;!?'()/+%-]")
# Future intent, in any game. "Will-O-Wisp" is a move, so "will" followed by a hyphen is allowed.
_FUTURE = re.compile(r"(?i)\b(next|plan|plans|planned|planning|going to|gonna|intend|intends|later|afterwards|"
                     r"then|tomorrow|tonight)\b|\bwill\b(?!-)")
# Werewolf private information: roles, checks, teammates, night actions, seats.
_WEREWOLF_PRIVATE = re.compile(r"(?i)wolf|wolves|\bseer|villager|investigat|\bcheck|\bclear|\bteam|\bally|\ballies|"
                               r"\bpartner|\bnight|\bkill|\brole|\bplayer|\bseat|\bsuspect|\blynch|\babstain")


def mode_from_env(env=None) -> str:
    """off / safe / detail from ARENA_REASONING (unset or unknown values: safe)."""
    value = str((os.environ if env is None else env).get(ENV, "") or "").strip().lower()
    if value in _OFF_VALUES:
        return OFF
    if value in _DETAIL_VALUES:
        return DETAIL
    return SAFE


def unwrap(decision):
    """The move inside a WithReasoning (local simulators, replays and tests look at the move itself)."""
    return decision.action if isinstance(decision, WithReasoning) else decision


def sanitize(text, cap: int = REASONING_CAP) -> str | None:
    """Printable ASCII from a small whitelist, one line, at most `cap` characters (cut at a word), or None."""
    if not isinstance(text, str):
        return None
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    t = " ".join(_DISALLOWED.sub(" ", t).split())
    if len(t) > cap:
        cut = t[: cap - 3]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        t = cut.rstrip(" ,;:-/(") + "..."
    return t or None


def leaks(text: str, game: str) -> bool:
    """True if the text says something no public line may say (checked after the templates, as a backstop)."""
    if _FUTURE.search(text):
        return True
    return game == "werewolf" and bool(_WEREWOLF_PRIVATE.search(text))


def build(raw: dict, game: str, decision, mode: str = SAFE) -> str | None:
    """The raw (unsanitized) public line for `decision` taken in state `raw`, or None for no reasoning."""
    if not isinstance(raw, dict):
        return None
    if game == "werewolf":
        return _werewolf(raw)
    if game.startswith("pokemon_"):
        phase = raw.get("phase")
        obs = raw.get("observation") if isinstance(raw.get("observation"), dict) else {}
        if phase == "draft":
            return _draft(raw, obs, decision)
        if phase == "teambuild":
            return "Team submitted."
        if phase == "team_preview":
            return _preview(obs, decision) if mode == DETAIL else \
                "Team preview: lineup locked in. The leads are revealed when the battle starts."
        if phase == "moving":
            return _battle(obs, decision, mode)
    return None                        # unknown game or phase: say nothing


def attach(decision, raw: dict, game: str, mode: str = SAFE):
    """(what to return to the runner, the public line or None). Never raises; with no line, `decision` unchanged."""
    if mode == OFF or decision is None or isinstance(decision, WithReasoning) \
            or not isinstance(decision, (LegalAction, dict, str, int)) or isinstance(decision, bool):
        return decision, None
    try:
        text = sanitize(build(raw, game or "", decision, mode))
    except Exception:                                     # noqa: BLE001 - reasoning must never cost a move
        log.debug("reasoning build failed", exc_info=True)
        return decision, None
    if text is None:
        return decision, None
    if leaks(text, game or ""):
        log.warning("public reasoning dropped by the leak guard: %r", text)
        return decision, None
    return WithReasoning(decision, text), text


# ---------------------------------------------------------------- Werewolf
def _werewolf(raw: dict) -> str | None:
    gs = raw.get("game_state") if isinstance(raw.get("game_state"), dict) else {}
    if gs.get("phase") != "day_vote":
        return None                    # night (wolves / seer only) or anything unknown: no line at all
    day = gs.get("day")
    head = f"Day {day} vote cast." if isinstance(day, int) and not isinstance(day, bool) and 0 < day < 100 \
        else "Vote cast."
    return head + " Ballots stay hidden until everyone has voted."


# ---------------------------------------------------------------- Pokemon helpers
def _action_of(decision) -> dict:
    """The structured action inside any decision shape our Contestant returns."""
    if isinstance(decision, LegalAction):
        act = (decision.input or {}).get("action")
        if isinstance(act, dict):
            return act
        aid = decision.action_id or ""
        return {"type": "draft_pick", "card_id": aid.split(":", 1)[1]} if aid.startswith("draft_pick:") else {}
    if isinstance(decision, dict):
        return decision
    if isinstance(decision, str) and decision.startswith("draft_pick:"):
        return {"type": "draft_pick", "card_id": decision.split(":", 1)[1]}
    return {}


def _types_text(types) -> str:
    return "/".join(str(t).capitalize() for t in types if t)


def _join(names: list[str]) -> str:
    names = [n for n in names if n]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _display(species: str, *views) -> str:
    """A species' display name: from the payload's own views, else the dex, else the id."""
    sid = to_id(species)
    for view in views:
        for p in view:
            if isinstance(p, dict) and to_id(p.get("species", "")) == sid and p.get("name"):
                return str(p["name"])
    return str(dex.species(sid).get("name") or species)


def _mons(x) -> list:
    if isinstance(x, dict):
        return [p for p in x.values() if isinstance(p, dict)]
    if isinstance(x, list):
        return [p for p in x if isinstance(p, dict)]
    return []


# ---------------------------------------------------------------- Pokemon draft (sequential, public)
def _draft(raw: dict, obs: dict, decision) -> str | None:
    cid = _action_of(decision).get("card_id")
    card = next((c for c in obs.get("available_cards") or [] if isinstance(c, dict) and c.get("card_id") == cid), None)
    if not cid or card is None or not card.get("species"):
        return None
    name = str(card["species"])
    mine = dex.types_of(name)
    n = obs.get("pick_number")
    head = f"Draft pick {n}: {name}" if isinstance(n, int) and not isinstance(n, bool) and 0 < n < 100 \
        else f"Draft: {name}"
    head += f" ({_types_text(mine)})." if mine else "."
    me = (raw.get("current_actor") or {}).get("agent_id") or obs.get("current_seat")
    rosters = obs.get("rosters") if isinstance(obs.get("rosters"), dict) else {}
    opp = [str(r.get("species")) for seat, ro in rosters.items() if seat != me
           for r in (ro or []) if isinstance(r, dict) and r.get("species")]
    fact = _draft_fact(mine, opp) if mine else None
    return f"{head} {fact}" if fact else head


def _draft_fact(mine: list[str], opp: list[str]) -> str | None:
    """One type-chart fact about our new card against the opponent's picks so far (public on both sides)."""
    typed = [(o, dex.types_of(o)) for o in opp]
    typed = [(o, ts) for o, ts in typed if ts]
    if not typed:
        return f"It adds {_types_text(mine)} typing to our team."
    hits, strong = [], []
    for o, ts in typed:
        se = [t for t in mine if effectiveness(t, ts) >= 2]
        if se:
            hits.append(o)
            strong += [t for t in se if t not in strong]
    if hits:
        return f"Its {_types_text(strong)} typing is super effective against their {_join(hits[:2])}."
    immune = [(o, s) for o, ts in typed for s in ts if effectiveness(s, mine) == 0]
    if immune:
        o, s = immune[0]
        return f"It is immune to the {s.capitalize()} type of their {o}."
    resist = [(o, s) for o, ts in typed for s in ts if effectiveness(s, mine) <= 0.5]
    if resist:
        o, s = resist[0]
        return f"It resists the {s.capitalize()} type of their {o}."
    return f"It adds {_types_text(mine)} typing to our team."


# ---------------------------------------------------------------- Pokemon team preview (simultaneous)
def _preview(obs: dict, decision) -> str | None:
    """detail mode only: the two leads (seen by everyone when the battle starts), never the two in the back."""
    act = _action_of(decision)
    leads = [x for x in act.get("leads") or [] if isinstance(x, str)]
    if not leads:
        return None
    fmt = to_id(obs.get("battle_format") or "")
    if fmt and "vgc" not in fmt and "doubles" not in fmt:
        leads = leads[:1]                                   # singles: one lead
    roster = _mons(obs.get("your_roster"))
    return "Team preview: leading with " + _join([_display(x, roster) for x in leads]) + "."


# ---------------------------------------------------------------- Pokemon battle (simultaneous)
def _battle(obs: dict, decision, mode: str) -> str | None:
    turn = obs.get("turn")
    head = f"Turn {turn}:" if isinstance(turn, int) and not isinstance(turn, bool) and 0 <= turn < 1000 else "Turn:"
    if mode == DETAIL:
        acted = _battle_detail(obs, decision)
        if acted:
            return f"{head} {acted}."
    theirs = sum(1 for p in _mons(obs.get("opponent_team")) if p.get("fainted"))
    ours = sum(1 for p in _mons(obs.get("team")) if p.get("fainted"))
    return f"{head} choices locked in. Knockouts so far: {theirs} for us, {ours} for them."


def _move_name(mid) -> str:
    return str(dex.move(str(mid)).get("name") or mid)


def _battle_detail(obs: dict, decision) -> str | None:
    """The action just chosen, per slot: move (and its target) or switch. Nothing about any other option."""
    act = _action_of(decision)
    if obs.get("is_doubles"):
        actives = list(obs.get("active_pokemon") or []) + [None, None]
        foes = list(obs.get("opponent_active_pokemon") or []) + [None, None]
        team = _mons(obs.get("team"))
        parts = []
        for i in (0, 1):
            c = act.get(f"slot_{i}")
            if not isinstance(c, dict):
                continue
            me = actives[i] if isinstance(actives[i], dict) else {}
            who = str(me.get("name") or me.get("species") or f"Slot {i + 1}")
            if c.get("type") == "move" and c.get("move_id"):
                line = f"{who} uses {_move_name(c['move_id'])}"
                t = c.get("target")
                if t in (1, 2) and isinstance(foes[t - 1], dict) and foes[t - 1].get("species"):
                    line += " on " + str(foes[t - 1].get("name") or foes[t - 1]["species"])
                elif t in (-1, -2):
                    line += " on itself" if -t - 1 == i else " on its partner"
                parts.append(line)
            elif c.get("type") == "switch" and c.get("species"):
                incoming = _display(c["species"], team)
                parts.append(f"{who} switches to {incoming}" if me.get("species") and not me.get("fainted")
                             else f"{incoming} comes in")
        return "; ".join(parts) or None
    me = obs.get("active_pokemon") if isinstance(obs.get("active_pokemon"), dict) else {}
    who = str(me.get("name") or me.get("species") or "Our active")
    if act.get("type") == "move" and act.get("move_id"):
        return f"{who} uses {_move_name(act['move_id'])}"
    if act.get("type") == "switch" and act.get("species"):
        return f"switch to {_display(act['species'], _mons(obs.get('team')))}"
    return None
