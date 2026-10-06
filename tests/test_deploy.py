"""Deployment readiness: supervisor knobs and steady discovery (agent/__main__.py), the official tournament runtime's
hardening (a transient re-authentication failure keeps the runtime and its matches, the start waits out an outage,
the run loop's execution id, `python -m agent` without run_agent.sh: control-plane default and a loud report), the
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
from altruagent.models import AgentSessions

import agent.__main__ as agent_main
from agent.pokemon import calc, engine

ROOT = Path(__file__).resolve().parents[1]
RUN_AGENT = ROOT / "scripts" / "run_agent.sh"
needs_linux = pytest.mark.skipif(not sys.platform.startswith("linux") or shutil.which("bash") is None,
                                 reason="run_agent.sh is tested on Linux")
needs_node = pytest.mark.skipif(shutil.which("node") is None or calc._js_dir() is None,
                                reason="needs node + agent/js/node_modules")


# ---- agent/__main__.py ---------------------------------------------------------------------------------------------
def test_supervisor_options_default_to_the_sdk_and_ignore_bad_values(capsys):
    assert agent_main._supervisor_options({}) == {}
    assert agent_main._supervisor_options({"ARENA_DISCOVERY_S": "10", "ARENA_WORKER_COOLDOWN_S": "7.5"}) == \
        {"discovery_interval": 10.0, "cooldown_seconds": 7.5}
    assert agent_main._supervisor_options({"ARENA_DISCOVERY_S": "0.5", "ARENA_WORKER_COOLDOWN_S": "soon"}) == {}
    assert agent_main._supervisor_options({"ARENA_DISCOVERY_S": "nan"}) == {}
    assert capsys.readouterr().out.count("Ignoring") == 3
    assert agent_main._tournament_options({}) == {}                   # (the SDK: a 10 s poll, a 60 s cooldown)
    assert agent_main._tournament_options({"ARENA_DISCOVERY_S": "5", "ARENA_WORKER_COOLDOWN_S": "3"}) == \
        {"poll_interval": 5.0, "cooldown_seconds": 3.0}


def test_python_m_agent_sets_the_10s_knobs_itself(monkeypatch):
    # the SDK's own defaults (15 s discovery, 60 s cooldown after a crashed worker) would cost a 55 s battle turn: a
    # plain `python -m agent` (a harness, the README's way) now gets run_agent.sh's 10 s / 10 s too
    monkeypatch.setattr(agent_main, "_local_checks", lambda check_search=True: [(True, "fine")])
    env = {"ALTRUAGENT_CONTROL_URL": "http://127.0.0.1:9"}
    agent_main._startup(["--tournament"], env)
    assert agent_main._tournament_options(env) == {"poll_interval": 10.0, "cooldown_seconds": 10.0}
    assert agent_main._supervisor_options(env) == {"discovery_interval": 10.0, "cooldown_seconds": 10.0}
    env = {"ALTRUAGENT_CONTROL_URL": "x", "ARENA_WORKER_COOLDOWN_S": "4"}      # (a value that is set stays)
    agent_main._startup([], env)
    assert env["ARENA_WORKER_COOLDOWN_S"] == "4" and env["ARENA_DISCOVERY_S"] == "10"


class _FlakyClient:
    def __init__(self, errors):
        self.errors = list(errors)
        self.closed = False

    def sessions(self):
        if self.errors:
            raise self.errors.pop(0)
        return AgentSessions(active=["m1"])

    def close(self):
        self.closed = True


def test_steady_discovery_keeps_running_through_failed_polls_and_raises_after_the_limit(capsys):
    blip = PlatformError("Could not reach /agents/me/sessions", status_code=None)
    steady = agent_main._SteadyDiscovery(_FlakyClient([blip, AuthenticationError("401", status_code=401)]),
                                         max_failures=3)
    assert steady.sessions().active == [] and steady.sessions().active == []   # two failures: nothing new, no raise
    assert steady.sessions().active == ["m1"] and steady.failures == 0          # recovered
    steady.close()                                                              # everything else passes through
    assert steady._client.closed
    out = capsys.readouterr().out
    assert "discovery failed (1 in a row" in out and "recovered after 2" in out
    dead = agent_main._SteadyDiscovery(_FlakyClient([blip] * 5), max_failures=3)
    dead.sessions(), dead.sessions()
    with pytest.raises(PlatformError):
        dead.sessions()


def test_main_hands_the_supervisor_steady_discovery_and_the_env_knobs(monkeypatch):
    import types
    seen = {}

    class FakeClient:
        def me(self):
            return types.SimpleNamespace(is_claimed=True, name="bot", id="agent-1", status="claimed")

        def close(self):
            pass

    def fake_supervisor(client, *, agent_id, **kw):
        seen.update(client=client, agent_id=agent_id, **kw)

    monkeypatch.setenv("ARENA_DISCOVERY_S", "10")
    monkeypatch.setenv("ARENA_WORKER_COOLDOWN_S", "10")
    monkeypatch.delenv("ALTRUAGENT_CLAIM_TOKEN", raising=False)
    monkeypatch.setattr(agent_main, "AltruAgentClient", lambda *a, **k: FakeClient())
    monkeypatch.setattr(agent_main, "run_forever_concurrent", fake_supervisor)
    assert agent_main.main([]) == 0
    assert isinstance(seen["client"], agent_main._SteadyDiscovery)
    assert seen["agent_id"] == "agent-1" and seen["discovery_interval"] == 10.0 and seen["cooldown_seconds"] == 10.0


# ---- the official tournament runtime (python -m agent --tournament) ------------------------------------------------
FAKE_EAK = "eak_live_" + "fa4e" * 16                 # obviously fake, but the shape the SDK's local check accepts


class _AuthBackend:
    """A control plane whose agent session expires after the first listing and whose re-authentication then fails
    with `status`/`code` until `recover_at` listings have been asked for (exp20's demo_fatal.py, as a test)."""

    def __init__(self, status: int, code: str | None, recover_at: int = 4):
        self.status, self.code, self.recover_at = status, code, recover_at
        self.calls = {"auth": 0, "list": 0}

    def __call__(self, request):
        import httpx
        path = request.url.path
        if path == "/tournament/agent/authenticate":
            self.calls["auth"] += 1
            if self.calls["auth"] == 1 or self.calls["list"] >= self.recover_at:
                return httpx.Response(200, json={"access_token": f"session-{self.calls['auth']}"})
            return httpx.Response(self.status, json={"error": self.code} if self.code else {})
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


@pytest.mark.parametrize("status, code", [(429, "rate_limited"), (503, None), (502, None), (500, "internal")])
def test_a_transient_reauthentication_failure_no_longer_ends_the_runtime_and_its_matches(status, code):
    # upstream defect (intel-2): assignments() -> 401 -> re-login -> 429 / 5xx raises OfficialAgentError, an
    # AuthenticationError that run_tournament_once does not catch: run_tournament_forever ended and its finally
    # terminated EVERY running worker. Through _SteadyOfficial the runtime keeps going and keeps its worker.
    from altruagent.supervisor import run_tournament_forever
    from tests.test_supervisor import FakeProcessFactory

    backend, factory, log = _AuthBackend(status, code), FakeProcessFactory(), []
    clock = {"t": 0.0}

    def sleep(s):
        clock["t"] += s

    official = _official(backend)
    agent_main._SteadyOfficial.install(official, now=lambda: clock["t"], log=log.append)
    run_tournament_forever(official, agent_spec="agent.agent:create_agent", sleep=sleep, now=lambda: clock["t"],
                           process_factory=factory, log=log.append, max_iterations=12, poll_interval=10.0,
                           cooldown_seconds=10.0)
    assert len(factory.processes) == 1                               # one worker, started once and never restarted
    assert not [line for line in log if "no longer assigned" in line]  # never stopped as "unassigned"
    assert any("re-authentication failed (1 in a row; running matches continue" in line for line in log)
    assert any("re-authentication recovered" in line for line in log) and "Assignment discovery recovered." in log
    if code == "rate_limited":                                       # a rate limit pauses the listing (15, 30 s ...)
        assert backend.calls["auth"] <= 4, backend.calls


def test_the_unfixed_sdk_path_still_dies_so_the_wrapper_is_what_keeps_the_matches():
    from altruagent.errors import AuthenticationError as AuthErr
    from altruagent.supervisor import run_tournament_forever
    from tests.test_supervisor import FakeProcessFactory

    factory = FakeProcessFactory()
    with pytest.raises(AuthErr):                                     # (report upstream: docs/deploy.md)
        run_tournament_forever(_official(_AuthBackend(503, None)), agent_spec="agent.agent:create_agent",
                               sleep=lambda s: None, process_factory=factory, log=lambda m: None, max_iterations=6)
    assert factory.processes and factory.processes[0].terminated     # ... taking the running worker with it


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
    agent_main._SteadyOfficial.install(official, now=lambda: clock["t"], log=log.append, hold_new_s=hold)
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
        agent_main._SteadyOfficial.install(official, now=lambda: clock["t"], log=log.append, hold_new_s=20.0,
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

    steady = agent_main._SteadyOfficial(lambda: [_Row("s1")], now=lambda: clock["t"], log=lambda m: None,
                                        hold_new_s=20.0, grant=broken_grant, execution_id="x" * 43)
    assert steady() == [] and seen == []                             # a new seat: left to the primary for 20 s
    clock["t"] = 20.0
    assert [r.seat_id for r in steady()] == ["s1"] and seen == ["s1"]  # the check itself failed: handed anyway
    clock["t"] = 30.0
    assert [r.seat_id for r in steady()] == ["s1"] and seen == ["s1"]  # handed once: never checked again
    plain = agent_main._SteadyOfficial(lambda: [_Row("s2")], now=lambda: clock["t"], log=lambda m: None,
                                       hold_new_s=20.0)             # nothing to check with: the hold alone, as before
    assert plain() == []
    clock["t"] = 50.0
    assert [r.seat_id for r in plain()] == ["s2"]


def test_a_rejected_official_key_stays_fatal():
    from altruagent.official import OfficialAgentError as OffErr
    official = _official(_AuthBackend(401, "invalid_official_agent_key"))
    agent_main._SteadyOfficial.install(official, log=lambda m: None)
    assert official.assignments()                                    # the first session works
    with pytest.raises(OffErr) as exc_info:
        official.assignments()                                       # the key is revoked: fatal at once, as upstream
    assert exc_info.value.error_code == "invalid_official_agent_key"
    forbidden = _official(_AuthBackend(403, "forbidden"))
    agent_main._SteadyOfficial.install(forbidden, log=lambda m: None)
    forbidden.assignments()
    with pytest.raises(OffErr):
        forbidden.assignments()


def test_transient_classification():
    from altruagent.official import OfficialAgentError as OffErr
    assert agent_main._transient(OffErr("x", status_code=429, error_code="rate_limited"))
    assert agent_main._transient(OffErr("x", status_code=503)) and agent_main._transient(OffErr("x", status_code=502))
    assert agent_main._transient(PlatformError("Could not reach", status_code=None))
    assert not agent_main._transient(OffErr("x", status_code=401, error_code="invalid_official_agent_key"))
    assert not agent_main._transient(OffErr("no session token in the answer"))          # (status None)
    assert not agent_main._transient(PlatformError("bad request", status_code=400))


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
    assert slept == [5.0, 10.0, 20.0] and "Could not connect as official tournament agent" in capsys.readouterr().out


def test_tournament_mode_hands_the_sdk_the_steady_client_and_the_knobs(monkeypatch):
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

    def fake_forever(client, *, agent_spec, **kw):
        seen.update(client=client, agent_spec=agent_spec, **kw)

    for k in ("ARENA_EXECUTION_ID", "ARENA_SEAT_BUSY_RETRY_S"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ARENA_DISCOVERY_S", "10")                     # (what _startup / run_agent.sh set)
    monkeypatch.setenv("ARENA_WORKER_COOLDOWN_S", "10")
    monkeypatch.setattr(agent_main, "OfficialAgentClient", lambda *a, **k: official)
    monkeypatch.setattr(agent_main, "run_tournament_forever", fake_forever)
    assert agent_main.main(["--tournament"]) == 0
    assert seen["client"] is official and isinstance(official.assignments, agent_main._SteadyOfficial)
    assert seen["poll_interval"] == 10.0 and seen["cooldown_seconds"] == 10.0     # (SDK: 10 s / 60 s)


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
    # applied to the SDK's supervisor module for this runtime (new_execution_id, SEAT_BUSY_RETRY_SECONDS), restored after
    before = (sup.new_execution_id, sup.SEAT_BUSY_RETRY_SECONDS)
    restore, used = agent_main._tournament_tuning({"ARENA_EXECUTION_ID": eid, "ARENA_SEAT_BUSY_RETRY_S": "12",
                                                   "ARENA_EXECUTION_ID_OWNER": str(os.getppid())})
    try:
        assert used == eid and sup.TournamentState().execution_id == eid and sup.SEAT_BUSY_RETRY_SECONDS == 12.0
    finally:
        restore()
    assert (sup.new_execution_id, sup.SEAT_BUSY_RETRY_SECONDS) == before
    assert sup.TournamentState().execution_id != eid
    restore, fresh = agent_main._tournament_tuning({})               # no run loop: a fresh id, as upstream, but fixed
    try:                                                             # here so the standby's seat check uses it too
        assert fresh != eid and len(fresh) >= 32 and sup.TournamentState().execution_id == fresh
    finally:
        restore()
    assert (sup.new_execution_id, sup.SEAT_BUSY_RETRY_SECONDS) == before


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
    assert agent_main._startup([], env) is True and env["ALTRUAGENT_CONTROL_URL"] == "https://api.altruagent-game.com"
    assert "DEGRADED" not in capsys.readouterr().out
    env = {}
    assert agent_main._startup(["--claim", "seatclaim_FAKE"], env) is True and env == {}   # claim mode: untouched
    monkeypatch.setattr(agent_main, "_local_checks", lambda check_search=True: [(True, "all fine")])
    assert agent_main._startup([], {"ALTRUAGENT_CONTROL_URL": "x"}) is True
    assert capsys.readouterr().out.strip() == "Local checks: all fine"


def _plain_agent(args, extra_env, timeout=120):
    """`python -m agent ARGS` as a harness would run it (no run_agent.sh), never able to reach a real platform: the
    keys are fake or malformed and the control plane is a dead local port."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ALTRUAGENT_", "ARENA_"))}
    env.update(PYTHONPATH=str(ROOT), ARENA_RECORD="0", ALTRUAGENT_CONTROL_URL="http://127.0.0.1:9", **extra_env)
    return subprocess.run([sys.executable, "-m", "agent", *args], cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=timeout)


def test_plain_python_m_agent_says_loudly_when_it_would_play_degraded():
    r = _plain_agent([], {"ALTRUAGENT_API_KEY": "sk_agent_FAKE_do_not_use_0000", "ARENA_CALC": "off",
                          "ARENA_BATTLE": "classic"})
    assert r.returncode == 1                                         # (the dead control plane: could not authenticate)
    assert "WARNING: THIS MACHINE PLAYS DEGRADED" in r.stdout and "WARNING: THIS MACHINE PLAYS DEGRADED" in r.stderr
    assert "damage calculator cannot run (ARENA_CALC=off)" in r.stderr
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
    key = "test-key-DO-NOT-LOG-12345"
    envf = tmp_path / "unit.env"
    envf.write_text(f"ALTRUAGENT_API_KEY={key}\nALTRUAGENT_CONTROL_URL=http://127.0.0.1:9\n")
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
    for k in ("ALTRUAGENT_API_KEY", "ARENA_ALLOW_NO_CALC", "INVOCATION_ID", "ARENA_RUN_ID"):
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
    assert r.returncode == 2 and "single-use seat" in r.stderr


@needs_linux
def test_run_agent_refuses_to_start_without_a_key(tmp_path):
    envf = tmp_path / "nokey.env"
    envf.write_text("ALTRUAGENT_API_KEY=sk_agent_your_key_here\n")
    env = dict(os.environ, LOG_DIR=str(tmp_path / "logs"), PYTHON=sys.executable)
    env.pop("ALTRUAGENT_API_KEY", None)
    r = subprocess.run(["bash", str(RUN_AGENT), "run", "--env", str(envf)], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2
    assert "ALTRUAGENT_API_KEY is not set" in (tmp_path / "logs" / "agent-nokey.out").read_text()


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
    envf.write_text("ALTRUAGENT_API_KEY=test-key-root-env-1\n")
    env = dict(os.environ, LOG_DIR=str(tmp_path / "logs"))
    for k in ("ARENA_BATTLE", "ALTRUAGENT_API_KEY"):
        env.pop(k, None)
    r = subprocess.run(["bash", str(root / "scripts" / "run_agent.sh"), "health", "--env", str(envf)], env=env,
                       capture_output=True, text=True, timeout=60)
    assert "search=classic(ARENA_BATTLE)" in r.stdout, r.stdout + r.stderr


@needs_linux
def test_run_agent_refuses_a_leftover_arena_calc_off_in_the_key_file(tmp_path):
    envf = tmp_path / "main.env"                                    # the real probe (real python): ARENA_CALC=off from
    envf.write_text("ALTRUAGENT_API_KEY=test-key-calc-off-123\nALTRUAGENT_CONTROL_URL=http://127.0.0.1:9\n"
                    "ARENA_CALC=off\n")                            # the key file is what the agent would see
    envf.chmod(0o600)
    env = dict(os.environ, LOG_DIR=str(tmp_path / "logs"), PYTHON=sys.executable, PYTHONPATH=str(ROOT),
               ARENA_LOCK_DIR=str(tmp_path))
    for k in ("ALTRUAGENT_API_KEY", "ARENA_CALC", "ARENA_ALLOW_NO_CALC", "ARENA_RUN_ID"):
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
    assert _ra(run_env, "start").returncode == 0
    other = dict(run_env["env"], LOG_DIR=str(run_env["tmp"] / "other-logs"))   # another checkout / LOG_DIR, same key
    r = _ra(run_env, "run", env=other)
    assert r.returncode == 3
    assert "already runs key" in (run_env["tmp"] / "other-logs" / "agent-unit.out").read_text()
    loop = int((run_env["logs"] / "agent-unit.pid").read_text())
    os.kill(loop, signal.SIGKILL)                                   # the loop is gone, its agent still plays:
    time.sleep(0.5)
    r = _ra(run_env, "run", env=other)                              # the agent itself still holds the key lock
    assert r.returncode == 3


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
    assert "fake agent args: -u -m agent --tournament" in log
    assert "mode tournament (ALTRUAGENT_OFFICIAL_AGENT_KEY is set)" in log
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
def test_the_official_mode_refuses_a_missing_or_malformed_key_and_can_be_forced_either_way(run_env):
    bad = run_env["tmp"] / "bad.env"
    bad.write_text("ALTRUAGENT_OFFICIAL_AGENT_KEY=eak_live_FAKE_not_hex\n")
    r = _ra_env(run_env, bad, "run")
    assert r.returncode == 2 and "doesn't look like an Official Agent Key" in r.stdout
    assert "eak_live_FAKE_not_hex" not in r.stdout + _log(run_env, "bad")
    forced = run_env["tmp"] / "forced.env"
    forced.write_text("ARENA_MODE=tournament\nALTRUAGENT_API_KEY=sk_agent_FAKE_only_the_generic_key\n")
    r = _ra_env(run_env, forced, "run")
    assert r.returncode == 2 and "tournament mode (ARENA_MODE) but ALTRUAGENT_OFFICIAL_AGENT_KEY is not set" in r.stdout
    both = _official_env(run_env, "both", "ALTRUAGENT_API_KEY=sk_agent_FAKE_generic_key_1234\n")
    r = _ra_env(run_env, both, "health")                              # both keys: the official one wins
    assert "mode=tournament" in r.stdout, r.stdout
    disc = _official_env(run_env, "disc", "ALTRUAGENT_API_KEY=sk_agent_FAKE_generic_key_1234\nARENA_MODE=discovery\n")
    r = _ra_env(run_env, disc, "start")
    assert r.returncode == 0 and "mode=discovery" in r.stdout, r.stdout
    _wait(lambda: "fake agent args" in _log(run_env, "disc"), timeout=10)
    assert "fake agent args: -u -m agent\n" in _log(run_env, "disc") + "\n"  # no --tournament
    assert "eid_len=0" in _log(run_env, "disc")
    assert _ra_env(run_env, disc, "stop").returncode == 0


@needs_linux
def test_a_second_runtime_with_the_same_official_key_starts_as_a_hot_standby(run_env):
    # sk_agent_ keys: a second instance is refused (exit 3, test_the_key_lock_covers_every_checkout_on_the_machine);
    # the official key: the platform's seat lease picks one runtime per seat, so a second one waits as a hot standby
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
def test_status_shows_the_mode_and_key_of_the_running_loop_not_of_the_asking_shell(run_env):
    # the runbook's competition-day check is `status` showing mode=tournament(primary). A loop started with
    # ARENA_MODE=discovery in its environment (key file: both keys) plays discovery; `status` from a shell without that
    # variable used to recompute tournament from the key file and show it, next to the discovery loop.
    env = {k: v for k, v in run_env["env"].items() if k != "ARENA_MODE"}
    both = _official_env(run_env, "both", "ALTRUAGENT_API_KEY=sk_agent_FAKE_generic_key_1234\n")
    r = _ra_env(run_env, both, "start", env=dict(env, ARENA_MODE="discovery"))
    assert r.returncode == 0 and "mode=discovery" in r.stdout, r.stdout
    sk = hashlib.sha256(b"sk_agent_FAKE_generic_key_1234").hexdigest()[:10]
    eak = hashlib.sha256(FAKE_EAK.encode()).hexdigest()[:10]
    r = _ra_env(run_env, both, "status", env=env)
    live = next(line for line in r.stdout.splitlines() if line.startswith("[health]"))
    assert "mode=discovery " in live and f"key=#{sk}" in live, r.stdout
    assert f"keyhash={sk}" in (run_env["logs"] / "agent-both.state").read_text()
    assert _ra_env(run_env, both, "stop", env=env).returncode == 0
    r = _ra_env(run_env, both, "health", env=env)                     # no loop: what a start from here would run
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
            official.assignments()                                    # the SDK's own client: the fatal error ...
        assert down.value.status_code == 503
        agent_main._SteadyOfficial.install(official, log=lambda m: None)
        with pytest.raises(PlatformError):
            official.assignments()                                    # ... which the runtime now skips instead
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
    (root / "agent" / "leak.py").write_text('KEY = "sk_agent_REALLOOKINGKEY123456"\n')
    r = _bundle(root, tmp_path / "dist2", "--no-node")
    assert r.returncode != 0 and "SECRET SCAN" in r.stderr and "REALLOOKINGKEY123456" not in r.stderr
    assert not (tmp_path / "dist2").exists() or not list((tmp_path / "dist2").glob("*.tar.gz"))


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


def test_the_runbook_has_no_placeholders_and_requires_the_calculator():
    doc = (ROOT / "docs" / "deploy.md").read_text()
    assert "REHEARSAL_TABLE" not in doc and "330 MB" not in doc
    assert not [x for x in doc.splitlines() if "docker run" in x and "--init" in x]
    assert "ARENA_ALLOW_NO_CALC" in doc and "--node-dist DIR`：连 Node" not in doc
