"""Entry point for `python -m agent`.

One way to run your agent: set `ALTRUAGENT_OFFICIAL_AGENT_KEY` to your
Official Agent Key (from the tournament dashboard) and run

    python -m agent --tournament          # your tournament games
    python -m agent --match               # your test matches (Testing page)
    python -m agent --tournament --match  # both, in one process

It authenticates as your registered self-hosted agent and keeps one worker
process per assigned game of the chosen kind(s) until Ctrl+C
(`altruagent.supervisor.run_tournament_forever`, which filters assignments
on their `context`). Nobody copies an id and nobody claims anything. A game
of the other kind is not played; the runtime says once per game that it is
waiting. `--check-tournament` verifies the connection and the agent without
playing. `--agent MODULE[:FACTORY]` picks a different factory than
`agent.agent:create_agent`.

Retired modes only print a notice pointing to the command above (see
`altruagent.notices`):

- `python -m agent` with no mode: the old platform API-key mode
  (`ALTRUAGENT_API_KEY`), turned off on the platform.
- `--claim seatclaim_...` (and `ALTRUAGENT_CLAIM_TOKEN`): Testing claim
  codes, replaced by the Official Agent Key.

Deployment hardening on top of the starter (none of it edits `altruagent/`;
see docs/deploy.md "看门脚本和 agent 在上游运行时之上额外做了什么"). The SDK
itself now keeps the runtime through a temporary sign-in failure and an
incomplete registration (updated Official Rules), backs off a seat whose
worker keeps failing, retries a game's temporary failures inside the game
(up to 90 s, never resending a move) and replaces a worker that never reached
its game at once while the connect window is open (upstream 1372ccc, 4cb083a,
PR #7); what is added here:

- supervisor tuning from ARENA_* knobs (`_tournament_options`): a 10 s
  assignment poll and a 10 s base cooldown before a failed seat's next worker
  (the SDK: 60 s, a lost battle turn after one crash); the SDK's per-seat
  back-off still doubles from there, but only up to 30 s instead of 10
  minutes (`_tournament_tuning`, ARENA_SEAT_BACKOFF_MAX_S), so a tournament
  seat whose first workers fail still connects inside its 4 min window;
- the first authentication waits out a temporary failure (the control plane
  down or rate limiting at the moment the runtime starts, a session the
  platform failed to mint) with a 5/10/20/30 s back-off instead of exiting
  (`_authenticate`); an incomplete registration starts the runtime anyway
  (the SDK's supervisor says what to do and keeps checking, as upstream's
  start); a refused key still ends the process at once;
- scripts/run_agent.sh can hand the runtime a per-run-loop execution id, so a
  restarted runtime resumes its own seat leases at once
  (`_take_execution_id`, `_tournament_tuning`);
- a hot standby (a second runtime of the same key, ARENA_NEW_SEAT_DELAY_S)
  plays only the seats no other runtime holds (`_HotStandby`);
- `python -m agent` run by anything but scripts/run_agent.sh gets the
  platform's control-plane URL when none is configured and says loudly when
  this machine would play degraded: no damage calculator, no engine search
  (`_startup`).

This file is deliberately thin — the assignment loop and worker lifecycle
live in `altruagent.supervisor`, one seat's setup in `altruagent.worker`,
and one game's play loop in `altruagent.runner`.

IMPORTANT (Windows multiprocessing): the `if __name__ == "__main__":` guard
at the bottom of this file is not just style — `multiprocessing`'s `spawn`
start method (required on Windows, used here unconditionally so behavior is
identical everywhere) re-imports this exact module in every worker process.
Without the guard, each freshly-spawned worker would re-run `main()` itself
and spawn further workers recursively.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import Callable

import altruagent.supervisor as _sdk_supervisor
from altruagent.agent_loader import DEFAULT_AGENT_SPEC
from altruagent.agent_loader import load_agent_factory as _load_agent_factory
from altruagent.errors import AltruAgentError, AuthenticationError, ConfigurationError, PlatformError
from altruagent.notices import AGENT_GUIDE_URL, CLAIM_CODES_RETIRED_NOTICE, PLATFORM_KEY_RETIRED_NOTICE
from altruagent.official import OfficialAgentClient, OfficialAgentError, is_fatal_auth_error, is_registration_incomplete
from altruagent.runner import _resolve_decision_fn
from altruagent.supervisor import ALL_KINDS, TESTING, TOURNAMENT, WAITING_MESSAGE, run_tournament_forever


CLAIM_TOKEN_ENV = "ALTRUAGENT_CLAIM_TOKEN"
DEFAULT_CONTROL_URL = "https://api.altruagent-game.com"   # the platform's control plane (.env.example)


def _retired(notice: str) -> int:
    """Print a retired mode's notice and fail: nothing was run."""
    print(notice)
    return 1


# What the runtime says it plays, by the kinds the flags chose.
PLAYING_MESSAGES = {
    ALL_KINDS: "Playing your test matches and tournament games.",
    frozenset({TOURNAMENT}): "Playing your tournament games only. Add --match to also play your test matches.",
    frozenset({TESTING}): "Playing your test matches only. Add --tournament to also play your tournament games.",
}


# ---- supervisor tuning ------------------------------------------------------------------------------------------------
_TOURNAMENT_ENV = (
    # (environment variable, run_tournament_forever keyword, smallest accepted value, what `python -m agent` sets)
    ("ARENA_DISCOVERY_S", "poll_interval", 2.0, "10"),
    ("ARENA_WORKER_COOLDOWN_S", "cooldown_seconds", 1.0, "10"),
)


def _env_number(env, name: str, low: float) -> float | None:
    raw = str(env.get(name) or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not value >= low:                          # also rejects nan
        print(f"Ignoring {name}={raw!r}: expected a number >= {low:g}.")
        return None
    return value


def _tournament_options(env=None) -> dict:
    """Optional run_tournament_forever tuning from the environment: ARENA_DISCOVERY_S = the assignment poll (SDK
    default 10 s), ARENA_WORKER_COOLDOWN_S = the wait before a seat whose worker ended gets a new one (SDK default
    60 s: after a worker crash, e.g. the runner's kill on an invalid action, a whole 55 s battle turn). The SDK doubles
    that cooldown with each failure of the same seat in a row (a finished match or seat_busy resets it) up to a cap
    that _tournament_tuning sets to 30 s (the SDK: 10 minutes), so 10 s is only the first retry. Unset = the SDK
    defaults; scripts/run_agent.sh and `python -m agent` itself (`_startup`) set both to 10 s. Bad values are ignored
    with a message, never fatal."""
    env = os.environ if env is None else env
    out: dict = {}
    for name, key, low, _ in _TOURNAMENT_ENV:
        value = _env_number(env, name, low)
        if value is not None:
            out[key] = value
    return out


# ---- the first authentication -----------------------------------------------------------------------------------------
# The platform has answered a failed session mint (its own sign-in service rejecting or rate-limiting the request) with
# 401 invalid_official_agent_key and a detail starting with this: a temporary platform problem, not a key problem.
# (The SDK's supervisor treats it the same way once the runtime runs: altruagent.official.is_fatal_auth_error.)
_MINT_FAILURE_PREFIX = "Failed to mint agent session"


def _mint_failure(exc: BaseException) -> bool:
    return (getattr(exc, "error_code", None) == "invalid_official_agent_key"
            and str(getattr(exc, "detail", None) or "").startswith(_MINT_FAILURE_PREFIX))


def _transient(exc: BaseException) -> bool:
    """A failure of the FIRST authentication that waiting fixes: rate limiting, a 5xx or gateway blip, a timeout, the
    network, a session the platform failed to mint. An incomplete registration is not one of these but does not end
    the start either (see _authenticate); anything else (a rejected or revoked key, any other 4xx, an answer without a
    status) ends the start at once, as upstream's start does. Once the runtime runs, the SDK's supervisor classifies
    re-authentication failures itself (is_fatal_auth_error, is_registration_incomplete)."""
    status = getattr(exc, "status_code", None)
    if getattr(exc, "error_code", None) == "rate_limited" or status in (408, 425, 429):
        return True
    if isinstance(status, int) and status >= 500:
        return True
    if _mint_failure(exc):
        return True
    return status is None and isinstance(exc, PlatformError)       # could not reach the control plane at all


STARTUP_RETRY_S = (5.0, 10.0, 20.0, 30.0)   # capped at 30 s: plays within ~30 s of the control plane coming back
_sleep = time.sleep                        # (tests replace it)


def _authenticate(official) -> bool:
    """The runtime's first authentication; True = go on and run. A transient failure (the control plane down or rate
    limiting at the moment the runtime starts, e.g. a restart during an outage) is waited out with a back-off instead
    of ending the process (upstream's start exits at once; scripts/run_agent.sh would then restart it, noisily). An
    incomplete registration (403 registration_incomplete, e.g. updated Official Rules to accept again) does not end it
    either, as upstream's start since PR #7: the key is fine and only the contestant can finish the registration, so
    the runtime starts without a session and the SDK's supervisor says what to do at its first poll (then at most
    every 5 minutes), checks again every 30 s and plays as soon as it is done (a restart loop in run_agent.sh would
    only add noise). A refused key ends it (False)."""
    attempt = 0
    while True:
        try:
            official.authenticate()
            print("Connected with your Official Agent Key.")
            return True
        except AltruAgentError as exc:
            if is_registration_incomplete(exc):
                return True
            if not _transient(exc):
                print(f"Could not connect with your Official Agent Key: {exc}")
                return False
            delay = STARTUP_RETRY_S[min(attempt, len(STARTUP_RETRY_S) - 1)]
            attempt += 1
            print(f"Could not connect with your Official Agent Key yet ({exc}); retrying in {delay:.0f}s.", flush=True)
            _sleep(delay)


# ---- execution id and seat_busy retry (scripts/run_agent.sh) ----------------------------------------------------------
EXECUTION_ID_ENV = "ARENA_EXECUTION_ID"
_EXECUTION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
SEAT_BUSY_RETRY_ENV = "ARENA_SEAT_BUSY_RETRY_S"
NEW_SEAT_DELAY_ENV = "ARENA_NEW_SEAT_DELAY_S"          # a hot standby's hold on new seats (see _HotStandby)
SEAT_BACKOFF_MAX_ENV = "ARENA_SEAT_BACKOFF_MAX_S"
# The longest wait before a new worker for a seat whose workers keep failing. The SDK doubles a failed seat's cooldown
# with each failure in a row, up to its SEAT_FAILURE_BACKOFF_MAX_SECONDS (600 s); a finished match, seat_busy or the
# seat leaving the listing resets the count. Review 10-07 measured at our 10 s cooldown and poll (SDK 5bc61a2): 5
# workers failing before a tournament seat connects put the 6th start at 360 s, past the 240 s connect window (a
# no-show), and a match whose worker crashes again and again waited 160 s, then 320 s: longer than Werewolf's 120 s
# idle clock and a Pokemon decision's 50 s. Capped at 30 s the starts are 0, 20, 50, 90, 130, 170, 210 s, and a seat
# whose game the server lost (SESSION_NOT_FOUND until the platform drops it) still costs at most one worker and one
# sign-in every 30 s (the platform allows 30 sign-ins a minute per address).
# Since PR #7 (SDK 951f4f7) the SDK handles the commonest pre-connect failures itself: a temporary grant failure is
# retried inside the worker for 60 s, and a worker that never reached its game for a temporary reason
# (EXIT_NOT_CONNECTED) is replaced in the same poll, without counting, while the connect window is open (at least
# 5 minutes). The cap still bounds every other failure (an agent or platform error before or during the game: exit 1
# / 2 / a kill). The same constant is now also the SDK's "long run" threshold: a worker that ran at least this long
# before failing (any exit but EXIT_NOT_CONNECTED) starts a new count, i.e. waits the 10 s base cooldown again. At 30 s
# that means a worker that played for 30 s or more and then crashed is back after 10 s each time (with 600 s it was
# 10, 20, 30 s): a few seconds less lost in a live match, the same one sign-in per worker (the supervisor hands its
# session to each worker), and a deterministic crash still costs only one worker per ~40 s.
SEAT_BACKOFF_MAX_S = 30.0


def _take_execution_id(env=None, parent_pid: int | None = None) -> str | None:
    """The execution id scripts/run_agent.sh hands this runtime: one random value per run loop, kept across the
    loop's restarts of `python -m agent`, so a restarted runtime still owns its seats' execution leases and resumes
    them at once instead of getting seat_busy for up to 30 s and then waiting SEAT_BUSY_RETRY_SECONDS (35 s). It is
    removed from the environment here (no worker or node child inherits it; the workers get it from the supervisor)
    and honoured only when ARENA_EXECUTION_ID_OWNER is this process's parent, the run loop that generated it: an id
    copied into a .env or onto another machine would let two runtimes hold one lease and both play the seat."""
    env = os.environ if env is None else env
    value = str(env.pop(EXECUTION_ID_ENV, None) or "").strip()
    owner = str(env.pop(EXECUTION_ID_ENV + "_OWNER", None) or "").strip()
    if not value:
        return None
    if not _EXECUTION_ID_RE.match(value) or owner != str(os.getppid() if parent_pid is None else parent_pid):
        print(f"Ignoring {EXECUTION_ID_ENV}: only scripts/run_agent.sh may set it; this runtime uses a fresh one.")
        return None
    return value


def _tournament_tuning(env=None) -> tuple[Callable[[], None], str]:
    """Apply this runtime's execution id, ARENA_SEAT_BUSY_RETRY_S and the per-seat back-off cap
    (ARENA_SEAT_BACKOFF_MAX_S, default SEAT_BACKOFF_MAX_S = 30 s) to the SDK's supervisor module (its module-level name
    and constants, read when the runtime starts / on every seat_busy / whenever a failed worker is reaped;
    altruagent/ itself is not edited).
    The id is the run loop's (see _take_execution_id) or, as upstream, a fresh one for this process, fixed here so
    that the standby's seat check (_HotStandby) uses the very id the runtime's workers use. Returns the function that
    restores all three, and the id."""
    env = os.environ if env is None else env
    saved = (_sdk_supervisor.new_execution_id, _sdk_supervisor.SEAT_BUSY_RETRY_SECONDS,
             _sdk_supervisor.SEAT_FAILURE_BACKOFF_MAX_SECONDS)
    execution_id = _take_execution_id(env) or saved[0]()
    _sdk_supervisor.new_execution_id = lambda: execution_id     # (called once per runtime, when it starts)
    retry = _env_number(env, SEAT_BUSY_RETRY_ENV, 5.0)
    if retry is not None:
        _sdk_supervisor.SEAT_BUSY_RETRY_SECONDS = retry
    cap = _env_number(env, SEAT_BACKOFF_MAX_ENV, 1.0)         # (below the cooldown = a fixed cooldown: the SDK's max())
    _sdk_supervisor.SEAT_FAILURE_BACKOFF_MAX_SECONDS = SEAT_BACKOFF_MAX_S if cap is None else cap

    def restore() -> None:
        (_sdk_supervisor.new_execution_id, _sdk_supervisor.SEAT_BUSY_RETRY_SECONDS,
         _sdk_supervisor.SEAT_FAILURE_BACKOFF_MAX_SECONDS) = saved
    return restore, execution_id


# ---- hot standby ------------------------------------------------------------------------------------------------------
class _HotStandby:
    """The official client's assignments() as run_tournament_forever calls it, for a hot standby only (hold_new_s > 0,
    ARENA_NEW_SEAT_DELAY_S; scripts/run_agent.sh sets 20 s for role=standby): the SDK gets only the seats no other
    runtime holds.

    The platform gives a seat to whichever runtime claims it first, and the primary claims a new seat within one poll
    (10 s), so the standby leaves a NEW seat alone for hold_new_s after it first lists it; from then on it asks at
    every poll whether the seat is free by requesting its grant with this runtime's execution id (`grant`,
    `execution_id`): seat_busy while the primary's lease lives, and once that lease lapses (the primary stopped
    renewing: ~30 s) the grant takes the seat and the SDK gets it at once. Without that check the SDK's own worker
    would find seat_busy and retry only SEAT_BUSY_RETRY_SECONDS (35 s) later, with a new worker process and a new agent
    session each time: a takeover of up to ~70 s (a lost 55 s battle turn) and steady authentication traffic on the
    key both runtimes share. Any unexpected error of the check hands the seat over (the SDK's worker then decides). A
    seat once handed to the SDK is never held back again, even after a listing that missed it (a seat missing from two
    listings in a row gets its worker stopped). The listing's own errors pass through untouched: the SDK's supervisor
    handles them (a temporary sign-in failure pauses the listing, a refused key ends the runtime).

    `install(official)` puts it in place of the client's own assignments (an instance attribute), so the SDK gets the
    very client object it always got; everything else about the client is untouched."""

    def __init__(self, list_assignments: Callable[[], list], *, now: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = print, hold_new_s: float = 0.0,
                 grant: Callable[[str, str], object] | None = None, execution_id: str | None = None):
        self._list = list_assignments
        self._now = now
        self._log = log
        self.hold_new_s = max(0.0, float(hold_new_s or 0.0))
        self._grant = grant                # the client's grant(seat_id, execution_id)
        self._execution_id = execution_id  # this runtime's: the one its workers use
        self._first_seen: dict = {}        # seat_id -> when this runtime first listed it
        self._handed: set = set()          # seat_ids already handed to the SDK
        self._told: set = set()            # seat_ids whose first failed check was logged

    @classmethod
    def install(cls, official, **kwargs) -> "_HotStandby":
        standby = cls(official.assignments, grant=getattr(official, "grant", None), **kwargs)
        official.assignments = standby
        return standby

    def __call__(self):
        rows = self._list()
        return self._hold_back(rows) if self.hold_new_s else rows

    def _hold_back(self, rows: list) -> list:
        now = self._now()
        listed = {getattr(r, "seat_id", None) for r in rows}
        # A seat held back and no longer listed is forgotten; a seat once handed stays handed (a few ids per match): a
        # listing that misses it once must not hold it back again, or the SDK would stop its worker as unassigned.
        for seat_id in [s for s in self._first_seen if s not in listed and s not in self._handed]:
            del self._first_seen[seat_id]
            self._told.discard(seat_id)
        out = []
        for r in rows:
            seat_id = getattr(r, "seat_id", None)
            if not seat_id or seat_id in self._handed:
                out.append(r)
                continue
            if seat_id not in self._first_seen:
                self._first_seen[seat_id] = now
                self._log(f"[agent] standby: new seat {seat_id}; leaving it to the primary for {self.hold_new_s:.0f}s, "
                          "then playing it only when no other runtime holds its lease")
            if now - self._first_seen[seat_id] >= self.hold_new_s and self._free(seat_id):
                self._handed.add(seat_id)
                out.append(r)
        return out

    def _free(self, seat_id: str) -> bool:
        """Whether the standby should play `seat_id` now: its grant with this runtime's execution id succeeds (no
        other runtime holds the seat's lease; this runtime now does, and its worker's own grant keeps it)."""
        if self._grant is None or not self._execution_id:
            return True                    # (no check possible: the SDK's worker finds out, seat_busy and all)
        try:
            self._grant(seat_id, self._execution_id)
        except AltruAgentError as exc:     # seat_busy (the primary plays it), the match ended, a transient failure
            if seat_id not in self._told:
                self._told.add(seat_id)
                self._log(f"[agent] standby: seat {seat_id} is held by another runtime (the primary); taking it over "
                          "once its lease lapses" if getattr(exc, "error_code", None) == "seat_busy" else
                          f"[agent] standby: seat {seat_id} not taken ({exc}); checking again at the next poll")
            return False
        except Exception as exc:  # noqa: BLE001 - a fault here must never leave a seat unplayed
            self._log(f"[agent] standby: checking seat {seat_id} failed ({exc!r}); handing it to its worker")
            return True
        self._log(f"[agent] standby: seat {seat_id} is free (no other runtime holds its lease): playing it")
        return True


# ---- the runtime ------------------------------------------------------------------------------------------------------
def _run_tournament(agent_spec: str, kinds: frozenset[str] = ALL_KINDS) -> int:
    """The runtime: authenticate with the Official Agent Key, then keep one
    worker process per assigned game of the chosen ``kinds`` (test matches,
    tournament games or both) until Ctrl+C.
    """
    try:
        _load_agent_factory(agent_spec)  # validated eagerly; built once per match, in its worker
    except ValueError as exc:
        print(f"Startup error: {exc}")
        return 1

    try:
        official = OfficialAgentClient()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}")
        return 1

    restore, execution_id = _tournament_tuning()
    try:
        if not _authenticate(official):
            return 1
        print(PLAYING_MESSAGES[kinds])
        print(f"{WAITING_MESSAGE} (Press Ctrl+C to stop.)", flush=True)
        hold = _env_number(os.environ, NEW_SEAT_DELAY_ENV, 0.0) or 0.0
        if hold:
            _HotStandby.install(official, hold_new_s=hold, execution_id=execution_id)
        run_tournament_forever(official, agent_spec=agent_spec, kinds=kinds, **_tournament_options())
        return 0
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    except AltruAgentError as exc:
        print(f"\nStopped due to an unrecoverable error: {exc}")
        return 1
    finally:
        restore()
        official.close()


def _mark(symbol: str, fallback: str) -> str:
    """``symbol`` if the console can print it (a redirected Windows console may not)."""
    try:
        symbol.encode(sys.stdout.encoding or "ascii")
        return symbol
    except (UnicodeEncodeError, LookupError):
        return fallback


def _check_tournament(agent_spec: str) -> int:
    """Pre-tournament connection check. Needs no assigned match; never prints
    the key or any token. Returns 0 only if every step passes.
    """
    ok_mark, fail_mark = _mark("✓", "[ok]"), _mark("✗", "[FAIL]")

    def ok(message: str) -> None:
        print(f"{ok_mark} {message}")

    def fail(message: str) -> int:
        print(f"{fail_mark} {message}")
        return 1

    try:
        official = OfficialAgentClient()
    except ConfigurationError as exc:
        return fail(str(exc))

    try:
        try:
            official.authenticate()
        except OfficialAgentError as exc:
            ok("Control plane reachable")
            if exc.status_code in (400, 401) and is_fatal_auth_error(exc):
                return fail(f"Official Agent Key rejected: {exc}")
            return fail(f"Official agent authentication failed: {exc}")
        except AltruAgentError as exc:
            return fail(f"Control plane not reachable at {official.control_url}: {exc}")
        ok("Control plane reachable")
        ok("Official Agent Key accepted")

        try:
            assignments = official.assignments()
        except AuthenticationError as exc:
            return fail(f"Tournament agent session was not accepted: {exc}")
        except AltruAgentError as exc:
            ok("Tournament agent authenticated")
            return fail(f"Assignment discovery failed: {exc}")
        ok("Tournament agent authenticated")
        ok(f"Assignment discovery available ({len(assignments)} active assignment(s))")

        try:
            _resolve_decision_fn(_load_agent_factory(agent_spec)())
        except Exception as exc:  # noqa: BLE001 - contestant code
            return fail(f"Agent {agent_spec} could not be created: {exc}")
        ok(f"Agent ready ({agent_spec})")
        ok("Ready to play Testing and tournament games")
        return 0
    finally:
        official.close()


_DESCRIPTION = """\
Run your AltruAgent agent.

  1. Set ALTRUAGENT_OFFICIAL_AGENT_KEY (in .env) to your Official Agent Key,
     generated on the tournament dashboard's Agent Configuration page.
  2. Run it with --tournament (your tournament games), --match (your test
     matches) or both, and leave it running.

It plays each game with agent/agent.py's create_agent(). While it waits for a
game it uses no AI tokens (it checks every ~10 s). Your agent must be
Self-hosted and your event registration complete.

This repository's agent/agent.py is our contestant (Pokémon VGC draft and
Werewolf; Red Alert, a real-time game it has no policy for, is answered with
WAIT). scripts/run_agent.sh keeps it running with both flags."""

_EPILOG = f"""examples:
  python -m agent --check-tournament                   check your key, connection and agent
  python -m agent --tournament                         play your tournament games (Ctrl+C to stop)
  python -m agent --match                              play your test matches
  python -m agent --tournament --match                 play both in one process
  bash scripts/run_agent.sh start                      the same, kept running (restarts, logs, health)
  python -m agent --check-tournament --agent examples.llm_agent
                                                       check the LLM example agent

guide: {AGENT_GUIDE_URL}"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agent",
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # --tournament and --match may be combined (both kinds in one process);
    # each excludes --check-tournament and --claim. --match's exclusions are
    # checked in main(): argparse puts an option in one exclusive group only.
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--tournament",
        action="store_true",
        help=(
            'play your tournament games (Swiss/bracket games, after you press "Register my agent"); '
            "runs until Ctrl+C"
        ),
    )
    parser.add_argument(
        "--match",
        action="store_true",
        help=(
            'play your test matches (Testing page: matches you create with "Mine (self-hosted)" '
            "or join from Open matches); runs until Ctrl+C"
        ),
    )
    mode.add_argument(
        "--check-tournament",
        action="store_true",
        help="check your Official Agent Key, connection and agent without playing anything",
    )
    # Retired: Testing claim codes. Still parsed so an old command prints the
    # notice instead of an argparse error; hidden from --help.
    mode.add_argument("--claim", nargs="?", const="", metavar="TOKEN", help=argparse.SUPPRESS)
    parser.add_argument(
        "--agent",
        metavar="MODULE[:FACTORY]",
        help=f"agent factory to use (default: {DEFAULT_AGENT_SPEC})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args([] if argv is None else argv)

    if args.match and args.check_tournament:
        parser.error("argument --match: not allowed with argument --check-tournament")
    if args.match and args.claim is not None:
        parser.error("argument --match: not allowed with argument --claim")

    if args.check_tournament:
        return _check_tournament(args.agent or DEFAULT_AGENT_SPEC)
    if args.tournament or args.match:
        kinds = frozenset(kind for kind, chosen in ((TOURNAMENT, args.tournament), (TESTING, args.match)) if chosen)
        return _run_tournament(args.agent or DEFAULT_AGENT_SPEC, kinds)

    if args.claim is not None:
        return _retired(CLAIM_CODES_RETIRED_NOTICE)
    if args.agent is not None:
        parser.error("--agent is only supported together with --tournament, --match or --check-tournament")
    if os.environ.get(CLAIM_TOKEN_ENV):
        return _retired(CLAIM_CODES_RETIRED_NOTICE)
    return _retired(PLATFORM_KEY_RETIRED_NOTICE)


# ---- `python -m agent` without scripts/run_agent.sh -------------------------------------------------------------------
def _control_url_default(env=None) -> str | None:
    """ALTRUAGENT_CONTROL_URL for a runtime given only its key (a harness that injects the key and runs `python -m
    agent`, a bundle's `export ALTRUAGENT_OFFICIAL_AGENT_KEY=...`): the platform's control plane, unless the
    environment or the .env the SDK will load names one (read, never loaded here). Returns the URL it set, or None."""
    env = os.environ if env is None else env
    if str(env.get("ALTRUAGENT_CONTROL_URL") or "").strip():
        return None
    try:
        from dotenv import dotenv_values, find_dotenv
        path = find_dotenv()                  # (the SDK's load_dotenv() finds the same file: it walks up from here)
        if path and str(dotenv_values(path).get("ALTRUAGENT_CONTROL_URL") or "").strip():
            return None
    except Exception:  # noqa: BLE001 - no python-dotenv / unreadable .env: the SDK reports it itself
        pass
    env["ALTRUAGENT_CONTROL_URL"] = DEFAULT_CONTROL_URL
    return DEFAULT_CONTROL_URL


DEGRADED_BANNER = """
******************************************************************************************
WARNING: THIS MACHINE PLAYS DEGRADED
{lines}
  scripts/run_agent.sh refuses such a start; python scripts/selfcheck.py shows the details.
  Deploy the bundle (scripts/bundle.sh: node, calculator, simulator) or fix the above.
******************************************************************************************"""


def _local_checks(check_search: bool = True) -> list[tuple[bool, str]]:
    """What this machine would play with: (ok, line) for the data files, the damage calculator (a real calc in a
    fresh node process) and, with ARENA_BATTLE=search, the engine search (the simulator loads). ~2 s."""
    from .pokemon import calc, dex, engine
    from .pokemon.policy import battle_mode

    out: list[tuple[bool, str]] = []
    missing = [p for p in (dex.DATA / "pokedex.json", dex.DATA / "moves.json",
                           dex.repo_root() / "agent" / "pokemon" / "draft_model.json") if not p.exists()]
    if missing:
        out.append((False, f"agent data files missing ({', '.join(str(p) for p in missing)}): every Pokemon "
                           "decision plays blind. Run from the unpacked bundle / checkout directory; never a "
                           "non-editable `pip install .`"))
    else:
        out.append((True, "agent data files and the draft model are present"))
    try:
        good, why = calc.probe()
    except Exception as exc:  # noqa: BLE001 - a report, never a crash
        good, why = False, f"the check raised {exc!r}"
    out.append((good, f"damage calculator: {why}" if good else
                f"damage calculator cannot run ({why}): the heuristic wins only ~12% against the calculator"))
    if check_search and battle_mode() == "search":
        try:
            good, why = engine.probe(30.0)
        except Exception as exc:  # noqa: BLE001
            good, why = False, f"the check raised {exc!r}"
        out.append((good, f"engine search: {why}" if good else
                    f"engine search cannot run ({why}): every battle decision plays the classic policy"))
    return out


_PLAYING_FLAGS = ("--tournament", "--match", "--check-tournament")


def _startup(argv: list[str], env=None) -> bool:
    """`python -m agent` only (main() stays what tests drive), and only for a mode that plays or checks
    (--tournament, --match, --check-tournament; a retired mode just prints its notice): the 10 s supervisor knobs
    (ARENA_DISCOVERY_S, ARENA_WORKER_COOLDOWN_S, as scripts/run_agent.sh sets them), the platform's control-plane URL
    when none is configured and, unless scripts/run_agent.sh already checked this machine (ARENA_RUN_ID), a report of
    what it would play with, LOUD (stdout and stderr) when it would play degraded. Never refuses to start: a degraded
    agent still beats a seat the platform random-plays. --check-tournament prints the checks as its first lines.
    Returns False when a local check failed."""
    env = os.environ if env is None else env
    if any(a in ("-h", "--help") for a in argv) or not any(a in _PLAYING_FLAGS for a in argv):
        return True                                   # (help, or a retired mode: nothing runs)
    for name, _, _, default in _TOURNAMENT_ENV:
        env.setdefault(name, default)
    url = _control_url_default(env)
    if url:
        print(f"ALTRUAGENT_CONTROL_URL is not set: using the platform's {url}")
    if env.get("ARENA_RUN_ID"):
        return True                                   # run_agent.sh's preflight ran the same checks and refuses
    checks = _local_checks()
    good = all(ok for ok, _ in checks)
    if "--check-tournament" in argv:
        ok_mark, fail_mark = _mark("✓", "[ok]"), _mark("✗", "[FAIL]")
        for ok, line in checks:
            print(f"{ok_mark if ok else fail_mark} {line[0].upper()}{line[1:]}")
        return good
    if good:
        print("Local checks: " + "; ".join(line for _, line in checks), flush=True)
        return True
    banner = DEGRADED_BANNER.format(lines="\n".join(f"  - {line}" for ok, line in checks if not ok))
    print(banner, flush=True)
    print(banner, file=sys.stderr, flush=True)
    return False


if __name__ == "__main__":
    _argv = sys.argv[1:]
    _local_ok = _startup(_argv)
    _code = main(_argv)
    raise SystemExit(_code or (1 if "--check-tournament" in _argv and not _local_ok else 0))
