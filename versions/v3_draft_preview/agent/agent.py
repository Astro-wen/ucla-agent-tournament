"""Our contestant: routes each match to the right game policy.

The official runner (altruagent/runner.py) kills a match's process on any
INVALID_ACTION or bad chat, and the platform then auto-plays our seat
(random move after 300 s in Pokémon; abstain / first target after 120 s in
Werewolf). So everything here is defensive: every policy call is wrapped,
structured actions are checked against the server's own template before we
send them, and chat is clipped to the platform limits.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from altruagent import TERMINATE_MESSAGING, DecisionContext, GameState, LegalAction, SendMessage

from .pokemon.dex import to_id
from .pokemon.policy import PokemonPolicy
from .werewolf.policy import WerewolfPolicy

log = logging.getLogger("agent")

MAX_CHATS_PER_WINDOW = 5
MAX_WORDS = 50
RECORD_DIR = os.environ.get("ARENA_RECORD_DIR", "logs")   # every state we act on -> logs/<session>.jsonl


def record(kind: str, context: DecisionContext, state: GameState, out=None) -> None:
    """Append the raw platform payload (and what we answered) so real schemas can be checked offline.

    Off when ARENA_RECORD=0 — local simulators and replays set it, otherwise a 400-game ladder writes GBs."""
    if os.environ.get("ARENA_RECORD", "1") == "0":
        return
    try:
        Path(RECORD_DIR).mkdir(parents=True, exist_ok=True)
        with open(Path(RECORD_DIR) / f"{context.session_id}.jsonl", "a") as f:
            f.write(json.dumps({"t": time.time(), "kind": kind, "game_type": context.game_type,
                                "raw": state.raw, "out": _jsonable(out)}) + "\n")
    except Exception:
        log.debug("record failed", exc_info=True)


def _jsonable(x):
    if x is None or isinstance(x, (dict, str, int, float, bool)):
        return x
    if hasattr(x, "action_id"):
        return {"action_id": x.action_id, "input": getattr(x, "input", None)}
    if hasattr(x, "content"):
        return {"content": x.content, "recipients": getattr(x, "recipients", None)}
    return str(x)


class Contestant:
    def __init__(self) -> None:
        self.ww = WerewolfPolicy()
        self.pk = PokemonPolicy()
        self._chat_day: object = None
        self._chat_queue: list[dict] = []

    # ---- moves -----------------------------------------------------------
    def choose_action(self, state: GameState, context: DecisionContext):
        out = self._choose_action(state, context)
        record("action", context, state, out)
        return out

    def _choose_action(self, state: GameState, context: DecisionContext):
        game = context.game_type or state.game_name or ""
        st = state.raw
        try:
            if game == "werewolf":
                return self._discrete(state, self.ww.act(st))
            if game.startswith("pokemon_"):
                return self._structured(state, self.pk.act(st))
        except Exception:
            log.exception("policy crashed; using safe fallback")
        return self._safe_fallback(state, game)

    def _discrete(self, state: GameState, kw: dict):
        aid = str(kw.get("action_id"))
        return next((a for a in state.legal_actions if a.action_id == aid), state.legal_actions[0])

    def _structured(self, state: GameState, kw: dict):
        action = kw.get("action")
        if action is None:                                   # singles move/switch: action_id form
            return self._discrete(state, kw)
        if action.get("type") == "draft_pick":               # return the real LegalAction when one matches
            want = f"draft_pick:{action.get('card_id')}"
            hit = next((a for a in state.legal_actions if a.action_id == want), None)
            return hit or state.legal_actions[0]
        if action.get("type") in ("move", "switch"):
            hit = next((a for a in state.legal_actions
                        if (a.input.get("action") or {}).get("type") == action["type"]
                        and (a.input.get("action") or {}).get("slot") == action.get("slot")), None)
            return hit or state.legal_actions[0]
        if action.get("type") == "doubles_turn" and not doubles_ok(state.raw, action):
            log.warning("doubles action failed local validation: %s", action)
            return self.pk.fallback(state.raw)["action"]
        return action

    def _safe_fallback(self, state: GameState, game: str):
        try:
            if game.startswith("pokemon_"):
                fb = self.pk.fallback(state.raw)
                return fb.get("action") or self._discrete(state, fb)
            if game == "werewolf":
                return self._discrete(state, self.ww.fallback(state.raw))
        except Exception:
            log.exception("fallback crashed")
        return state.legal_actions[0]

    # ---- Werewolf discussion ----------------------------------------------
    def choose_message(self, state: GameState, context: DecisionContext):
        out = self._choose_message(state, context)
        record("message", context, state, out)
        return out

    def _choose_message(self, state: GameState, context: DecisionContext):
        st = state.raw
        self.ww.observe(st)
        day = (st.get("game_state") or {}).get("day")
        if day != self._chat_day:
            self._chat_day = day
            try:
                self._chat_queue = list(self.ww.chat(st) or [])[:MAX_CHATS_PER_WINDOW]
            except Exception:
                log.exception("chat policy crashed")
                self._chat_queue = []
        while self._chat_queue:
            m = self._chat_queue.pop(0)
            text = " ".join(str(m.get("content", "")).split()[:MAX_WORDS])
            rec = m.get("recipients") or None
            if rec is not None and (len(rec) != 1 or rec[0] == st.get("your_position")):
                rec = None
            if text:
                return SendMessage(text, recipients=rec)
        return TERMINATE_MESSAGING


def doubles_ok(raw: dict, action: dict) -> bool:
    """Check a doubles_turn against the server's per-slot option template."""
    try:
        tmpl = raw["legal_actions"]["actions"][0]["input"]["action"]
        switched = []
        for slot in tmpl.get("slots", []):
            choice = action.get(f"slot_{slot['slot']}")
            if not choice:
                return False
            opts = slot.get("options", [])
            if choice["type"] == "move":
                ok = any(o.get("type") == "move" and to_id(o.get("move_id")) == to_id(choice.get("move_id"))
                         and choice.get("target") in (o.get("targets") or [0]) for o in opts)
            elif choice["type"] == "switch":
                ok = any(o.get("type") == "switch" and to_id(o.get("species")) == to_id(choice.get("species"))
                         for o in opts)
                switched.append(to_id(choice.get("species")))
            else:
                ok = any(o.get("type") == "pass" for o in opts)
            if not ok:
                return False
        return len(switched) == len(set(switched))
    except (KeyError, IndexError, TypeError):
        return False


def create_agent():
    return Contestant()
