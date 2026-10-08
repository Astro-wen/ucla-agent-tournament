"""Deployment readiness: supervisor knobs (agent/__main__.py), the event runtime's hardening on top of upstream's
(`python -m agent --tournament --match`: the SDK's supervisor keeps the runtime and its matches through a transient
re-authentication failure, the start waits out an outage, the run loop's execution id, the hot standby, `python -m
agent` without run_agent.sh: control-plane default and a loud report), the
calculator's deployment knobs and probe (agent/pokemon/calc.py; node children get no keys and go first under memory
pressure), scripts/selfcheck.py (refuses a machine whose calculator, or with ARENA_BATTLE=search whose engine search,
cannot run; names the configured key and mode), scripts/run_agent.sh (restart, rotation, locks, clean stop, no
orphans, calculator and engine search refusal, no key in logs, the official tournament mode, hot standby,
ARENA_ALLOW_NO_CALC alone, an unwritable LOG_DIR), local/fake_platform.py against the real SDK client (real-shaped
payloads, the official control plane with seat leases, a Werewolf table), scripts/bundle.sh (allowlist, pinned node,
secret scan incl. Official Agent Keys, .dockerignore, wheels, one whole sample match) and the packaging files
(requirements.lock, deploy/Dockerfile, deploy/arena-agent@.service, .gitignore, docs)."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

from altruagent.errors import AuthenticationError, PlatformError

import agent.__main__ as agent_main
from agent.pokemon import calc, engine

ROOT = Path(__file__).resolve().parents[1]
RUN_AGENT = ROOT / "scripts" / "run_agent.sh"
needs_linux = pytest.mark.skipif(not sys.platform.startswith("linux") or shutil.which("bash") is None,
                                 reason="run_agent.sh is tested on Linux")
needs_node = pytest.mark.skipif(shutil.which("node") is None or calc._js_dir() is None,
                                reason="needs node + agent/js/node_modules")


# ---- agent/__main__.py ---------------------------------------------------------------------------------------------
def test_tournament_options_default_to_the_sdk_and_ignore_bad_values(capsys):
    assert agent_main._tournament_options({}) == {}                   # (the SDK: a 10 s poll, a 60 s cooldown)
    assert agent_main._tournament_options({"ARENA_DISCOVERY_S": "10", "ARENA_WORKER_COOLDOWN_S": "7.5"}) == \
        {"poll_interval": 10.0, "cooldown_seconds": 7.5}
    assert agent_main._tournament_options({"ARENA_DISCOVERY_S": "0.5", "ARENA_WORKER_COOLDOWN_S": "soon"}) == {}
    assert agent_main._tournament_options({"ARENA_DISCOVERY_S": "nan"}) == {}
    assert capsys.readouterr().out.count("Ignoring") == 3


def test_python_m_agent_sets_the_10s_knobs_itself(monkeypatch):
    # the SDK's own default (a 60 s cooldown after a crashed worker, doubling per failure in a row) would cost a 55 s
    # battle turn: a plain `python -m agent --tournament --match` (a harness, the README's way) gets run_agent.sh's
    # 10 s / 10 s too; a retired mode (no flag) only prints its notice and gets nothing
    monkeypatch.setattr(agent_main, "_local_checks", lambda check_search=True: [(True, "fine")])
    env = {"ALTRUAGENT_CONTROL_URL": "http://127.0.0.1:9"}
    agent_main._startup(["--tournament", "--match"], env)
    assert agent_main._tournament_options(env) == {"poll_interval": 10.0, "cooldown_seconds": 10.0}
    env = {"ALTRUAGENT_CONTROL_URL": "x", "ARENA_WORKER_COOLDOWN_S": "4"}      # (a value that is set stays)
    agent_main._startup(["--match"], env)
    assert env["ARENA_WORKER_COOLDOWN_S"] == "4" and env["ARENA_DISCOVERY_S"] == "10"
    env = {}
    assert agent_main._startup([], env) is True and env == {}           # (the retired platform-key mode: untouched)


SDK_BACKOFF = [11.0, 21.0, 41.0, 81.0, 161.0, 321.0, 601.0, 601.0]


@pytest.mark.parametrize("env, gaps_expected", [
    (None, SDK_BACKOFF),                                              # the SDK alone (upstream 4cb083a): up to 600 s
    ({}, [11.0, 21.0, 31.0, 31.0, 31.0, 31.0, 31.0, 31.0]),          # our runtime (_tournament_tuning): up to 30 s
    ({"ARENA_SEAT_BACKOFF_MAX_S": "600"}, SDK_BACKOFF),               # (the knob gives the SDK's cap back)
    ({"ARENA_SEAT_BACKOFF_MAX_S": "5"}, [11.0] * 8),                  # below the cooldown: a fixed 10 s, as v4.2 did
])
def test_the_sdks_per_seat_backoff_starts_from_our_10s_cooldown_and_stops_at_30s(env, gaps_expected):
    # upstream 4cb083a: a seat whose worker keeps failing waits cooldown, 2x, 4x ... up to 600 s; a finished match
    # resets it. Our 10 s cooldown shortens the FIRST retry (one crash = at most one lost battle turn) and our runtime
    # caps the doubling at 30 s (review 10-07: at 600 s, 5 failed workers put a tournament seat's 6th start at 360 s,
    # past its 240 s connect window, and a match whose worker kept crashing waited 160 s, then 320 s)
    import altruagent.supervisor as sup
    from altruagent.models import OfficialAssignment
    from altruagent.supervisor import TournamentState, run_tournament_once
    from tests.test_supervisor import FakeProcessFactory

    class Official:
        def assignments(self):
            return [OfficialAssignment(match_id="m1", seat_id="s1", game_type="werewolf", context="testing")]

    sdk_cap = sup.SEAT_FAILURE_BACKOFF_MAX_SECONDS
    restore = agent_main._tournament_tuning(env)[0] if env is not None else (lambda: None)
    try:
        clock, factory, state = {"t": 0.0}, FakeProcessFactory(), TournamentState()

        def tick():
            run_tournament_once(Official(), state, agent_spec="agent.agent:create_agent", now=lambda: clock["t"],
                                cooldown_seconds=10.0, process_factory=factory, log=lambda m: None)

        tick()
        gaps = []
        for _ in range(8):
            factory.processes[-1].finish(exitcode=1)                # the worker fails at once, every time
            n, failed_at = len(factory.processes), clock["t"]
            while len(factory.processes) == n:
                clock["t"] += 1.0                                    # (a 1 s poll: reaped 1 s after the failure)
                tick()
            gaps.append(clock["t"] - failed_at)
    finally:
        restore()
    assert gaps == gaps_expected
    assert sup.SEAT_FAILURE_BACKOFF_MAX_SECONDS == sdk_cap == 600.0  # (given back when the runtime ends)


# ---- the event runtime (python -m agent --tournament --match) -------------------------------------------------------
FAKE_EAK = "eak_live_" + "fa4e" * 16                 # obviously fake, but the shape the SDK's local check accepts
UNIT_EAK = "eak_live_" + "7e57" * 16                 # (a second fake one: the run_agent.sh tests' default key file)


class _AuthBackend:
    """A control plane whose agent session expires after the first listing and whose re-authentication then fails
    with `status`/`code` until `recover_at` listings have been asked for (exp20's demo_fatal.py, as a test)."""

    def __init__(self, status: int, code: str | None, recover_at: int = 4, detail: str | None = None):
        self.status, self.code, self.recover_at, self.detail = status, code, recover_at, detail
        self.calls = {"auth": 0, "list": 0}

    def __call__(self, request):
        import httpx
        path = request.url.path
        if path == "/tournament/agent/authenticate":
            self.calls["auth"] += 1
            if self.calls["auth"] == 1 or self.calls["list"] >= self.recover_at:
                return httpx.Response(200, json={"access_token": f"session-{self.calls['auth']}"})
            body = {"error": self.code} if self.code else {}
            if self.detail:
                body["detail"] = self.detail
            return httpx.Response(self.status, json=body)
        if path == "/tournament/agent/assignments":
            self.calls["list"] += 1
            if self.calls["list"] == 1 or (self.calls["list"] > self.recover_at
                                           and request.headers["authorization"] != "Bearer session-1"):
                return httpx.Response(200, json={"assignments": [
                    {"match_id": "m1", "seat_id": "s1", "game_type": "pokemon_vgc_doubles_draft"}]})
            return httpx.Response(401, json={"error": "invalid_agent_session"})
        return httpx.Response(404)


def _official(backend):
    import httpx

    from altruagent.official import OfficialAgentClient
    return OfficialAgentClient("https://control.example.test", FAKE_EAK, load_env_file=False,
                               transport=httpx.MockTransport(backend))


MINT_DETAIL = "Failed to mint agent session: Request rate limit reached"      # (upstream 1372ccc's wording)


@pytest.mark.parametrize("status, code, detail", [(429, "rate_limited", None), (503, None, None), (502, None, None),
                                                  (500, "internal", None),
                                                  (503, "agent_session_unavailable", "try again"),
                                                  (401, "invalid_official_agent_key", MINT_DETAIL)])
def test_a_transient_reauthentication_failure_no_longer_ends_the_runtime_and_its_matches(status, code, detail):
    # the upstream defect we wrapped since v4.1.2 (intel-2: assignments() -> 401 -> re-login -> 429 / 5xx raised an
    # AuthenticationError run_tournament_once did not catch, so the runtime ended and terminated EVERY worker) is fixed
    # in the SDK itself since 1372ccc (PR #5): the supervisor keeps its workers and pauses the listing (10 s doubling
    # to 60 s, 60 s after a 429). This pins it, through the real client, so the wrapper is not needed any more.
    from altruagent.supervisor import run_tournament_forever
    from tests.test_supervisor import FakeProcessFactory

    backend, factory, log = _AuthBackend(status, code, detail=detail), FakeProcessFactory(), []
    clock = {"t": 0.0}

    def sleep(s):
        clock["t"] += s

    official = _official(backend)
    run_tournament_forever(official, agent_spec="agent.agent:create_agent", sleep=sleep, now=lambda: clock["t"],
                           process_factory=factory, log=log.append, max_iterations=20, poll_interval=10.0,
                           cooldown_seconds=10.0)
    assert len(factory.processes) == 1                               # one worker, started once and never restarted
    assert not [line for line in log if "no longer assigned" in line]  # never stopped as "unassigned"
    assert sum("Could not renew your agent session" in line for line in log) == 1     # logged once, not every poll
    assert "Assignment discovery recovered." in log
    assert [line for line in log if "active match worker(s)" in line] == ["Stopping 1 active match worker(s)..."]
    assert log[-1] == "Stopping 1 active match worker(s)..."         # (only when the bounded test loop ends)
    assert backend.calls["auth"] <= 4, backend.calls                 # a pause between re-logins, not one per poll
    if status == 401:                                                # not "copy the key again": it is not the key
        assert not [line for line in log if "copy the key again" in line.lower()]
        assert any("couldn't start an agent session" in line and "not your key" in line for line in log)


def test_a_fresh_session_the_platform_still_refuses_keeps_the_runtime_and_its_matches():
    # review 10-07 (A6): the re-login succeeds but the retried listing is still 401: the SDK raises a plain
    # AuthenticationError, which used to end the runtime and terminate every worker (upstream 1372ccc: temporary)
    import httpx

    from altruagent.supervisor import run_tournament_forever
    from tests.test_supervisor import FakeProcessFactory

    calls, factory, log, clock = {"auth": 0, "list": 0}, FakeProcessFactory(), [], {"t": 0.0}

    def backend(request):
        if request.url.path == "/tournament/agent/authenticate":
            calls["auth"] += 1
            return httpx.Response(200, json={"access_token": f"session-{calls['auth']}"})
        calls["list"] += 1
        if calls["list"] == 1 or calls["list"] >= 6:                 # lists 2-5 (two tries: 2+3, 4+5) refused
            return httpx.Response(200, json={"assignments": [
                {"match_id": "m1", "seat_id": "s1", "game_type": "pokemon_vgc_doubles_draft"}]})
        return httpx.Response(401, json={"error": "invalid_agent_session", "detail": "session not accepted"})

    def sleep(s):
        clock["t"] += s

    official = _official(backend)
    run_tournament_forever(official, agent_spec="agent.agent:create_agent", sleep=sleep, now=lambda: clock["t"],
                           process_factory=factory, log=log.append, max_iterations=10, poll_interval=10.0,
                           cooldown_seconds=10.0)
    assert len(factory.processes) == 1 and not [line for line in log if "no longer assigned" in line]
    assert sum("Could not renew your agent session" in line for line in log) == 1, log
    assert "Assignment discovery recovered." in log
    assert calls["auth"] == 3                                        # paused 10 s, 20 s: not one re-login per poll


@pytest.mark.parametrize("status, code, detail", [(401, "invalid_official_agent_key", None),
                                                  (401, "invalid_official_agent_key", "Invalid official agent key")])
def test_a_refused_key_at_relogin_still_ends_the_runtime_even_through_the_standby(status, code, detail):
    # a key the platform refuses (revoked, replaced, the agent not Self-hosted) cannot get better by retrying: the SDK
    # ends the runtime (its finally terminates the workers; run_agent.sh restarts it with its back-off and says so).
    # The standby wrapper passes the listing's errors through untouched. (An incomplete registration no longer ends
    # it since SDK PR #7: next test.)
    from altruagent.official import OfficialAgentError as OffErr
    from altruagent.official import is_fatal_auth_error
    from altruagent.supervisor import run_tournament_forever
    from tests.test_supervisor import FakeProcessFactory

    for standby in (False, True):
        factory, official = FakeProcessFactory(), _official(_AuthBackend(status, code, detail=detail))
        if standby:
            agent_main._HotStandby.install(official, log=lambda m: None, hold_new_s=20.0)
        with pytest.raises(OffErr) as exc_info:
            run_tournament_forever(official, agent_spec="agent.agent:create_agent", sleep=lambda s: None,
                                   process_factory=factory, log=lambda m: None, max_iterations=6)
        assert is_fatal_auth_error(exc_info.value) and exc_info.value.error_code == code
        assert all(p.terminated for p in factory.processes)


RULES_LINE = ("Accept the updated Official Rules on your dashboard "
              "(https://platform.altruagent-game.com/tournament/dashboard); I'll keep trying.")


@pytest.mark.parametrize("standby", [False, True])
def test_updated_rules_at_relogin_keep_the_runtime_and_its_matches_and_say_what_to_do(standby):
    # v4.2.4 (SDK PR #7): the Official Rules updated mid-event (re-login answers 403 registration_incomplete,
    # next_step "rules") used to end the runtime and terminate every worker, and run_agent.sh restarted it in a loop
    # until the owner re-accepted. Now the runtime keeps its running games, says what to do (once, then at most every
    # 5 minutes), asks again every 30 s and picks the listing up as soon as the Rules are accepted. Also through the
    # hot standby's wrapper, which passes the listing's errors through untouched.
    import httpx

    from altruagent.supervisor import run_tournament_forever
    from tests.test_supervisor import FakeProcessFactory

    calls, clock, log, factory = {"auth": 0, "list": 0, "refused": 0}, {"t": 0.0}, [], FakeProcessFactory()
    accepted_at = 100.0                                   # the owner accepts the updated Rules here

    def backend(request):
        path = request.url.path
        if path == "/tournament/agent/authenticate":
            calls["auth"] += 1
            if calls["auth"] == 1 or clock["t"] >= accepted_at:
                return httpx.Response(200, json={"access_token": f"session-{calls['auth']}"})
            calls["refused"] += 1
            return httpx.Response(403, json={"error": "registration_incomplete", "detail": "Official Rules updated",
                                             "next_step": "rules"})
        if path.endswith("/grant"):                       # (the standby's seat check: no other runtime holds it)
            return httpx.Response(200, json={"access_token": "seat-token", "game_session_id": "g1", "agent_id": "a1",
                                             "gameapi_server_url": "https://game.example.test", "seat_id": "s1"})
        calls["list"] += 1
        if calls["list"] == 1 or request.headers["authorization"] != "Bearer session-1":
            return httpx.Response(200, json={"assignments": [
                {"match_id": "m1", "seat_id": "s1", "game_type": "pokemon_vgc_doubles_draft"}]})
        return httpx.Response(401, json={"error": "invalid_agent_session"})   # session-1 expired after one listing

    def sleep(s):
        clock["t"] += s

    official = _official(backend)
    if standby:
        agent_main._HotStandby.install(official, now=lambda: clock["t"], log=log.append, hold_new_s=0.0)
    run_tournament_forever(official, agent_spec="agent.agent:create_agent", sleep=sleep, now=lambda: clock["t"],
                           process_factory=factory, log=log.append, max_iterations=16, poll_interval=10.0,
                           cooldown_seconds=10.0)
    assert len(factory.processes) == 1                    # the seat's worker, started once, never stopped or restarted
    assert not [line for line in log if "no longer assigned" in line]
    assert log.count(RULES_LINE) == 1                     # said what to do, once (the reminder is every 5 minutes)
    assert "Assignment discovery recovered." in log      # picked up again once the Rules were accepted
    assert 2 <= calls["refused"] <= 5                     # asked again every 30 s (t = 10, 40, 70), not every poll
    assert [line for line in log if "active match worker(s)" in line] == ["Stopping 1 active match worker(s)..."]
    assert log[-1] == "Stopping 1 active match worker(s)..."          # (only when the bounded test loop ends)


@pytest.mark.parametrize("hold, expected", [(0.0, [0.0, 40.0]), (20.0, [20.0, 60.0])])
def test_a_standby_holds_new_seats_back_and_keeps_the_ones_it_started(hold, expected):
    # the platform gives a new seat to whichever runtime claims it first: a standby (ARENA_NEW_SEAT_DELAY_S) leaves it
    # to the primary, which claims it within one poll; a seat it did hand to the SDK stays listed (never "unassigned"),
    # even across a listing that misses it once (the SDK tolerates one; a second, held-back one would stop its worker)
    import httpx

    from altruagent.supervisor import run_tournament_forever
    from tests.test_supervisor import FakeProcessFactory

    clock, starts, log, factory = {"t": 0.0}, [], [], FakeProcessFactory()

    def backend(request):
        if request.url.path == "/tournament/agent/authenticate":
            return httpx.Response(200, json={"access_token": "session-1"})
        rows = [{"match_id": "m1", "seat_id": "s1", "game_type": "werewolf"}]
        if clock["t"] >= 40 and clock["t"] != 70:
            rows.append({"match_id": "m2", "seat_id": "s2", "game_type": "pokemon_vgc_doubles_draft"})
        return httpx.Response(200, json={"assignments": rows})

    def spawn(target, args=(), daemon=None):
        starts.append(clock["t"])
        return factory(target, args=args, daemon=daemon)

    def sleep(s):
        clock["t"] += s

    official = _official(backend)
    agent_main._HotStandby.install(official, now=lambda: clock["t"], log=log.append, hold_new_s=hold)
    run_tournament_forever(official, agent_spec="agent.agent:create_agent", sleep=sleep, now=lambda: clock["t"],
                           process_factory=spawn, log=log.append, max_iterations=9, poll_interval=10.0,
                           cooldown_seconds=10.0)
    assert starts == expected
    assert len(factory.processes) == 2 and not [x for x in log if "no longer assigned" in x]
    assert any("standby: new seat s2; leaving it to the primary for 20s" in x for x in log) == bool(hold)


def test_a_standby_plays_only_seats_no_runtime_holds_and_takes_one_over_as_its_lease_lapses():
    # A standby checks each held seat with its grant (the runtime's own execution id) at every poll: seat_busy while
    # the primary holds the lease -> no worker (no process, no agent session); the grant that succeeds (the lease
    # lapsed) hands the seat to the SDK in that same poll, instead of a seat_busy worker retried 35 s later.
    import httpx

    from altruagent.supervisor import run_tournament_forever
    from tests.test_supervisor import FakeProcessFactory

    clock, starts, log, factory, grants = {"t": 0.0}, [], [], FakeProcessFactory(), []

    def backend(request):
        path = request.url.path
        if path == "/tournament/agent/authenticate":
            return httpx.Response(200, json={"access_token": "session-1"})
        if path.endswith("/grant"):
            seat = path.split("/")[-2]
            grants.append((clock["t"], seat, json.loads(request.content)["execution_id"]))
            if seat == "s1" and clock["t"] < 50:                    # the primary's lease on s1 lapses at t=50
                return httpx.Response(409, json={"error": "seat_busy"})
            if seat == "s2":                                        # (a seat whose match just ended)
                return httpx.Response(409, json={"error": "assignment_not_grantable"})
            return httpx.Response(200, json={"access_token": "seat-token", "game_session_id": "g1", "agent_id": "a1",
                                             "gameapi_server_url": "https://game.example.test", "seat_id": seat})
        rows = [{"match_id": "m1", "seat_id": "s1", "game_type": "pokemon_vgc_doubles_draft"}]
        if clock["t"] >= 30:
            rows.append({"match_id": "m2", "seat_id": "s2", "game_type": "werewolf"})
        return httpx.Response(200, json={"assignments": rows})

    def spawn(target, args=(), daemon=None):
        starts.append((clock["t"], args[0].seat_id, args[0].execution_id))
        return factory(target, args=args, daemon=daemon)

    def sleep(s):
        clock["t"] += s

    official = _official(backend)
    restore, eid = agent_main._tournament_tuning({})                 # (as _run_tournament: this runtime's id, fixed)
    try:
        agent_main._HotStandby.install(official, now=lambda: clock["t"], log=log.append, hold_new_s=20.0,
                                       execution_id=eid)
        run_tournament_forever(official, agent_spec="agent.agent:create_agent", sleep=sleep, now=lambda: clock["t"],
                               process_factory=spawn, log=log.append, max_iterations=10, poll_interval=10.0,
                               cooldown_seconds=10.0)
    finally:
        restore()
    assert starts == [(50.0, "s1", eid)]                             # s1 once its lease was free; never s2
    assert [t for t, seat, _ in grants if seat == "s1"] == [20.0, 30.0, 40.0, 50.0]   # held 20 s, then every poll
    assert [t for t, seat, _ in grants if seat == "s2"] == [50.0, 60.0, 70.0, 80.0, 90.0]
    assert {e for _, _, e in grants} == {eid}                        # the id its workers use
    assert sum("seat s1 is held by another runtime" in x for x in log) == 1          # logged once, not every poll
    assert sum("seat s1 is free" in x for x in log) == 1 and sum("seat s2 not taken" in x for x in log) == 1
    assert "Another runtime is playing this match with your Official Agent Key" not in " ".join(log)


class _Row:
    def __init__(self, seat_id):
        self.seat_id = seat_id


def test_the_standbys_seat_check_never_keeps_a_seat_unplayed_on_a_fault_of_its_own():
    clock, seen = {"t": 0.0}, []

    def broken_grant(seat_id, execution_id):
        seen.append(seat_id)
        raise TypeError("a bug")

    standby = agent_main._HotStandby(lambda: [_Row("s1")], now=lambda: clock["t"], log=lambda m: None,
                                     hold_new_s=20.0, grant=broken_grant, execution_id="x" * 43)
    assert standby() == [] and seen == []                            # a new seat: left to the primary for 20 s
    clock["t"] = 20.0
    assert [r.seat_id for r in standby()] == ["s1"] and seen == ["s1"]  # the check itself failed: handed anyway
    clock["t"] = 30.0
    assert [r.seat_id for r in standby()] == ["s1"] and seen == ["s1"]  # handed once: never checked again
    plain = agent_main._HotStandby(lambda: [_Row("s2")], now=lambda: clock["t"], log=lambda m: None,
                                   hold_new_s=20.0)                 # nothing to check with: the hold alone, as before
    assert plain() == []
    clock["t"] = 50.0
    assert [r.seat_id for r in plain()] == ["s2"]


def test_transient_classification():
    # what the FIRST authentication waits out (_authenticate); once the runtime runs, the SDK's is_fatal_auth_error
    # decides about re-authentication failures (the tests above)
    from altruagent.official import OfficialAgentError as OffErr
    assert agent_main._transient(OffErr("x", status_code=429, error_code="rate_limited"))
    assert agent_main._transient(OffErr("x", status_code=503)) and agent_main._transient(OffErr("x", status_code=502))
    assert agent_main._transient(PlatformError("Could not reach", status_code=None))
    assert not agent_main._transient(OffErr("x", status_code=401, error_code="invalid_official_agent_key"))
    assert not agent_main._transient(OffErr("no session token in the answer"))          # (status None)
    assert not agent_main._transient(PlatformError("bad request", status_code=400))
    # review 10-07, as upstream 1372ccc: a failed session mint is temporary ...
    assert agent_main._transient(OffErr("x", status_code=401, error_code="invalid_official_agent_key", detail=MINT_DETAIL))
    # ... a refused key, a key the platform names Oracle-hosted, an incomplete registration are not
    assert not agent_main._transient(OffErr("x", status_code=401, error_code="invalid_official_agent_key",
                                            detail="Invalid official agent key"))
    assert not agent_main._transient(OffErr("x", status_code=401, error_code="invalid_official_agent_key",
                                            detail="This agent is Oracle Hosted; ..."))
    assert not agent_main._transient(OffErr("x", status_code=403, error_code="registration_incomplete"))
    assert not agent_main._transient(OffErr("not accepted", status_code=401))
    assert not agent_main._transient(OffErr("x", status_code=401, error_code="invalid_official_agent_key",
                                            detail={"not": "a string"}))


def test_the_first_authentication_waits_out_a_transient_failure(monkeypatch, capsys):
    from altruagent.official import OfficialAgentError as OffErr
    slept = []
    monkeypatch.setattr(agent_main, "_sleep", slept.append)

    class Flaky:
        def __init__(self, errors):
            self.errors = list(errors)

        def authenticate(self):
            if self.errors:
                raise self.errors.pop(0)

    assert agent_main._authenticate(Flaky([OffErr("down", status_code=503), PlatformError("net", status_code=None),
                                           OffErr("slow down", status_code=429, error_code="rate_limited")]))
    assert slept == [5.0, 10.0, 20.0] and capsys.readouterr().out.count("retrying in") == 3
    assert not agent_main._authenticate(Flaky([OffErr("The Official Agent Key was not accepted.", status_code=401)]))
    assert slept == [5.0, 10.0, 20.0] and "Could not connect with your Official Agent Key: The Official Agent Key" \
        in capsys.readouterr().out


def test_an_incomplete_registration_at_start_starts_the_runtime_anyway(monkeypatch, capsys):
    # v4.2.4 (as upstream's start since SDK PR #7): 403 registration_incomplete (e.g. updated Official Rules) used to
    # end the start ("Could not connect ...", exit 1) and run_agent.sh restarted it in a loop. The key is fine: the
    # runtime starts without a session and its supervisor says what to do and keeps checking (the test above and
    # tests/test_main_tournament.py). No retry of our own here, no "Connected" line.
    from altruagent.official import OfficialAgentError as OffErr
    slept, tries = [], []
    monkeypatch.setattr(agent_main, "_sleep", slept.append)

    class Rules:
        def authenticate(self):
            tries.append(1)
            err = OffErr("Accept the updated Official Rules on your dashboard (x).", status_code=403,
                         error_code="registration_incomplete")
            err.next_step = "rules"
            raise err

    assert agent_main._authenticate(Rules()) is True
    assert len(tries) == 1 and slept == []
    out = capsys.readouterr().out
    assert "Connected with your Official Agent Key" not in out and "Could not connect" not in out


def test_a_failed_session_mint_at_start_is_waited_out_not_reported_as_a_bad_key(monkeypatch, capsys):
    # review 10-07 (A7): through the real client, 401 invalid_official_agent_key "Failed to mint agent session" ended
    # the start after one try with "The Official Agent Key was not accepted. Copy it again ..." (a working key)
    import httpx
    slept, tries = [], {"n": 0}
    monkeypatch.setattr(agent_main, "_sleep", slept.append)

    def backend(request):
        tries["n"] += 1
        if tries["n"] <= 2:
            return httpx.Response(401, json={"error": "invalid_official_agent_key", "detail": MINT_DETAIL})
        return httpx.Response(200, json={"access_token": "session-1"})

    assert agent_main._authenticate(_official(backend)) and tries["n"] == 3
    out = capsys.readouterr().out
    assert slept == [5.0, 10.0] and out.count("couldn't start an agent session") == 2 and "not your key" in out
    assert "copy the key again" not in out.lower()

    def revoked(request):
        return httpx.Response(401, json={"error": "invalid_official_agent_key", "detail": "Invalid official agent key"})

    assert not agent_main._authenticate(_official(revoked))
    assert slept == [5.0, 10.0] and "Could not connect with your Official Agent Key: The Official Agent Key was not" \
        in capsys.readouterr().out


def test_check_tournament_does_not_call_a_failed_session_mint_a_rejected_key(monkeypatch, capsys):
    from altruagent.official import OfficialAgentError as OffErr

    class Official:
        control_url = "https://control.example.test"

        def __init__(self, error):
            self.error = error

        def authenticate(self):
            raise self.error

        def close(self):
            pass

    for error, expected in [
        (OffErr("x", status_code=401, error_code="invalid_official_agent_key", detail=MINT_DETAIL),
         "Official agent authentication failed: x"),
        (OffErr("not accepted", status_code=401, error_code="invalid_official_agent_key",
                detail="Invalid official agent key"), "Official Agent Key rejected: not accepted"),
    ]:
        monkeypatch.setattr(agent_main, "OfficialAgentClient", lambda error=error: Official(error))
        assert agent_main._check_tournament("agent.agent:create_agent") == 1
        assert expected in capsys.readouterr().out.splitlines()[-1]


def test_outage_backoffs_are_capped_at_30s(monkeypatch):
    # review 10-06: the 40 / 60 s steps (start-up retry, rate-limit pause) left the runtime blind to new seats for up to
    # ~60 s after the control plane recovered (measured 35 s and 56 s); a Werewolf seat has 120 s, a team preview 90 s
    from altruagent.official import OfficialAgentError as OffErr
    slept = []
    monkeypatch.setattr(agent_main, "_sleep", slept.append)

    class Down:
        def __init__(self, n):
            self.n = n

        def authenticate(self):
            if self.n:
                self.n -= 1
                raise OffErr("down", status_code=503)

    assert agent_main._authenticate(Down(6))
    assert slept == [5.0, 10.0, 20.0, 30.0, 30.0, 30.0]


@pytest.mark.parametrize("hold, wrapped", [(None, False), ("20", True)])
def test_the_event_runtime_hands_the_sdk_both_kinds_the_knobs_and_the_standby_only_when_asked(monkeypatch, hold,
                                                                                             wrapped):
    from altruagent.supervisor import ALL_KINDS
    seen = {}

    class FakeOfficial:
        control_url = "https://control.example.test"

        def authenticate(self):
            pass

        def assignments(self):
            return []

        def close(self):
            pass

    official = FakeOfficial()

    import altruagent.supervisor as sup

    def fake_forever(client, *, agent_spec, **kw):
        seen.update(client=client, agent_spec=agent_spec, backoff_max=sup.SEAT_FAILURE_BACKOFF_MAX_SECONDS, **kw)

    for k in ("ARENA_EXECUTION_ID", "ARENA_SEAT_BUSY_RETRY_S", "ARENA_NEW_SEAT_DELAY_S", "ARENA_SEAT_BACKOFF_MAX_S"):
        monkeypatch.delenv(k, raising=False)
    if hold:
        monkeypatch.setenv("ARENA_NEW_SEAT_DELAY_S", hold)            # (run_agent.sh: role=standby)
    monkeypatch.setenv("ARENA_DISCOVERY_S", "10")                     # (what _startup / run_agent.sh set)
    monkeypatch.setenv("ARENA_WORKER_COOLDOWN_S", "10")
    monkeypatch.setattr(agent_main, "OfficialAgentClient", lambda *a, **k: official)
    monkeypatch.setattr(agent_main, "run_tournament_forever", fake_forever)
    assert agent_main.main(["--tournament", "--match"]) == 0
    assert seen["client"] is official and seen["kinds"] == ALL_KINDS
    assert isinstance(official.assignments, agent_main._HotStandby) == wrapped
    assert seen["poll_interval"] == 10.0 and seen["cooldown_seconds"] == 10.0     # (SDK: 10 s / 60 s)
    assert seen["backoff_max"] == 30.0 and sup.SEAT_FAILURE_BACKOFF_MAX_SECONDS == 600.0   # (30 s while it ran)


def test_the_run_loops_execution_id_is_used_only_from_its_run_loop(monkeypatch, capsys):
    import altruagent.supervisor as sup
    eid = "ab" * 32
    env = {"ARENA_EXECUTION_ID": eid, "ARENA_EXECUTION_ID_OWNER": "4242"}
    assert agent_main._take_execution_id(env, parent_pid=4242) == eid
    assert env == {}                                                  # gone: no worker or node child inherits it
    env = {"ARENA_EXECUTION_ID": eid, "ARENA_EXECUTION_ID_OWNER": "4242"}
    assert agent_main._take_execution_id(env, parent_pid=999) is None     # copied into a .env / onto another machine
    assert "Ignoring ARENA_EXECUTION_ID" in capsys.readouterr().out and env == {}
    assert agent_main._take_execution_id({"ARENA_EXECUTION_ID": "short", "ARENA_EXECUTION_ID_OWNER": "1"}, 1) is None
    assert agent_main._take_execution_id({}) is None
    # applied to the SDK's supervisor module for this runtime (new_execution_id, SEAT_BUSY_RETRY_SECONDS,
    # SEAT_FAILURE_BACKOFF_MAX_SECONDS), restored after
    before = (sup.new_execution_id, sup.SEAT_BUSY_RETRY_SECONDS, sup.SEAT_FAILURE_BACKOFF_MAX_SECONDS)
    restore, used = agent_main._tournament_tuning({"ARENA_EXECUTION_ID": eid, "ARENA_SEAT_BUSY_RETRY_S": "12",
                                                   "ARENA_SEAT_BACKOFF_MAX_S": "45",
                                                   "ARENA_EXECUTION_ID_OWNER": str(os.getppid())})
    try:
        assert used == eid and sup.TournamentState().execution_id == eid and sup.SEAT_BUSY_RETRY_SECONDS == 12.0
        assert sup.SEAT_FAILURE_BACKOFF_MAX_SECONDS == 45.0
    finally:
        restore()
    assert (sup.new_execution_id, sup.SEAT_BUSY_RETRY_SECONDS, sup.SEAT_FAILURE_BACKOFF_MAX_SECONDS) == before
    assert sup.TournamentState().execution_id != eid
    restore, fresh = agent_main._tournament_tuning({})               # no run loop: a fresh id, as upstream, but fixed
    try:                                                             # here so the standby's seat check uses it too
        assert fresh != eid and len(fresh) >= 32 and sup.TournamentState().execution_id == fresh
        assert sup.SEAT_FAILURE_BACKOFF_MAX_SECONDS == agent_main.SEAT_BACKOFF_MAX_S == 30.0     # (the default cap)
    finally:
        restore()
    assert (sup.new_execution_id, sup.SEAT_BUSY_RETRY_SECONDS, sup.SEAT_FAILURE_BACKOFF_MAX_SECONDS) == before
    restore, _ = agent_main._tournament_tuning({"ARENA_SEAT_BACKOFF_MAX_S": "soon"})     # a bad value: the default
    try:
        assert sup.SEAT_FAILURE_BACKOFF_MAX_SECONDS == 30.0
        assert "Ignoring ARENA_SEAT_BACKOFF_MAX_S='soon'" in capsys.readouterr().out
    finally:
        restore()
    assert (sup.new_execution_id, sup.SEAT_BUSY_RETRY_SECONDS, sup.SEAT_FAILURE_BACKOFF_MAX_SECONDS) == before


# ---- `python -m agent` without run_agent.sh: a control-plane default, and loud about a degraded machine -------------
def test_the_control_url_defaults_to_the_platform_unless_configured(monkeypatch, tmp_path):
    import dotenv
    monkeypatch.setattr(dotenv, "find_dotenv", lambda *a, **k: "")
    env = {}
    assert agent_main._control_url_default(env) == "https://api.altruagent-game.com"
    assert env["ALTRUAGENT_CONTROL_URL"] == "https://api.altruagent-game.com"
    env = {"ALTRUAGENT_CONTROL_URL": "http://127.0.0.1:9"}
    assert agent_main._control_url_default(env) is None and env["ALTRUAGENT_CONTROL_URL"] == "http://127.0.0.1:9"
    dot = tmp_path / ".env"
    dot.write_text("ALTRUAGENT_CONTROL_URL=http://127.0.0.1:8\n")
    monkeypatch.setattr(dotenv, "find_dotenv", lambda *a, **k: str(dot))
    env = {}
    assert agent_main._control_url_default(env) is None and env == {}   # (the SDK's load_dotenv() will read it)


def test_startup_is_loud_about_a_degraded_machine_and_quiet_under_run_agent(monkeypatch, capsys):
    import dotenv
    monkeypatch.setattr(dotenv, "find_dotenv", lambda *a, **k: "")
    bad = [(True, "agent data files and the draft model are present"),
           (False, "damage calculator cannot run (no node found): the heuristic wins only ~12% against the calculator")]
    monkeypatch.setattr(agent_main, "_local_checks", lambda check_search=True: bad)
    monkeypatch.setattr(agent_main, "_mark", lambda symbol, fallback: symbol)
    env = {"ALTRUAGENT_CONTROL_URL": "http://127.0.0.1:9"}
    assert agent_main._startup(["--tournament"], env) is False
    out, err = capsys.readouterr()
    assert "WARNING: THIS MACHINE PLAYS DEGRADED" in out and "WARNING: THIS MACHINE PLAYS DEGRADED" in err
    assert "damage calculator cannot run (no node found)" in err
    assert agent_main._startup(["--check-tournament"], env) is False
    assert capsys.readouterr().out.splitlines() == ["✓ Agent data files and the draft model are present",
                                                    "✗ Damage calculator cannot run (no node found): the heuristic "
                                                    "wins only ~12% against the calculator"]
    env = {"ARENA_RUN_ID": "main@/x"}                                 # run_agent.sh already checked and refuses
    assert agent_main._startup(["--tournament", "--match"], env) is True
    assert env["ALTRUAGENT_CONTROL_URL"] == "https://api.altruagent-game.com"
    assert "DEGRADED" not in capsys.readouterr().out
    env = {}
    assert agent_main._startup(["--claim", "seatclaim_FAKE"], env) is True and env == {}   # retired: untouched
    assert agent_main._startup(["--tournament", "--help"], env) is True and env == {}
    monkeypatch.setattr(agent_main, "_local_checks", lambda check_search=True: [(True, "all fine")])
    assert agent_main._startup(["--match"], {"ALTRUAGENT_CONTROL_URL": "x"}) is True
    assert capsys.readouterr().out.strip() == "Local checks: all fine"


def _plain_agent(args, extra_env, timeout=120):
    """`python -m agent ARGS` as a harness would run it (no run_agent.sh), never able to reach a real platform: the
    keys are fake or malformed and the control plane is a dead local port."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ALTRUAGENT_", "ARENA_"))}
    env.update(PYTHONPATH=str(ROOT), ARENA_RECORD="0", ALTRUAGENT_CONTROL_URL="http://127.0.0.1:9", **extra_env)
    return subprocess.run([sys.executable, "-m", "agent", *args], cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=timeout)


def test_plain_python_m_agent_says_loudly_when_it_would_play_degraded():
    r = _plain_agent(["--tournament", "--match"], {"ALTRUAGENT_OFFICIAL_AGENT_KEY": "eak_live_FAKE_not_hex",
                                                   "ARENA_CALC": "off", "ARENA_BATTLE": "classic"})
    assert r.returncode == 1                                         # (the malformed key: a configuration error)
    assert "WARNING: THIS MACHINE PLAYS DEGRADED" in r.stdout and "WARNING: THIS MACHINE PLAYS DEGRADED" in r.stderr
    assert "damage calculator cannot run (ARENA_CALC=off)" in r.stderr
    assert "doesn't look like an Official Agent Key" in r.stdout
    assert "eak_live_FAKE_not_hex" not in r.stdout + r.stderr


def test_plain_python_m_agent_without_a_mode_only_prints_the_retirement_notice():
    from altruagent.notices import PLATFORM_KEY_RETIRED
    r = _plain_agent([], {"ALTRUAGENT_API_KEY": "sk_agent_FAKE_do_not_use_0000", "ARENA_CALC": "off"})
    assert r.returncode == 1 and r.stdout.startswith(PLATFORM_KEY_RETIRED), r.stdout + r.stderr
    assert "python -m agent --tournament --match" in r.stdout
    assert "DEGRADED" not in r.stdout + r.stderr                     # (nothing runs: no local checks either)
    assert "sk_agent_FAKE_do_not_use_0000" not in r.stdout + r.stderr


def test_plain_check_tournament_reports_the_machine_and_a_malformed_key_without_contacting_anything():
    r = _plain_agent(["--check-tournament"], {"ALTRUAGENT_OFFICIAL_AGENT_KEY": "eak_live_FAKE_not_hex",
                                              "ARENA_BATTLE": "classic"})
    assert r.returncode == 1
    lines = r.stdout.splitlines()
    assert any(x.startswith(("✓ Agent data files", "[ok] Agent data files")) for x in lines), r.stdout
    assert any("doesn't look like an Official Agent Key" in x for x in lines)
    assert "eak_live_FAKE_not_hex" not in r.stdout + r.stderr


# ---- agent/pokemon/calc.py deployment knobs ------------------------------------------------------------------------
def _fake_node(path: Path, script: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + script + "\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def clean_calc_env(monkeypatch):
    monkeypatch.delenv("ARENA_CALC", raising=False)
    monkeypatch.delenv("ARENA_NODE", raising=False)
    calc._node_version.cache_clear()
    yield monkeypatch
    calc._node_version.cache_clear()


def test_arena_calc_off_turns_the_calculator_off(clean_calc_env):
    clean_calc_env.setenv("ARENA_CALC", "off")
    dc = calc.DamageCalc()
    assert not dc.enabled and dc.batch([calc.WARM_CALC]) is None and dc.proc is None and dc.starts == 0
    assert calc.probe() == (False, "ARENA_CALC=off")


def test_a_stale_arena_node_is_skipped_not_fatal(clean_calc_env, tmp_path, caplog):
    clean_calc_env.setattr(calc.dex, "repo_root", lambda: tmp_path)             # no node/ here
    clean_calc_env.setenv("ARENA_NODE", "/nonexistent/node")
    clean_calc_env.setattr(calc.shutil, "which", lambda name: "/usr/bin/node" if name == "node" else None)
    with caplog.at_level("WARNING", logger="agent.calc"):
        assert calc._node_bin() == "node"                                      # PATH node, not "calculator off"
    clean_calc_env.setattr(calc.shutil, "which", lambda name: None)             # and no node anywhere: off
    assert calc._node_bin() is None


def test_unset_knobs_keep_the_old_node_detection(clean_calc_env):
    assert calc.DamageCalc().enabled == (shutil.which("node") is not None and calc._js_dir() is not None)
    if shutil.which("node") and not (calc.dex.repo_root() / "node" / "bin" / "node").exists():
        assert calc.DamageCalc._command(Path("/x"))[0] == "node"


def test_a_bundled_node_that_runs_comes_before_path(clean_calc_env, tmp_path):
    clean_calc_env.setattr(calc.dex, "repo_root", lambda: tmp_path)
    clean_calc_env.setattr(calc.shutil, "which", lambda name: "/usr/bin/node" if name == "node" else None)
    assert calc._node_bin() == "node"                                          # no node/ in a dev checkout
    node = _fake_node(tmp_path / "node" / "bin" / "node", "echo v22.23.3")
    calc._node_version.cache_clear()
    assert calc._node_bin() == str(node)                                       # the bundle's pinned build first
    _fake_node(node, "exit 1")                                                 # a binary that cannot run here
    calc._node_version.cache_clear()
    assert calc._node_bin() == "node"
    clean_calc_env.setattr(calc.shutil, "which", lambda name: None)
    assert calc._node_bin() is None


def test_probe_names_what_is_wrong(clean_calc_env, tmp_path):
    clean_calc_env.setattr(calc.dex, "repo_root", lambda: tmp_path)
    clean_calc_env.setattr(calc.shutil, "which", lambda name: None)
    ok, why = calc.probe()
    assert not ok and why.startswith("no node found")
    old = _fake_node(tmp_path / "old" / "node", "echo v16.20.0")
    clean_calc_env.setenv("ARENA_NODE", str(old))
    if calc._js_paths() is not None:
        ok, why = calc.probe()
        assert not ok and "older than 18" in why
    broken = _fake_node(tmp_path / "broken" / "node", 'case "$1" in --version) echo v22.1.0 ;; *) exit 3 ;; esac')
    clean_calc_env.setenv("ARENA_NODE", str(broken))
    if calc._js_paths() is not None:
        ok, why = calc.probe(attempts=1)
        assert not ok and "did not answer a calc" in why


def test_arena_calc_off_turns_off_the_calculator_only_not_the_engine_search(clean_calc_env):
    # deploy-1: engine.py used the calculator's node lookup, so ARENA_CALC=off also switched the engine search off
    clean_calc_env.setattr(calc.shutil, "which", lambda name: "/usr/bin/node" if name == "node" else None)
    clean_calc_env.setattr(calc.dex, "repo_root", lambda: Path("/nonexistent"))
    clean_calc_env.setenv("ARENA_CALC", "off")
    assert calc._node_bin() is None and calc.find_node() == "node" and engine._node_bin() == "node"
    assert engine.available()[1] != "no node"
    clean_calc_env.setattr(calc.shutil, "which", lambda name: None)
    assert engine.available() == (False, "no node")


def test_node_children_get_no_keys(monkeypatch):
    # robust-8: calc.js / search.js inherited the whole environment, the agent keys included
    for k, v in {"ALTRUAGENT_API_KEY": "sk_agent_FAKE_canary_0000", "ALTRUAGENT_OFFICIAL_AGENT_KEY": FAKE_EAK,
                 "OPENAI_API_KEY": "sk-FAKE-canary", "NODE_PATH": "/extra", "ARENA_SHOWDOWN": "/ps",
                 "PATH": "/usr/bin:/bin"}.items():
        monkeypatch.setenv(k, v)
    env = calc.node_env([Path("/mods/a"), Path("/mods/b")])
    assert not [k for k in env if "KEY" in k] and "sk_agent_FAKE_canary_0000" not in env.values()
    assert env["NODE_PATH"] == os.pathsep.join(["/mods/a", "/mods/b", "/extra"])
    assert env["PATH"] == "/usr/bin:/bin" and env["ARENA_SHOWDOWN"] == "/ps"
    assert set(env) <= set(calc.NODE_CHILD_ENV) | {"NODE_PATH"}


@needs_linux
def test_the_node_version_check_gets_no_keys_either(clean_calc_env, tmp_path):
    # robust-8 covered calc.js / search.js; `node/bin/node --version` (find_node: once in every worker of a bundle,
    # and the probes) still inherited the whole environment, the Official Agent Key included
    clean_calc_env.setenv("ALTRUAGENT_OFFICIAL_AGENT_KEY", FAKE_EAK)
    clean_calc_env.setenv("ALTRUAGENT_API_KEY", "sk_agent_FAKE_canary_0000")
    clean_calc_env.setattr(calc.dex, "repo_root", lambda: tmp_path)
    seen = tmp_path / "seen-env"
    node = _fake_node(tmp_path / "node" / "bin" / "node", f'env > "{seen}"; echo v22.23.3')
    assert calc.find_node() == str(node)
    assert "PATH=" in seen.read_text() and "ALTRUAGENT" not in seen.read_text() and FAKE_EAK not in seen.read_text()


def _environ_and_oom(pid: int) -> tuple[bytes, int]:
    return Path(f"/proc/{pid}/environ").read_bytes(), int(Path(f"/proc/{pid}/oom_score_adj").read_text())


@needs_linux
@needs_node
def test_the_calculator_process_has_no_keys_and_goes_first_under_memory_pressure(clean_calc_env):
    clean_calc_env.setenv("ALTRUAGENT_OFFICIAL_AGENT_KEY", FAKE_EAK)
    clean_calc_env.setenv("ALTRUAGENT_API_KEY", "sk_agent_FAKE_canary_0000")
    dc = calc.DamageCalc()
    try:
        assert dc._start()
        env, oom = _environ_and_oom(dc.proc.pid)
        assert FAKE_EAK.encode() not in env and b"sk_agent_FAKE_canary_0000" not in env and b"NODE_PATH=" in env
        assert oom == calc.CALC_OOM_SCORE == 500
    finally:
        dc._kill()


@needs_linux
@pytest.mark.skipif(shutil.which("node") is None or engine._paths() is None, reason="needs node + pokemon-showdown")
def test_the_search_process_has_no_keys_and_is_the_first_oom_victim(monkeypatch):
    monkeypatch.setenv("ALTRUAGENT_OFFICIAL_AGENT_KEY", FAKE_EAK)
    e = engine.SearchEngine()
    try:
        assert e._start()
        env, oom = _environ_and_oom(e.proc.pid)
        assert FAKE_EAK.encode() not in env and oom == engine.SEARCH_OOM_SCORE == 800
    finally:
        e._kill()


def test_a_failed_recording_warns_once(monkeypatch, tmp_path, caplog):
    import types

    from agent import agent as agent_py
    blocker = tmp_path / "file"
    blocker.write_text("x")                                          # a "directory" that is a file: unwritable
    monkeypatch.setenv("ARENA_RECORD", "1")
    monkeypatch.setattr(agent_py, "RECORD_DIR", str(blocker / "logs"))
    monkeypatch.setattr(agent_py, "_record_failed", [])
    ctx = types.SimpleNamespace(game_type="werewolf", session_id="s1")
    state = types.SimpleNamespace(raw={"phase": "moving"})
    with caplog.at_level("WARNING", logger="agent"):
        agent_py.record("action", ctx, state)
        agent_py.record("action", ctx, state)
    warnings = [r for r in caplog.records if "recording to" in r.getMessage()]
    assert len(warnings) == 1 and "ARENA_RECORD_DIR" in warnings[0].getMessage()


@needs_node
def test_probe_and_arena_node_run_a_real_calc(clean_calc_env):
    clean_calc_env.setenv("ARENA_NODE", shutil.which("node"))
    ok, why = calc.probe()
    assert ok and shutil.which("node") in why, why
    dc = calc.DamageCalc()
    try:
        assert dc.enabled and dc._command(Path("/x"))[0] == shutil.which("node")
        res = dc.batch([calc.WARM_CALC])
        assert res and res[0]
    finally:
        dc._kill()


# ---- scripts/selfcheck.py: the calculator is required ----------------------------------------------------------------
def _selfcheck(env_extra: dict, *args) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(ROOT), **env_extra)
    for k in ("ARENA_ALLOW_NO_CALC", "ARENA_NODE"):
        if k not in env_extra:
            env.pop(k, None)
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "selfcheck.py"), "--no-replay", *args], env=env,
                          capture_output=True, text=True, timeout=180)


def test_selfcheck_fails_without_the_calculator_unless_allowed():
    r = _selfcheck({"ARENA_CALC": "off"})
    assert r.returncode == 1, r.stdout
    assert "FAIL  damage calculator OFF (ARENA_CALC=off)" in r.stdout and "THE DAMAGE CALCULATOR CANNOT RUN" in r.stdout
    assert "selfcheck: FAILED" in r.stdout
    r = _selfcheck({"ARENA_CALC": "off", "ARENA_ALLOW_NO_CALC": "1"})
    assert r.returncode == 0 and "WARN  damage calculator OFF" in r.stdout and "selfcheck: ready" in r.stdout, r.stdout
    r = _selfcheck({"ARENA_CALC": "off"}, "--allow-no-calc")
    assert r.returncode == 0, r.stdout


@needs_node
def test_selfcheck_is_ready_when_the_calculator_answers():
    r = _selfcheck({"ARENA_CALC": "on"}, "--require-calc")                     # (the old flag is still accepted)
    assert r.returncode == 0 and "OK    damage calculator answers: node v" in r.stdout, r.stdout


# ---- the engine search's simulator is required while ARENA_BATTLE=search (the default battle policy) -----------------
def _load_selfcheck(monkeypatch):
    import importlib.util
    monkeypatch.setenv("ARENA_RECORD", os.environ.get("ARENA_RECORD", "0"))    # (selfcheck sets it on import)
    spec = importlib.util.spec_from_file_location("selfcheck_under_test", ROOT / "scripts" / "selfcheck.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_selfcheck_fails_when_the_engine_search_cannot_run(monkeypatch, capsys):
    # without its simulator every battle decision would quietly play the classic policy (a git checkout set up with
    # agent/js's npm ci only, a Docker build from the repository root): FAIL unless ARENA_BATTLE=classic
    sc = _load_selfcheck(monkeypatch)
    monkeypatch.setattr(engine, "probe", lambda timeout=60.0: (False, "pokemon-showdown missing (test)"))
    monkeypatch.setenv("ARENA_BATTLE", "search")
    sc.check_search()
    assert len(sc.FAILS) == 1 and "engine search cannot run (pokemon-showdown missing (test))" in sc.FAILS[0]
    assert "ARENA_BATTLE=classic" in sc.FAILS[0] and "cd local/js && npm ci" in sc.FAILS[0]
    monkeypatch.delenv("ARENA_BATTLE")                                         # unset = the default = search
    sc.check_search()
    assert len(sc.FAILS) == 2
    monkeypatch.setenv("ARENA_BATTLE", "classic")                              # the classic policy on purpose
    capsys.readouterr()
    sc.check_search()
    assert len(sc.FAILS) == 2 and "OK    battle policy: classic" in capsys.readouterr().out
    monkeypatch.setenv("ARENA_BATTLE", "search")
    monkeypatch.setattr(engine, "probe", lambda timeout=60.0: (True, "simulator loads in 0.9s (test)"))
    sc.check_search()
    assert len(sc.FAILS) == 2 and "OK    battle policy: engine search" in capsys.readouterr().out


def test_selfcheck_lets_allow_no_calc_cover_the_search_only_when_node_itself_cannot_run(monkeypatch, capsys):
    sc = _load_selfcheck(monkeypatch)
    monkeypatch.setattr(engine, "probe", lambda timeout=60.0: (False, "no node"))
    monkeypatch.setenv("ARENA_BATTLE", "search")
    sc.check_search(calc_ok=False, allow_no_calc=True)                # node cannot run: a warning, like run_agent.sh
    assert not sc.FAILS and len(sc.WARNS) == 1 and "ARENA_BATTLE=classic" in sc.WARNS[0]
    sc.check_search(calc_ok=True, allow_no_calc=True)                 # node runs, the simulator is missing: FAIL
    assert len(sc.FAILS) == 1
    sc.check_search(calc_ok=False, allow_no_calc=False)
    assert len(sc.FAILS) == 2


def test_selfcheck_names_the_official_key_and_its_mode_never_the_key(monkeypatch, capsys):
    sc = _load_selfcheck(monkeypatch)
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)   # (never the checkout's real .env)
    for k in ("ALTRUAGENT_API_KEY", "ALTRUAGENT_OFFICIAL_AGENT_KEY", "ARENA_MODE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ALTRUAGENT_OFFICIAL_AGENT_KEY", FAKE_EAK)
    sc.check_key()
    out = capsys.readouterr().out
    assert "OK    Official Agent Key configured (#" in out and "mode tournament" in out and "check-tournament" in out
    assert FAKE_EAK not in out and not sc.FAILS
    monkeypatch.setenv("ALTRUAGENT_OFFICIAL_AGENT_KEY", "eak_live_FAKE_not_hex")
    sc.check_key()
    assert len(sc.FAILS) == 1 and "doesn't look like an Official Agent Key" in sc.FAILS[0]
    assert "eak_live_FAKE_not_hex" not in capsys.readouterr().out
    monkeypatch.delenv("ALTRUAGENT_OFFICIAL_AGENT_KEY")
    sc.check_key()
    assert "WARN  no key configured" in capsys.readouterr().out


def test_engine_probe_names_what_is_missing(monkeypatch):
    monkeypatch.setattr(engine, "_paths", lambda: None)
    monkeypatch.delenv("ARENA_SHOWDOWN", raising=False)
    good, why = engine.probe(5)
    assert not good and ("cd local/js && npm ci" in why or why == "no node"), why


@pytest.mark.skipif(shutil.which("node") is None or engine._paths() is None or calc._js_dir() is None,
                    reason="needs node, agent/js/node_modules and pokemon-showdown")
def test_selfcheck_loads_the_engine_search_for_real():
    r = _selfcheck({"ARENA_BATTLE": "search"})
    assert r.returncode == 0 and "OK    battle policy: engine search" in r.stdout, r.stdout
    assert "simulator loads in" in r.stdout


# ---- scripts run this tree's code -------------------------------------------------------------------------------------
_TREE_IMPORT = re.compile(r"^\s*(?:from|import)\s+(?:agent|local|champion|versions)\b", re.M)


def test_scripts_that_import_the_agent_put_their_own_tree_first_on_sys_path():
    """Review: the venv's editable install maps `agent` / `local` to the main checkout and `python scripts/x.py` puts
    only scripts/ on sys.path, so scripts/replay_real.py run in a worktree replayed main's code, not the branch's.
    Every script importing this tree's packages puts the tree root first on sys.path before the first such import."""
    checked = []
    for path in sorted((ROOT / "scripts").glob("*.py")):
        src = path.read_text(encoding="utf-8")
        first = _TREE_IMPORT.search(src)
        if first:
            ins = src.find("sys.path.insert(0")
            assert 0 <= ins < first.start(), f"{path.name} imports this tree's code without its root on sys.path"
            checked.append(path.name)
    assert {"replay_real.py", "preview_bench.py", "preview_ladder.py", "calibrate_calc.py",
            "compare_vgc.py"} <= set(checked)


# ---- scripts/run_agent.sh --------------------------------------------------------------------------------------------
FAKE_PY = r"""#!/usr/bin/env bash
# stands in for python in run_agent.sh tests: `-c ...` (preflight) succeeds, except the calculator probe when
# $FAKE_CALC holds "broken" and the engine search probe when $FAKE_ENGINE does; `-u -m agent` behaves per $FAKE_MODE
if [ "$1" = "-c" ]; then
  case "$2" in
    *calc.probe*)
      if [ "$(cat "$FAKE_CALC" 2>/dev/null)" = broken ]; then echo "PROBE no node found (fake)"; exit 1; fi
      echo "PROBE node v22.23.3 (fake), 0.01s incl. start-up" ;;
    *engine.probe*)
      if [ "$(cat "$FAKE_ENGINE" 2>/dev/null)" = broken ]; then echo "ENGINE pokemon-showdown missing (fake)"; exit 1; fi
      echo "ENGINE simulator loads in 0.9s (fake)" ;;
  esac
  exit 0
fi
echo "fake agent pid $$"
echo "fake agent args: $*"
owner=no; [ -n "${ARENA_EXECUTION_ID_OWNER:-}" ] && [ "${ARENA_EXECUTION_ID_OWNER}" = "$PPID" ] && owner=yes
echo "fake agent env: record_dir=${ARENA_RECORD_DIR:-unset} battle=${ARENA_BATTLE:-unset} eid_len=${#ARENA_EXECUTION_ID} eid_owner_ok=$owner new_seat_delay=${ARENA_NEW_SEAT_DELAY_S:-unset}"
[ -n "${FAKE_EID_FILE:-}" ] && printf '%s\n' "${ARENA_EXECUTION_ID:-}" >> "$FAKE_EID_FILE"
case "$*" in *--check-tournament*) echo "check: official key ${ALTRUAGENT_OFFICIAL_AGENT_KEY:+set} url ${ALTRUAGENT_CONTROL_URL:-unset}"; exit 0 ;; esac
case "$(cat "$FAKE_MODE" 2>/dev/null)" in
  crash) echo "crashing"; exit 1 ;;
  spam) for i in $(seq 1 300); do echo "line $i ................................................................"; done ;;
  termparent) kill -TERM "$PPID" ;;
esac
sleep 1000 &
echo "worker $!"
wait
"""


def _wait(pred, timeout=15.0, step=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


def _text(p: Path) -> str:
    try:
        return p.read_text()
    except OSError:
        return ""


def _state(logs: Path, name: str) -> dict:
    try:
        return dict(line.split("=", 1) for line in (logs / f"agent-{name}.state").read_text().splitlines() if "=" in line)
    except OSError:
        return {}


def _group_alive(pgid: int) -> list[int]:
    out = subprocess.run(["ps", "-eo", "pgid=,pid="], capture_output=True, text=True).stdout.split("\n")
    return [int(p) for g, p in (line.split() for line in out if line.strip()) if int(g) == pgid]


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def _procs_with_env(entry: str) -> list[int]:
    """Live processes whose environment holds `entry` (every process a run_agent.sh test starts inherits one)."""
    found = []
    for d in os.listdir("/proc"):
        if d.isdigit() and int(d) != os.getpid():
            try:
                with open(f"/proc/{d}/environ", "rb") as f:
                    if entry.encode() in f.read().split(b"\0") and _alive(int(d)):
                        found.append(int(d))
            except OSError:
                pass
    return found


def _cmd(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    except OSError:
        return "?"


@pytest.fixture
def run_env(tmp_path):
    fake = tmp_path / "fakepy"
    fake.write_text(FAKE_PY)
    fake.chmod(0o755)
    key = UNIT_EAK                                  # (a fake Official Agent Key: the only kind run_agent.sh plays)
    envf = tmp_path / "unit.env"
    envf.write_text(f"ALTRUAGENT_OFFICIAL_AGENT_KEY={key}\nALTRUAGENT_CONTROL_URL=http://127.0.0.1:9\n")
    envf.chmod(0o600)
    mode = tmp_path / "mode"
    mode.write_text("run")
    calcf = tmp_path / "calc"
    calcf.write_text("ok")
    enginef = tmp_path / "engine"
    enginef.write_text("ok")
    (tmp_path / "locks").mkdir()
    env = dict(os.environ, PYTHON=str(fake), LOG_DIR=str(tmp_path / "logs"), HEALTH_EVERY="1", RESTART_DELAY="1",
               STOP_TIMEOUT="3", FAKE_MODE=str(mode), FAKE_CALC=str(calcf), FAKE_ENGINE=str(enginef),
               LOG_MAX_BYTES="4000", LOG_KEEP="2", ARENA_LOCK_DIR=str(tmp_path / "locks"))
    for k in ("ALTRUAGENT_API_KEY", "ALTRUAGENT_OFFICIAL_AGENT_KEY", "ARENA_ALLOW_NO_CALC", "INVOCATION_ID",
              "ARENA_RUN_ID", "ARENA_MODE", "ARENA_ROLE", "ARENA_NEW_SEAT_DELAY_S"):
        env.pop(k, None)
    yield {"env": env, "envf": envf, "mode": mode, "calc": calcf, "engine": enginef, "logs": tmp_path / "logs",
           "key": key, "tmp": tmp_path}
    subprocess.run(["bash", str(RUN_AGENT), "stop", "--env", str(envf)], env=env, capture_output=True, timeout=60)
    for pid in _procs_with_env(f"FAKE_MODE={mode}"):                       # never leave a test process behind
        os.kill(pid, signal.SIGKILL)


def _ra(run_env, *args, timeout=90, env=None):
    return subprocess.run(["bash", str(RUN_AGENT), *args, "--env", str(run_env["envf"])], env=env or run_env["env"],
                          capture_output=True, text=True, timeout=timeout)


def _run_bg(run_env, env=None):
    return subprocess.Popen(["bash", str(RUN_AGENT), "run", "--env", str(run_env["envf"])], env=env or run_env["env"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _git(cwd, *args) -> str:
    """git in a throwaway test repository (its own identity, no signing: never our repository's settings)."""
    return subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def _copy_run_agent(root: Path) -> Path:
    """A checkout / bundle directory holding only scripts/run_agent.sh (its ROOT is `root`)."""
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(RUN_AGENT, root / "scripts" / "run_agent.sh")
    return root / "scripts" / "run_agent.sh"


@needs_linux
def test_run_agent_restarts_rotates_locks_and_stops_clean(run_env):
    logs = run_env["logs"]
    r = _ra(run_env, "start")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "agent=up" in r.stdout
    first = int(_state(logs, "unit")["child"])
    assert _ra(run_env, "run").returncode == 3                     # same name: the lock refuses a second loop
    run_env["mode"].write_text("spam")
    os.kill(first, signal.SIGKILL)                                 # crash: restarted, orphaned worker killed
    second = _wait(lambda: int(_state(logs, "unit").get("child", first)) != first and int(_state(logs, "unit")["child"]))
    assert second and second != first
    assert _wait(lambda: not _group_alive(first), timeout=5) is not None
    assert _wait(lambda: (logs / "agent-unit.out.2").exists(), timeout=10)
    assert not (logs / "agent-unit.out.3").exists()                # LOG_KEEP=2
    _wait(lambda: "[health]" in _text(logs / "agent-unit.out"), timeout=5)    # (a read may hit a rotation)
    r = _ra(run_env, "stop")
    assert r.returncode == 0 and "stopped" in r.stdout
    assert not _group_alive(second)
    assert not (logs / "agent-unit.pid").exists()
    text = "".join(p.read_text() for p in logs.glob("agent-unit.out*"))
    assert _state(logs, "unit")["restarts"] == "1"                 # (the log lines about it were rotated away)
    assert "[health]" in text and "[run_agent] exit" in text
    assert run_env["key"] not in text                              # the key is never logged
    assert _wait(lambda: not _procs_with_env(f"FAKE_MODE={run_env['mode']}"), timeout=10)


@needs_linux
def test_run_agent_backs_off_a_crash_loop_and_refuses_claim_mode(run_env):
    run_env["mode"].write_text("crash")
    _run_bg(run_env)
    assert _wait(lambda: "restart #3 in 4s" in _log(run_env, "unit"), timeout=20), _log(run_env, "unit")
    assert "restart #2 in 2s" in _log(run_env, "unit")                # (all rotated files: LOG_MAX_BYTES=4000)
    r = _ra(run_env, "stop")
    assert r.returncode == 0
    r = subprocess.run(["bash", str(RUN_AGENT), "run", "--", "--claim", "seatclaim_x"], env=run_env["env"],
                       capture_output=True, text=True)
    assert r.returncode == 2 and "claim codes (--claim) were retired" in r.stderr


@needs_linux
def test_run_agent_keeps_a_whole_event_day_of_log_by_default(run_env):
    # Official Rules section 9: records are kept until 30 days after the results. The defaults keep 41 files of 50 MB
    # (an event day writes a few KB per match); with a tiny rotation size the default LOG_KEEP still holds every line
    # this run wrote, its start lines included.
    env = dict(run_env["env"], LOG_MAX_BYTES="2000")
    env.pop("LOG_KEEP")
    env.pop("LOG_MAX_MB", None)
    run_env["mode"].write_text("spam")                              # ~33 KB of output: about 16 rotations
    r = _ra(run_env, "start", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _wait(lambda: "line 300 ." in _log(run_env, "unit"), timeout=20), _log(run_env, "unit")[-500:]
    rotated = [p.name for p in run_env["logs"].glob("agent-unit.out.*") if p.suffix[1:].isdigit()]
    assert 10 <= len(rotated) <= 40, sorted(rotated)
    text = _log(run_env, "unit")
    assert "(rotated at 50 MB, 40 kept)" in text
    assert "[run_agent] start name=unit" in text and "line 1 ." in text and "deployed: " in text
    assert _ra(run_env, "stop", env=env).returncode == 0


@needs_linux
@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_run_agent_logs_the_deployed_commit_and_every_arena_setting(run_env):
    # Official Rules section 9 (the deployed commit and configuration): every start names the checkout's full commit
    # id, says when tracked files differ from it, and lists every ARENA_* setting; never the key or the loop's ids
    root = run_env["tmp"] / "checkout"
    script = _copy_run_agent(root)
    (root / "README.md").write_text("x\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "x")
    sha = _git(root, "rev-parse", "HEAD")
    env = dict(run_env["env"], ARENA_WW_DM="on")

    def ra(*args):
        return subprocess.run(["bash", str(script), *args, "--env", str(run_env["envf"])], env=env,
                              capture_output=True, text=True, timeout=90)

    r = ra("start")
    assert r.returncode == 0, r.stdout + r.stderr
    log = _log(run_env, "unit")
    assert f"[run_agent] deployed: commit {sha} (git checkout)\n" in log
    settings = next(line for line in log.splitlines() if "[run_agent] settings: " in line)
    assert "ARENA_WW_DM=on" in settings and "ARENA_DISCOVERY_S=10" in settings and "ARENA_WORKER_COOLDOWN_S=10" in settings
    assert f"ARENA_RECORD_DIR={run_env['logs']} " in settings and settings.endswith("(every other ARENA_* at its default)")
    assert "ARENA_RUN_ID" not in settings and "ARENA_EXECUTION_ID" not in settings
    assert run_env["key"] not in log
    assert ra("stop").returncode == 0
    (root / "README.md").write_text("changed\n")                   # a tracked file differs from the commit: said so
    assert ra("start").returncode == 0
    assert f"[run_agent] deployed: commit {sha} (git checkout, with uncommitted changes)\n" in _log(run_env, "unit")
    assert ra("stop").returncode == 0


@needs_linux
def test_run_agent_logs_a_bundles_commit_file_and_a_change_under_a_running_loop(run_env):
    root = run_env["tmp"] / "bundle"                               # no .git: the COMMIT file scripts/bundle.sh writes
    script = _copy_run_agent(root)
    old, new = "0123456789abcdef" * 2 + "01234567", "fedcba9876543210" * 2 + "76543210"
    (root / "COMMIT").write_text(f"commit {old}\nbundle arena-agent-0123456, built 2026-10-13T20:00Z\n")
    r = subprocess.run(["bash", str(script), "start", "--env", str(run_env["envf"])], env=run_env["env"],
                       capture_output=True, text=True, timeout=90)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"deployed: commit {old} (bundle arena-agent-0123456, built 2026-10-13T20:00Z)\n" in _log(run_env, "unit")
    first = int(_state(run_env["logs"], "unit")["child"])
    (root / "COMMIT").write_text(f"commit {new}\nbundle arena-agent-fedcba9, built 2026-10-13T21:00Z\n")
    os.kill(first, signal.SIGKILL)                                  # the agent restarts on other code: logged
    assert _wait(lambda: f"deployed: commit {new} (bundle arena-agent-fedcba9, built 2026-10-13T21:00Z) (changed "
                         "since the agent last started)" in _log(run_env, "unit"), timeout=20), _log(run_env, "unit")
    r = subprocess.run(["bash", str(script), "stop", "--env", str(run_env["envf"])], env=run_env["env"],
                       capture_output=True, text=True, timeout=90)
    assert r.returncode == 0
    (root / "COMMIT").unlink()                                      # neither: says so, still starts
    r = subprocess.run(["bash", str(script), "start", "--env", str(run_env["envf"])], env=run_env["env"],
                       capture_output=True, text=True, timeout=90)
    assert r.returncode == 0 and f"deployed: unknown (no git checkout and no COMMIT file in {root})" in _log(run_env, "unit")


@needs_linux
@pytest.mark.parametrize("content, says", [
    ("ALTRUAGENT_OFFICIAL_AGENT_KEY=eak_live_your_key_here\n", "still the .env.example placeholder"),
    ("ALTRUAGENT_API_KEY=sk_agent_FAKE_retired_0000\n", "only ALTRUAGENT_API_KEY (an sk_agent_ key) is set"),
    ("ALTRUAGENT_CONTROL_URL=http://127.0.0.1:9\n", "ALTRUAGENT_OFFICIAL_AGENT_KEY is not set")])
def test_run_agent_refuses_to_start_without_a_key(tmp_path, content, says):
    envf = tmp_path / "nokey.env"
    envf.write_text(content)
    env = dict(os.environ, LOG_DIR=str(tmp_path / "logs"), PYTHON=sys.executable)
    for k in ("ALTRUAGENT_API_KEY", "ALTRUAGENT_OFFICIAL_AGENT_KEY", "ARENA_MODE"):
        env.pop(k, None)
    r = subprocess.run(["bash", str(RUN_AGENT), "run", "--env", str(envf)], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2
    log = (tmp_path / "logs" / "agent-nokey.out").read_text()
    assert says in log and "sk_agent_FAKE_retired_0000" not in log


@needs_linux
def test_run_agent_refuses_to_start_when_the_calculator_cannot_run(run_env):
    run_env["calc"].write_text("broken")
    r = _ra(run_env, "start")
    assert r.returncode == 1 and "did not come up" in r.stdout
    log = (run_env["logs"] / "agent-unit.out").read_text()
    assert "THE DAMAGE CALCULATOR CANNOT RUN (no node found (fake)): NOT STARTING" in log
    assert "preflight failed; exiting (code 2)" in log and "agent started" not in log
    r = _ra(run_env, "run")                                        # the foreground loop: exit 2, and loud on stdout
    assert r.returncode == 2 and "CANNOT RUN" in r.stdout
    r = _ra(run_env, "start", env=dict(run_env["env"], ARENA_ALLOW_NO_CALC="1"))
    assert r.returncode == 0 and "agent=up" in r.stdout, r.stdout
    log = "".join(p.read_text() for p in sorted(run_env["logs"].glob("agent-unit.out*")))   # (LOG_MAX_BYTES rotates)
    assert "WARNING: THE DAMAGE CALCULATOR CANNOT RUN" in log and "Starting anyway because ARENA_ALLOW_NO_CALC=1" in log


@needs_linux
def test_run_agent_refuses_to_start_when_the_engine_search_cannot_run(run_env):
    # ARENA_BATTLE=search (the default) without its simulator would quietly play the classic policy: refused, loudly
    run_env["engine"].write_text("broken")
    search = dict(run_env["env"], ARENA_BATTLE="search")
    r = _ra(run_env, "start", env=search)
    assert r.returncode == 1 and "did not come up" in r.stdout
    log = (run_env["logs"] / "agent-unit.out").read_text()
    assert "ARENA_BATTLE=search BUT THE ENGINE SEARCH CANNOT RUN (pokemon-showdown missing (fake)): NOT STARTING" in log
    assert "preflight failed; exiting (code 2)" in log and "agent started" not in log
    unset = dict(search)
    unset.pop("ARENA_BATTLE")                                      # unset = the default = search: refused too
    r = _ra(run_env, "run", env=unset)
    assert r.returncode == 2 and "ENGINE SEARCH CANNOT RUN" in r.stdout
    r = _ra(run_env, "start", env=dict(run_env["env"], ARENA_BATTLE="classic"))   # the classic policy on purpose
    assert r.returncode == 0 and "agent=up" in r.stdout and "search=classic(ARENA_BATTLE)" in r.stdout, r.stdout
    assert "battle policy: classic (ARENA_BATTLE=classic)" in "".join(
        p.read_text() for p in sorted(run_env["logs"].glob("agent-unit.out*")))
    assert _ra(run_env, "stop").returncode == 0
    run_env["engine"].write_text("ok")                             # a simulator that loads: starts with the search
    r = _ra(run_env, "start", env=search)
    assert r.returncode == 0 and "agent=up" in r.stdout, r.stdout
    every = "".join(p.read_text() for p in sorted(run_env["logs"].glob("agent-unit.out*")))   # (LOG_MAX_BYTES rotates)
    assert "battle policy: engine search (ARENA_BATTLE=search), simulator loads in 0.9s (fake)" in every


@needs_linux
def test_run_agent_reads_arena_battle_from_the_checkouts_env_like_the_agent(tmp_path):
    # the SDK's load_dotenv() gives the agent the checkout's .env too: ARENA_BATTLE there counts for the start check
    root = tmp_path / "co"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(RUN_AGENT, root / "scripts" / "run_agent.sh")
    (root / ".env").write_text("ARENA_BATTLE=classic\n")
    envf = tmp_path / "other.env"
    envf.write_text(f"ALTRUAGENT_OFFICIAL_AGENT_KEY={UNIT_EAK}\n")
    env = dict(os.environ, LOG_DIR=str(tmp_path / "logs"))
    for k in ("ARENA_BATTLE", "ALTRUAGENT_API_KEY", "ALTRUAGENT_OFFICIAL_AGENT_KEY"):
        env.pop(k, None)
    r = subprocess.run(["bash", str(root / "scripts" / "run_agent.sh"), "health", "--env", str(envf)], env=env,
                       capture_output=True, text=True, timeout=60)
    assert "search=classic(ARENA_BATTLE)" in r.stdout, r.stdout + r.stderr


@needs_linux
def test_run_agent_refuses_a_leftover_arena_calc_off_in_the_key_file(tmp_path):
    envf = tmp_path / "main.env"                                    # the real probe (real python): ARENA_CALC=off from
    envf.write_text(f"ALTRUAGENT_OFFICIAL_AGENT_KEY={UNIT_EAK}\nALTRUAGENT_CONTROL_URL=http://127.0.0.1:9\n"
                    "ARENA_CALC=off\n")                            # the key file is what the agent would see
    envf.chmod(0o600)
    env = dict(os.environ, LOG_DIR=str(tmp_path / "logs"), PYTHON=sys.executable, PYTHONPATH=str(ROOT),
               ARENA_LOCK_DIR=str(tmp_path))
    for k in ("ALTRUAGENT_API_KEY", "ALTRUAGENT_OFFICIAL_AGENT_KEY", "ARENA_CALC", "ARENA_ALLOW_NO_CALC",
              "ARENA_RUN_ID", "ARENA_MODE"):
        env.pop(k, None)
    r = subprocess.run(["bash", str(RUN_AGENT), "run", "--env", str(envf)], env=env, capture_output=True, text=True,
                       timeout=120)
    log = (tmp_path / "logs" / "agent-main.out").read_text()
    assert r.returncode == 2, log
    assert "THE DAMAGE CALCULATOR CANNOT RUN (ARENA_CALC=off): NOT STARTING" in log and "agent started" not in log


@needs_linux
def test_restart_during_the_restart_delay_brings_the_agent_back(run_env):
    run_env["mode"].write_text("crash")
    env = dict(run_env["env"], RESTART_DELAY="25")
    _run_bg(run_env, env)
    assert _wait(lambda: "restart #1 in 25s" in _log(run_env, "unit"), timeout=20), _log(run_env, "unit")
    run_env["mode"].write_text("run")
    t = time.monotonic()
    r = _ra(run_env, "restart", env=env)                           # the back-off sleep holds no lock any more
    assert r.returncode == 0 and "agent=up" in r.stdout, r.stdout + _log(run_env, "unit")[-2000:]
    assert time.monotonic() - t < 20


@needs_linux
def test_a_sigkilled_run_loop_leaves_nothing_behind(run_env):
    logs = run_env["logs"]
    assert _ra(run_env, "start").returncode == 0
    loop = int((logs / "agent-unit.pid").read_text())
    child = int(_state(logs, "unit")["child"])
    _wait(lambda: "[health]" in _log(run_env, "unit"), timeout=5)
    os.kill(loop, signal.SIGKILL)                                   # (cmd_stop's own fallback, the OOM killer ...)
    time.sleep(2.5)                                                 # HEALTH_EVERY=1: an orphan health loop would log
    n = _log(run_env, "unit").count("[health]")
    time.sleep(3)
    assert _log(run_env, "unit").count("[health]") == n             # nothing outlives the loop and logs
    assert _alive(child)                                            # (the agent runs on until the next start / stop)
    r = _ra(run_env, "stop")
    assert r.returncode == 0
    left = _wait(lambda: not _procs_with_env(f"FAKE_MODE={run_env['mode']}"), timeout=10)
    assert left, [(p, _cmd(p)) for p in _procs_with_env(f"FAKE_MODE={run_env['mode']}")]


@needs_linux
def test_stop_leaves_not_even_a_sleep_behind(run_env):
    env = dict(run_env["env"], HEALTH_EVERY="30")                  # the health loop sleeps 5 s at a time
    assert _ra(run_env, "start", env=env).returncode == 0
    time.sleep(1)
    assert _ra(run_env, "stop", env=env).returncode == 0
    left = _wait(lambda: not _procs_with_env(f"FAKE_MODE={run_env['mode']}"), timeout=2)
    assert left, [(p, _cmd(p)) for p in _procs_with_env(f"FAKE_MODE={run_env['mode']}")]


@needs_linux
def test_the_key_lock_covers_every_checkout_on_the_machine(run_env):
    # another checkout / LOG_DIR with the same key: the platform's seat lease picks one runtime per seat, so the second
    # one starts as a hot standby (it never plays a seat the first one holds); the key lock is how it knows
    assert _ra(run_env, "start").returncode == 0
    other = dict(run_env["env"], LOG_DIR=str(run_env["tmp"] / "other-logs"))
    r = _ra(run_env, "start", env=other)
    assert r.returncode == 0 and "mode=tournament(standby)" in r.stdout, r.stdout
    assert "already runs Official Agent Key" in (run_env["tmp"] / "other-logs" / "agent-unit.out").read_text()
    assert _ra(run_env, "stop", env=other).returncode == 0
    loop = int((run_env["logs"] / "agent-unit.pid").read_text())
    os.kill(loop, signal.SIGKILL)                                   # the loop is gone, its agent still plays:
    time.sleep(0.5)
    r = _ra(run_env, "start", env=other)                            # the agent itself still holds the key lock
    assert r.returncode == 0 and "mode=tournament(standby)" in r.stdout, r.stdout
    assert _ra(run_env, "stop", env=other).returncode == 0


@needs_linux
def test_stop_works_with_an_inherited_invocation_id(run_env):
    env = dict(run_env["env"], INVOCATION_ID="0123456789abcdef0123456789abcdef")   # e.g. a GNOME Terminal shell
    assert _ra(run_env, "start", env=env).returncode == 0
    child = int(_state(run_env["logs"], "unit")["child"])
    r = _ra(run_env, "stop", env=env)
    assert r.returncode == 0 and "stopped" in r.stdout, r.stdout + r.stderr
    assert _wait(lambda: not _alive(child), timeout=10)


@needs_linux
def test_a_stale_pidfile_never_names_somebody_elses_process(run_env):
    logs = run_env["logs"]
    logs.mkdir(parents=True, exist_ok=True)
    bystander = subprocess.Popen(["sleep", "1000"])                 # same user; its pid is in a pidfile that a
    try:                                                            # SIGKILL / OOM / reboot left behind
        (logs / "agent-unit.pid").write_text(f"{bystander.pid}\n")
        r = _ra(run_env, "stop")
        assert r.returncode == 0
        time.sleep(0.5)
        assert bystander.poll() is None                             # never signalled
        (logs / "agent-unit.pid").write_text(f"{bystander.pid}\n")
        r = _ra(run_env, "start")                                   # not "already running": the agent starts
        assert r.returncode == 0 and "agent=up" in r.stdout, r.stdout
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait()


@needs_linux
def test_a_stop_at_the_moment_the_agent_starts_is_not_lost(run_env):
    run_env["mode"].write_text("termparent")                        # the agent SIGTERMs its loop as it starts
    p = _run_bg(run_env)
    assert p.wait(timeout=20) == 0
    log = (run_env["logs"] / "agent-unit.out").read_text()
    assert "agent stopped" in log and "[run_agent] exit" in log
    assert _wait(lambda: not _procs_with_env(f"FAKE_MODE={run_env['mode']}"), timeout=10)


# ---- run_agent.sh in the official tournament mode -------------------------------------------------------------------
def _official_env(run_env, name="official", extra=""):
    envf = run_env["tmp"] / f"{name}.env"
    envf.write_text(f"ALTRUAGENT_OFFICIAL_AGENT_KEY={FAKE_EAK}\nALTRUAGENT_CONTROL_URL=http://127.0.0.1:9\n{extra}")
    envf.chmod(0o600)
    return envf


def _ra_env(run_env, envf, *args, env=None, timeout=90):
    return subprocess.run(["bash", str(RUN_AGENT), *args, "--env", str(envf)], env=env or run_env["env"],
                          capture_output=True, text=True, timeout=timeout)


def _log(run_env, name) -> str:
    """The loop's whole log, every rotated file included, oldest first (a file can vanish mid-rotation: '')."""
    files = sorted(run_env["logs"].glob(f"agent-{name}.out*"), key=lambda p: -int(p.suffix[1:]) if p.suffix[1:].isdigit() else 0)
    return "".join(_text(p) for p in files)


@needs_linux
def test_run_agent_runs_the_official_tournament_mode_with_only_the_official_key(run_env):
    envf = _official_env(run_env)
    eids = run_env["tmp"] / "eids"
    env = dict(run_env["env"], FAKE_EID_FILE=str(eids))
    r = _ra_env(run_env, envf, "start", env=env)
    assert r.returncode == 0 and "agent=up" in r.stdout, r.stdout + r.stderr + _log(run_env, "official")
    assert "mode=tournament(primary)" in r.stdout and f"key=#{hashlib.sha256(FAKE_EAK.encode()).hexdigest()[:10]}" in r.stdout
    first = int(_state(run_env["logs"], "official")["child"])
    _wait(lambda: "fake agent env" in _log(run_env, "official"), timeout=10)
    log = _log(run_env, "official")
    assert "fake agent args: -u -m agent --tournament --match\n" in log   # test matches + tournament games, 1 process
    assert "mode tournament (python -m agent --tournament --match: the Official Agent Key's test matches" in log
    assert f"record_dir={run_env['logs']}" in log                    # recordings next to the log
    assert "eid_len=64 eid_owner_ok=yes" in log                       # the loop's execution id, its own pid as owner
    run_env["mode"].write_text("crash")
    os.kill(first, signal.SIGKILL)                                    # the loop restarts the agent: SAME execution id
    assert _wait(lambda: len(eids.read_text().split()) >= 2, timeout=20)
    ids = eids.read_text().split()
    assert len(set(ids)) == 1 and len(ids[0]) == 64
    text = _log(run_env, "official") + (run_env["logs"] / "agent-official.state").read_text()
    assert ids[0] not in text and FAKE_EAK not in text                # never logged, never in the state file
    assert _ra_env(run_env, envf, "stop").returncode == 0


@needs_linux
def test_a_new_run_loop_keeps_the_last_loops_execution_id_only_when_that_is_safe(run_env):
    # review 10-06: every run loop drew a NEW execution id, so a new loop (systemd's Restart=, `restart`, a loop that
    # died) found its live seats leased to its own old id: seat_busy until the lease lapsed, then the SDK's 35 s retry
    # (measured 65 s and a timer move in a VGC battle). The id now lives in LOG_DIR/agent-NAME.execid (0600) with the
    # boot, key and run it belongs to, and a loop reuses it only while it holds the key lock.
    envf = _official_env(run_env)
    eids = run_env["tmp"] / "eids"
    env = dict(run_env["env"], FAKE_EID_FILE=str(eids), LOG_MAX_BYTES="1000000")
    logs = run_env["logs"]
    f = logs / "agent-official.execid"

    def ids():
        return _text(eids).split()

    assert _ra_env(run_env, envf, "start", env=env).returncode == 0
    assert _wait(lambda: len(ids()) >= 1, timeout=10)
    assert f.stat().st_mode & 0o777 == 0o600 and "execution id: new" in _log(run_env, "official")
    assert _ra_env(run_env, envf, "restart", env=env).returncode == 0  # a new run loop: the same id
    assert _wait(lambda: len(ids()) >= 2, timeout=10)
    assert "execution id: the last run loop's" in _log(run_env, "official")
    st = _state(logs, "official")
    loop, child = int(st["loop"]), int(st["child"])
    assert _wait(lambda: len(re.findall(r"worker (\d+)", _log(run_env, "official"))) >= 2, timeout=10)
    worker = int(re.findall(r"worker (\d+)", _log(run_env, "official"))[-1])   # (the second agent's)
    os.kill(loop, signal.SIGKILL)                                     # the loop dies and so does its agent, but not
    os.kill(child, signal.SIGKILL)                                    # the agent's worker, which could still play a
    assert _wait(lambda: not _alive(child), timeout=5) and _alive(worker)   # seat with that id: the next loop kills it
    r = _ra_env(run_env, envf, "start", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _wait(lambda: len(ids()) >= 3, timeout=10) and _wait(lambda: not _alive(worker), timeout=5)
    assert f"killing leftover processes of agent group {child} (from a run loop that died): {worker}" in \
        _log(run_env, "official")
    assert len(set(ids())) == 1 and len(ids()[0]) == 64
    text = _log(run_env, "official") + (logs / "agent-official.state").read_text()
    assert ids()[0] not in text and FAKE_EAK not in text + f.read_text()   # the id: only in its own 0600 file
    assert _ra_env(run_env, envf, "stop", env=env).returncode == 0
    # an id file of another boot (or one copied from another machine): a fresh id, saved in its place
    f.write_text(re.sub(r"(?m)^boot=.*$", "boot=00000000-0000-0000-0000-000000000000", f.read_text()))
    assert _ra_env(run_env, envf, "start", env=env).returncode == 0
    assert _wait(lambda: len(ids()) >= 4, timeout=10)
    assert ids()[3] != ids()[0] and f"id={ids()[3]}\n" in f.read_text() and f.stat().st_mode & 0o777 == 0o600
    # another runtime of this key on this machine holds the key lock: never a saved id, even one that matches
    other_logs = run_env["tmp"] / "other-logs"
    other_logs.mkdir()
    copied = other_logs / "agent-official.execid"
    copied.write_text(f.read_text().replace(f"run=official@{logs}\n", f"run=official@{other_logs}\n"))
    copied.chmod(0o600)
    assert f"run=official@{other_logs}\n" in copied.read_text()      # (boot, key and run all match: only the lock)
    seids = run_env["tmp"] / "seids"
    other = dict(env, LOG_DIR=str(other_logs), FAKE_EID_FILE=str(seids))
    r = _ra_env(run_env, envf, "start", env=other)
    assert r.returncode == 0 and "mode=tournament(standby)" in r.stdout, r.stdout
    assert _wait(lambda: _text(seids).split(), timeout=10)
    assert _text(seids).split()[0] not in ids()
    assert _ra_env(run_env, envf, "stop", env=other).returncode == 0
    assert _ra_env(run_env, envf, "stop", env=env).returncode == 0


@needs_linux
def test_status_exits_0_while_the_loop_runs_and_3_when_it_does_not(run_env):
    env = dict(run_env["env"], HEALTH_EVERY="300")                    # no [health] line logged yet: status used to
    r = _ra(run_env, "status", env=env)                               # exit 1 (2 without a log file) on a healthy agent
    assert r.returncode == 3 and "run loop: not running" in r.stdout, r.stdout + r.stderr
    assert _ra(run_env, "start", env=env).returncode == 0
    r = _ra(run_env, "status", env=env)
    assert r.returncode == 0 and "run loop: pid" in r.stdout and "last logged" not in r.stdout, r.stdout
    assert _ra(run_env, "stop", env=env).returncode == 0
    assert _ra(run_env, "status", env=env).returncode == 3


def _user_systemd() -> bool:
    if not sys.platform.startswith("linux") or not shutil.which("systemd-run") or not shutil.which("systemctl"):
        return False
    try:
        r = subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.stdout.strip() in ("running", "degraded")


@needs_linux
@pytest.mark.skipif(not _user_systemd(), reason="needs a systemd user manager (systemd-run --user)")
def test_stop_refuses_a_loop_under_systemd_even_from_a_shell_without_the_user_bus(run_env):
    # review 10-06: from a shell without XDG_RUNTIME_DIR (su, sudo -u, cron) `systemctl --user` failed, the check
    # found no unit, and `stop` stopped the loop, which systemd at once restarted: a stop that was a restart.
    unit = f"arena-test-{os.getpid()}-{int(time.time())}.service"
    keep = ("PATH", "HOME", "LANG", "PYTHON", "LOG_DIR", "HEALTH_EVERY", "RESTART_DELAY", "STOP_TIMEOUT", "FAKE_MODE",
            "FAKE_CALC", "FAKE_ENGINE", "LOG_MAX_BYTES", "LOG_KEEP", "ARENA_LOCK_DIR")
    cmd = ["systemd-run", "--user", "--unit", unit, "--collect", "-p", "Restart=always", "-p", "RestartSec=2",
           "-p", "KillMode=mixed", "--working-directory", str(ROOT)]
    for k in keep:
        if k in run_env["env"]:
            cmd += ["--setenv", f"{k}={run_env['env'][k]}"]
    cmd += ["/bin/bash", str(RUN_AGENT), "run", "--env", str(run_env["envf"])]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    try:
        logs = run_env["logs"]
        assert _wait(lambda: _state(logs, "unit").get("child") and _alive(int(_state(logs, "unit")["child"])),
                     timeout=30), _log(run_env, "unit")
        loop = int((logs / "agent-unit.pid").read_text())
        nobus = {k: v for k, v in run_env["env"].items() if k not in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")}
        r = _ra(run_env, "stop", env=nobus)                           # systemctl --user reaches the manager anyway
        assert r.returncode == 1 and f"runs under systemd, which would just restart it: systemctl --user stop {unit}" \
            in r.stderr, r.stdout + r.stderr
        fakebin = run_env["tmp"] / "nobus-bin"                        # ... and when it cannot: still refused
        fakebin.mkdir()
        (fakebin / "systemctl").write_text("#!/bin/sh\necho 'Failed to connect to bus: No medium found' >&2\nexit 1\n")
        (fakebin / "systemctl").chmod(0o755)
        r = _ra(run_env, "stop", env=dict(nobus, PATH=f"{fakebin}:{nobus['PATH']}"))
        assert r.returncode == 1 and f"runs in the systemd unit {unit}" in r.stderr, r.stdout + r.stderr
        assert f"systemctl --user stop {unit}" in r.stderr
        r = _ra(run_env, "restart", env=nobus)
        assert r.returncode == 1 and f"systemctl --user restart {unit}" in r.stderr, r.stdout + r.stderr
        time.sleep(1)
        assert _alive(loop) and int((logs / "agent-unit.pid").read_text()) == loop   # never stopped behind systemd's back
    finally:
        subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True, timeout=90)
        subprocess.run(["systemctl", "--user", "reset-failed", unit], capture_output=True, timeout=30)


@needs_linux
def test_the_runtime_refuses_a_malformed_or_retired_key_and_plays_the_kinds_given_after_dashdash(run_env):
    bad = run_env["tmp"] / "bad.env"
    bad.write_text("ALTRUAGENT_OFFICIAL_AGENT_KEY=eak_live_FAKE_not_hex\n")
    r = _ra_env(run_env, bad, "run")
    assert r.returncode == 2 and "doesn't look like an Official Agent Key" in r.stdout
    assert "eak_live_FAKE_not_hex" not in r.stdout + _log(run_env, "bad")
    old = run_env["tmp"] / "old.env"                                  # the retired sk_agent_ key alone
    old.write_text("ALTRUAGENT_API_KEY=sk_agent_FAKE_only_the_generic_key\n")
    r = _ra_env(run_env, old, "run")
    assert r.returncode == 2 and "only ALTRUAGENT_API_KEY (an sk_agent_ key) is set" in r.stdout
    assert "platform_agents_retired" in r.stdout and "sk_agent_FAKE_only_the_generic_key" not in r.stdout
    both = _official_env(run_env, "both", "ALTRUAGENT_API_KEY=sk_agent_FAKE_generic_key_1234\n")
    r = _ra_env(run_env, both, "health")                              # both keys: the official one, the old one ignored
    assert "mode=tournament(primary)" in r.stdout and f"key=#{hashlib.sha256(FAKE_EAK.encode()).hexdigest()[:10]}" \
        in r.stdout, r.stdout
    disc = _official_env(run_env, "disc", "ARENA_MODE=discovery\n")   # a leftover switch to the retired mode
    r = _ra_env(run_env, disc, "run")
    assert r.returncode == 2 and "ARENA_MODE=discovery: the discovery mode (sk_agent_ keys) was retired" in r.stdout
    envf = _official_env(run_env, "one")                              # `-- --match`: test matches only, on purpose
    r = subprocess.run(["bash", str(RUN_AGENT), "start", "--env", str(envf), "--", "--match"], env=run_env["env"],
                       capture_output=True, text=True, timeout=90)
    assert r.returncode == 0, r.stdout + r.stderr
    _wait(lambda: "fake agent args" in _log(run_env, "one"), timeout=10)
    assert "fake agent args: -u -m agent --match\n" in _log(run_env, "one")
    assert "the kinds of game given after --" in _log(run_env, "one")
    assert _ra_env(run_env, envf, "stop").returncode == 0


@needs_linux
def test_a_second_runtime_with_the_same_official_key_starts_as_a_hot_standby(run_env):
    # the platform's seat lease picks one runtime per seat, so a second runtime of the key waits as a hot standby
    envf = _official_env(run_env)
    assert _ra_env(run_env, envf, "start").returncode == 0
    other = dict(run_env["env"], LOG_DIR=str(run_env["tmp"] / "standby-logs"))
    r = _ra_env(run_env, envf, "start", env=other)
    assert r.returncode == 0 and "agent=up" in r.stdout and "mode=tournament(standby)" in r.stdout, r.stdout
    standby_log = run_env["tmp"] / "standby-logs" / "agent-official.out"
    assert _wait(lambda: "fake agent env" in standby_log.read_text(), timeout=10)
    text = standby_log.read_text()
    assert "HOT STANDBY" in text and "A standby belongs on ANOTHER machine" in text
    assert "new_seat_delay=20" in text                                # new seats: the primary's (first claim wins)
    assert _wait(lambda: "fake agent env" in _log(run_env, "official"), timeout=10)
    assert "new_seat_delay=unset" in _log(run_env, "official")
    assert _ra_env(run_env, envf, "stop", env=other).returncode == 0
    assert _ra_env(run_env, envf, "stop").returncode == 0
    # the standby machine of a two-machine setup: nothing else runs the key here, its key file says ARENA_ROLE=standby
    sb = _official_env(run_env, "sb", "ARENA_ROLE=standby\nARENA_NEW_SEAT_DELAY_S=25\n")
    r = _ra_env(run_env, sb, "start")
    assert r.returncode == 0 and "mode=tournament(standby)" in r.stdout, r.stdout
    assert _wait(lambda: "fake agent env" in _log(run_env, "sb"), timeout=10)
    assert "ARENA_ROLE=standby: a HOT STANDBY" in _log(run_env, "sb") and "new_seat_delay=25" in _log(run_env, "sb")
    assert _ra_env(run_env, sb, "stop").returncode == 0


@needs_linux
def test_status_shows_the_role_and_key_of_the_running_loop_not_of_the_asking_shell(run_env):
    # the runbook's competition-day check is `status` showing mode=tournament(primary) and the key's hash. A loop
    # started with another key and ARENA_ROLE=standby in its environment (the real environment wins over the key file)
    # runs THAT; `status` from a shell without them must show the loop's role and key, not recompute them.
    env = {k: v for k, v in run_env["env"].items() if k not in ("ARENA_ROLE", "ALTRUAGENT_OFFICIAL_AGENT_KEY")}
    envf = _official_env(run_env, "both")
    r = _ra_env(run_env, envf, "start", env=dict(env, ARENA_ROLE="standby", ALTRUAGENT_OFFICIAL_AGENT_KEY=UNIT_EAK))
    assert r.returncode == 0 and "mode=tournament(standby)" in r.stdout, r.stdout
    unit = hashlib.sha256(UNIT_EAK.encode()).hexdigest()[:10]
    eak = hashlib.sha256(FAKE_EAK.encode()).hexdigest()[:10]
    r = _ra_env(run_env, envf, "status", env=env)
    live = next(line for line in r.stdout.splitlines() if line.startswith("[health]"))
    assert "mode=tournament(standby) " in live and f"key=#{unit}" in live, r.stdout
    assert f"keyhash={unit}" in (run_env["logs"] / "agent-both.state").read_text()
    assert _ra_env(run_env, envf, "stop", env=env).returncode == 0
    r = _ra_env(run_env, envf, "health", env=env)                     # no loop: what a start from here would run
    assert "mode=tournament(primary) " in r.stdout and f"key=#{eak}" in r.stdout, r.stdout


@needs_linux
def test_check_tournament_is_a_one_shot_command_with_the_key_file_loaded(run_env):
    envf = _official_env(run_env)
    r = subprocess.run(["bash", str(RUN_AGENT), "run", "--env", str(envf), "--", "--check-tournament"],
                       env=run_env["env"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "one-shot check" in r.stderr
    r = _ra_env(run_env, envf, "check-tournament")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "fake agent args: -u -m agent --check-tournament" in r.stdout
    assert "check: official key set url http://127.0.0.1:9" in r.stdout and FAKE_EAK not in r.stdout


@needs_linux
def test_arena_allow_no_calc_alone_starts_with_the_classic_policy_when_node_cannot_run(run_env):
    run_env["calc"].write_text("broken")                              # node itself cannot run: calc AND search fail
    run_env["engine"].write_text("broken")
    env = dict(run_env["env"], ARENA_ALLOW_NO_CALC="1", ARENA_BATTLE="search")
    r = _ra(run_env, "start", env=env)
    assert r.returncode == 0 and "agent=up" in r.stdout and "search=classic(ARENA_BATTLE)" in r.stdout, r.stdout
    _wait(lambda: "fake agent env" in _log(run_env, "unit"), timeout=10)
    log = _log(run_env, "unit")
    assert "WARNING: THE ENGINE SEARCH CANNOT RUN EITHER" in log and "battle=classic" in log
    assert _ra(run_env, "stop", env=env).returncode == 0
    run_env["calc"].write_text("ok")                                  # node works, only the simulator is missing:
    r = _ra(run_env, "run", env=env)                                  # still refused (ARENA_BATTLE=classic is that switch)
    assert r.returncode == 2 and "ENGINE SEARCH CANNOT RUN" in r.stdout and "EITHER" not in r.stdout


@needs_linux
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can write anywhere")
def test_an_unwritable_log_dir_is_an_error_not_already_running(run_env):
    ro = run_env["tmp"] / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        env = dict(run_env["env"], LOG_DIR=str(ro / "logs"))
        r = _ra(run_env, "run", env=env)
        assert r.returncode == 2 and "is not writable" in r.stderr and "already running" not in r.stderr, r.stderr
        env = dict(run_env["env"], LOG_DIR=str(ro))
        r = _ra(run_env, "start", env=env)
        assert r.returncode == 2 and "is not writable" in r.stderr
    finally:
        ro.chmod(0o700)


def test_run_agent_script_parses():
    assert subprocess.run(["bash", "-n", str(RUN_AGENT)]).returncode == 0
    assert subprocess.run(["bash", "-n", str(ROOT / "scripts" / "bundle.sh")]).returncode == 0


# ---- local/fake_platform.py speaks the real SDK's protocol ---------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(shutil.which("node") is None or not (ROOT / "local" / "js" / "node_modules").exists(),
                    reason="needs node + local/js/node_modules (Showdown)")
def test_fake_platform_plays_a_match_through_the_official_client(tmp_path):
    from altruagent import AltruAgentClient
    from altruagent.runner import run_match

    from local.fake_platform import AGENT_ID, DEFAULT_KEY
    from local.pokemon_env import random_action

    port = _free_port()
    stats = tmp_path / "platform.json"
    proc = subprocess.Popen([sys.executable, "-m", "local.fake_platform", "--port", str(port), "--concurrent", "1",
                             "--max-matches", "1", "--token-ttl", "2", "--stats", str(stats)], cwd=ROOT,
                            env=dict(os.environ, PYTHONPATH=str(ROOT)), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        client = AltruAgentClient(control_url=f"http://127.0.0.1:{port}", api_key=DEFAULT_KEY, load_env_file=False)
        me = _wait(lambda: _try(client.me), timeout=20)
        assert me and me.is_claimed and me.id == AGENT_ID
        match = _wait(lambda: (client.sessions().active or [None])[0], timeout=20)

        def choose(state, context):
            time.sleep(0.3)                                  # long enough for the 2 s token to expire mid-match
            return random_action(state.raw)

        from altruagent.mcp_transport import MCPToolError
        for attempt in range(3):
            try:
                final = run_match(match, AGENT_ID, choose)
                break
            except MCPToolError as e:
                # SDK / mcp race (upstream, docs/deploy.md): now and then an expired token's 401 on an MCP call comes
                # back as a BrokenResourceError, so the SDK's re-login-and-retry does not run. In production the
                # worker exits and the supervisor restarts it (10 s cooldown); here the match just resumes.
                if "BrokenResourceError" not in str(e) or attempt == 2:
                    raise
        assert final.is_terminal and AGENT_ID in (final.returns or {})
        _wait(lambda: stats.exists() and json.loads(stats.read_text())["totals"]["matches_done"] == 1, timeout=15)
        tot = json.loads(stats.read_text())["totals"]
        assert tot["decisions"] >= 8 and tot["invalid_actions"] == 0 and tot["timer_decisions"] == 0
        assert tot["unauthorized"] >= 1 and tot["logins"] >= 2        # expired token -> 401 -> re-login worked
        assert client.sessions().active == []
        client.close()
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def _try(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - server still starting
        return None


def test_the_fake_platforms_drain_file_holds_back_new_matches(tmp_path):
    # scripts/rehearsal_official.py ends its run this way: no new match or table while the file exists
    import threading

    from local.fake_platform import FakePlatform

    p = FakePlatform(concurrent=1, opponent="random", max_matches=None, api_key="k", token_ttl=60.0, seed=1,
                     stats=None, game_server="127.0.0.1:1", werewolf=1)
    started: list[str] = []
    p._new_match = lambda: started.append("vgc") or p.stopping.set()
    p._new_werewolf = lambda: started.append("werewolf") or p.stopping.set()
    p.drain_file = tmp_path / "drain"
    p.drain_file.write_text("drain")
    t = threading.Thread(target=p.scheduler, daemon=True)
    t.start()
    time.sleep(1.2)
    assert started == []
    p.drain_file.unlink()
    t.join(timeout=5)
    assert started == ["werewolf"]


@pytest.mark.skipif(shutil.which("node") is None or not (ROOT / "local" / "js" / "node_modules").exists(),
                    reason="needs node + local/js/node_modules (Showdown)")
def test_fake_official_control_plane_leases_seats_and_serves_a_werewolf_table(tmp_path, monkeypatch):
    from altruagent.official import OfficialAgentClient, OfficialAgentError
    from altruagent.worker import TournamentWorkerInput, run_tournament_worker

    from local.fake_platform import DEFAULT_OFFICIAL_KEY

    port = _free_port()
    stats, outage = tmp_path / "platform.json", tmp_path / "outage"
    proc = subprocess.Popen([sys.executable, "-m", "local.fake_platform", "--port", str(port), "--official",
                             "--werewolf", "1", "--ww-seat", "3", "--max-matches", "1", "--concurrent", "1",
                             "--stats", str(stats), "--outage-file", str(outage)], cwd=ROOT,
                            env=dict(os.environ, PYTHONPATH=str(ROOT)), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        official = OfficialAgentClient(url, DEFAULT_OFFICIAL_KEY, load_env_file=False)
        assert _wait(lambda: _try(official.authenticate) is None and len(_try(official.assignments) or []) == 2,
                     timeout=30)
        rows = {r.game_type: r for r in official.assignments()}
        assert (rows["werewolf"].seat_position, rows["werewolf"].seat_count) == (3, 7)
        assert {r.context for r in rows.values()} == {"tournament", "testing"}          # the event's two kinds
        assert all(bool(r.connect_deadline_at) == (r.context == "tournament") for r in rows.values())
        vgc = rows["pokemon_vgc_doubles_draft"]
        assert vgc.seat_count == 2 and vgc.seat_id
        a, b = "exec-A-" + "a" * 40, "exec-B-" + "b" * 40
        grant = official.grant(vgc.seat_id, a)
        assert grant.game_session_id and grant.gameapi_server_url == f"127.0.0.1:{port}"
        with pytest.raises(OfficialAgentError) as busy:
            official.grant(vgc.seat_id, b)                            # another runtime holds the lease
        assert busy.value.error_code == "seat_busy"
        assert official.renew_lease(vgc.seat_id, a)["seat_id"] == vgc.seat_id
        with pytest.raises(OfficialAgentError) as not_held:
            official.renew_lease(vgc.seat_id, b)
        assert not_held.value.error_code == "lease_not_held"
        with pytest.raises(OfficialAgentError):
            OfficialAgentClient(url, "eak_live_" + "0" * 64, load_env_file=False).authenticate()   # a wrong key
        outage.write_text("auth503")                                  # every session expired + authenticate 503
        time.sleep(0.6)
        with pytest.raises(OfficialAgentError) as down:
            official.assignments()                                    # the client raises the failed re-login ...
        assert down.value.status_code == 503
        from altruagent.official import is_fatal_auth_error
        assert not is_fatal_auth_error(down.value)                    # ... which the SDK's supervisor waits out
        outage.unlink()
        time.sleep(0.6)
        assert len(official.assignments()) == 2                       # and recovers from
        # our agent plays the Werewolf seat through the SDK's own tournament worker
        monkeypatch.setenv("ALTRUAGENT_CONTROL_URL", url)
        monkeypatch.setenv("ALTRUAGENT_OFFICIAL_AGENT_KEY", DEFAULT_OFFICIAL_KEY)
        ww = rows["werewolf"]
        code = run_tournament_worker(TournamentWorkerInput(seat_id=ww.seat_id, match_id=ww.match_id,
                                                           game_type="werewolf", agent_spec="agent.agent:create_agent",
                                                           execution_id="exec-C-" + "c" * 40))
        assert code == 0
        assert _wait(lambda: stats.exists() and json.loads(stats.read_text())["totals"]["matches_done"] >= 1, timeout=20)
        st = json.loads(stats.read_text())
        table = next(m for m in st["matches"] if m["game_type"] == "werewolf")
        assert table["terminal"] and table["decisions"] >= 1 and table["invalid_actions"] == 0 and not table["error"]
        assert table["timer"] == {"idle": 0} and table["race_errors"] == 0
        off = st["official"]
        assert off["seat_busy"] == 1 and off["lease_not_held"] == 1 and off["authenticate_injected"] >= 1
        assert off["moves_by_holder"] >= 1 and off["moves_by_non_holder"] == 0 and off["moves_unattributed"] == 0
        assert ww.seat_id not in [r.seat_id for r in official.assignments()]   # an ended match leaves the listing
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_the_fake_official_plane_serves_both_kinds_and_scores_a_missed_connect_window_as_a_no_show(monkeypatch):
    # 04-tournaments.md: a runtime that does not connect to a tournament seat within its window (4 minutes, Red Alert
    # 4.5) loses it as a no-show; the rehearsals check every seat's connect time against that window
    import types

    from altruagent.models import OfficialAssignment
    from altruagent.supervisor import TESTING, TOURNAMENT, assignment_kind
    from local import fake_official

    clock = {"t": 1000.0}
    monkeypatch.setattr(fake_official.time, "time", lambda: clock["t"])

    class Match:
        def __init__(self, n, game_type):
            self.n, self.seat, self.game_type, self.sid, self.terminal, self.forfeited = n, 0, game_type, f"g{n}", False, 0

        def forfeit_no_show(self):
            self.forfeited += 1

    games = [Match(0, "pokemon_vgc_doubles_draft"), Match(1, "werewolf"), Match(2, "red_alert")]
    platform = types.SimpleNamespace(order=games, issue=lambda *a, **k: "seat-token", revoke=lambda tok: None,
                                     agent_id="a1", game_server="127.0.0.1:1", outage=lambda: None)
    plane = fake_official.OfficialPlane(platform, "k", session_ttl=60, seat_token_ttl=60, lease_s=30)
    rows = [OfficialAssignment.from_dict(r) for r in plane.assignments()["assignments"]]
    assert [assignment_kind(r) for r in rows] == [TOURNAMENT, TESTING, TOURNAMENT]
    assert rows[0].tournament_name == "Local Fake Cup" and rows[0].opponents == ("local-bot",)
    assert rows[1].tournament_name is None
    assert [r.connect_deadline_at for r in rows] == ["1970-01-01T00:20:40Z", None,          # (Testing: no deadline)
                                                     "1970-01-01T00:21:10Z"]                # 240 s; Red Alert 270 s
    clock["t"] = 1100.0
    assert plane.grant(rows[1].seat_id, "exec-1")[0] == 200            # connected 100 s after it was assigned
    clock["t"] = 1241.0                                                # the VGC seat's window passed unconnected
    assert [r["seat_id"] for r in plane.assignments()["assignments"]] == [rows[1].seat_id, rows[2].seat_id]
    code, body = plane.grant(rows[0].seat_id, "exec-1")
    assert (code, body["error"]) == (409, "assignment_not_grantable") and games[0].forfeited == 1
    clock["t"] = 1271.0
    assert [r["seat_id"] for r in plane.assignments()["assignments"]] == [rows[1].seat_id]
    st = plane.stats()
    assert (st["no_shows"], st["connected"]) == (2, 1) and games[0].forfeited == 1 and games[1].forfeited == 0
    assert st["seats"][rows[1].seat_id]["connected_s"] == 100.0 and st["seats"][rows[0].seat_id]["no_show"]


def test_realistic_payloads_match_the_recorded_shape():
    import types

    from local import fake_platform as fp
    fm = types.SimpleNamespace(match=None, seat=0, n=3, bank_used=100.0)
    raw = {"phase": "moving", "observation": {"turn": 4, "fields": ["Grassy Terrain"], "weather": None,
                                              "team": {"p1a": {"species": "Pikachu", "fainted": False}},
                                              "protocol_log": ["|switch|p1a: Pikachu|Pikachu, L50|100/100"]}}
    obs = fp.realistic(raw, fm)["observation"]
    assert obs["fields"] == {"GRASSY_TERRAIN": 0} and obs["weather"] == {}
    assert obs["protocol_log"][-1] == "|inactive|Time left: 55 sec this turn | 410 sec total"
    assert obs["team"]["p1a"]["revealed"] is True and obs["team"]["p1a"]["is_terastallized"] is False
    assert obs["target_legend"]["-1"].startswith("board position A") and obs["battle_tag"].endswith("-3")
    assert fp.realistic({"phase": "draft", "observation": {"x": 1}}, fm) == {"phase": "draft", "observation": {"x": 1}}
    # every blind_every'th battle request is a blind state: the template stays, the battle view is gone
    plat = types.SimpleNamespace(realistic=True, blind_every=2)
    m = fp.FakeMatch(plat, 3, "random", 1)
    tmpl = {"actions": [{"action_id": "doubles_turn", "input": {"action": {"type": "doubles_turn", "slots": []}}}]}
    shaped = [m.shape({"phase": "moving", "legal_actions": copy.deepcopy(tmpl),
                       "observation": {"turn": 3, "team": {}, "protocol_log": []}}) for _ in range(2)]
    assert "protocol_log" in shaped[0]["observation"] and shaped[1]["observation"] == fp.BLIND_OBS
    assert shaped[1]["legal_actions"] == tmpl and m.st["blind"] == 1
    plain = fp.FakeMatch(types.SimpleNamespace(realistic=False, blind_every=2), 4, "random", 1)
    raw = {"phase": "moving", "observation": {"turn": 3}}
    assert plain.shape(copy.deepcopy(raw)) == raw                       # --plain: the old local shape


# ---- packaging ------------------------------------------------------------------------------------------------------
BUNDLE_FILES = ["agent", "altruagent", "data", "deploy", "pyproject.toml", "requirements.lock", ".env.example",
                "README.md", "GAMES.md", "docs/deploy.md", "docs/restart_load.md", "scripts/run_agent.sh",
                "scripts/selfcheck.py", "scripts/check_connection.py", "scripts/replay_real.py", "scripts/bundle.sh"]


def _mini_checkout(dst: Path) -> Path:
    for p in BUNDLE_FILES:
        src = ROOT / p
        (dst / p).parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst / p, symlinks=True, ignore=shutil.ignore_patterns("node_modules", "__pycache__"))
        else:
            shutil.copy2(src, dst / p)
    return dst


def _bundle(root: Path, out: Path, *args, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(root / "scripts" / "bundle.sh"), "--out", str(out), "--no-node-modules",
                           "--no-sample-logs", *args], capture_output=True, text=True, timeout=300, env=env)


def _names(dist: Path) -> list[str]:
    with tarfile.open(next(dist.glob("*.tar.gz"))) as t:
        return t.getnames()


def _bundle_file(dist: Path, suffix: str) -> str:
    with tarfile.open(next(dist.glob("*.tar.gz"))) as t:
        return t.extractfile(next(n for n in t.getnames() if n.endswith(suffix))).read().decode()


@needs_linux
def test_bundle_leaves_env_files_out_and_its_secret_scan_blocks_keys(tmp_path):
    root = _mini_checkout(tmp_path / "checkout")
    (root / ".env").write_text("ALTRUAGENT_API_KEY=sk_agent_REALLOOKINGKEY123456\n")
    (root / ".env.apocalypse").write_text("ALTRUAGENT_API_KEY=sk_agent_OTHERKEY9876543\n")
    r = _bundle(root, tmp_path / "dist", "--no-node")
    assert r.returncode == 0, r.stdout + r.stderr
    names = _names(tmp_path / "dist")
    top = names[0].split("/")[0]
    assert f"{top}/agent/agent.py" in names and f"{top}/agent/pokemon/draft_model.json" in names
    assert f"{top}/requirements.lock" in names and f"{top}/deploy/Dockerfile" in names
    assert f"{top}/docs/deploy.md" in names and f"{top}/docs/restart_load.md" in names   # (deploy.md points to it)
    assert f"{top}/SHA256SUMS" in names and f"{top}/BUNDLE_README.txt" in names
    assert not [n for n in names if "/.env" in n and not n.endswith(".env.example")]
    assert not [n for n in names if "__pycache__" in n or "/logs/" in n or "/tests/" in n or "/local/" in n]
    assert not [n for n in names if "/node/" in n]
    readme = _bundle_file(tmp_path / "dist", "BUNDLE_README.txt")
    assert "NO node binary is included" in readme and "--init" not in readme and "must end with" in readme
    assert f"{top}/COMMIT" in names                                 # (not a git checkout: says so, never a guess)
    assert _bundle_file(tmp_path / "dist", "/COMMIT").startswith(f"commit unknown (not built from a git checkout)\nbundle {top}, built ")
    (root / "agent" / "leak.py").write_text('KEY = "sk_agent_REALLOOKINGKEY123456"\n')
    r = _bundle(root, tmp_path / "dist2", "--no-node")
    assert r.returncode != 0 and "SECRET SCAN" in r.stderr and "REALLOOKINGKEY123456" not in r.stderr
    assert not (tmp_path / "dist2").exists() or not list((tmp_path / "dist2").glob("*.tar.gz"))


@needs_linux
@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_bundle_names_the_full_commit_it_was_built_from(tmp_path):
    # Official Rules sections 8 and 9: the deployed version must be identifiable. The bundle's COMMIT file (logged by
    # run_agent.sh at every start) holds the full commit id, and says so when tracked files differed from it.
    root = _mini_checkout(tmp_path / "checkout")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "x")
    sha = _git(root, "rev-parse", "HEAD")
    r = _bundle(root, tmp_path / "dist", "--no-node")
    assert r.returncode == 0, r.stdout + r.stderr
    commit = _bundle_file(tmp_path / "dist", "/COMMIT").splitlines()
    assert commit[0] == f"commit {sha}" and commit[1].startswith(f"bundle arena-agent-{sha[:7]}, built "), commit
    assert f"Source: commit {sha} (also in COMMIT" in _bundle_file(tmp_path / "dist", "BUNDLE_README.txt")
    (root / "README.md").write_text("changed\n")
    r = _bundle(root, tmp_path / "dist2", "--no-node")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _bundle_file(tmp_path / "dist2", "/COMMIT").splitlines()[0] == f"commit {sha} (built with uncommitted changes)"


@needs_linux
def test_bundle_ships_node_by_default_and_only_the_pinned_build(tmp_path):
    root = _mini_checkout(tmp_path / "checkout")
    text = (root / "scripts" / "bundle.sh").read_text()
    version = re.search(r"^NODE_VERSION=(v\d+\.\d+\.\d+)", text, re.M).group(1)
    assert int(version[1:].split(".")[0]) >= 18
    # a tarball that is not the pinned official build: refused (a download would be checked the same way)
    fake = tmp_path / f"node-{version}-linux-x64.tar.xz"
    fake.write_bytes(b"not node")
    r = _bundle(root, tmp_path / "dist", "--node-tarball", str(fake))
    assert r.returncode != 0 and "NODE CHECKSUM MISMATCH" in r.stderr, r.stderr
    cache = tmp_path / "cache"                                      # the default: the pinned build, from the cache
    cache.mkdir()
    (cache / f"node-{version}-linux-x64.tar.xz").write_bytes(b"tampered")
    (cache / f"node-{version}-linux-x64.tar.gz").write_bytes(b"tampered")
    env = dict(os.environ, ARENA_NODE_CACHE=str(cache), PATH=f"{tmp_path / 'nonet'}:{os.environ['PATH']}")
    for tool in ("curl", "wget"):                                   # no network in tests: downloads fail
        _fake_node(tmp_path / "nonet" / tool, "exit 7")
    r = _bundle(root, tmp_path / "dist0", env=env)
    assert r.returncode != 0 and "download failed" in r.stderr and not (tmp_path / "dist0").exists(), r.stderr
    # --node-dist: another official build >= 18
    nd = tmp_path / "nodedist"
    _fake_node(nd / "bin" / "node", "echo v18.99.0")
    r = _bundle(root, tmp_path / "dist3", "--node-dist", str(nd))
    assert r.returncode == 0, r.stderr
    assert any(n.endswith("/node/bin/node") for n in _names(tmp_path / "dist3"))
    assert "Node.js v18.99.0 is included" in _bundle_file(tmp_path / "dist3", "BUNDLE_README.txt")
    _fake_node(nd / "bin" / "node", "echo v16.20.0")
    r = _bundle(root, tmp_path / "dist4", "--node-dist", str(nd))
    assert r.returncode != 0 and "not a node >= 18" in r.stderr


@needs_linux
def test_bundle_scans_for_official_keys_and_ships_a_dockerignore_wheels_and_one_whole_match(tmp_path):
    root = _mini_checkout(tmp_path / "checkout")
    (root / ".env").write_text(f"ALTRUAGENT_OFFICIAL_AGENT_KEY={FAKE_EAK}\n")   # never copied (allowlist)
    draft = json.dumps({"kind": "action", "raw": {"phase": "draft"}})
    battle = json.dumps({"kind": "action", "raw": {"phase": "moving"}})
    for seat, sessions in {"yoriko": {"a": 3, "b": 9, "nodraft": 40}, "apocalypse": {"a": 3, "b": 8, "c": 50,
                                                                                    "nodraft": 40}}.items():
        (root / "logs" / seat).mkdir(parents=True)
        for s, n in sessions.items():
            rows = ([] if s == "nodraft" else [draft]) + [battle] * n
            (root / "logs" / seat / f"{s}.jsonl").write_text("\n".join(rows) + "\n")
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "httpx-0.0-py3-none-any.whl").write_bytes(b"PK\x03\x04 fake wheel")
    r = subprocess.run(["bash", str(root / "scripts" / "bundle.sh"), "--out", str(tmp_path / "dist"), "--no-node",
                        "--no-node-modules", "--wheels", str(wheels)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    names = _names(tmp_path / "dist")
    top = names[0].split("/")[0]
    assert f"{top}/logs/yoriko/b.jsonl" in names and f"{top}/logs/apocalypse/b.jsonl" in names   # both seats, one match
    assert not [n for n in names if n.endswith(("/a.jsonl", "/c.jsonl", "/nodraft.jsonl"))]
    assert f"{top}/wheels/httpx-0.0-py3-none-any.whl" in names and not [n for n in names if "/.env" in n and
                                                                         not n.endswith(".env.example")]
    ignore = _bundle_file(tmp_path / "dist", "/.dockerignore").split()
    assert {".env", ".env.*", ".venv", "logs", "node", "wheels"} <= set(ignore) and "**/node_modules" not in ignore
    readme = _bundle_file(tmp_path / "dist", "BUNDLE_README.txt")
    assert "--no-index --find-links wheels" in readme and "glibc >= 2.28" in readme and 'Never "pip install ."' in readme
    assert "check-tournament" in readme and "both seats of real match b" in readme and "--log-opt max-size" in readme
    # the secret scan knows the Official Agent Key
    (root / "agent" / "leak.py").write_text(f'KEY = "{FAKE_EAK}"\n')
    r = _bundle(root, tmp_path / "dist2", "--no-node")
    assert r.returncode != 0 and "SECRET SCAN" in r.stderr and FAKE_EAK not in r.stderr
    (root / "agent" / "leak.py").write_text('ALTRUAGENT_OFFICIAL_AGENT_KEY="some_real_looking_value_123"\n')
    r = _bundle(root, tmp_path / "dist3", "--no-node")
    assert r.returncode != 0 and "SECRET SCAN" in r.stderr
    (root / "agent" / "leak.py").unlink()
    r = _bundle(root, tmp_path / "dist4", "--no-node", "--wheels", str(tmp_path / "nowheels"))
    assert r.returncode != 0 and "no .whl files" in r.stderr


def test_replay_real_runs_from_a_bundle_without_the_project_installed(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "replay_real.py")], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=120)
    assert "ModuleNotFoundError" not in r.stderr, r.stderr


def test_requirements_lock_pins_the_runtime_dependencies():
    lines = [x for x in (ROOT / "requirements.lock").read_text().splitlines() if x and not x.startswith("#")]
    pins = dict(x.split("==") for x in lines)
    assert all("==" in x for x in lines)
    assert {"httpx", "python-dotenv", "mcp"} <= set(pins)
    assert pins["mcp"].startswith("1.")                             # pyproject: mcp>=1.12.0,<2.0.0
    assert "pytest" not in pins


def test_dockerfile_unit_and_ignores_keep_keys_and_logs_out():
    docker = (ROOT / "deploy" / "Dockerfile").read_text()
    version = re.search(r"^NODE_VERSION=v(\S+)", (ROOT / "scripts" / "bundle.sh").read_text(), re.M).group(1)
    assert "FROM python:3.12" in docker and f"FROM node:{version}-bookworm-slim" in docker   # the bundle's node
    assert "COPY --from=calc /usr/local/bin/node" in docker and f"v{version}" in docker
    assert "requirements.lock" in docker and "run_agent.sh" in docker and "selfcheck.py --no-replay" in docker
    assert "--allow-no-calc" not in docker and "ENV ARENA_ALLOW_NO_CALC" not in docker and "ARENA_CALC=off" not in docker
    assert ".env " not in docker.replace(".env.example", "") and "COPY logs" not in docker
    assert "--init" not in docker.replace("(No --init", "")                # tini is already the entrypoint
    ignore = (ROOT / ".dockerignore").read_text().split()
    assert {".env", ".env.*", "logs", "**/node_modules", "node"} <= set(ignore)
    unit = (ROOT / "deploy" / "arena-agent@.service").read_text()
    for needle in ("KillMode=mixed", "Restart=always", "run_agent.sh run --name %i", "RestartPreventExitStatus=3",
                   "OOMPolicy=continue"):                          # (one OOM kill must not stop every match)
        assert needle in unit
    assert "HEALTHCHECK" in docker and "agent=up" in docker          # (docker ps shows an agent that is down)
    assert 'ENTRYPOINT ["/usr/bin/tini", "--", "bash", "/app/scripts/run_agent.sh"]' in docker
    assert 'CMD ["run", "--name", "main"]' in docker and "check-tournament" in docker and "--log-opt max-size" in docker
    assert "OOMPolicy=continue" in (ROOT / "scripts" / "rehearsal.py").read_text()
    gitignore = (ROOT / ".gitignore").read_text().split()
    assert "logs/*" in gitignore and ".env" in gitignore and "dist/" in gitignore and "/node/" in gitignore


def test_the_runbook_installs_the_engine_search_and_docker_builds_from_a_bundle():
    doc = (ROOT / "docs" / "deploy.md").read_text()
    assert "(cd local/js && npm ci)" in doc and "OK    battle policy: engine search" in doc
    assert "ARENA_BATTLE=classic" in doc and "search=ok" in doc
    docker = (ROOT / "deploy" / "Dockerfile").read_text()
    assert "extracted bundle directory" in docker and "selfcheck.py --no-replay" in docker
    assert "ARENA_BATTLE=search" in (ROOT / "scripts" / "run_agent.sh").read_text()


def test_the_runbook_covers_the_official_tournament_mode_and_holds_no_key():
    doc = (ROOT / "docs" / "deploy.md").read_text()
    for needle in ("--tournament", "check-tournament", "ALTRUAGENT_OFFICIAL_AGENT_KEY", "mode=tournament(primary)",
                   "HOT STANDBY", "OOMPolicy=continue", "ARENA_EXECUTION_ID", "rehearsal_official.py", "glibc ≥ 2.28",
                   "pip install .", "--wheels", "--log-opt max-size", "上游缺陷"):
        assert needle in doc, needle
    for f in (ROOT / "docs" / "deploy.md", ROOT / "deploy" / "Dockerfile", ROOT / ".env.example"):   # (bundled files)
        assert not re.search(r"eak_live_[0-9a-fA-F]{16,}", f.read_text()), f


def test_pointers_into_the_runbook_name_sections_it_has():
    # review 10-07: every bundle's BUNDLE_README.txt and agent/__main__.py pointed at deploy.md sections ("官方正式赛",
    # "官方模式下看门脚本和 agent 额外做了什么") that the 10-07 rewrite had renamed
    headings = [x.lstrip("#").strip() for x in (ROOT / "docs" / "deploy.md").read_text().splitlines()
                if x.startswith("#")]
    bundle = re.search(r'Runbook: docs/deploy\.md \(the official tournament: section "([^"]+)"\)',
                       (ROOT / "scripts" / "bundle.sh").read_text())
    runtime = re.search(r'docs/deploy\.md "([^"]+)"', (ROOT / "agent" / "__main__.py").read_text())
    assert bundle and runtime
    for name in (bundle.group(1), runtime.group(1)):
        assert any(h.startswith(name) for h in headings), name


def test_the_runbook_has_no_placeholders_and_requires_the_calculator():
    doc = (ROOT / "docs" / "deploy.md").read_text()
    assert "REHEARSAL_TABLE" not in doc and "330 MB" not in doc
    assert not [x for x in doc.splitlines() if "docker run" in x and "--init" in x]
    assert "ARENA_ALLOW_NO_CALC" in doc and "--node-dist DIR`：连 Node" not in doc
