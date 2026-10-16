"""Actual CLI -> supervisor -> spawned workers -> loopback HTTP/MCP -> exit.

No run_agent.sh, service manager, official host, real key, or SDK patch. The
subprocess starts from an explicit environment allowlist with a temporary HOME.
These portable POSIX checks run on macOS and Linux; Linux systemd/cgroup limits
and service-unit deployment are deliberately not claimed by this test.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

from local.hok_fake_official import FAKE_KEY, FakeHokMatch, FakeHokOfficial, isolated_environment

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.name != "posix", reason="owned-PID POSIX shutdown; no Windows process claims")


def wait_until(predicate, timeout, failure):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(failure() if callable(failure) else failure)


def audit_rows(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def own_group_member(pid, group):
    try:
        return os.getpgid(pid) == group
    except ProcessLookupError:
        return False


@contextmanager
def fake_server(peer):
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(peer.build_app(port), host="127.0.0.1", port=port,
                                         log_level="error", access_log=False, timeout_graceful_shutdown=2))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), name="owned-hok-fake-server", daemon=True)
    thread.start()
    try:
        wait_until(lambda: server.started, 10, "loopback MCP server did not start")
        yield port
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive(), "owned loopback server thread leaked"


@contextmanager
def cli_process(temporary, port, *, record=False, block_recording=False):
    env = isolated_environment(ROOT, temporary, port, record=record)
    if block_recording:
        blocked = temporary / "not-a-recording-directory"
        blocked.write_text("a regular file, so mkdir/open cannot record here")
        env["ARENA_RECORD_DIR"] = str(blocked)
    output = temporary / "cli-output.log"
    audit = temporary / "process-boundary.jsonl"
    with output.open("w") as stream:
        process = subprocess.Popen(
            [sys.executable, "-m", "agent", "--tournament", "--match"],
            cwd=temporary, env=env, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            yield process, output, audit, env
        finally:
            test_failed = sys.exc_info()[0] is not None
            forced_children = []
            # SIGINT is sent only to our supervisor, never a service or a
            # machine-wide process name. Its own SDK cleans up its own workers.
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            pids = {row["pid"] for row in audit_rows(audit) if row["event"] == "start"}
            grace = time.monotonic() + 2
            while time.monotonic() < grace and any(own_group_member(pid, process.pid) for pid in pids):
                time.sleep(0.05)
            # Even on test failure, only audited children still in our private
            # process group may be signalled. A reused PID in another group is
            # never touched.
            for pid in pids - {process.pid}:
                if own_group_member(pid, process.pid):
                    try:
                        forced_children.append(pid)
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            end = time.monotonic() + 5
            while time.monotonic() < end and any(own_group_member(pid, process.pid) for pid in pids):
                time.sleep(0.05)
            for pid in pids:
                if own_group_member(pid, process.pid):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            end = time.monotonic() + 2
            while time.monotonic() < end and any(own_group_member(pid, process.pid) for pid in pids):
                time.sleep(0.05)
            assert not any(own_group_member(pid, process.pid) for pid in pids), "owned subprocess survived cleanup"
            if not test_failed:
                assert not forced_children, f"children needed forced cleanup instead of exiting: {forced_children}"


@pytest.mark.parametrize("record,inject_races,block_recording", [(False, False, False), (True, True, False),
                                                               (True, False, True)],
                         ids=["off-by-default", "recording-with-refusal-and-regrant", "recording-io-failure"])
def test_cli_dispatches_both_contexts_and_pacing_modes_finishes_and_reaps_workers(
        tmp_path, record, inject_races, block_recording):
    realtime = FakeHokMatch("realtime", context="tournament", steps=90,
                           reject_first=inject_races, reconnect=inject_races)
    lockstep = FakeHokMatch("lockstep", context="testing", steps=8, camp=2)
    peer = FakeHokOfficial([realtime, lockstep])

    with fake_server(peer) as port:
        with cli_process(tmp_path, port, record=record, block_recording=block_recording) as (process, output, audit, env):
            def completed():
                text = output.read_text()
                return (all(m.results == 1 for m in peer.matches.values())
                        and text.count("[match-summary]") == 2 and text.count("finished: your agent") == 2)

            wait_until(completed, 60, lambda: f"CLI failed to finish both matches:\n{output.read_text()}")
            text = output.read_text()
            assert process.poll() is None, "the supervisor must keep waiting after its matches finish"
            worker_pids = {int(pid) for pid in re.findall(r"starting \(pid=(\d+)\)", text)}
            assert len(worker_pids) == 2, text
            wait_until(lambda: all(not own_group_member(pid, process.pid) for pid in worker_pids), 8,
                       "finished workers were not naturally reaped by the supervisor")
            starts = [row for row in audit_rows(audit) if row["event"] == "start"]
            assert worker_pids <= {row["pid"] for row in starts}
            assert all(row["pgid"] == process.pid for row in starts)
            # Python/macOS may synthesize locale/launcher bookkeeping after
            # exec; these are not inherited secrets or deployment settings.
            os_added = {"LC_CTYPE", "__PYVENV_LAUNCHER__", "__CF_USER_TEXT_ENCODING"}
            assert all(set(row["env_keys"]) <= set(env) | os_added for row in starts)

            assert peer.counters["authenticate"] >= 1 and peer.counters["assignments"] >= 1
            assert not peer.counters["bad_auth"] and not peer.counters["seat_busy"]
            for match in (realtime, lockstep):
                assert match.terminal and match.results == 1
                assert match.connected_s is not None and match.connected_s < 45, "fake connect-window no-show"
                assert not match.invalid, f"{match.mode}: illegal client actions: {match.invalid}"
                assert {c["seat"] for c in match.accepted} == set(match.seats)
                assert all(c["reasoning_summary"] for c in match.accepted)
            assert len(lockstep.accepted) == 8 * 5
            assert len(realtime.accepted) == 5, "real-time standing orders must not be sent every step"
            if inject_races:
                assert realtime.injected_refusals == 1 and realtime.injected_401 == 1
                assert realtime.grants >= 2, "expired seat token must reconnect by re-grant, not lose the worker"
            else:
                assert realtime.injected_refusals == 0 and realtime.grants == 1

            summaries = [line for line in text.splitlines() if "[match-summary]" in line]
            for match in (realtime, lockstep):
                line = next(s for s in summaries if f"session={match.sid[:8]}" in s)
                assert f"orders={len(match.attempts)}" in line
                assert f"refused={match.injected_refusals}" in line
                assert re.search(r"decisions=[1-9]\d*.*hok views=[1-9]\d*", line)
            assert "unexpected error" not in text and "produced an invalid action" not in text
            assert FAKE_KEY not in text
            assert not list((tmp_path / "state").iterdir())
            recordings = list((tmp_path / "logs").glob("*.jsonl"))
            if block_recording:
                assert not recordings
                assert text.count("this match's states are not saved") == 2, "one I/O warning per worker, not per view"
            elif not record:
                assert not recordings, "full local lifecycle must keep decision recording off by default"
            else:
                assert len(recordings) == 2
                for match in (realtime, lockstep):
                    rows = [json.loads(line) for line in (tmp_path / "logs" / f"{match.sid}.jsonl").read_text().splitlines()]
                    assert 1 <= len(rows) <= (match.steps + 49) // 50 + 2
                    assert all("WithReasoning(" not in str(row["out"]) for row in rows)
            events = audit_rows(audit)
            assert not [row for row in events if row["event"].startswith("denied")], events
            connects = [row for row in events if row["event"] == "connect"]
            assert connects and all(row["port"] == port and row["host"] in ("127.0.0.1", "::1", "localhost")
                                    for row in connects)
        assert process.returncode == 0, output.read_text()


def test_subprocess_boundary_blocks_external_resolution_and_dotenv_without_reading_them(tmp_path):
    env = isolated_environment(ROOT, tmp_path, 43210)
    code = (
        "import socket\n"
        "for probe in (lambda: socket.getaddrinfo('forbidden.invalid',443), lambda: open('.env')):\n"
        " try: probe()\n"
        " except PermissionError: pass\n"
        " else: raise AssertionError('test isolation boundary failed')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True,
                            text=True, timeout=10, start_new_session=True)
    assert result.returncode == 0, result.stdout + result.stderr
    events = audit_rows(tmp_path / "process-boundary.jsonl")
    assert {row["event"] for row in events} >= {"start", "denied-network", "denied-file"}


def test_supervisor_replaces_only_its_killed_worker_and_resumes_server_standing_orders(tmp_path):
    match = FakeHokMatch("realtime", context="tournament", steps=35, pause_after_first_order=True)
    peer = FakeHokOfficial([match])
    with fake_server(peer) as port:
        try:
            with cli_process(tmp_path, port) as (process, output, audit, env):
                wait_until(lambda: len(match.accepted) == 1, 20, lambda: output.read_text())
                initial = re.findall(r"starting \(pid=(\d+)\)", output.read_text())
                assert len(initial) == 1
                stopped_pid = int(initial[0])
                assert stopped_pid in {row["pid"] for row in audit_rows(audit) if row["event"] == "start"}
                assert own_group_member(stopped_pid, process.pid), "refuse to signal any non-owned PID"
                # This is the only deliberately stopped match worker. It is
                # created by this test, never a live agent or a name-based kill.
                os.kill(stopped_pid, signal.SIGTERM)
                match.resume_reads.set()
                wait_until(lambda: match.results == 1 and "[match-summary]" in output.read_text(),
                           25, lambda: output.read_text())
                text = output.read_text()
                workers = [int(pid) for pid in re.findall(r"starting \(pid=(\d+)\)", text)]
                assert len(workers) == 2 and workers[0] != workers[1], text
                assert process.poll() is None
                wait_until(lambda: all(not own_group_member(pid, process.pid) for pid in workers), 8,
                           "replaced/finished workers must both be reaped")
                assert match.grants == 2 and not peer.counters["seat_busy"], "replacement must reuse its runtime lease"
                assert match.connected_s is not None and match.connected_s < 45
                assert not match.invalid
                assert len(match.accepted) == 5 and {row["seat"] for row in match.accepted} == set(match.seats)
                assert "orders=4" in text, "fresh worker should respect the earlier worker's accepted standing order"
                assert "exit code -15" in text and "finished: your agent" in text
                assert not list((tmp_path / "logs").iterdir())
                assert not [row for row in audit_rows(audit) if row["event"].startswith("denied")]
            assert process.returncode == 0
        finally:
            match.resume_reads.set()
