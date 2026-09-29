"""Our contestant: routes each match to the right game policy.

The official runner (altruagent/runner.py) kills a match's process on any
INVALID_ACTION, bad chat or exception out of choose_action, and the platform
then auto-plays our seat (Showdown's timer in Pokémon; abstain / first target
after 120 s in Werewolf). So everything here is defensive:
  * every decision gets a time budget (agent/timecontrol.py: from the battle
    timer the platform shows, conservative defaults otherwise); the policy runs
    under a watchdog thread and is answered for by a safe legal action once the
    budget's hard deadline passes (the policy itself degrades before that);
  * choose_action / choose_message never raise: a crashed policy, an unknown
    game type or phase, an unexpected payload shape or action template is
    logged to stderr and answered with a safe legal action (agent/fallback.py,
    deterministic);
  * structured actions are checked against the server's own template before we
    send them, and chat is clipped to the platform limits;
  * a doubles turn is also checked as a pair (Showdown's rule: never the same
    switch-in twice, no `pass` while a switch-in is still free for that slot)
    right before it goes to the runner, whatever path produced it (policy,
    fallback, watchdog, crash): a pair the server would reject (or one with a
    wrong or missing `type`) is repaired minimally (agent/fallback.repair_doubles),
    logged and counted; when the doubles template is the state's one legal
    action, an answer that is no pair at all (e.g. the template's own
    LegalAction, which the runner would send unfilled) is replaced by the
    template filled in.

Moves carry a short public `reasoning_summary` (altruagent.WithReasoning) built
by agent/reasoning.py from public information only, after the move is fixed;
ARENA_REASONING=off turns it off.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from altruagent import TERMINATE_MESSAGING, DecisionContext, GameState, LegalAction, SendMessage

from . import reasoning, timecontrol
from .fallback import doubles_from_template, doubles_problems, is_template, joint_problems, repair_doubles, safe_action
from .pokemon.dex import to_id
from .pokemon.policy import PokemonPolicy, switch_ranking
from .werewolf.policy import WerewolfPolicy

log = logging.getLogger("agent")

MAX_CHATS_PER_WINDOW = 5
MAX_WORDS = 50
RECORD_DIR = os.environ.get("ARENA_RECORD_DIR", "logs")   # every state we act on -> logs/<session>.jsonl
BUSY_WAIT_S = 1.0          # how long a decision waits for an earlier, overrun policy call to finish
_UNSET = object()
_DAY_RE = re.compile(r"Day\s+(\d+)\s*\|")


def record(kind: str, context: DecisionContext, state: GameState, out=None, public_reason: str | None = None) -> None:
    """Append the raw platform payload (and what we answered) so real schemas can be checked offline.

    Off when ARENA_RECORD=0 — local simulators and replays set it, otherwise a 400-game ladder writes GBs."""
    if os.environ.get("ARENA_RECORD", "1") == "0":
        return
    try:
        Path(RECORD_DIR).mkdir(parents=True, exist_ok=True)
        entry = {"t": time.time(), "kind": kind, "game_type": context.game_type,
                 "raw": state.raw, "out": _jsonable(out)}
        if public_reason is not None:
            entry["reasoning_summary"] = public_reason
        with open(Path(RECORD_DIR) / f"{context.session_id}.jsonl", "a") as f:
            f.write(json.dumps(entry) + "\n")
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
        self._chat_day: object = _UNSET                # the day the chat state below belongs to
        self._chat_queue: list[dict] = []
        self._chat_done = False                       # we terminated this window: never chat again in it
        self._chat_sent = 0
        self._chat_said: set = set()
        self.watchdog = os.environ.get("ARENA_TC_WATCHDOG", "1") != "0"   # 0: run the policy inline (debugging)
        self._busy: threading.Thread | None = None   # a policy call that overran its deadline and is still running
        self._warned: set = set()                     # unknown game types / phases already reported
        self.last_decision: dict | None = None        # kind, budget, source, path, ms of the last decision
        self.overruns = 0                             # decisions the watchdog answered for
        self.fallbacks = 0                            # decisions answered by a fallback (crash, overrun, unknown)
        self.joint_repairs = 0                        # doubles answers repaired as a pair before sending
        self._path = "policy"                         # how the current decision is being answered
        self.reasoning_mode = reasoning.mode_from_env()   # ARENA_REASONING: off | safe (default) | detail
        self.last_reasoning: str | None = None             # the public line sent with the last move, if any

    # ---- moves -----------------------------------------------------------
    def choose_action(self, state: GameState, context: DecisionContext):
        """Never raises; always answers within the decision's hard deadline (see the module docstring)."""
        t0 = time.monotonic()
        game = ""
        try:
            game = str(getattr(context, "game_type", None) or getattr(state, "game_name", None) or "")
            dl = timecontrol.budget_for(getattr(state, "raw", None), game)
        except Exception:
            dl = timecontrol.Deadline(timecontrol.BATTLE_S, "unknown", "error")
        self._path = "policy"
        try:
            with timecontrol.decision(dl):
                out = self._choose_action(state, context, game, dl)
        except Exception:
            log.exception("choose_action failed; answering with a safe legal action")
            out = self._fallback(state, game, "crash")
        out = self._joint_guard(state, out)              # every path: a doubles pair the server takes as a whole
        # The move is fixed above; the public line is attached afterwards and can only drop itself, never the move.
        sent, self.last_reasoning = out, None
        try:
            sent, self.last_reasoning = reasoning.attach(out, getattr(state, "raw", None), game, self.reasoning_mode)
        except Exception:
            log.exception("public reasoning failed; sending the move without it")
            sent, self.last_reasoning = out, None
        self.last_decision = {"kind": dl.kind, "budget": round(dl.seconds, 3), "source": dl.source,
                              "path": self._path, "ms": round(1000 * (time.monotonic() - t0), 1)}
        record("action", context, state, out, self.last_reasoning)
        return sent

    def _choose_action(self, state: GameState, context: DecisionContext, game: str, dl):
        raw = _raw_with_legal(state)
        if game == "werewolf":
            kw = self._guarded(lambda: self.ww.act(raw), dl, "werewolf policy")
            if isinstance(kw, dict):
                return self._discrete(state, kw)
        elif game.startswith("pokemon_"):
            kw = self._guarded(lambda: self.pk.act(raw), dl, "pokemon policy")
            if isinstance(kw, dict):
                return self._structured(state, kw)
        else:
            self._warn_once(("game", game), "unknown game type %r: playing a safe legal action", game)
            self._path = "unknown-game"
            return safe_action(state, raw)
        return self._fallback(state, game, "no policy answer")

    def _guarded(self, fn, dl, what: str):
        """fn() under the watchdog: its answer, or None when it raised or did not answer by the hard deadline. One
        policy call at a time: while an earlier call that overran is still running (the policies are not
        thread-safe), later decisions are answered by the fallback."""
        if not self.watchdog:
            try:
                return fn()
            except Exception:
                log.exception("%s crashed; using the safe fallback", what)
                return None
        if self._busy is not None:
            self._busy.join(timeout=max(0.0, min(BUSY_WAIT_S, 0.25 * dl.left())))
            if self._busy.is_alive():
                log.error("%s still busy with an earlier decision; answering with the safe fallback", what)
                self._path = "busy"
                return None
            self._busy = None
        box: dict = {}

        def run():
            try:
                box["out"] = fn()
            except BaseException as e:                       # noqa: BLE001 - reported in the caller's thread
                box["exc"] = e

        ctx = contextvars.copy_context()                     # the thread sees this decision's deadline
        t = threading.Thread(target=ctx.run, args=(run,), name="agent-decision", daemon=True)
        t.start()
        t.join(timeout=dl.hard_left())
        if t.is_alive():
            self._busy = t
            self.overruns += 1
            self._path = "overrun"
            log.error("%s did not answer within %.1fs (%s); answering with the safe fallback", what,
                      dl.hard - dl.start, dl.kind)
            return None
        if "exc" in box:
            e = box["exc"]
            if not isinstance(e, Exception):                 # KeyboardInterrupt / SystemExit in the worker
                raise e
            log.error("%s crashed; using the safe fallback", what, exc_info=(type(e), e, e.__traceback__))
            self._path = "crash"
            return None
        return box.get("out")

    def _warn_once(self, key, msg: str, *args) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.error(msg, *args)

    def _discrete(self, state: GameState, kw: dict):
        aid = str(kw.get("action_id"))
        hit = next((a for a in state.legal_actions if a.action_id == aid), None)
        if hit is None:
            log.warning("policy answered %r, not a legal action id; using the safe fallback", aid)
            self._path = "fallback"
            return safe_action(state)
        return hit

    def _structured(self, state: GameState, kw: dict):
        action = kw.get("action")
        if action is None:                                   # singles move/switch: action_id form
            return self._discrete(state, kw)
        if not isinstance(action, dict):
            raise TypeError(f"policy answered a {type(action).__name__} action")
        if action.get("type") == "draft_pick":               # return the real LegalAction when one matches
            want = f"draft_pick:{action.get('card_id')}"
            hit = next((a for a in state.legal_actions if a.action_id == want), None)
            return hit or self._fallback(state, "pokemon_", "draft pick not legal")
        if action.get("type") in ("move", "switch"):
            hit = next((a for a in state.legal_actions
                        if (a.input.get("action") or {}).get("type") == action["type"]
                        and (a.input.get("action") or {}).get("slot") == action.get("slot")), None)
            return hit or self._fallback(state, "pokemon_", "singles action not legal")
        raw = _raw_with_legal(state)
        if action.get("type") == "doubles_turn" and not doubles_ok(raw, action):
            fixed = self._repair(raw, action, "doubles action failed local validation")
            if fixed is not None:
                self._path = "repaired"
                return fixed
            log.warning("doubles action failed local validation: %s", action)
            return self._fallback(state, "pokemon_", "doubles action not legal")
        if action.get("type") == "select_lineup" and not lineup_ok(raw, action):
            log.warning("lineup failed local validation: %s", action)
            return self._fallback(state, "pokemon_", "lineup not legal")
        if is_template(action):
            log.warning("policy answered an unfilled action template: %s", action)
            return self._fallback(state, "pokemon_", "template")
        return action

    def _fallback(self, state: GameState, game: str, why: str):
        """The game policy's own fallback, else the generic safe action. Never raises."""
        self.fallbacks += 1
        if self._path == "policy":
            self._path = "fallback"
        raw = _raw_with_legal(state)
        try:
            if game.startswith("pokemon_"):
                fb = self.pk.fallback(raw)
                out = fb.get("action") if isinstance(fb, dict) else None
                if isinstance(out, dict) and not is_template(out) and self._fallback_ok(raw, out):
                    # a listed concrete action (draft pick, singles move): send its id, as the policy path does
                    hit = next((a for a in state.legal_actions if (a.input or {}).get("action") == out), None)
                    return hit or out
                if out is None and isinstance(fb, dict) and fb.get("action_id") is not None:
                    hit = next((a for a in state.legal_actions if a.action_id == str(fb["action_id"])), None)
                    if hit is not None:
                        return hit
            elif game == "werewolf":
                aid = str(self.ww.fallback(raw).get("action_id"))
                hit = next((a for a in state.legal_actions if a.action_id == aid), None)
                if hit is not None:
                    return hit
        except Exception:
            log.exception("policy fallback crashed (%s)", why)
        return safe_action(state, raw)

    @staticmethod
    def _fallback_ok(raw: dict, out: dict) -> bool:
        if out.get("type") == "doubles_turn":
            return doubles_ok(raw, out)
        if out.get("type") == "select_lineup":
            return lineup_ok(raw, out)
        return True

    def _repair(self, raw: dict, action: dict, why: str) -> dict | None:
        """`action` repaired minimally into a legal doubles_turn (fallback.repair_doubles; a slot that must change takes
        the switch-in the policy's ranking prefers), or None when that is not possible. Logs and counts a repair."""
        tmpl = _doubles_template(raw)
        if tmpl is None:
            return None
        fixed, notes = repair_doubles(action, tmpl, prefer=_switch_prefs(raw))
        if not doubles_ok(raw, fixed):
            return None
        if notes:
            self.joint_repairs += 1
            log.warning("%s (%s); repaired: %s -> %s", why, "; ".join(notes), action, fixed)
        return fixed

    def _joint_guard(self, state: GameState, out):
        """The last line of defence, on every path (policy, repair, fallback, watchdog overrun, crash): in a doubles
        state what goes to the runner is a legal doubles_turn. A pair (a doubles_turn, or any dict with slot_* keys)
        the server would reject, also for a wrong or missing `type`, is repaired. When the doubles template is the
        state's one legal action (every recorded platform doubles turn), an answer that is no pair at all (the
        template's own LegalAction, which the runner would send unfilled, a stale select_lineup, junk) is replaced by
        the template filled in (its first options: moves stay moves). Never raises (the answer then goes as is)."""
        try:
            raw = _raw_with_legal(state)
            tmpl = _doubles_template(raw)
            if tmpl is None or (tmpl.get("type") or "doubles_turn") != "doubles_turn":
                return out
            pair = isinstance(out, dict) and (out.get("type") == "doubles_turn"
                                              or any(str(k).startswith("slot_") for k in out))
            if not pair:
                if not _doubles_only(raw, tmpl):
                    return out                            # another legal action is listed: the answer may be that one
                filled = doubles_from_template(tmpl)
                log.error("doubles state answered with %r, not a doubles_turn; sending the template filled in: %s",
                          out, filled)
                self.joint_repairs += 1
                self._path += "+filled"
                out = filled
            if not doubles_problems(tmpl, out):
                return out
            fixed = self._repair(raw, out, "doubles answer not legal right before sending")
            if fixed is None:
                log.error("doubles answer %s is not legal and could not be repaired; sending it", out)
                return out
            self._path += "+repaired"
            return fixed
        except Exception:
            log.exception("doubles pair check failed; sending the answer as it is")
            return out

    # ---- Werewolf discussion ----------------------------------------------
    def choose_message(self, state: GameState, context: DecisionContext):
        """Never raises: any failure ends our turn to talk (TERMINATE_MESSAGING)."""
        try:
            dl = timecontrol.budget_for(getattr(state, "raw", None), getattr(context, "game_type", None))
            with timecontrol.decision(dl):
                out = self._guarded(lambda: self._choose_message(state, context), dl, "chat policy")
            if not (out is TERMINATE_MESSAGING or isinstance(out, SendMessage)):
                out = TERMINATE_MESSAGING
        except Exception:
            log.exception("choose_message failed; terminating messaging")
            out = TERMINATE_MESSAGING
        record("message", context, state, out)
        return out

    def _choose_message(self, state: GameState, context: DecisionContext):
        """One chat or TERMINATE per call. The runner calls again after every change while the window is open, so a
        reactive policy (`reactive_chat`) is asked again once its queued messages are sent: it can answer what was
        said meanwhile. After we terminate, and whenever the server lists us as terminated, only TERMINATE (a
        no-op server side) goes out: a chat then could be rejected, and a rejected chat ends the match process."""
        st = state.raw if isinstance(state.raw, dict) else {}
        self.ww.observe(st)
        gs = st.get("game_state")
        day = gs.get("day") if isinstance(gs, dict) else None
        if day is None:                                   # the observation carries it too ("Day 2 | phase ...")
            m = _DAY_RE.search(str(st.get("observation") or ""))
            day = int(m.group(1)) if m else None
        if day != self._chat_day:
            self._chat_day, self._chat_done, self._chat_sent, self._chat_said = day, False, 0, set()
            self._chat_queue = self._ask_chat(st)
        me, term = st.get("your_position"), st.get("terminated_messaging")
        if isinstance(term, list) and isinstance(me, int) and me in term:
            self._chat_done = True
        if not self._chat_done and not self._chat_queue and self._chat_sent and getattr(self.ww, "reactive_chat", False):
            self._chat_queue = self._ask_chat(st)
        while not self._chat_done and self._chat_queue and self._chat_sent < MAX_CHATS_PER_WINDOW:
            m = self._chat_queue.pop(0)
            text = " ".join(str(m.get("content", "")).split()[:MAX_WORDS])
            rec = m.get("recipients") or None
            if rec is not None and (not isinstance(rec, list) or len(rec) != 1 or not isinstance(rec[0], int)
                                    or isinstance(rec[0], bool) or not 0 <= rec[0] <= 6 or rec[0] == me):
                continue                                  # a private message with a bad recipient is dropped, never broadcast
            if text and (text, tuple(rec or ())) not in self._chat_said:     # never the same message twice a day
                self._chat_said.add((text, tuple(rec or ())))
                self._chat_sent += 1
                return SendMessage(text, recipients=rec)
        self._chat_done = True
        return TERMINATE_MESSAGING

    def _ask_chat(self, st: dict) -> list[dict]:
        try:
            return [m for m in list(self.ww.chat(st) or []) if isinstance(m, dict)][:MAX_CHATS_PER_WINDOW]
        except Exception:
            log.exception("chat policy crashed")
            return []


def _raw_with_legal(state) -> dict:
    """The raw state the policies read, with `legal_actions` filled in from state.legal_actions when the server left
    it out of the state (the runner then fetches them separately; the raw payload keeps None). Never mutates it."""
    raw = getattr(state, "raw", None)
    raw = raw if isinstance(raw, dict) else {}
    la = raw.get("legal_actions")
    if isinstance(la, dict) and isinstance(la.get("actions"), list) and la["actions"]:
        return raw
    acts = [getattr(a, "raw", None) or {"action_id": getattr(a, "action_id", ""), "label": getattr(a, "label", None),
                                         "input": getattr(a, "input", {}) or {}}
            for a in (getattr(state, "legal_actions", None) or [])]
    if not acts:
        return raw
    out = dict(raw)
    out["legal_actions"] = {"state_version": getattr(state, "state_version", raw.get("state_version")), "actions": acts}
    return out


def lineup_ok(raw: dict, action: dict) -> bool:
    """A select_lineup brings bring_count (4) different Pokémon of our roster and leads lead_count (2) of them."""
    try:
        obs = raw.get("observation") or {}
        roster = {to_id(p.get("species", "")) for p in obs.get("your_roster") or [] if isinstance(p, dict)}
        tmpl = (raw.get("legal_actions") or {}).get("actions", [{}])[0].get("input", {}).get("action") or {}
        roster |= {to_id(r) for r in tmpl.get("roster") or [] if isinstance(r, str)}
        n_bring = int(tmpl.get("bring_count") or 4)
        n_lead = int(tmpl.get("lead_count") or 2)
        bring = [to_id(b) for b in action.get("bring") or []]
        leads = [to_id(x) for x in action.get("leads") or []]
        return (len(bring) == n_bring and len(set(bring)) == n_bring and (not roster or set(bring) <= roster)
                and len(leads) == n_lead and len(set(leads)) == n_lead and set(leads) <= set(bring))
    except Exception:
        return False


def _doubles_template(raw: dict) -> dict | None:
    """The doubles_turn template (per-slot options) of a state, or None."""
    try:
        tmpl = raw["legal_actions"]["actions"][0]["input"]["action"]
        return tmpl if isinstance(tmpl, dict) and isinstance(tmpl.get("slots"), list) else None
    except (KeyError, IndexError, TypeError):
        return None


def _doubles_only(raw: dict, tmpl: dict) -> bool:
    """The state's one legal action is a doubles_turn template (every recorded platform doubles turn is): then only a
    doubles_turn answer can be legal."""
    try:
        return (tmpl.get("type") or "doubles_turn") == "doubles_turn" and len(raw["legal_actions"]["actions"]) == 1
    except (KeyError, TypeError, AttributeError):
        return False


def _switch_prefs(raw: dict) -> list[str] | None:
    """The policy's switch-in ranking for a repair (best first), or None. Never raises."""
    try:
        return switch_ranking(raw)
    except Exception:
        log.debug("switch ranking failed", exc_info=True)
        return None


def doubles_ok(raw: dict, action: dict) -> bool:
    """Check a doubles_turn against the server's per-slot option template, and the two slots as a pair (never the same
    switch-in twice, no `pass` while a switch-in is still free for that slot: fallback.joint_problems)."""
    try:
        tmpl = raw["legal_actions"]["actions"][0]["input"]["action"]
        switched = []
        for slot in tmpl.get("slots", []):
            choice = action.get(f"slot_{slot['slot']}")
            if not choice:
                return False
            opts = slot.get("options", [])
            if choice["type"] == "move":
                ok = any(o.get("type") == "move" and o.get("move_id") is not None
                         and to_id(o.get("move_id")) == to_id(choice.get("move_id"))
                         and choice.get("target") in (o.get("targets") or [0]) for o in opts)
            elif choice["type"] == "switch":
                ok = any(o.get("type") == "switch" and to_id(o.get("species")) == to_id(choice.get("species"))
                         for o in opts)
                switched.append(to_id(choice.get("species")))
            else:                                  # a `pass`; any other type is never an option
                ok = choice["type"] == "pass" and any(o.get("type") == "pass" for o in opts)
            if not ok:
                return False
        return len(switched) == len(set(switched)) and not joint_problems(tmpl, action)
    except (KeyError, IndexError, TypeError):
        return False


def create_agent():
    return Contestant()
