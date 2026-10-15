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
  * Red Alert (`red_alert`, real time) is played by our rule-based bot
    (agent/redalert/, no LLM): each view is answered with a locally validated
    order batch or altruagent.WAIT, every play_action answer reaches it through
    on_action_result, and it never raises; ARENA_RA=off restores the old
    behaviour (WAIT for the whole match). Any other real-time game
    (`pacing.mode == "realtime"`) we have no policy for is answered with WAIT
    for its whole match: nothing is sent, the runner waits for the next view;
  * a move or chat the runner could not send for a temporary reason is not resent: it re-reads the state and asks
    again (SDK PR #7), and every decision is simply made again from that state. A Werewolf chat that did not reach
    the server is then not counted (quota, never-twice rule, saved match state) and is decided again: the next
    state's `new_messages` shows whether it landed (Contestant._settle_pending). A Pokemon battle decision asked again
    (the same decision point, whatever its state_version) starts from the battle memory of its first ask, and a move
    the server refused (on_action_result) is rolled back out of that memory (PokemonPolicy._same_decision /
    undelivered): a move that did not land never counts as played (Fake Out freshness, Protect chains);
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

A worker restarted mid-match (a new process, a new create_agent()) resumes with the memory the previous one saved
after its last decision (agent/matchstate.py): the same answers an uninterrupted instance would give. Every decision
records how it was answered (`decision`: path, time; `search`: the engine search's outcome in a battle turn), each
match's worker prints one `[match-summary]` line when it exits, and the live counters feed run_agent.sh's health
line (agent/health.py).
"""
from __future__ import annotations

import atexit
import contextvars
import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path

from altruagent import TERMINATE_MESSAGING, WAIT, DecisionContext, GameState, LegalAction, SendMessage, WithReasoning

from . import matchstate, reasoning, timecontrol
from .fallback import (action_template, doubles_from_template, doubles_problems, gives_up, is_template, joint_problems,
                       repair_doubles, safe_action)
from .pokemon.dex import to_id
from .pokemon.policy import MACHINE_REASONS, PokemonPolicy, switch_ranking
from .werewolf.policy import WerewolfPolicy

log = logging.getLogger("agent")

MAX_CHATS_PER_WINDOW = 5
MAX_WORDS = 50
MAX_MESSAGE_CHARS = 1024   # MESSAGE_TOO_LONG above it (werewolf.md §8); checked in UTF-8 bytes (never fewer than chars)
RECORD_DIR = os.environ.get("ARENA_RECORD_DIR", "logs")   # every state we act on -> logs/<session>.jsonl
BUSY_WAIT_S = 1.0          # how long a decision waits for an earlier, overrun policy call to finish
_UNSET = object()
_DAY_RE = re.compile(r"Day\s+(\d+)\s*\|")

# Werewolf listening (ARENA_WW_LISTEN, see Contestant.choose_message): after the opening chat we keep the discussion
# window open and read it, and send at most LISTEN_FOLLOWUPS more chats that day reacting to what was said since
# (follow a seer claim, counter a fake one); we end our part of the window when a stop rule fires. Times are fractions
# of the window's length W, the `messaging_seconds_left` of the first state we see in it (WINDOW_S when the state has
# no countdown). On by default since v4.3 (10-08): without it, under the official runner's timing we speak once at
# t = 0 and can answer nothing said later (red team: 0 of 1007 frames answered); ARENA_WW_LISTEN=off turns it off.
LISTEN_ENV = "ARENA_WW_LISTEN"
LISTEN_DEFAULT = "on"
WINDOW_S = 120.0           # a Werewolf discussion window closes 2 minutes after it opens (werewolf.md §8)
LISTEN_MARGIN = 1 / 8      # terminate at the latest this long before the window closes (15 s of 120)
LISTEN_QUIET = 1 / 5       # ... or once no other seat has chatted for this long (24 s of 120)
LISTEN_POLL = 1 / 24       # the longest single wait while listening (5 s of 120): another seat's terminate is seen
LISTEN_FOLLOWUPS = 1       # chats a day after the opening ones, with listening on
LISTEN_MIN_WAIT_S = 0.25   # a wait answered at once with nothing new is followed by this pause (no busy loop)
_ON = {"1", "on", "true", "yes", "enable", "enabled"}
# Errors on a sent move that mean the server did not take THIS send (on_action_result): the runner's race codes
# (pokemon.md section 8). For a first send of a decision point the decision had closed (the move timer played for us);
# after an earlier send of the same point that got no answer, they can also mean that send landed (NOT_YOUR_TURN:
# already submitted; STALE_STATE: the version moved), which PokemonPolicy.undelivered tells apart. Not listed: a 5xx
# and RUNTIME_TEMPORARILY_UNAVAILABLE (turn-based: a transient error the runner treats as "may have landed").
REFUSED_MOVE = frozenset({"STALE_STATE", "NOT_YOUR_TURN", "WRONG_PHASE"})

RA_ENV = "ARENA_RA"              # Red Alert bot: on (default) | off (WAIT for the whole match, as before v4.3)
_OFF = {"0", "off", "false", "no", "none", "disable", "disabled", "wait"}


def ra_enabled(env=None) -> bool:
    """ARENA_RA: the rule-based Red Alert bot plays (default) unless it is off / 0 / false / no / wait."""
    env = os.environ if env is None else env
    return str(env.get(RA_ENV) or "on").strip().lower() not in _OFF


def listen_enabled(env=None) -> bool:
    """ARENA_WW_LISTEN: on | off (LISTEN_DEFAULT when unset)."""
    env = os.environ if env is None else env
    return str(env.get(LISTEN_ENV) or LISTEN_DEFAULT).strip().lower() in _ON


class _Hold:
    """`Contestant.message_step`'s third answer besides a chat and TERMINATE_MESSAGING: nothing to say yet, keep
    listening (our part of the window stays open). Never handed to the runner. Use the HOLD singleton."""
    __slots__ = ()

    def __repr__(self) -> str:
        return "HOLD"


HOLD = _Hold()


def _runner_game():
    """The game session `altruagent.runner.run_game` is playing the current decision with (its `game`), or None.

    choose_message gets a state and a context but no way to read the game (DecisionContext has no game handle, and
    altruagent/ is the SDK, not ours to edit), so listening finds the runner's own session on the call stack. run_game
    calls choose_message synchronously and waits for its answer, so nothing else uses the session meanwhile, and only
    reads are made with it (wait_for_update, which the runner itself would call next). Must run in the runner's
    thread. None under any other caller (local simulators, replays, tests) or if the SDK changes shape."""
    try:
        from altruagent import runner as sdk_runner
        code = getattr(getattr(sdk_runner, "run_game", None), "__code__", None)
        f = sys._getframe(1)
        for _ in range(24):
            if f is None or code is None:
                return None
            if f.f_code is code:
                game = f.f_locals.get("game")
                return game if callable(getattr(game, "wait_for_update", None)) else None
            f = f.f_back
    except Exception:
        log.debug("runner game lookup failed", exc_info=True)
    return None


def _seen_seq(state) -> int | None:
    """The highest message seq in a state's new_messages (what wait_for_update's since_message_seq takes)."""
    seqs = [m.index for m in getattr(state, "new_messages", None) or [] if isinstance(getattr(m, "index", None), int)]
    return max(seqs) if seqs else None


def record(kind: str, context: DecisionContext, state: GameState, out=None, public_reason: str | None = None,
           extra: dict | None = None) -> None:
    """Append the raw platform payload (and what we answered) so real schemas can be checked offline. `extra`: how the
    decision was answered (`decision`: path, budget, ms; `search`: the engine search's outcome of a battle turn).

    Off when ARENA_RECORD=0 — local simulators and replays set it, otherwise a 400-game ladder writes GBs."""
    if os.environ.get("ARENA_RECORD", "1") == "0":
        return
    try:
        Path(RECORD_DIR).mkdir(parents=True, exist_ok=True)
        entry = {"t": time.time(), "kind": kind, "game_type": context.game_type,
                 "raw": state.raw, "out": _jsonable(out)}
        if public_reason is not None:
            entry["reasoning_summary"] = public_reason
        for k, v in (extra or {}).items():
            if v is not None and k not in entry:
                entry[k] = v
        with open(Path(RECORD_DIR) / f"{context.session_id}.jsonl", "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        if not _record_failed:                  # once per process (= per match): a read-only disk must not go unseen
            _record_failed.append(e)
            log.warning("recording to %s failed (%r): this match's states are not saved; set ARENA_RECORD_DIR (or "
                        "run_agent.sh's LOG_DIR) to a writable directory", RECORD_DIR, e)
        log.debug("record failed", exc_info=True)


_record_failed: list = []


def clip_chat(content) -> str:
    """A chat as the platform takes it: whitespace collapsed, at most MAX_WORDS words and MAX_MESSAGE_CHARS (in UTF-8
    bytes), cut at a word ("" when not even the first word fits). Our templates are far below both caps."""
    words = str(content if content is not None else "").split()[:MAX_WORDS]
    text = " ".join(words)
    while words and len(text.encode("utf-8", "replace")) > MAX_MESSAGE_CHARS:
        words.pop()
        text = " ".join(words)
    return text


def _jsonable(x):
    if x is None or isinstance(x, (dict, str, int, float, bool)):
        return x
    if hasattr(x, "action_id"):
        return {"action_id": x.action_id, "input": getattr(x, "input", None)}
    if hasattr(x, "content"):
        return {"content": x.content, "recipients": getattr(x, "recipients", None)}
    return str(x)


class Contestant:
    HOLD = HOLD                 # message_step's "keep listening" answer, for the local simulators

    def __init__(self) -> None:
        self.ww = WerewolfPolicy()
        self.pk = PokemonPolicy()
        self._chat_day: object = _UNSET                # the day the chat state below belongs to
        self._chat_queue: list[dict] = []
        self._chat_done = False                       # we terminated this window: never chat again in it
        self._chat_sent = 0
        self._chat_said: set = set()
        # The chat last handed to the runner, until the next state shows whether it reached the server (see
        # _settle_pending), and whether this match has shown one of our own chats back in new_messages yet.
        self._chat_pending: dict | None = None
        self._echo = False
        self.undelivered = 0                          # chats un-counted because the next state did not show them
        # Listening (ARENA_WW_LISTEN): chats of the day's opening batch still queued, chats sent after it, and the
        # window's clock (its length W, our first sight, when another seat last chatted; see _listen_wait).
        self.listen = listen_enabled()
        self._chat_opening = 0
        self._followups = 0
        self._win: dict | None = None
        self._clock = time.monotonic
        self.last_listen_s = 0.0                      # seconds the last choose_message spent listening
        self.listen_stats = {"holds": 0, "followups": 0, "seconds": 0.0, "max_s": 0.0, "errors": 0, "no_game": 0}
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
        # The match this instance plays ((agent id, session id), bound at its first decision) and its memory across a
        # worker restart (agent/matchstate.py): `resumed` is how far the match was when a restarted worker took over.
        self._match: tuple | None = None
        self._seat: int | None = None                 # our seat in it, when known (part of the saved state's key)
        self._family = ""                             # "pokemon" / "werewolf" / "" of the match
        self._game = ""                               # its game type (canonical, see game_kind)
        self.resumed: list | None = None
        self._resumes = 0                             # earlier resumes of this match (saved with its state)
        self.decisions = 0
        self.doubles_turns = 0                        # doubles battle decisions (the engine search's share of them)
        self.waits = 0                                # real-time views answered with WAIT (no policy for the game)
        self.refused_moves = 0                        # Pokemon moves the server refused (rolled back, on_action_result)
        self._last_raw = None                         # the state of the last decision (what its saved memory is of)
        self._summarized = False
        self.ra_on = ra_enabled()                     # ARENA_RA: the Red Alert bot plays (else WAIT, as before)
        self._ra = None                               # this match's RedAlertPolicy (agent/redalert), made lazily

    # ---- moves -----------------------------------------------------------
    def choose_action(self, state: GameState, context: DecisionContext):
        """Never raises; always answers within the decision's hard deadline (see the module docstring)."""
        if self.ra_on and _is_red_alert(state, context):
            return self._red_alert(state, context)
        if _realtime(state):
            out = self._realtime_wait(state, context)
            if out is not None:
                return out
        t0 = time.monotonic()
        game, family = "", ""
        try:
            family, game = game_kind(getattr(context, "game_type", None) or getattr(state, "game_name", None),
                                     getattr(state, "raw", None))
            dl = timecontrol.budget_for(getattr(state, "raw", None), game)
        except Exception:
            dl = timecontrol.Deadline(timecontrol.BATTLE_S, "unknown", "error")
        self._path = "policy"
        self._bind(state, context, family, game)
        self.pk.last_search_brief = None
        try:
            with timecontrol.decision(dl):
                out = self._choose_action(state, context, game, dl, family)
        except Exception:
            log.exception("choose_action failed; answering with a safe legal action")
            out = self._fallback(state, game, "crash", family)
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
        if family == "pokemon" and dl.kind == "battle" and _doubles_template(_raw_with_legal(state)) is not None:
            self.doubles_turns += 1                     # (the engine search plays VGC doubles turns only)
        brief = self.pk.last_search_brief if family == "pokemon" and dl.kind == "battle" else None
        if brief is not None and self._path.split("+")[0] in ("policy", "repaired"):
            self.last_decision["battle"] = "search" if brief.get("used") else "classic"
        elif family == "pokemon" and dl.kind == "battle" and self._path.split("+")[0] in ("policy", "repaired"):
            self.last_decision["battle"] = "classic"            # (ARENA_BATTLE=classic, or a singles battle)
        if self.resumed is not None:
            self.last_decision["resumed"] = self.resumed
        record("action", context, state, out, self.last_reasoning,
               extra={"decision": self.last_decision, "search": brief})
        self._after_decision(state, family, battle=dl.kind == "battle")
        return sent

    def _realtime_wait(self, state: GameState, context: DecisionContext):
        """A real-time game none of our policies plays (Red Alert): WAIT, the runner's "nothing to send right now" (it
        waits for the next view and asks again), at every view until the match ends. Never a guessed move: the generic
        safe action is no order batch, and the runner, which no longer stops on a refused batch in real time, would send
        one at every view (~5 a second for 10-20 minutes). Cheap, since it runs at every view: logged once per match,
        the first view recorded once (a real payload sample), the count in the [match-summary] line. None for a game we
        do play (a known family): the normal path answers it. Never raises."""
        try:
            family, game = game_kind(getattr(context, "game_type", None) or getattr(state, "game_name", None),
                                     getattr(state, "raw", None))
            if family:
                return None
            self.waits += 1
            self._path = "realtime-wait"
            self.last_decision = {"kind": "realtime", "budget": 0.0, "source": "realtime", "path": self._path, "ms": 0.0}
            if self.waits == 1:
                self._bind(state, context, family, game)
                log.error("real-time game %r: no policy of ours plays it; answering every view with WAIT (no orders "
                          "are sent) until the match ends", game)
                record("action", context, state, WAIT, extra={"decision": self.last_decision})
        except Exception:
            log.debug("real-time wait bookkeeping failed", exc_info=True)
        return WAIT

    def _red_alert(self, state: GameState, context: DecisionContext):
        """A Red Alert view, answered by this match's rule-based bot (agent/redalert): an order batch (with a short
        public phase tag as reasoning_summary unless ARENA_REASONING=off) or WAIT. In the operator's windows mode
        (turn-based for the runner, where WAIT and INVALID_ACTION are fatal) the bot always answers a valid batch,
        a harmless no-op when there is nothing to do. Runs inline (no watchdog thread: the bot is fast and
        deterministic, about 1-10 ms a view). States are recorded sparsely (the first view, then about one every 10
        game seconds, plus views with an exception or a new refusal reason), never every view. Never raises."""
        t0 = time.monotonic()
        raw = getattr(state, "raw", None)
        raw = raw if isinstance(raw, dict) else {}
        pacing = raw.get("pacing")
        windows = isinstance(pacing, dict) and pacing.get("mode") == "windows"
        out = WAIT
        text = None
        try:
            self._bind(state, context, "", "red_alert")
            if self._ra is None:
                from .redalert import RedAlertPolicy
                self._ra = RedAlertPolicy()
                log.warning("red_alert: the rule-based bot plays this match (ARENA_RA=off: WAIT only)")
            orders, tag = self._ra.decide(raw, getattr(context, "game_config", None))
            self.decisions += 1
            if orders:
                out = {"type": "orders", "orders": orders}
                if tag and self.reasoning_mode != reasoning.OFF:
                    text = reasoning.sanitize(tag)
                    if text and not reasoning.leaks(text, "red_alert"):
                        out = WithReasoning(out, text)
                    else:
                        text = None
            self._path = "red-alert"
            self.last_decision = {"kind": "realtime", "budget": 0.0, "source": "red_alert", "path": self._path,
                                  "ms": round(1000 * (time.monotonic() - t0), 2)}
            self.last_reasoning = text
            if self._ra.want_record:
                record("action", context, state, out, text, extra={"decision": self.last_decision})
        except Exception:
            log.exception("red_alert decision failed; answering WAIT")
            out, text = WAIT, None
        if out is WAIT and windows:
            out = _ra_noop(raw)
        return out

    def _choose_action(self, state: GameState, context: DecisionContext, game: str, dl, family: str = ""):
        raw = _raw_with_legal(state)
        if family and raw.get("game_type") and raw.get("game_type") != game:
            raw = {**raw, "game_type": game}              # a game type in another spelling: the policy reads ours
        if family == "werewolf":
            kw = self._guarded(lambda: self.ww.act(raw), dl, "werewolf policy")
            if isinstance(kw, dict):
                return self._discrete(state, kw)
        elif family == "pokemon":
            kw = self._guarded(lambda: self.pk.act(raw), dl, "pokemon policy")
            if isinstance(kw, dict):
                return self._structured(state, kw)
        else:
            self._warn_once(("game", game), "unknown game type %r: playing a safe legal action", game)
            self._path = "unknown-game"
            return safe_action(state, raw)
        return self._fallback(state, game, "no policy answer", family)

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

    def _fallback(self, state: GameState, game: str, why: str, family: str = ""):
        """The game policy's own fallback, else the generic safe action. Never raises."""
        self.fallbacks += 1
        if self._path == "policy":
            self._path = "fallback"
        raw = _raw_with_legal(state)
        try:
            family = family or ("pokemon" if str(game).startswith("pokemon") else "werewolf" if game == "werewolf"
                                else "")
            if family == "pokemon":
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
            elif family == "werewolf":
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
                if not (_doubles_only(raw, tmpl) or _unfilled(out, tmpl)):
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
        """Never raises: any failure ends our turn to talk (TERMINATE_MESSAGING).

        With listening on (ARENA_WW_LISTEN), once our opening chat is out and we have nothing more to say yet, we do
        not terminate: we keep reading the window (the runner's own session, read-only wait_for_update calls, see
        _listen) until there is something worth answering (one follow-up a day) or a stop rule fires, then answer.
        Off, or without a runner to read with, this is message_step: one chat or TERMINATE per call."""
        self.last_listen_s = 0.0
        out = self.message_step(state, context)
        if out is HOLD:
            out = self._listen(state, context)
        return out

    def message_step(self, state: GameState, context: DecisionContext):
        """One discussion decision, never blocking and never raising: a SendMessage, TERMINATE_MESSAGING, or (listening
        on) HOLD = nothing to say yet, ask again at the next change (the local simulators drive this directly)."""
        family = ""
        try:
            family, _ = game_kind(getattr(context, "game_type", None), getattr(state, "raw", None))
            self._bind(state, context, family, _)
            dl = timecontrol.budget_for(getattr(state, "raw", None), getattr(context, "game_type", None))
            with timecontrol.decision(dl):
                out = self._guarded(lambda: self._choose_message(state, context), dl, "chat policy")
            if not (out is TERMINATE_MESSAGING or out is HOLD or isinstance(out, SendMessage)):
                out = TERMINATE_MESSAGING
        except Exception:
            log.exception("choose_message failed; terminating messaging")
            out = TERMINATE_MESSAGING
        if out is TERMINATE_MESSAGING:
            self._chat_done = True                        # whatever answered it: never a chat after our terminate
        record("message", context, state, out)
        self._after_decision(state, family)
        return out

    def _choose_message(self, state: GameState, context: DecisionContext):
        """One chat or TERMINATE per call. The runner calls again after every change while the window is open, so a
        reactive policy (`reactive_chat`) is asked again once its queued messages are sent: it can answer what was
        said meanwhile. After we terminate, and whenever the server lists us as terminated, only TERMINATE (a
        no-op server side) goes out: a chat then could be rejected, and a rejected chat ends the match process.

        Listening on: the policy is asked again at every call (not only right after our own chat), at most
        LISTEN_FOLLOWUPS chats go out after the opening ones, and HOLD replaces TERMINATE while _may_hold allows."""
        st = state.raw if isinstance(state.raw, dict) else {}
        self.ww.observe(st)
        gs = st.get("game_state")
        day = gs.get("day") if isinstance(gs, dict) else None
        if day is None:                                   # the observation carries it too ("Day 2 | phase ...")
            m = _DAY_RE.search(str(st.get("observation") or ""))
            day = int(m.group(1)) if m else None
        me, term = st.get("your_position"), st.get("terminated_messaging")
        if day != self._chat_day:
            self._chat_day, self._chat_done, self._chat_sent, self._chat_said = day, False, 0, set()
            self._followups, self._win, self._chat_pending = 0, None, None
            # A restarted worker without its predecessor's saved memory (agent/matchstate.py) can join a window our seat
            # already chatted in: new_messages carries the whole window, so count and remember our own chats there (the
            # 5-chat quota and the no-repeat rule then hold across the restart). At a window's first call in one
            # process there are none.
            msgs = st.get("new_messages") if isinstance(st.get("new_messages"), list) else []
            for m in msgs if isinstance(me, int) and not isinstance(me, bool) else []:
                if isinstance(m, dict) and m.get("sender") == me and m.get("type", "chat") == "chat" \
                        and m.get("day", day) in (day, None):
                    self._chat_sent += 1
                    self._chat_said.add((clip_chat(m.get("content", "")), tuple(m.get("recipients") or ())))
                    self._echo = True
            self._chat_queue = self._ask_chat(st)
            self._chat_opening = len(self._chat_queue)
        self._settle_pending(st, me, day)
        if self.listen:
            self._listen_wait(st)                         # the window's clock starts at our first sight of it
        if isinstance(term, list) and isinstance(me, int) and me in term:
            self._chat_done = True
        if not self._chat_done and not self._chat_queue and getattr(self.ww, "reactive_chat", False):
            if not self.listen:
                if self._chat_sent:
                    self._chat_queue = self._ask_chat(st)
            elif self._followups < self._followup_cap(st):
                self._chat_queue = self._ask_chat(st)[:self._followup_cap(st) - self._followups]
        while not self._chat_done and self._chat_queue and self._chat_sent < MAX_CHATS_PER_WINDOW:
            m = self._chat_queue.pop(0)
            opening = self._chat_opening > 0
            self._chat_opening = max(0, self._chat_opening - 1)
            text = clip_chat(m.get("content", ""))
            rec = m.get("recipients") or None
            if rec is not None and (not isinstance(rec, list) or len(rec) != 1 or not isinstance(rec[0], int)
                                    or isinstance(rec[0], bool) or not 0 <= rec[0] <= 6 or rec[0] == me):
                continue                                  # a private message with a bad recipient is dropped, never broadcast
            if text and (text, tuple(rec or ())) not in self._chat_said:     # never the same message twice a day
                self._chat_said.add((text, tuple(rec or ())))
                self._chat_sent += 1
                if not opening:
                    self._followups += 1
                    if self.listen:
                        self.listen_stats["followups"] += 1
                self._chat_pending = {"day": day, "text": text, "rec": tuple(rec or ()), "opening": opening,
                                      "m": dict(m)}
                return SendMessage(text, recipients=rec)
        if self.listen and not self._chat_done and self._may_hold(st, me):
            return HOLD
        self._chat_done = True
        return TERMINATE_MESSAGING

    def _settle_pending(self, st: dict, me, day) -> None:
        """Did the chat we last handed the runner reach the server? Since PR #7 the runner does not end the match
        when a send_message fails for a temporary reason (a dropped connection, a 5xx, RUNTIME_TEMPORARILY_UNAVAILABLE,
        TOO_MANY_WAITS): it never resends, it re-reads the state and asks us again. `new_messages` carries the open
        window's messages, our own included ("You always see your own messages", 06-messaging.md), so a chat that
        went out is in the next state we are asked with. One that is missing there (its text not among our chats
        shown, and fewer of ours shown than we counted) did not land: it is un-counted (the 5-chat quota, the
        never-twice rule, the follow-up budget) and queued first again, so this call decides again with it, and the
        saved match state (written after this decision) does not count it either.

        Only once this match has shown one of our own chats back (`_echo`): if new_messages left our own chats out,
        every chat would look undelivered and be sent again. Before that first echo a chat stays counted, as before.
        The count never drops below the chats of ours the server shows (the quota is counted there): never a 6th chat
        (a refused chat ends the match process). Nothing to settle without a list of messages, on another day, or
        without a seat. Never raises."""
        p, self._chat_pending = self._chat_pending, None
        if p is None:
            return
        try:
            msgs = st.get("new_messages")
            if not isinstance(msgs, list) or p.get("day") != day or not isinstance(me, int) or isinstance(me, bool):
                return
            own = [m for m in msgs if isinstance(m, dict) and m.get("sender") == me and m.get("type", "chat") == "chat"
                   and m.get("day", day) in (day, None)]
            if own:
                self._echo = True
            key = (p.get("text"), tuple(p.get("rec") or ()))
            if not self._echo or len(own) >= self._chat_sent or any(
                    (clip_chat(m.get("content", "")), tuple(m.get("recipients") or ())) == key for m in own):
                return                # (as many of ours shown as counted: it landed, even if its text came back changed)
            # (never below what the server shows: the quota is counted there)
            self._chat_sent = max(len(own), self._chat_sent - 1)
            self._chat_said.discard(key)
            if p.get("opening"):
                self._chat_opening += 1
            else:
                self._followups = max(0, self._followups - 1)
                if self.listen:
                    self.listen_stats["followups"] = max(0, self.listen_stats["followups"] - 1)
            self._chat_queue.insert(0, dict(p.get("m") or {"content": key[0], "recipients": list(key[1]) or None}))
            self.undelivered += 1
            log.warning("our chat did not reach the server (not in the state read after it): deciding again, it is "
                        "not counted (%d chat(s) counted this window)", self._chat_sent)
        except Exception:
            log.debug("settling the last chat failed", exc_info=True)

    # ---- Werewolf listening (ARENA_WW_LISTEN) -------------------------------
    def _followup_cap(self, st: dict) -> int:
        """LISTEN_FOLLOWUPS, plus what the policy allows on top today (ARENA_WW_BLUFF: 1 on the day of its seer bluff;
        0 otherwise and whenever the switch is off). Never raises."""
        try:
            extra = self.ww.extra_followups(st) if hasattr(self.ww, "extra_followups") else 0
            return LISTEN_FOLLOWUPS + (1 if extra == 1 else 0)
        except Exception:
            return LISTEN_FOLLOWUPS

    def _may_hold(self, st: dict, me) -> bool:
        """Keep our part of the window open, i.e. not terminate yet: the window is open, we are alive, a follow-up
        (and chat quota for it) is left, some other living seat is still talking, and no time rule says stop."""
        if self._followups >= self._followup_cap(st) or self._chat_sent >= MAX_CHATS_PER_WINDOW:
            return False
        if st.get("phase") != "messaging" or st.get("is_terminal") or st.get("eliminated") \
                or st.get("messaging_enabled") is False:
            return False
        seat, alive = self.ww.seats(st)
        if not isinstance(me, int) or isinstance(me, bool) or seat != me or me not in alive:
            return False
        others = [s for s in alive if s != me]
        term = st.get("terminated_messaging")
        if not others or (isinstance(term, list) and all(s in term for s in others)):
            return False                                  # nobody else can still say anything
        return self._listen_wait(st) is not None

    def _listen_wait(self, st: dict) -> float | None:
        """Seconds to the next time-based stop (at most one poll), or None = stop now: the window closes within the
        margin, or no other seat has chatted for the quiet time. The clock is the state's own countdown
        (`messaging_seconds_left`); without one, WINDOW_S from our first sight of the window."""
        now = self._clock()
        if self._win is None:
            self._win = {"t0": now, "w": None, "quiet_from": None, "chats": 0}
        w = self._win
        left = st.get("messaging_seconds_left")
        counted = isinstance(left, (int, float)) and not isinstance(left, bool) and left == left and left >= 0
        left = float(left) if counted else WINDOW_S - (now - w["t0"])
        if w["w"] is None:
            w["w"], w["quiet_from"] = max(left, 1.0), left
        elif counted:                                     # a countdown that stopped moving cannot keep us here
            left = min(left, w["w"] - (now - w["t0"]) + 1.0)
        me = st.get("your_position")
        chats = sum(1 for m in st.get("new_messages") or [] if isinstance(m, dict)
                    and m.get("type", "chat") == "chat" and m.get("sender") != me)
        if chats > w["chats"]:
            w["chats"], w["quiet_from"] = chats, left
        margin = max(1.0, LISTEN_MARGIN * w["w"])
        until = min(left - margin, LISTEN_QUIET * w["w"] - (w["quiet_from"] - left))
        if until <= 0:
            return None
        return min(until, max(0.5, LISTEN_POLL * w["w"]))

    def _listen(self, state: GameState, context: DecisionContext):
        """Read the open window until message_step has an answer other than HOLD (a follow-up chat, or TERMINATE when
        a stop rule fires); TERMINATE_MESSAGING as well if the window closed meanwhile (the runner's terminate then
        meets WRONG_PHASE, a race it re-reads), if there is no runner session to read with, or on any error.
        Reads do not count as activity (werewolf.md §9); the stop rules end it well before the 120 s limits."""
        t0 = time.monotonic()
        self.listen_stats["holds"] += 1
        st = state
        try:
            game = _runner_game()
            if game is None:
                self.listen_stats["no_game"] += 1
                self._warn_once(("listen", "no runner"), "Werewolf listening: no runner session to read the window "
                                "with; ending our part of the discussion instead")
            else:
                while True:
                    wait = self._listen_wait(st.raw if isinstance(st.raw, dict) else {})
                    if wait is None:
                        break
                    t_wait = time.monotonic()
                    nxt = game.wait_for_update(since_version=st.state_version, since_message_seq=_seen_seq(st),
                                               since_is_current_actor=bool(st.is_current_actor), since_phase=st.phase,
                                               timeout_seconds=wait)
                    if not isinstance(nxt, GameState):
                        break
                    if time.monotonic() - t_wait < LISTEN_MIN_WAIT_S and nxt.state_version == st.state_version \
                            and _seen_seq(nxt) == _seen_seq(st) and nxt.phase == st.phase:
                        time.sleep(LISTEN_MIN_WAIT_S)     # nothing new, answered at once: never spin on the server
                    if nxt.is_terminal or nxt.phase != "messaging" or (nxt.raw or {}).get("eliminated"):
                        self.ww.observe(nxt.raw if isinstance(nxt.raw, dict) else {})
                        break
                    out = self.message_step(nxt, context)
                    if out is not HOLD:
                        return out
                    st = nxt
        except Exception:
            self.listen_stats["errors"] += 1
            log.warning("Werewolf listening failed; ending our part of the discussion", exc_info=True)
        finally:
            spent = time.monotonic() - t0
            self.last_listen_s = spent
            self.listen_stats["seconds"] += spent
            self.listen_stats["max_s"] = max(self.listen_stats["max_s"], spent)
        self._chat_done = True
        return TERMINATE_MESSAGING

    def _ask_chat(self, st: dict) -> list[dict]:
        try:
            return [m for m in list(self.ww.chat(st) or []) if isinstance(m, dict)][:MAX_CHATS_PER_WINDOW]
        except Exception:
            log.exception("chat policy crashed")
            return []

    # ---- the match across worker restarts (agent/matchstate.py) and its summary ----------------------------------
    def _bind(self, state, context, family: str, game: str) -> None:
        """Tie this instance to its match at its first decision. When a saved state of the same match applies (a worker
        restarted mid-match; matchstate.resumable), take it over: this instance then answers every later decision as
        the one that saved it would have. Handed another match (the SDK never does: one instance per match), start it
        with fresh memory. Never raises."""
        try:
            sid = str(getattr(context, "session_id", None) or "")
            aid = str(getattr(context, "agent_id", None) or "")
            if self._match == (aid, sid):
                return
            if self._match is not None and self._match[1] and sid and sid != self._match[1]:
                self._summary()
                self._fresh()
            self._match, self._family, self._game = (aid, sid), family, game
            self._seat = _seat_of(context, getattr(state, "raw", None))
            global _LAST
            _LAST = self
            if not family or not matchstate.enabled():
                return
            saved = matchstate.load(aid, sid, self._seat)
            if saved is None or not matchstate.resumable(saved, getattr(state, "raw", None), family, self._seat):
                return
            try:
                self._import(saved)
            except Exception:
                log.warning("saved match state unusable; playing on with fresh memory", exc_info=True)
                self._fresh()
                return
            self.resumed = list(saved.get("progress") or [])
            log.warning("resumed match %s after a worker restart: memory up to %s restored (%d decisions before)",
                        sid[:8], self.resumed, self.decisions)
        except Exception:
            log.warning("match binding failed", exc_info=True)

    def _fresh(self) -> None:
        self.ww, self.pk = WerewolfPolicy(), PokemonPolicy()
        self._chat_day, self._chat_queue, self._chat_done = _UNSET, [], False
        self._chat_sent, self._chat_said = 0, set()
        self._chat_opening, self._followups, self._win = 0, 0, None
        self._chat_pending, self._echo, self.undelivered = None, False, 0
        self.overruns = self.fallbacks = self.joint_repairs = self.decisions = self._resumes = self.doubles_turns = 0
        self.waits = self.refused_moves = 0
        self._last_raw = None
        self.resumed, self._summarized = None, False
        self._ra = None

    def _export(self, family: str, raw) -> dict:
        chat = {"day": None if self._chat_day is _UNSET else self._chat_day, "has_day": self._chat_day is not _UNSET,
                "queue": self._chat_queue, "done": self._chat_done, "sent": self._chat_sent, "said": self._chat_said,
                "opening": self._chat_opening, "followups": self._followups, "pending": self._chat_pending,
                "echo": self._echo, "undelivered": self.undelivered}
        out = {"game": family, "game_type": self._game, "seat": self._seat,
               "progress": matchstate.progress(raw, family), "t": round(time.time(), 3), "chat": chat,
               "counters": {"decisions": self.decisions, "overruns": self.overruns, "fallbacks": self.fallbacks,
                            "joint_repairs": self.joint_repairs, "doubles_turns": self.doubles_turns,
                            "refused_moves": self.refused_moves,
                            "resumes": self._resumes + (self.resumed is not None)}}
        if family == "pokemon":
            out["pk"] = self.pk.export_memory()
        elif family == "werewolf":
            out["ww"] = self.ww.export_memory()
        return out

    def _import(self, saved: dict) -> None:
        chat = saved.get("chat") or {}
        self._chat_day = chat.get("day") if chat.get("has_day") else _UNSET
        self._chat_queue = list(chat.get("queue") or [])
        self._chat_done = bool(chat.get("done"))
        self._chat_sent = int(chat.get("sent") or 0)
        self._chat_said = set(chat.get("said") or ())
        self._chat_opening = int(chat.get("opening") or 0)
        self._followups = int(chat.get("followups") or 0)
        self._chat_pending = dict(chat["pending"]) if isinstance(chat.get("pending"), dict) else None
        self._echo = bool(chat.get("echo"))
        self.undelivered = int(chat.get("undelivered") or 0)
        c = saved.get("counters") or {}
        self.decisions = int(c.get("decisions") or 0)
        self.overruns = int(c.get("overruns") or 0)
        self.fallbacks = int(c.get("fallbacks") or 0)
        self.joint_repairs = int(c.get("joint_repairs") or 0)
        self.doubles_turns = int(c.get("doubles_turns") or 0)
        self.refused_moves = int(c.get("refused_moves") or 0)
        self._resumes = int(c.get("resumes") or 0)
        if isinstance(saved.get("pk"), dict):
            self.pk.import_memory(saved["pk"])
        if isinstance(saved.get("ww"), dict):
            self.ww.import_memory(saved["ww"])

    def _after_decision(self, state, family: str, battle: bool = False) -> None:
        """Save the match's memory (unless an overrun policy call is still changing it: the next decision saves) and,
        after a battle turn, its live counters for the health line. Never raises."""
        self.decisions += 1
        self._last_raw = getattr(state, "raw", None)
        try:
            if not family or self._match is None or not matchstate.enabled():
                return
            aid, sid = self._match
            if self._busy is None or not self._busy.is_alive():
                matchstate.save(aid, sid, self._export(family, self._last_raw), seat=self._seat)
            if battle and family == "pokemon":
                matchstate.write_health(aid, sid, self.counters(), seat=self._seat)
        except Exception:
            log.debug("match state not saved", exc_info=True)

    def on_action_result(self, result, context) -> None:
        """The runner's report on each move it sent (altruagent.runner: the play_action answer, or {"error": code, ...}
        when the runner recovered from an error). Never raises: the runner ends the match on an exception here.

        Red Alert: the bot learns its verdicts from it (RedAlertPolicy.on_result); nothing else for a Red Alert match.

        Pokemon: a move the server refused (REFUSED_MOVE: a race such as STALE_STATE / NOT_YOUR_TURN, i.e. the
        decision had closed and the move timer played for us) never landed, so what its decision wrote about our
        action (Protect chains, last actions) goes back (PokemonPolicy.undelivered, which keeps it when an earlier
        unanswered send of the same point may have landed), and the saved match state with it (a restarted worker
        resumes from that). Only when the policy itself answered that decision: on any other path (busy, overrun,
        crash, fallback) the memory does not describe the move sent, and on the busy path it still belongs to an
        earlier decision. Any other error (a 5xx, a busy engine: the move may have landed) and a send that failed
        without an answer are left to the re-read state: the same decision point again restores that memory
        (PokemonPolicy._same_decision)."""
        try:
            if self._ra is not None and self._game == "red_alert":
                self._ra.on_result(result)
                return
        except Exception:
            log.debug("on_action_result failed", exc_info=True)
            return
        try:
            if not isinstance(result, dict) or result.get("error") not in REFUSED_MOVE or self._family != "pokemon":
                return
            if self._busy is not None and self._busy.is_alive():
                return                                    # an overrun policy call still owns the memory
            if str((self.last_decision or {}).get("path") or "").split("+")[0] not in ("policy", "repaired"):
                return                                    # the policy did not answer the refused decision
            if not self.pk.undelivered():
                return
            self.refused_moves += 1
            log.warning("our move was refused (%s): it is not remembered as played", str(result.get("error"))[:40])
            if self._match is not None and matchstate.enabled():
                aid, sid = self._match
                matchstate.save(aid, sid, self._export(self._family, self._last_raw), seat=self._seat)
        except Exception:
            log.debug("move result bookkeeping failed", exc_info=True)

    def counters(self) -> dict:
        """The match's battle-decision counters (health line, summary)."""
        ss = self.pk.search_stats
        machine = {k: v for k, v in (ss.get("reasons") or {}).items() if str(k).startswith(MACHINE_REASONS)}
        ms = sorted(self.pk.search_ms)
        return {"session": (self._match or ("", ""))[1][:8], "game": self._game, "mode": self.pk.battle_mode,
                "battle": self.doubles_turns, "used": ss.get("used", 0), "degraded": ss.get("degraded", 0),
                "machine": machine, "lite": bool(self.pk.search_lite), "fallbacks": self.fallbacks,
                "overruns": self.overruns, "ms_p50": ms[len(ms) // 2] if ms else None, "ms_max": ms[-1] if ms else None}

    def summary_line(self) -> str:
        """One line about the whole match: how its battle decisions were answered, search latency, engine health."""
        c = self.counters()
        ss = self.pk.search_stats
        head = f"[match-summary] session={c['session'] or '?'} game={self._game or '?'} decisions={self.decisions}"
        resumed = f"at {self.resumed}" if self.resumed is not None else "no"
        if self._game == "red_alert" and self._ra is not None:
            try:
                ra = self._ra.summary_text()
            except Exception:
                ra = "summary unavailable"
            return f"{head} {ra}"
        if self._family != "pokemon":
            waits = f" waits={self.waits}" if self.waits else ""        # (a real-time game answered with WAIT)
            waits += f" undelivered_chats={self.undelivered}" if self.undelivered else ""
            ls = self.listen_stats
            listen = (f" listen=on holds={ls['holds']} followups={ls['followups']} listen_s={ls['seconds']:.1f} "
                      f"max_s={ls['max_s']:.1f} listen_errors={ls['errors']} no_game={ls['no_game']}"
                      if self.listen and self._family == "werewolf" else "")
            return f"{head} fallbacks={self.fallbacks} overruns={self.overruns} resumed={resumed}{waits}{listen}"
        by_count = sorted((ss.get("reasons") or {}).items(), key=lambda kv: -kv[1])
        reasons = f" ({', '.join(f'{k} x{v}' for k, v in by_count)})" if by_count else ""
        # (only when it happened: a decision point asked again, a move the server refused)
        redo = f" redecided={self.pk.redecided}" if getattr(self.pk, "redecided", 0) else ""
        redo += f" refused_moves={self.refused_moves}" if self.refused_moves else ""
        return (f"{head} battle={c['battle']} search={c['mode']} used={c['used']} classic={c['battle'] - c['used']}"
                f"{reasons} degraded={c['degraded']}{' lite' if c['lite'] else ''} cut={ss.get('cut', 0)} "
                f"search_ms p50={c['ms_p50']} max={c['ms_max']} fallbacks={self.fallbacks} overruns={self.overruns} "
                f"repairs={self.joint_repairs}{redo} resumed={resumed} engine[{_engine_stats()}] calc[{_calc_stats()}]")

    def _summary(self) -> None:
        if self._summarized or not (self.decisions or self.waits):
            return
        self._summarized = True
        try:
            log.warning("%s", self.summary_line())
        except Exception:
            log.debug("match summary failed", exc_info=True)


_LAST: "Contestant | None" = None          # the instance playing this process's match (one per worker process)


def _engine_stats() -> str:
    try:
        from .pokemon.engine import SearchEngine
        e = SearchEngine._inst
        if e is None:
            return "not started"
        s = e.stats()
        return " ".join(f"{k}={s[k]}" for k in ("starts", "timeouts", "late", "busy", "hung", "denied", "slot"))
    except Exception:
        return "?"


def _calc_stats() -> str:
    try:
        from .pokemon.calc import DamageCalc
        d = DamageCalc._inst
        if d is None:
            return "not started"
        s = d.stats()
        return " ".join(f"{k}={s.get(k)}" for k in ("enabled", "starts", "errors", "timeouts"))
    except Exception:
        return "?"


@atexit.register
def _summary_at_exit() -> None:
    """A match worker exits when its match ends (or fails): the summary of the match it played. ARENA_SUMMARY=0: off."""
    if os.environ.get("ARENA_SUMMARY", "1") != "0" and _LAST is not None:
        _LAST._summary()


def game_kind(game_type, raw=None) -> tuple[str, str]:
    """(family, game type) of a decision: family "pokemon" / "werewolf" / "" (unknown), the game type lower-cased with
    a "pokemon_" prefix. The platform's names ("pokemon_vgc_doubles_draft", "werewolf") come back as they are; another
    spelling ("Pokemon_VGC_Doubles_Draft", "pokemon", none) is recognised by name or by the state's shape (its
    legal actions: draft picks, a doubles turn, a lineup, moves / switches; a seat and a day for Werewolf) instead of
    ending in a whole match of safe-action play. A Pokemon name without a format is the tournament's VGC doubles
    draft unless the state is a singles battle. Never raises."""
    try:
        g = re.sub(r"[\s-]+", "_", str(game_type or "").strip().lower())
        raw = raw if isinstance(raw, dict) else {}
        if "werewolf" in g:
            return "werewolf", "werewolf"
        acts = [a for a in ((raw.get("legal_actions") or {}).get("actions") or []) if isinstance(a, dict)] \
            if isinstance(raw.get("legal_actions"), dict) else []
        obs = raw.get("observation")
        kinds = {str(((a.get("input") or {}).get("action") or {}).get("type") or "") for a in acts}
        ids = [str(a.get("action_id") or "") for a in acts if not gives_up(a)]
        pokemon_shape = bool(kinds & {"draft_pick", "doubles_turn", "select_lineup", "submit_team", "move", "switch"}) \
            or (bool(ids) and all(i.startswith(("draft_pick:", "move:", "switch:")) for i in ids))
        if not (g.startswith("pokemon") or "vgc" in g or pokemon_shape):
            if g in ("", "unknown", "none") and "your_position" in raw and (
                    isinstance(raw.get("game_state"), dict) or _DAY_RE.search(str(obs or ""))):
                return "werewolf", "werewolf"
            return "", g
        rest = g[len("pokemon"):].strip("_") if g.startswith("pokemon") else g
        canon = f"pokemon_{rest}" if rest else "pokemon"
        tokens = canon.split("_")
        if "vgc" in canon or "doubles" in canon or any(t.startswith("gen") or t in _SINGLES_WORDS for t in tokens):
            return "pokemon", canon
        obs = obs if isinstance(obs, dict) else {}
        singles = obs.get("is_doubles") is False or raw.get("phase") == "teambuild" or (
            bool(ids) and all(i.startswith(("move:", "switch:")) for i in ids))
        return "pokemon", ("pokemon_singles" if singles else "pokemon_vgc_doubles_draft")
    except Exception:
        return "", str(game_type or "")


_SINGLES_WORDS = {"ou", "ubers", "uu", "random", "randombattle", "same", "singles", "teambuild"}


def _realtime(state) -> bool:
    """A real-time game's state: `pacing.mode == "realtime"`, the very signal the runner keys its real-time path on
    (altruagent.runner._is_realtime: WAIT allowed, a refused batch not fatal). Pokemon and Werewolf states carry no
    `pacing` at all. Never raises."""
    try:
        raw = getattr(state, "raw", None)
        pacing = raw.get("pacing") if isinstance(raw, dict) else None
        return isinstance(pacing, dict) and pacing.get("mode") == "realtime"
    except Exception:
        return False


_RA_NAMES = {"red_alert", "redalert", "red_alert_1v1", "ra", "openra", "openra_ra"}


def _is_red_alert(state, context) -> bool:
    """A Red Alert decision: its game type says so (any spelling: "red_alert", "Red Alert", "red-alert", "redalert"),
    or, with no known game type, a real-time / windows-mode state shaped like Red Alert's (an observation with
    `available_production` or `economy`). Pokemon and Werewolf never match. Never raises."""
    try:
        raw = getattr(state, "raw", None)
        raw = raw if isinstance(raw, dict) else {}
        for name in (getattr(context, "game_type", None), getattr(state, "game_name", None), raw.get("game_type")):
            g = re.sub(r"[\s-]+", "_", str(name or "").strip().lower())
            if g in _RA_NAMES:
                return True
            if g and g not in ("unknown", "none"):
                return False
        pacing = raw.get("pacing")
        obs = raw.get("observation")
        return isinstance(pacing, dict) and pacing.get("mode") in ("realtime", "windows") and \
            isinstance(obs, dict) and ("available_production" in obs or "economy" in obs)
    except Exception:
        return False


def _ra_noop(raw: dict):
    """Windows mode's last resort when the bot could not answer: one harmless order that is valid on any live state
    (power on a building that is not powered down: `ok already_on`, no engine command; else re-set a unit's stance),
    or WAIT when there is nothing at all to name."""
    try:
        obs = raw.get("observation") if isinstance(raw.get("observation"), dict) else {}
        for b in obs.get("buildings") or []:
            if isinstance(b, dict) and isinstance(b.get("id"), int) and not isinstance(b.get("id"), bool) \
                    and not b.get("powered_down"):
                return {"type": "orders", "orders": [{"cmd": "power_toggle", "building": b["id"], "on": True}]}
        for u in obs.get("units") or []:
            if isinstance(u, dict) and isinstance(u.get("id"), int) and not isinstance(u.get("id"), bool) \
                    and not u.get("wreck"):
                st = u.get("stance") if u.get("stance") in ("hold_fire", "return_fire", "defend",
                                                            "attack_anything") else "defend"
                return {"type": "orders", "orders": [{"cmd": "set_stance", "units": [u["id"]], "stance": st}]}
    except Exception:
        pass
    return WAIT


def _seat_of(context, raw) -> int | None:
    """Our seat in the match, when known: the granted / claimed seat (context.seat_position: the official runtime and
    claimed Testing seats; the same for every worker of the seat), else the seat a seat-based game shows us
    (`your_position`: Werewolf). None for a Pokemon match found by discovery (its draft keys the two sides by agent id,
    so one agent never holds both)."""
    for v in (getattr(context, "seat_position", None), raw.get("your_position") if isinstance(raw, dict) else None):
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            return v
    return None


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
        tmpl = action_template((raw.get("legal_actions") or {}).get("actions"), "select_lineup") or {}
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
    """The doubles_turn template (per-slot options) of a state, wherever its legal actions list it, or None."""
    try:
        return action_template(raw["legal_actions"]["actions"], "doubles_turn")
    except (KeyError, IndexError, TypeError, AttributeError):
        return None


def _doubles_only(raw: dict, tmpl: dict) -> bool:
    """The state's one legal action is a doubles_turn template (every recorded platform doubles turn is): then only a
    doubles_turn answer can be legal."""
    try:
        return (tmpl.get("type") or "doubles_turn") == "doubles_turn" and len(raw["legal_actions"]["actions"]) == 1
    except (KeyError, TypeError, AttributeError):
        return False


def _unfilled(out, tmpl: dict) -> bool:
    """`out` is the doubles template itself (its LegalAction or its action dict): the runner would send it unfilled."""
    try:
        inp = out.get("input") if isinstance(out, dict) else getattr(out, "input", None)
        act = inp.get("action") if isinstance(inp, dict) else None
        return is_template(out) or (isinstance(act, dict) and act == tmpl)
    except Exception:
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
    switch-in twice, no `pass` while a switch-in is still free for that slot: fallback.joint_problems). An answer to a
    state without such a template is never ok."""
    try:
        tmpl = _doubles_template(raw)
        if tmpl is None:
            return False
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
