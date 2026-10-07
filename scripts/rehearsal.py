"""Unattended rehearsal: the real deployment stack against the LOCAL fake platform (never the real one), for hours,
with scheduled failures, sampling memory / open files / processes. Linux only (reads /proc).

    python scripts/rehearsal.py --minutes 120                        # scripts/run_agent.sh start (tmux / nohup path)
    python scripts/rehearsal.py --minutes 120 --supervise systemd    # the same loop as a transient systemd user unit
    python scripts/rehearsal.py --minutes 10 --no-chaos --concurrent 1

Stack: local/fake_platform.py --official (127.0.0.1, the FAKE Official Agent Key; assignments of both kinds, test
matches and tournament games; payloads shaped like the real platform's unless --plain) <- scripts/run_agent.sh run
-> python -m agent --tournament --match (supervisor) -> one worker process per seat -> node agent/js/calc.js.
The shorter scripted dry run (seat leases, a Werewolf table, a re-login outage, a hot standby):
scripts/rehearsal_official.py. Everything the rehearsal writes goes to --out
(default logs/rehearsal/<timestamp>/): the agent's log (LOG_DIR), platform.json (per-match results and answer
latencies), samples.jsonl (every --sample-every s), events.jsonl (failures injected) and report.json.

Failures injected at fixed fractions of the run (--no-chaos to skip): kill -9 of python -m agent (15%), of one
match worker (30%), of one node calculator (45%); a 90 s outage of the assignment listing only (55%) and a 45 s
outage of every platform request (70%); kill -9 of the run loop itself (85%): systemd restarts it, in the start mode
the rehearsal runs `run_agent.sh start` again after 10 s like an operator would.

Checks in report.json: no node calculator outside the agent's process group and never more calculators than
workers (leaks), zombies, memory / fd / thread growth of the long-lived supervisor, restarts, timer decisions and
invalid actions per match, the slowest answers (a recovery shows up as one slow answer at the failure's time), and
nothing left running after the final stop.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local.fake_platform import DEFAULT_OFFICIAL_KEY as KEY  # noqa: E402  (FAKE: accepted by the fake platform only)

NAME = "rehearsal"
UNIT = "arena-rehearsal"
_START = re.compile(r"\[match (\S+) seat=(\S+)\] starting \(pid=(\d+)\)")    # altruagent/worker.py's first line


def procs() -> list[dict]:
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as f:
                stat = f.read()
            rp = stat.rindex(")")
            comm = stat[stat.index("(") + 1:rp]
            fields = stat[rp + 2:].split()
            with open(f"/proc/{d}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
            rss = int(fields[21]) * os.sysconf("SC_PAGE_SIZE") // 1024
            out.append({"pid": int(d), "state": fields[0], "ppid": int(fields[1]), "pgid": int(fields[2]),
                        "comm": comm, "cmd": cmd, "rss_kb": rss, "threads": int(fields[17])})
        except (OSError, ValueError, IndexError):
            continue
    return out


def _proc_text(pid: int, what: str) -> str:
    """/proc/<pid>/cwd (link target) or /proc/<pid>/environ (text); '' when the process is gone or not ours."""
    try:
        if what == "cwd":
            return os.readlink(f"/proc/{pid}/cwd")
        with open(f"/proc/{pid}/{what}", "rb") as f:
            return f.read().replace(b"\0", b"\n").decode(errors="replace")
    except OSError:
        return ""


def nfds(pid: int) -> int:
    try:
        return len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        return -1


class Rehearsal:
    def __init__(self, a):
        self.a = a
        self.out = Path(a.out or ROOT / "logs" / "rehearsal" / time.strftime("%Y%m%d-%H%M%S")).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.env_file = self.out / f"{NAME}.env"
        self.state = self.out / f"agent-{NAME}.state"
        self.pidfile = self.out / f"agent-{NAME}.pid"
        self.events = open(self.out / "events.jsonl", "a")
        self.samples = open(self.out / "samples.jsonl", "a")
        self.t0 = time.time()
        self.platform = None
        self.rng = random.Random(a.seed)

    # ---- helpers ------------------------------------------------------------------------------------------------
    def env(self) -> dict:
        e = dict(os.environ, LOG_DIR=str(self.out), HEALTH_EVERY=str(self.a.health_every), PYTHON=sys.executable,
                 PYTHONPATH=str(ROOT), ARENA_RECORD="0")
        for kv in self.a.agent_env or []:
            k, _, v = kv.partition("=")
            e[k] = v
        return e

    def event(self, kind: str, **kw) -> None:
        row = {"t": round(time.time() - self.t0, 1), "clock": time.strftime("%H:%M:%S"), "event": kind, **kw}
        self.events.write(json.dumps(row) + "\n")
        self.events.flush()
        print(f"[rehearsal {row['clock']}] {kind} {kw}", flush=True)

    def child(self) -> int | None:
        try:
            for line in self.state.read_text().splitlines():
                if line.startswith("child="):
                    return int(line.split("=", 1)[1])
        except (OSError, ValueError):
            pass
        return None

    def mine(self, pid: int) -> bool:
        """A process of this rehearsal's agent stack (run_agent.sh exports LOG_DIR=<out> to the whole tree)."""
        return f"LOG_DIR={self.out}\n" in _proc_text(pid, "environ")

    def runloop_pid(self) -> int | None:
        try:
            return int(self.pidfile.read_text().strip())
        except (OSError, ValueError):
            return None

    def platform_totals(self) -> dict:
        try:
            return json.loads((self.out / "platform.json").read_text())["totals"]
        except (OSError, ValueError, KeyError):
            return {}

    # ---- lifecycle ----------------------------------------------------------------------------------------------
    def start_platform(self) -> None:
        self.env_file.write_text(f"ALTRUAGENT_CONTROL_URL=http://127.0.0.1:{self.a.port}\n"
                                 f"ALTRUAGENT_OFFICIAL_AGENT_KEY={KEY}\nARENA_RECORD=0\n")
        os.chmod(self.env_file, 0o600)
        cmd = ["nice", "-n", str(self.a.nice), sys.executable, "-m", "local.fake_platform", "--port", str(self.a.port),
               "--official", "--contexts", "tournament,testing",
               "--concurrent", str(self.a.concurrent), "--gap", str(self.a.gap), "--opponent", self.a.opponent,
               "--stats", str(self.out / "platform.json"), "--outage-file", str(self.out / "outage")]
        cmd += ["--plain"] if self.a.plain else []                  # (default: payloads shaped like the real platform's)
        penv = dict(os.environ, PYTHONPATH=str(ROOT), ARENA_RECORD="0", FAKE_OFFICIAL_KEY=KEY)   # (no LOG_DIR: not
                                                                                                  # part of mine())
        self.platform = subprocess.Popen(cmd, cwd=ROOT, env=penv, stdout=open(self.out / "platform.out", "w"),
                                         stderr=subprocess.STDOUT, start_new_session=True)
        time.sleep(3)
        self.event("platform_started", pid=self.platform.pid)

    def start_agent(self) -> None:
        run_agent = str(ROOT / "scripts" / "run_agent.sh")
        if self.a.supervise == "systemd":
            env = self.env()
            props = ["KillMode=mixed", "TimeoutStopSec=45", "Restart=always", "RestartSec=5",
                     "RestartPreventExitStatus=3", "OOMPolicy=continue", f"Nice={self.a.nice}"]   # (= the unit file)
            cmd = ["systemd-run", "--user", "--collect", f"--unit={UNIT}", f"--working-directory={ROOT}"]
            cmd += [f"--property={p}" for p in props]
            keys = ["LOG_DIR", "HEALTH_EVERY", "PYTHON", "PYTHONPATH", "ARENA_RECORD", "PATH"]
            keys += [kv.partition("=")[0] for kv in self.a.agent_env or []]
            for k in dict.fromkeys(keys):
                cmd.append(f"--setenv={k}={env[k]}")
            cmd += ["/bin/bash", run_agent, "run", "--env", str(self.env_file)]
            subprocess.run(cmd, check=True)
            for _ in range(75):
                time.sleep(0.2)
                if self.child():
                    break
        else:
            subprocess.run(["nice", "-n", str(self.a.nice), "bash", run_agent, "start", "--env", str(self.env_file)],
                           cwd=ROOT, env=self.env(), check=False)
        self.event("agent_started", runloop=self.runloop_pid(), child=self.child())

    def stop_agent(self) -> float:
        t = time.time()
        if self.a.supervise == "systemd":
            subprocess.run(["systemctl", "--user", "stop", UNIT], check=False)
        else:
            subprocess.run(["bash", str(ROOT / "scripts" / "run_agent.sh"), "stop", "--env", str(self.env_file)],
                           cwd=ROOT, env=self.env(), check=False)
        return time.time() - t

    # ---- sampling -----------------------------------------------------------------------------------------------
    def sample(self) -> dict:
        ps = procs()
        child = self.child()
        grp = [p for p in ps if child and p["pgid"] == child]
        main = next((p for p in grp if p["pid"] == child and p["state"] != "Z"), None)
        workers = [p for p in grp if "spawn_main" in p["cmd"] and "resource_tracker" not in p["cmd"]]
        calcs = [p for p in grp if p["comm"] == "node" and "calc.js" in p["cmd"]]
        stray = [p["pid"] for p in ps if p["comm"] == "node" and "calc.js" in p["cmd"]
                 and not (child and p["pgid"] == child) and self.mine(p["pid"])]
        stray_py = [p["pid"] for p in ps if "spawn_main" in p["cmd"] and not (child and p["pgid"] == child)
                    and self.mine(p["pid"])]
        runloop = self.runloop_pid()
        plat = next((p for p in ps if self.platform and p["pid"] == self.platform.pid), None)
        tot = self.platform_totals()
        s = {"t": round(time.time() - self.t0, 1), "clock": time.strftime("%H:%M:%S"),
             "agent_up": main is not None, "child": child, "runloop_up": bool(runloop and os.path.exists(f"/proc/{runloop}")),
             "main_rss_mb": round(main["rss_kb"] / 1024, 1) if main else None,
             "main_fds": nfds(child) if main else None, "main_threads": main["threads"] if main else None,
             "group_rss_mb": round(sum(p["rss_kb"] for p in grp) / 1024, 1), "group_procs": len(grp),
             "group_fds": sum(max(0, nfds(p["pid"])) for p in grp),
             "workers": len(workers), "calcs": len(calcs), "zombies": sum(1 for p in grp if p["state"] == "Z"),
             "stray_calcs": stray, "stray_workers": stray_py,
             "platform_rss_mb": round(plat["rss_kb"] / 1024, 1) if plat else None,
             "matches_done": tot.get("matches_done"), "timer_decisions": tot.get("timer_decisions"),
             "invalid_actions": tot.get("invalid_actions")}
        self.samples.write(json.dumps(s) + "\n")
        self.samples.flush()
        return s

    # ---- chaos ----------------------------------------------------------------------------------------------------
    def chaos(self, kind: str) -> None:
        ps = procs()
        child = self.child()
        grp = [p for p in ps if child and p["pgid"] == child]
        if kind in ("kill_main", "kill_worker") and not getattr(self, "_waited", False):
            self._waited = True                    # hit a match in its battle (the costly case), waiting up to 2 min
            for _ in range(240):
                if self._battle_worker():
                    break
                time.sleep(0.5)
                self.sample() if _ % 20 == 0 else None
            return self.chaos(kind)
        self._waited = False
        ps = procs()
        child = self.child()
        grp = [p for p in ps if child and p["pgid"] == child]
        before = self.platform_totals()
        t_inject = time.time()
        session = None
        self._old_child = child
        if kind == "kill_main":
            self.event(kind, pid=child)
            if child:
                os.kill(child, signal.SIGKILL)
        elif kind in ("kill_worker", "kill_calc"):
            pick = [p for p in grp if ("spawn_main" in p["cmd"] and "resource_tracker" not in p["cmd"])] \
                if kind == "kill_worker" else [p for p in grp if p["comm"] == "node" and "calc.js" in p["cmd"]]
            if not pick:                                   # between matches: wait for one (up to 3 min)
                for _ in range(360):
                    time.sleep(0.5)
                    if self._has(kind):
                        return self.chaos(kind)
                self.event(kind + "_skipped", reason="no target process for 3 minutes")
                return None
            battle = self._battle_worker() if kind == "kill_worker" else None
            victim = next((p for p in pick if p["pid"] == battle), None) or self.rng.choice(pick)
            wpid = victim["pid"] if kind == "kill_worker" else victim["ppid"]
            seat = next((s for _, s, p in self._starts() if p == wpid), None)
            session = self._seat_sessions().get(seat)
            self.event(kind, pid=victim["pid"], worker=wpid, seat=seat, session=session)
            os.kill(victim["pid"], signal.SIGKILL)
            if kind == "kill_calc":
                session = None                          # the worker restarts its calculator itself: no new worker
            else:
                session = (session, wpid, seat) if session else None
        elif kind in ("outage_assignments", "outage_all"):
            secs = 90 if kind == "outage_assignments" else 45
            self.event(kind, seconds=secs)
            (self.out / "outage").write_text(kind.split("_")[1])
            end = time.time() + secs
            while time.time() < end:
                time.sleep(5)
                self.sample()
            (self.out / "outage").unlink()
            self.event(kind + "_over")
        elif kind == "kill_runloop":
            rl = self.runloop_pid()
            self.event(kind, pid=rl)
            if rl:
                os.kill(rl, signal.SIGKILL)
            if self.a.supervise != "systemd":
                time.sleep(10)
                self.event("operator_restart")
                self.start_agent()
        self.pending_check = (kind, t_inject, before, session)

    def log_lines(self) -> list[str]:
        try:
            return (self.out / f"agent-{NAME}.out").read_text(errors="replace").splitlines()
        except OSError:
            return []

    def worst_answer_since(self, since: float) -> list:
        """The slowest answers the platform saw after `since` (latency s, clock, phase): a failure's cost."""
        try:
            matches = json.loads((self.out / "platform.json").read_text())["matches"]
        except (OSError, ValueError, KeyError):
            return []
        day = time.strftime("%Y-%m-%d ", time.localtime(since))
        out = []
        for m in matches:
            for lat, clock, phase in m.get("slow") or []:
                t = time.mktime(time.strptime(day + clock, "%Y-%m-%d %H:%M:%S"))
                if t >= since - 1:
                    out.append((lat, clock, phase))
        return sorted(out, reverse=True)[:3]

    def _starts(self) -> list[tuple[str, str, int]]:
        """(clock, seat_id, pid) of every worker start in the log: run_agent.sh's timestamp, then the worker's own
        first line (altruagent/worker.py)."""
        out = []
        for line in self.log_lines():
            m = _START.search(line)
            if m:
                out.append((line[:19], m.group(2), int(m.group(3))))
        return out

    def _seat_sessions(self) -> dict:
        """seat_id -> the platform session of its match (the fake platform's official stats)."""
        try:
            seats = json.loads((self.out / "platform.json").read_text())["official"]["seats"]
            return {sid: s.get("match") for sid, s in seats.items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return {}

    def _battle_worker(self) -> int | None:
        """pid of a live worker whose match is in its battle phase (platform phase 'moving'), if any."""
        battle = {r["session_id"] for r in self._matches() if not r.get("terminal") and r.get("phase") == "moving"}
        if not battle:
            return None
        child = self.child()
        alive = {p["pid"] for p in procs() if child and p["pgid"] == child and "spawn_main" in p["cmd"]}
        sessions = self._seat_sessions()
        for _, seat, pid in reversed(self._starts()):
            if sessions.get(seat) in battle and pid in alive:
                return pid
        return None

    def _matches(self) -> list:
        try:
            return json.loads((self.out / "platform.json").read_text())["matches"]
        except (OSError, ValueError, KeyError):
            return []

    def _has(self, kind: str) -> bool:
        child = self.child()
        for p in procs():
            if child and p["pgid"] == child:
                if kind == "kill_worker" and "spawn_main" in p["cmd"] and "resource_tracker" not in p["cmd"]:
                    return True
                if kind == "kill_calc" and p["comm"] == "node" and "calc.js" in p["cmd"]:
                    return True
        return False

    def recovery(self, kind: str, since: float, before: dict, victim: tuple | None = None) -> None:
        """Wait (max 3 min) until the agent is up again with a worker (for a killed worker: a new worker for its
        match, or the match's end); then 60 s more, and log the times and what the failure cost."""
        t_up = t_session = None
        session, vpid, vseat = victim or (None, None, None)
        old = self._old_child if kind in ("kill_main", "kill_runloop") else None
        for _ in range(180):
            s = self.sample()
            if t_up is None and s["agent_up"] and s["runloop_up"] and s["workers"] >= 1 and s["child"] != old:
                t_up = round(time.time() - since, 1)
            if session and t_session is None:
                after = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since))
                if any(seat == vseat and pid != vpid and clock >= after for clock, seat, pid in self._starts()) or \
                        any(r.get("session_id") == session and r.get("terminal") for r in self._matches()):
                    t_session = round(time.time() - since, 1)
            if t_up is not None and (not session or t_session is not None):
                break
            time.sleep(1)
        time.sleep(60)
        after = self.platform_totals()
        self.event(kind + "_recovered", seconds_to_worker=t_up, seconds_to_new_worker_for_its_match=t_session,
                   worst_answers_after=self.worst_answer_since(since),
                   timer_decisions_delta=(after.get("timer_decisions") or 0) - (before.get("timer_decisions") or 0),
                   forfeits_delta=(after.get("forfeits") or 0) - (before.get("forfeits") or 0),
                   invalid_delta=(after.get("invalid_actions") or 0) - (before.get("invalid_actions") or 0))

    # ---- main -----------------------------------------------------------------------------------------------------
    def run(self) -> dict:
        self.start_platform()
        self.start_agent()
        total = self.a.minutes * 60
        plan = [] if self.a.no_chaos else [(0.15, "kill_main"), (0.30, "kill_worker"), (0.45, "kill_calc"),
                                           (0.55, "outage_assignments"), (0.70, "outage_all"), (0.85, "kill_runloop")]
        last_done, last_change, stalled = None, time.time(), False
        try:
            while time.time() - self.t0 < total:
                done = self.platform_totals().get("matches_done")
                if done != last_done:
                    last_done, last_change, stalled = done, time.time(), False
                elif not stalled and time.time() - last_change > 600:
                    stalled = True                     # no match finished for 10 min: the load stopped
                    self.event("stall", matches_done=done)
                el = time.time() - self.t0
                if plan and el >= plan[0][0] * total:
                    _, kind = plan.pop(0)
                    self.pending_check = None
                    self.chaos(kind)
                    if self.pending_check:
                        self.recovery(*self.pending_check)
                    continue
                self.sample()
                time.sleep(self.a.sample_every)
        finally:
            final = self.sample()
            stop_s = self.stop_agent()
            time.sleep(3)
            # everything of the agent stack counts: python / node, and the run loop's bash helpers (health loop, log
            # writer) and their sleeps
            left = [p for p in procs() if p["state"] != "Z" and "fake_platform" not in p["cmd"] and self.mine(p["pid"])]
            self.event("agent_stopped", seconds=round(stop_s, 1), leftovers=[(p["pid"], p["cmd"][:80]) for p in left])
            if self.platform:
                self.platform.send_signal(signal.SIGTERM)
                try:
                    self.platform.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    self.platform.kill()
        return self.report(final, stop_s, left)

    def report(self, final: dict, stop_s: float, left: list) -> dict:
        rows = [json.loads(x) for x in (self.out / "samples.jsonl").read_text().splitlines() if x.strip()]
        events = [json.loads(x) for x in (self.out / "events.jsonl").read_text().splitlines() if x.strip()]
        try:
            plat = json.loads((self.out / "platform.json").read_text())
        except (OSError, ValueError):
            plat = {"totals": {}, "matches": []}
        # supervisor growth: per agent incarnation (child pid), RSS / fds / threads first -> last sample
        segs: dict = {}
        for r in rows:
            if r["agent_up"]:
                segs.setdefault(r["child"], []).append(r)
        growth = []
        for pid, rs in segs.items():
            if len(rs) >= 2:
                hours = max(1e-9, (rs[-1]["t"] - rs[0]["t"]) / 3600)
                growth.append({"child": pid, "minutes": round(hours * 60, 1),
                               "rss_mb": [rs[0]["main_rss_mb"], max(r["main_rss_mb"] for r in rs), rs[-1]["main_rss_mb"]],
                               "fds": [rs[0]["main_fds"], max(r["main_fds"] for r in rs), rs[-1]["main_fds"]],
                               "threads": [rs[0]["main_threads"], max(r["main_threads"] for r in rs), rs[-1]["main_threads"]]})
        matches = plat.get("matches") or []
        longest, since, prev = 0.0, rows[0]["t"] if rows else 0.0, None
        for r in rows:                          # the longest stretch without a finished match (load kept going?)
            if r["matches_done"] != prev:
                prev, since = r["matches_done"], r["t"]
            longest = max(longest, r["t"] - since)
        restarts = None
        try:
            for line in (self.out / f"agent-{NAME}.state").read_text().splitlines():
                if line.startswith("restarts="):
                    restarts = int(line.split("=")[1])
        except (OSError, ValueError):
            pass
        log = self.out / f"agent-{NAME}.out"
        logtext = "".join(p.read_text(errors="replace") for p in sorted(self.out.glob(f"agent-{NAME}.out*")))
        rep = {
            "out": str(self.out), "minutes": round((time.time() - self.t0) / 60, 1), "supervise": self.a.supervise,
            "concurrent": self.a.concurrent, "platform": plat.get("totals"),
            "matches_with_timer_decisions": sum(1 for m in matches if sum(m["timer"].values())),
            "longest_minutes_without_a_finished_match": round(longest / 60, 1),
            "max_group_rss_mb": max((r["group_rss_mb"] for r in rows), default=None),
            "max_workers": max((r["workers"] for r in rows), default=None),
            "max_calcs": max((r["calcs"] for r in rows), default=None),
            "max_calcs_minus_workers": max((r["calcs"] - r["workers"] for r in rows), default=None),
            "samples_with_stray_calcs": sum(1 for r in rows if r["stray_calcs"]),
            "samples_with_stray_workers": sum(1 for r in rows if r["stray_workers"]),
            "max_zombies": max((r["zombies"] for r in rows), default=None),
            "supervisor_growth": growth,
            "platform_rss_mb": [rows[0]["platform_rss_mb"] if rows else None, rows[-1]["platform_rss_mb"] if rows else None],
            "events": [e for e in events if e["event"].endswith(("_recovered", "_skipped"))
                       or e["event"] in ("agent_stopped", "stall")],
            "stop_seconds": round(stop_s, 1), "leftovers_after_stop": len(left),
            "log_lines": logtext.count("\n"), "log_files": len(list(self.out.glob(f"agent-{NAME}.out*"))),
            "health_lines": logtext.count("[health]"), "tracebacks": logtext.count("Traceback"),
            # restarts of `python -m agent` by the loop over the whole run (every loop incarnation), each with its
            # time: compare with the injected failures in "events"; restarts_last_loop only covers the last loop
            "agent_restarts": [line[:19] for line in logtext.splitlines() if "[run_agent] agent exited with code" in line],
            "unexpected_errors": logtext.count("unexpected error"),
            "restarts_last_loop": restarts, "log_bytes": log.stat().st_size if log.exists() else 0,
        }
        (self.out / "report.json").write_text(json.dumps(rep, indent=1))
        return rep


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--minutes", type=float, default=120.0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--supervise", choices=("start", "systemd"), default="start")
    ap.add_argument("--concurrent", type=int, default=2)
    ap.add_argument("--gap", type=float, default=20.0, help="idle seconds between matches (default 20)")
    ap.add_argument("--opponent", default="mixed", choices=("random", "baseline", "mixed"))
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--nice", type=int, default=10)
    ap.add_argument("--sample-every", type=float, default=30.0)
    ap.add_argument("--health-every", type=int, default=300)
    ap.add_argument("--no-chaos", action="store_true")
    ap.add_argument("--plain", action="store_true",
                    help="the fake platform's old local payloads instead of the real platform's shapes (six-entry "
                         "team, protocol_log with the timer line, blind states)")
    ap.add_argument("--agent-env", action="append", metavar="KEY=VALUE",
                    help="extra environment for the agent only, e.g. ARENA_CALC=off plus ARENA_ALLOW_NO_CALC=1 (the "
                         "calculator off; the engine search still runs), or ARENA_BATTLE=classic")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args(argv)
    if not os.path.isdir("/proc"):
        sys.exit("rehearsal.py reads /proc: run it on Linux")
    if a.supervise == "systemd" and not shutil.which("systemd-run"):
        sys.exit("systemd-run not found (use --supervise start)")
    rep = Rehearsal(a).run()
    print(json.dumps(rep, indent=1))


if __name__ == "__main__":
    main()
