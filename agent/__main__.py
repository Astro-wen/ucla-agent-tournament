"""Entry point for `python -m agent`.

Authenticates using the existing `.env`/environment configuration,
discovers matches assigned to this agent, and plays them CONCURRENTLY — one
independent worker process per active match — through
`agent.agent.create_agent()` via the committed supervisor
(`altruagent.supervisor.run_forever_concurrent`). Stops cleanly on Ctrl+C;
does not resign or otherwise touch any match on shutdown.

Claim mode — `python -m agent --claim seatclaim_...` (or `--claim -` to be
prompted without echo, or `ALTRUAGENT_CLAIM_TOKEN` in the environment) —
instead claims exactly ONE seat of a self-hosted tournament test match and
plays it right here, in this process: no API key, no `me()`/`sessions()`
discovery, no supervisor or worker processes. The process itself is the
isolation boundary, so self-play is simply one terminal per seat.
`--agent MODULE[:FACTORY]` picks a different factory than
`agent.agent:create_agent` for that one seat.

Official tournament mode — `python -m agent --tournament` (with
`ALTRUAGENT_OFFICIAL_AGENT_KEY` set) — authenticates as the contestant's
registered tournament agent and keeps one worker process per active official
assignment until Ctrl+C (`altruagent.supervisor.run_tournament_forever`).
`--check-tournament` verifies the connection and the agent without playing.

Deployment hardening on top of the SDK (none of it edits `altruagent/`, see
docs/deploy.md "官方正式赛"): supervisor tuning from ARENA_* knobs with 10 s
defaults in both modes; a failed discovery poll or a TRANSIENT re-authentication
failure (429, 5xx, network) no longer ends the runtime and its running matches
(`_SteadyDiscovery`, `_SteadyOfficial`); the official runtime waits out a
transient failure at start instead of exiting; scripts/run_agent.sh can hand it
a per-run-loop execution id so a restarted runtime resumes its own seat leases
at once; a hot standby plays only the seats no other runtime holds (checked
with the seat's grant; a new seat is left to the primary for
ARENA_NEW_SEAT_DELAY_S); and `python -m agent` run by anything but
scripts/run_agent.sh gets the platform's control-plane URL when none is
configured and says loudly when this machine would play degraded (`_startup`).

This file is deliberately thin — discovery/worker lifecycle lives in
`altruagent.supervisor`, one match's play loop in `altruagent.runner`, and
each worker's own setup in `altruagent.worker`.

IMPORTANT (Windows multiprocessing): the `if __name__ == "__main__":` guard
at the bottom of this file is not just style — `multiprocessing`'s `spawn`
start method (required on Windows, used here unconditionally so behavior is
identical everywhere) re-imports this exact module in every worker process.
Without the guard, each freshly-spawned worker would re-run `main()` itself
and spawn further workers recursively.
"""

from __future__ import annotations

import argparse
import getpass
import os
import random
import re
import sys
import time
from typing import Callable

import altruagent.supervisor as _sdk_supervisor
from altruagent.agent_loader import DEFAULT_AGENT_SPEC
from altruagent.agent_loader import load_agent_factory as _load_agent_factory
from altruagent.auth import SeatClaimError, SeatGrantAuth
from altruagent.client import AltruAgentClient
from altruagent.errors import AltruAgentError, AuthenticationError, ConfigurationError, PlatformError
from altruagent.mcp_game import MCPGameSession
from altruagent.models import AgentSessions, DecisionContext, GameState, SeatGrant
from altruagent.official import OFFICIAL_AGENT_KEY_ENV, OfficialAgentClient, OfficialAgentError
from altruagent.runner import RunnerError, _resolve_decision_fn, run_game
from altruagent.supervisor import WAITING_MESSAGE, run_forever_concurrent, run_tournament_forever

from . import agent as agent_module

DEFAULT_CONTROL_URL = "https://api.altruagent-game.com"   # the platform's control plane (.env.example)


def _resolve_create_agent(module: object) -> Callable:
    """Look up ``create_agent`` on the given agent module. Fails clearly
    (no fallback name, no signature inspection) if it's missing or not
    callable. Deliberately does NOT call it here — construction happens
    once per match, inside that match's own worker process, not once in
    the parent (see altruagent.worker.run_worker).
    """
    create_agent = getattr(module, "create_agent", None)
    if not callable(create_agent):
        raise ValueError(
            "agent/agent.py must define a callable create_agent() function "
            "that returns your decision logic (a function, or an object "
            "exposing choose_action(state, context))."
        )
    return create_agent


_SUPERVISOR_ENV = (
    # (environment variable, run_forever_concurrent keyword, smallest accepted value, what `python -m agent` sets)
    ("ARENA_DISCOVERY_S", "discovery_interval", 2.0, "10"),
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


def _supervisor_options(env=None) -> dict:
    """Optional supervisor tuning from the environment. Unset = the SDK defaults (a sessions() poll every 15 s, and
    a 60 s cooldown before a match whose worker crashed gets a new worker: a lost 55 s battle turn); both the
    deployment wrapper scripts/run_agent.sh and `python -m agent` itself (`_startup`) set both to 10 s. Bad values
    are ignored with a message, never fatal."""
    env = os.environ if env is None else env
    out: dict = {}
    for name, key, low, _ in _SUPERVISOR_ENV:
        value = _env_number(env, name, low)
        if value is not None:
            out[key] = value
    return out


def _tournament_options(env=None) -> dict:
    """The same two knobs for run_tournament_forever: ARENA_DISCOVERY_S = the assignment poll (SDK default 10 s),
    ARENA_WORKER_COOLDOWN_S = the wait before a seat whose worker ended gets a new one (SDK default 60 s: after a
    worker crash, e.g. the runner's kill on an invalid action, a whole 55 s battle turn)."""
    names = {"discovery_interval": "poll_interval", "cooldown_seconds": "cooldown_seconds"}
    return {names[k]: v for k, v in _supervisor_options(env).items()}


class _SteadyDiscovery:
    """The client as the supervisor sees it, except that a failed sessions() poll (network blip, 5xx, rate limit,
    a transient 401) no longer ends `python -m agent` — the SDK's supervisor would then terminate EVERY running
    match worker, although each worker handles its own errors. The failed poll answers "no active matches" instead:
    the supervisor keeps the workers it has (it never stops one because of a poll) and starts new ones at the next
    good poll. After ARENA_DISCOVERY_MAX_FAILURES failures in a row (default 30) the error is raised as before, and
    the wrapper (scripts/run_agent.sh / systemd) restarts the whole agent."""

    def __init__(self, client, max_failures: int | None = None) -> None:
        self._client = client
        if max_failures is None:
            max_failures = int(_env_number(os.environ, "ARENA_DISCOVERY_MAX_FAILURES", 1) or 30)
        self.max_failures = max_failures
        self.failures = 0

    def sessions(self) -> AgentSessions:
        try:
            found = self._client.sessions()
        except AltruAgentError as exc:
            self.failures += 1
            if self.failures >= self.max_failures:
                raise
            print(f"[agent] discovery failed ({self.failures} in a row; running workers continue): {exc}", flush=True)
            return AgentSessions()
        if self.failures:
            print(f"[agent] discovery recovered after {self.failures} failed poll(s)", flush=True)
        self.failures = 0
        return found

    def __getattr__(self, name: str):
        return getattr(self._client, name)


def _run_discovery() -> int:
    """The normal workflow: authenticate with the API key, then keep one
    worker process per assigned active match. Unchanged by claim mode.
    """
    try:
        _resolve_create_agent(agent_module)  # validated eagerly; not invoked here
    except ValueError as exc:
        print(f"Startup error: {exc}")
        return 1

    try:
        client = AltruAgentClient()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}")
        print("Copy .env.example to .env and fill in ALTRUAGENT_API_KEY.")
        return 1

    try:
        me = client.me()
    except AltruAgentError as exc:
        print(f"Could not authenticate: {exc}")
        client.close()
        return 1

    if not me.is_claimed:
        print(
            f"Agent '{me.name}' is not claimed yet (status={me.status}). "
            "Have a human claim it with your claim_token before running this."
        )
        client.close()
        return 1

    print(f"Authenticated as agent '{me.name}' ({me.id}).")
    print(
        "Watching for assigned matches — each gets its own process. "
        "Press Ctrl+C to stop.\n"
    )

    try:
        run_forever_concurrent(_SteadyDiscovery(client), agent_id=me.id, **_supervisor_options())
        return 0
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    except AltruAgentError as exc:
        print(f"\nStopped due to an unrecoverable error: {exc}")
        return 1
    finally:
        client.close()


CLAIM_TOKEN_ENV = "ALTRUAGENT_CLAIM_TOKEN"
_PROMPT = "-"


def _resolve_claim_token(arg: str | None, *, prompt: Callable[[str], str]) -> str | None:
    """``--claim TOKEN`` wins; ``--claim -`` prompts without echo; otherwise
    ``ALTRUAGENT_CLAIM_TOKEN`` from the real process environment (never from
    ``.env`` — a claim token is single-use and shouldn't be saved in a file).
    ``None`` means "not claim mode".
    """
    if arg == _PROMPT:
        return prompt("Seat claim token (input hidden): ")
    if arg is not None:
        return arg
    return os.environ.get(CLAIM_TOKEN_ENV) or None


def _describe_seat(grant: SeatGrant) -> list[str]:
    if isinstance(grant.seat_position, int) and isinstance(grant.seat_count, int):
        seat = f"Claimed seat {grant.seat_position + 1}/{grant.seat_count}"
    else:
        seat = "Claimed seat"
    match = f"Match: {grant.match_id or 'unknown'}"
    if grant.match_status == "starting":
        match += " (waiting for every seat's agent to connect)"
    return [seat, f"Game: {grant.game_type or 'unknown'}", match]


class _ProgressGameSession(MCPGameSession):
    """Claim mode's game session: identical calls and results, plus a few
    lifecycle lines so a long match doesn't look idle — "Connected" after
    the first successful state read, one line per phase change, and a count
    of submitted decisions. Never prints state contents, actions, or tokens.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.decisions = 0
        self._connected = False
        self._phase: str | None = None

    def _observe(self, phase: str | None) -> None:
        if not self._connected:
            self._connected = True
            print("Connected. Playing — press Ctrl+C to stop.", flush=True)
        if phase and phase != self._phase:
            self._phase = phase
            print(f"Phase: {phase}", flush=True)

    def get_state(self) -> GameState:
        state = super().get_state()
        self._observe(state.phase)
        return state

    def wait_for_update(self, **kwargs) -> GameState:
        state = super().wait_for_update(**kwargs)
        self._observe(state.phase)
        return state

    def play_action(self, **kwargs) -> dict:
        result = super().play_action(**kwargs)
        self.decisions += 1
        post_move = result.get("state") if isinstance(result, dict) else None
        if isinstance(post_move, dict):
            self._observe(post_move.get("phase"))
        return result


# Waiting for a match whose open seats are still being filled (the platform's
# 409 match_not_ready): its own retry_after_seconds, else this, plus jitter so
# several local seats don't retry in lockstep.
CLAIM_WAIT_DEFAULT_S = 20.0
CLAIM_WAIT_JITTER_S = 2.0
# rate_limited while waiting: the platform allows 30 claim requests a minute.
CLAIM_RATE_LIMITED_WAIT_S = 30.0
# The control plane unreachable while waiting: a few spaced retries, then stop.
CLAIM_NETWORK_WAIT_S = 10.0
CLAIM_NETWORK_RETRIES = 5


class _StoppedBeforeClaim(Exception):
    """Ctrl+C while waiting to claim: nothing was claimed."""


def _claim_with_wait(client: AltruAgentClient) -> None:
    """Claim the seat, waiting while the match's open seats are being filled.

    ``SeatGrantAuth.login()`` stays a single attempt; this loop decides when to
    call it again. Every attempt goes through the same client, so the same
    ``SeatGrantAuth`` and therefore the same claim_key. Waits only on
    ``match_not_ready`` and ``rate_limited`` (and a few network failures);
    every other error is raised, as before. Prints nothing secret.
    """
    announced = False
    network_failures = 0
    while True:
        try:
            try:
                client.login()
                return
            except SeatClaimError as exc:
                if exc.error_code == "match_not_ready":
                    if not announced:
                        print(
                            "Waiting for the match's open seats to be filled — "
                            "this seat is claimed as soon as the match fills (Ctrl+C to stop)...",
                            flush=True,
                        )
                        announced = True
                    wait = exc.retry_after_seconds or CLAIM_WAIT_DEFAULT_S
                    time.sleep(wait + random.uniform(0, CLAIM_WAIT_JITTER_S))
                    continue
                if exc.error_code == "rate_limited":
                    time.sleep(CLAIM_RATE_LIMITED_WAIT_S)
                    continue
                raise
            except PlatformError as exc:
                if exc.status_code is None and network_failures < CLAIM_NETWORK_RETRIES:
                    network_failures += 1
                    time.sleep(CLAIM_NETWORK_WAIT_S)
                    continue
                raise
        except KeyboardInterrupt:
            raise _StoppedBeforeClaim() from None


def _run_claim(claim_token: str, create_agent: Callable) -> int:
    """Claim one Testing seat and play it to completion in this process."""
    try:
        auth = SeatGrantAuth(claim_token)
    except SeatClaimError as exc:
        print(f"Claim error: {exc}")
        return 1

    try:
        client = AltruAgentClient(auth=auth)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}")
        print("Claim mode needs only ALTRUAGENT_CONTROL_URL (no API key) — copy .env.example to .env.")
        return 1

    try:
        # Build and check the contestant BEFORE claiming: a claimed seat is
        # bound to this process's in-memory claim key, so a process that
        # fails after claiming can't hand the seat to a fixed-up rerun.
        try:
            contestant = create_agent()
            _resolve_decision_fn(contestant)
        except Exception as exc:  # noqa: BLE001 - contestant code; report it, don't claim
            print(f"Startup error: your agent factory failed, so the seat was not claimed: {exc!r}")
            return 1

        try:
            _claim_with_wait(client)  # the claim itself, waiting for the lobby if needed
        except _StoppedBeforeClaim:
            print("\nStopped before the seat was claimed.")
            return 0
        except AltruAgentError as exc:
            print(f"Could not claim seat: {exc}")
            return 1

        grant = auth.grant
        for line in _describe_seat(grant):
            print(line)
        print("Connecting to GameAPI...", flush=True)

        game = _ProgressGameSession(
            client, session_id=grant.game_session_id, game_server_url=grant.gameapi_server_url
        )
        context = DecisionContext(
            session_id=grant.game_session_id,
            tournament_id=None,
            game_type=grant.game_type,
            agent_id=grant.agent_id,
            seat_position=grant.seat_position,
        )
        final_state = run_game(game, context, contestant)
        print(
            f"Match finished (termination_reason={final_state.termination_reason}) "
            f"after {game.decisions} decision(s)."
        )
        returns = final_state.returns or {}
        if grant.agent_id in returns:
            print(f"Your score: {returns[grant.agent_id]}")
        return 0
    except KeyboardInterrupt:
        print("\nStopped. This seat stays claimed and can't be claimed again by another process.")
        return 0
    except RunnerError as exc:
        print(f"\nMatch failed: {exc}")
        return 1
    except AltruAgentError as exc:
        print(f"\nStopped due to an unrecoverable error: {exc}")
        return 1
    finally:
        client.close()


def _transient(exc: BaseException) -> bool:
    """A control-plane failure that retrying fixes (rate limiting, a 5xx or gateway blip, a timeout, the network),
    as opposed to one it cannot (a rejected or revoked key: any other 4xx; a malformed answer)."""
    status = getattr(exc, "status_code", None)
    if getattr(exc, "error_code", None) == "rate_limited" or status in (408, 425, 429):
        return True
    if isinstance(status, int) and status >= 500:
        return True
    return status is None and isinstance(exc, PlatformError)       # could not reach the control plane at all


# Listing pauses after the 1st, 2nd, 3rd+ rate-limited re-login in a row. Capped at 30 s: while paused the runtime sees
# no new seat (a Werewolf seat has 120 s, a team preview 90 s), and one re-login per 30 s is still far gentler than the
# SDK's own poll (10 s).
RATE_LIMIT_BACKOFF_S = (15.0, 20.0, 30.0)


class _SteadyOfficial:
    """The official client's assignments() as run_tournament_forever calls it, except that a TRANSIENT failure to
    re-authenticate the agent session (429 rate_limited, a 5xx, a gateway blip, a timeout) no longer ends the runtime.

    Upstream defect (reported to the organizers, see docs/deploy.md): assignments() re-authenticates after a 401, and
    OfficialAgentAuth raises OfficialAgentError (an AuthenticationError, not a PlatformError) for ANY non-200 answer
    of /tournament/agent/authenticate; run_tournament_once catches only PlatformError, so one rate-limited or 503
    re-login ends run_tournament_forever, whose finally terminates EVERY running match worker. Here such a failure is
    re-raised as PlatformError: the SDK skips that one listing and keeps every worker. After rate_limited the listing
    also pauses (RATE_LIMIT_BACKOFF_S) instead of asking again at every poll. A key the platform rejects (any other
    4xx) stays fatal at once, as upstream intends: the wrapper (run_agent.sh / systemd) restarts the runtime with its
    back-off. Never answers with an empty list: two empty listings in a row make the SDK stop the running workers.

    `install(official)` puts it in place of the client's own assignments (an instance attribute), so the SDK gets the
    very client object it always got; everything else about the client is untouched.

    A hot standby (hold_new_s > 0, ARENA_NEW_SEAT_DELAY_S; scripts/run_agent.sh sets 20 s for role=standby) hands the
    SDK only the seats no other runtime holds. The platform gives a seat to whichever runtime claims it first, and the
    primary claims a new seat within one poll (10 s), so the standby leaves a NEW seat alone for hold_new_s after it
    first lists it; from then on it asks at every poll whether the seat is free by requesting its grant with this
    runtime's execution id (`grant`, `execution_id`): seat_busy while the primary's lease lives, and once that lease
    lapses (the primary stopped renewing: ~30 s) the grant takes the seat and the SDK gets it at once. Without that
    check the SDK's own worker would find seat_busy and retry only SEAT_BUSY_RETRY_SECONDS (35 s) later, with a new
    worker process and a new agent session each time: a takeover of up to ~70 s (a lost 55 s battle turn) and steady
    authentication traffic on the key both runtimes share. Any unexpected error of the check hands the seat over
    (the SDK's worker then decides). A seat once handed to the SDK is never held back again, even after a listing that
    missed it (a seat missing from two listings in a row gets its worker stopped)."""

    def __init__(self, list_assignments: Callable[[], list], *, now: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = print, hold_new_s: float = 0.0,
                 grant: Callable[[str, str], object] | None = None, execution_id: str | None = None):
        self._list = list_assignments
        self._now = now
        self._log = log
        self.failures = 0                  # transient re-authentication failures in a row
        self.rate_limited = 0              # ... of which rate_limited, in a row
        self._resume_at = 0.0
        self.hold_new_s = max(0.0, float(hold_new_s or 0.0))
        self._grant = grant                # the client's grant(seat_id, execution_id) (standby only)
        self._execution_id = execution_id  # this runtime's: the one its workers use
        self._first_seen: dict = {}        # seat_id -> when this runtime first listed it (hold_new_s only)
        self._handed: set = set()          # seat_ids already handed to the SDK
        self._told: set = set()            # seat_ids whose first failed check was logged

    @classmethod
    def install(cls, official, **kwargs) -> "_SteadyOfficial":
        steady = cls(official.assignments, grant=getattr(official, "grant", None), **kwargs)
        official.assignments = steady
        return steady

    def __call__(self):
        if self._now() < self._resume_at:
            raise PlatformError("still backing off after a rate-limited re-authentication", status_code=429,
                                error_code="rate_limited")
        try:
            rows = self._list()
        except AuthenticationError as exc:
            if not _transient(exc):
                raise                      # a rejected key: fatal, as upstream
            self.failures += 1
            if exc.error_code == "rate_limited" or exc.status_code == 429:
                self.rate_limited += 1
                pause = RATE_LIMIT_BACKOFF_S[min(self.rate_limited, len(RATE_LIMIT_BACKOFF_S)) - 1]
                self._resume_at = self._now() + pause
            if self.failures == 1 or self.failures % 30 == 0:
                self._log(f"[agent] re-authentication failed ({self.failures} in a row; running matches continue, "
                          f"retrying): {exc}")
            raise PlatformError(f"transient re-authentication failure: {exc}", status_code=exc.status_code,
                                error_code=exc.error_code) from None
        if self.failures:
            self._log(f"[agent] re-authentication recovered after {self.failures} failure(s)")
        self.failures = self.rate_limited = 0
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


STARTUP_RETRY_S = (5.0, 10.0, 20.0, 30.0)   # capped at 30 s: plays within ~30 s of the control plane coming back
_sleep = time.sleep                        # (tests replace it)


def _authenticate(official) -> bool:
    """The runtime's first authentication. A transient failure (the control plane down or rate limiting at the
    moment the runtime starts, e.g. a restart during an outage) is waited out with a back-off instead of ending the
    process; a rejected key ends it (False)."""
    attempt = 0
    while True:
        try:
            official.authenticate()
            return True
        except AltruAgentError as exc:
            if not _transient(exc):
                print(f"Could not connect as official tournament agent: {exc}")
                return False
            delay = STARTUP_RETRY_S[min(attempt, len(STARTUP_RETRY_S) - 1)]
            attempt += 1
            print(f"Could not connect as official tournament agent yet ({exc}); retrying in {delay:.0f}s.", flush=True)
            _sleep(delay)


EXECUTION_ID_ENV = "ARENA_EXECUTION_ID"
_EXECUTION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
SEAT_BUSY_RETRY_ENV = "ARENA_SEAT_BUSY_RETRY_S"
NEW_SEAT_DELAY_ENV = "ARENA_NEW_SEAT_DELAY_S"          # a hot standby's hold on new seats (see _SteadyOfficial)


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
    """Apply this runtime's execution id and ARENA_SEAT_BUSY_RETRY_S to the SDK's supervisor module (its module-level
    name and constant, read when the runtime starts / on every seat_busy; altruagent/ itself is not edited). The id is
    the run loop's (see _take_execution_id) or, as upstream, a fresh one for this process, fixed here so that the
    standby's seat check (_SteadyOfficial) uses the very id the runtime's workers use. Returns the function that
    restores both, and the id."""
    env = os.environ if env is None else env
    saved = (_sdk_supervisor.new_execution_id, _sdk_supervisor.SEAT_BUSY_RETRY_SECONDS)
    execution_id = _take_execution_id(env) or saved[0]()
    _sdk_supervisor.new_execution_id = lambda: execution_id     # (called once per runtime, when it starts)
    retry = _env_number(env, SEAT_BUSY_RETRY_ENV, 5.0)
    if retry is not None:
        _sdk_supervisor.SEAT_BUSY_RETRY_SECONDS = retry

    def restore() -> None:
        _sdk_supervisor.new_execution_id, _sdk_supervisor.SEAT_BUSY_RETRY_SECONDS = saved
    return restore, execution_id


def _run_tournament(agent_spec: str) -> int:
    """Official tournament runtime: authenticate with the Official Agent Key,
    then keep one worker process per active official assignment until Ctrl+C.
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
        print("Connected as official tournament agent.")
        print(f"{WAITING_MESSAGE} (Press Ctrl+C to stop.)", flush=True)
        _SteadyOfficial.install(official, hold_new_s=_env_number(os.environ, NEW_SEAT_DELAY_ENV, 0.0) or 0.0,
                                execution_id=execution_id)
        run_tournament_forever(official, agent_spec=agent_spec, **_tournament_options())
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
            if exc.status_code in (400, 401):
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
        ok("Ready for tournament")
        return 0
    finally:
        official.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agent",
        description=(
            "With no arguments: play every match assigned to your registered agent "
            "(ALTRUAGENT_API_KEY). With --claim: claim and play one tournament "
            "test-match seat (no API key needed). With --tournament: play your "
            f"official tournament assignments ({OFFICIAL_AGENT_KEY_ENV})."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--claim",
        metavar="TOKEN",
        help=(
            "claim one test-match seat with its seatclaim_... token and play it; "
            f"'-' prompts for the token without echo (or set {CLAIM_TOKEN_ENV})"
        ),
    )
    mode.add_argument(
        "--tournament",
        action="store_true",
        help=f"official tournament mode: wait for and play your official assignments ({OFFICIAL_AGENT_KEY_ENV})",
    )
    mode.add_argument(
        "--check-tournament",
        action="store_true",
        help="check your official tournament connection and agent without playing anything",
    )
    parser.add_argument(
        "--agent",
        metavar="MODULE[:FACTORY]",
        help=f"with --claim/--tournament/--check-tournament: agent factory to use (default: {DEFAULT_AGENT_SPEC})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args([] if argv is None else argv)

    if args.tournament or args.check_tournament:
        agent_spec = args.agent or DEFAULT_AGENT_SPEC
        return _check_tournament(agent_spec) if args.check_tournament else _run_tournament(agent_spec)

    claim_token = _resolve_claim_token(args.claim, prompt=getpass.getpass)
    if claim_token is None:
        if args.agent is not None:
            parser.error("--agent is only supported together with --claim, --tournament, or --check-tournament")
        return _run_discovery()

    if not claim_token.strip():
        print("Claim error: no seat claim token was given.")
        return 1

    try:
        if args.agent is not None:
            create_agent = _load_agent_factory(args.agent)
        else:
            create_agent = _resolve_create_agent(agent_module)
    except ValueError as exc:
        print(f"Startup error: {exc}")
        return 1

    return _run_claim(claim_token, create_agent)


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


def _startup(argv: list[str], env=None) -> bool:
    """`python -m agent` only (main() stays what tests drive): discovery and tournament modes get the 10 s supervisor
    knobs (ARENA_DISCOVERY_S, ARENA_WORKER_COOLDOWN_S, as scripts/run_agent.sh sets them) and the platform's
    control-plane URL when none is configured and, unless scripts/run_agent.sh already checked this machine
    (ARENA_RUN_ID), a report of what it would play with, LOUD (stdout and stderr) when it would play degraded. Never
    refuses to start: a degraded agent still beats a seat the platform random-plays. --check-tournament prints the
    checks as its first lines. Returns False when a local check failed."""
    env = os.environ if env is None else env
    if any(a in ("-h", "--help") or a.startswith("--claim") for a in argv) or env.get(CLAIM_TOKEN_ENV):
        return True                                   # (claim mode: one terminal seat, as upstream)
    for name, _, _, default in _SUPERVISOR_ENV:
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
