"""Deployment readiness: supervisor knobs and steady discovery (agent/__main__.py), the calculator's deployment
knobs and probe (agent/pokemon/calc.py), scripts/selfcheck.py (refuses a machine whose calculator, or with
ARENA_BATTLE=search whose engine search, cannot run), scripts/run_agent.sh (restart, rotation, locks, clean stop, no
orphans, calculator and engine search refusal, no key in logs),
local/fake_platform.py against the real SDK client, scripts/bundle.sh (allowlist, pinned node, secret scan) and the
packaging files (requirements.lock, deploy/Dockerfile, deploy/arena-agent@.service, .gitignore, docs)."""
from __future__ import annotations

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
    _wait(lambda: "[health]" in (logs / "agent-unit.out").read_text(), timeout=5)
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
    log = run_env["logs"] / "agent-unit.out"
    assert _wait(lambda: log.exists() and "restart #3 in 4s" in log.read_text(), timeout=20), log.read_text()
    assert "restart #2 in 2s" in log.read_text()
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
    log = (run_env["logs"] / "agent-unit.out").read_text()
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
    log = run_env["logs"] / "agent-unit.out"
    assert _wait(lambda: log.exists() and "restart #1 in 25s" in log.read_text(), timeout=20), log.read_text()
    run_env["mode"].write_text("run")
    t = time.monotonic()
    r = _ra(run_env, "restart", env=env)                           # the back-off sleep holds no lock any more
    assert r.returncode == 0 and "agent=up" in r.stdout, r.stdout + log.read_text()[-2000:]
    assert time.monotonic() - t < 20


@needs_linux
def test_a_sigkilled_run_loop_leaves_nothing_behind(run_env):
    logs = run_env["logs"]
    assert _ra(run_env, "start").returncode == 0
    loop = int((logs / "agent-unit.pid").read_text())
    child = int(_state(logs, "unit")["child"])
    _wait(lambda: "[health]" in (logs / "agent-unit.out").read_text(), timeout=5)
    os.kill(loop, signal.SIGKILL)                                   # (cmd_stop's own fallback, the OOM killer ...)
    time.sleep(2.5)                                                 # HEALTH_EVERY=1: an orphan health loop would log
    n = (logs / "agent-unit.out").read_text().count("[health]")
    time.sleep(3)
    assert (logs / "agent-unit.out").read_text().count("[health]") == n    # nothing outlives the loop and logs
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

        final = run_match(match, AGENT_ID, choose)
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


# ---- packaging ------------------------------------------------------------------------------------------------------
BUNDLE_FILES = ["agent", "altruagent", "data", "deploy", "pyproject.toml", "requirements.lock", ".env.example",
                "README.md", "GAMES.md", "docs/deploy.md", "scripts/run_agent.sh", "scripts/selfcheck.py",
                "scripts/check_connection.py", "scripts/replay_real.py", "scripts/bundle.sh"]


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
    for needle in ("KillMode=mixed", "Restart=always", "run_agent.sh run --name %i", "RestartPreventExitStatus=3"):
        assert needle in unit
    gitignore = (ROOT / ".gitignore").read_text().split()
    assert "logs/*" in gitignore and ".env" in gitignore and "dist/" in gitignore and "/node/" in gitignore


def test_the_runbook_installs_the_engine_search_and_docker_builds_from_a_bundle():
    doc = (ROOT / "docs" / "deploy.md").read_text()
    assert "(cd local/js && npm ci)" in doc and "OK    battle policy: engine search" in doc
    assert "ARENA_BATTLE=classic" in doc and "search=ok" in doc
    docker = (ROOT / "deploy" / "Dockerfile").read_text()
    assert "extracted bundle directory" in docker and "selfcheck.py --no-replay" in docker
    assert "ARENA_BATTLE=search" in (ROOT / "scripts" / "run_agent.sh").read_text()


def test_the_runbook_has_no_placeholders_and_requires_the_calculator():
    doc = (ROOT / "docs" / "deploy.md").read_text()
    assert "REHEARSAL_TABLE" not in doc and "330 MB" not in doc
    assert not [x for x in doc.splitlines() if "docker run" in x and "--init" in x]
    assert "ARENA_ALLOW_NO_CALC" in doc and "--node-dist DIR`：连 Node" not in doc
