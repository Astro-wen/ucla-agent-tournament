"""Dry run of the event runtime (`python -m agent --tournament --match`, the Official Agent Key) end to end, against
the LOCAL fake platform only (never the real one).

    python scripts/rehearsal_official.py --bundle /tmp/x/arena-agent-VERSION --out /tmp/official-dry-run
    python scripts/rehearsal_official.py --out /tmp/official-dry-run            # this checkout instead of a bundle

The platform side runs from this checkout: local/fake_platform.py --official on 127.0.0.1 with one FAKE Official Agent
Key (never a real one), VGC doubles draft matches one after another (payloads shaped like the real platform's, blind
states included) and, at the same time, 7-seat Werewolf tables (our seat + six local bots taking ~3 s per move, a new
table when one ends): two assignments at once all along. The assignments alternate between the event platform's two
kinds (`context`: "tournament", "testing"), and each carries its connect window (connect_deadline_at, --connect-window
s; a seat not connected by then is a no-show, a loss). The agent side is the bundle's own scripts/run_agent.sh (its
.venv, its node) in a clean environment, with a key file that holds ONLY ALTRUAGENT_OFFICIAL_AGENT_KEY.

Timeline (each step waits for the previous one, --minutes caps the whole run):
  1. start: both assignments get a worker (`python -m agent --tournament --match`, one worker per seat)
  2. kill -9 of the VGC worker in its battle: a new worker resumes the seat (same runtime, same execution id)
  3. kill -9 of the runtime itself (`python -m agent`) in a VGC battle: the run loop starts a new runtime with the
     loop's execution id, so its workers get their seats back at once (no seat_busy, no takeover, no timer move)
  4. `auth503` for --outage-s: authenticate answers 503 and every agent session is expired, so every re-login fails
     (the upstream runtime used to end there, with every match; since upstream 1372ccc its supervisor waits it out):
     the runtime and its workers must survive
  5. a hot standby: a second run_agent.sh loop with the same key (another NAME and LOG_DIR): its seat checks get
     seat_busy, it starts no worker and makes no move while the primary plays, and every seat assigned while both run
     goes to the primary (the standby leaves new seats alone for 20 s)
  6. the primary stops: the standby takes the seats over as their leases lapse (~30-40 s)
  7. the standby plays --after-takeover more whole VGC matches, then the platform starts no new match (its drain file)
     and the live ones end (or time runs out); everything stops; nothing may be left running
New matches keep coming until step 7 (--vgc-matches / --werewolf-tables are only caps), so every step finds a match
to act on however fast the matches end.
OUT/report.json: the platform's totals (decisions, timer decisions, invalid actions, blind states), its official
counters (grants, seat_busy, takeovers, moves_by_non_holder: must be 0, no_shows: must be 0), both kinds of game
played, every seat's connect time against its window, each step's timings and checks, the processes' peak memory, and
a scan of every log for the key. Linux only (reads /proc).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local.fake_platform import DEFAULT_OFFICIAL_KEY  # noqa: E402  (the FAKE key the fake platform accepts)

VGC = "pokemon_vgc_doubles_draft"
STATS_EVERY_S = 2.0         # the platform's stats file (what every step reads) is at most this old
_START = re.compile(r"\[match (\S+) seat=(\S+)\] starting \(pid=(\d+)\)")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _environ(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            return f.read().replace(b"\0", b"\n").decode(errors="replace")
    except OSError:
        return ""


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def _rss_kb(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") // 1024
    except (OSError, ValueError, IndexError):
        return 0


class DryRun:
    def __init__(self, a):
        self.a = a
        self.out = Path(a.out).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.bundle = Path(a.bundle).resolve() if a.bundle else ROOT
        self.port = a.port or _free_port()
        self.t0 = time.time()
        self.events: list[dict] = []
        self.checks: dict[str, bool] = {}
        self.platform = None
        self.peak = {"tree_mb": 0.0, "procs": 0}
        self.seats_at_standby: set = set()
        self.key = DEFAULT_OFFICIAL_KEY                           # FAKE: accepted by the local fake platform only

    # ---- helpers ------------------------------------------------------------------------------------------------------
    def event(self, kind: str, **kw) -> None:
        row = {"t": round(time.time() - self.t0, 1), "clock": time.strftime("%H:%M:%S"), "event": kind, **kw}
        self.events.append(row)
        print(f"[dry-run {row['clock']} +{row['t']}s] {kind} {json.dumps(kw)}", flush=True)

    def check(self, name: str, ok: bool, **kw) -> bool:
        self.checks[name] = bool(ok)
        self.event("check", name=name, ok=bool(ok), **kw)
        return ok

    def stats(self) -> dict:
        try:
            return json.loads((self.out / "platform.json").read_text())
        except (OSError, ValueError):
            return {}

    def vgc(self) -> dict:
        """The current (latest) VGC match."""
        return next((m for m in reversed(self.stats().get("matches") or []) if m.get("game_type") == VGC), {})

    def match(self, session_id: str | None) -> dict:
        return next((m for m in self.stats().get("matches") or [] if m.get("session_id") == session_id), {})

    def in_battle(self) -> bool:
        """The current VGC match is in its battle, two battle decisions in (a kill there costs turn-clock time)."""
        m = self.vgc()
        return (m.get("phase") == "moving" and not m.get("terminal")
                and sum(1 for p, *_ in m.get("lat") or [] if p == "moving") >= 2)

    def settled_decisions(self, session_id: str | None) -> int:
        """Right after a kill: the match's decisions once the stats file has caught up (it is up to STATS_EVERY_S old,
        and a move the killed process had in flight may still land), so the next one is the replacement's."""
        time.sleep(STATS_EVERY_S + 0.5)
        return self.match(session_id).get("decisions", 0)

    def vgc_done(self) -> int:
        return sum(1 for m in self.stats().get("matches") or [] if m.get("game_type") == VGC and m.get("terminal"))

    def log(self, name: str) -> str:
        files = sorted((self.out / name).glob(f"agent-{name}.out*"),
                       key=lambda p: -int(p.suffix[1:]) if p.suffix[1:].isdigit() else 0)
        return "".join(p.read_text(errors="replace") for p in files if p.exists())

    def workers(self, name: str) -> list[tuple[str, str, int]]:
        """(game, seat_id, pid) for every worker the runtime of `name` has started, in order."""
        return [(m.group(1), m.group(2), int(m.group(3))) for m in _START.finditer(self.log(name))]

    def state(self, name: str) -> dict:
        try:
            text = (self.out / name / f"agent-{name}.state").read_text()
            return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        except OSError:
            return {}

    def mine(self) -> list[int]:
        """Live processes of this dry run's agent side (run_agent.sh exports LOG_DIR=<out>/<name> to the tree)."""
        pids = []
        for d in os.listdir("/proc"):
            if d.isdigit() and int(d) != os.getpid():
                env = _environ(int(d))
                if f"LOG_DIR={self.out}/" in env:
                    try:
                        with open(f"/proc/{d}/stat") as f:
                            if f.read().rsplit(")", 1)[1].split()[0] != "Z":
                                pids.append(int(d))
                    except (OSError, IndexError):
                        pass
        return pids

    def sample(self) -> None:
        pids = self.mine()
        mb = sum(_rss_kb(p) for p in pids) / 1024
        self.peak["tree_mb"] = round(max(self.peak["tree_mb"], mb), 1)
        self.peak["procs"] = max(self.peak["procs"], len(pids))

    def wait(self, pred, timeout: float, step: float = 2.0):
        end = time.time() + timeout
        while time.time() < end and time.time() - self.t0 < self.a.minutes * 60:
            v = pred()
            if v:
                return v
            self.sample()
            time.sleep(step)
        return None

    # ---- the two sides ------------------------------------------------------------------------------------------------
    def start_platform(self) -> None:
        cmd = ["nice", "-n", str(self.a.nice), sys.executable, "-m", "local.fake_platform", "--port", str(self.port),
               "--official", "--concurrent", "1", "--max-matches", str(self.a.vgc_matches), "--gap", "3",
               "--blind-every", str(self.a.blind_every), "--werewolf", "1",
               "--max-werewolf", str(self.a.werewolf_tables), "--ww-think", str(self.a.ww_think),
               "--session-ttl", str(self.a.session_ttl), "--token-ttl", str(self.a.token_ttl), "--lease-s", "30",
               "--opponent", "baseline", "--seed", str(self.a.seed), "--stats", str(self.out / "platform.json"),
               "--stats-every", str(STATS_EVERY_S), "--outage-file", str(self.out / "outage"),
               "--drain-file", str(self.out / "drain"), "--contexts", "tournament,testing",
               "--connect-window", str(self.a.connect_window)]
        env = dict(os.environ, PYTHONPATH=str(ROOT), ARENA_RECORD="0", FAKE_OFFICIAL_KEY=self.key)
        self.platform = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=open(self.out / "platform.out", "w"),
                                         stderr=subprocess.STDOUT, start_new_session=True)
        time.sleep(3)
        if self.platform.poll() is not None:
            raise SystemExit(f"fake platform did not start: {(self.out / 'platform.out').read_text()[-400:]}")
        self.event("platform_started", port=self.port)

    def key_file(self, name: str) -> Path:
        f = self.out / f"{name}.env"
        f.write_text(f"ALTRUAGENT_OFFICIAL_AGENT_KEY={self.key}\nALTRUAGENT_CONTROL_URL=http://127.0.0.1:{self.port}\n"
                     "ARENA_RECORD=0\n")
        f.chmod(0o600)
        return f

    def agent_env(self, name: str) -> dict:
        home = self.out / "home"
        home.mkdir(exist_ok=True)
        py = self.bundle / ".venv" / "bin" / "python"
        env = {"PATH": self.a.path or os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "HOME": str(home),
               "LOG_DIR": str(self.out / name), "PYTHON": str(py if py.exists() else sys.executable),
               "ARENA_RECORD": "0", "ARENA_LOCK_DIR": str(self.out), "HEALTH_EVERY": "30"}
        if self.bundle == ROOT:
            env["PYTHONPATH"] = str(ROOT)
        return env

    def run_agent(self, name: str, cmd: str) -> subprocess.CompletedProcess:
        return subprocess.run(["nice", "-n", str(self.a.nice), "bash", str(self.bundle / "scripts" / "run_agent.sh"),
                               cmd, "--env", str(self.key_file(name))], cwd="/", env=self.agent_env(name),
                              capture_output=True, text=True, timeout=300)

    # ---- the steps -----------------------------------------------------------------------------------------------------
    def step_start(self) -> None:
        r = self.run_agent("primary", "start")
        self.event("primary_started", code=r.returncode, status=r.stdout.strip().splitlines()[-2:] if r.stdout else None)
        self.check("primary_is_tournament_mode", "mode=tournament(primary)" in r.stdout)
        seats = self.wait(lambda: len({s for _, s, _ in self.workers("primary")}) >= 2 and self.workers("primary"),
                          timeout=240)
        self.check("both_assignments_get_a_worker", bool(seats), workers=[(g, s) for g, s, _ in seats or []])
        log = self.log("primary")
        self.check("runtime_plays_test_matches_and_tournament_games",
                   "mode tournament (python -m agent --tournament --match" in log
                   and "Playing your test matches and tournament games." in log)

    def step_kill_worker(self) -> None:
        for attempt in range(1, 4):                                  # (a match the killed worker had just won: again)
            if not self.wait(self.in_battle, timeout=600):
                self.check("vgc_reached_its_battle", False)
                return
            vgc = [(s, p) for g, s, p in self.workers("primary") if g == VGC]
            seat, pid = vgc[-1]
            before = self.vgc()
            sid, timer0 = before.get("session_id"), sum((before.get("timer") or {}).values())
            t = time.time()
            os.kill(pid, signal.SIGKILL)
            n0 = self.settled_decisions(sid)
            self.event("kill_vgc_worker", pid=pid, seat=seat, decisions_before=n0, attempt=attempt)
            if not self.match(sid).get("terminal"):
                break
            self.event("match_ended_at_the_kill", attempt=attempt)
        new = self.wait(lambda: [p for g, s, p in self.workers("primary") if s == seat and p != pid], timeout=180, step=1)
        t_new = round(time.time() - t, 1) if new else None
        resumed = self.wait(lambda: self.match(sid).get("decisions", 0) > n0 or self.match(sid).get("terminal"),
                            timeout=180, step=1)
        t_resume = round(time.time() - t, 1) if resumed else None
        after = self.match(sid)
        self.event("vgc_worker_recovered", new_worker_s=t_new, next_decision_s=t_resume,
                   timer_decisions_delta=sum((after.get("timer") or {}).values()) - timer0,
                   runtime_restarts=self.log("primary").count("[run_agent] agent exited"))
        self.check("killed_worker_is_replaced_and_the_match_resumes", bool(new and resumed),
                   new_worker_s=t_new, next_decision_s=t_resume)

    def step_kill_runtime(self) -> None:
        # The runtime crashes (OOM, a bug) mid-battle: the run loop kills what is left of its group and starts a new
        # runtime with the loop's own execution id, so the new workers resume their seats' leases at once. With a fresh
        # id they would get seat_busy until the old lease lapses (30 s) and then wait out the 35 s retry.
        for attempt in range(1, 4):                                  # (a match the old workers had just won: again)
            if not self.wait(self.in_battle, timeout=600):
                self.check("vgc_reached_its_battle_again", False)
                return
            off0 = self.stats().get("official") or {}
            grants0 = {sid: list(s["grants"]) for sid, s in (off0.get("seats") or {}).items()}
            m0 = self.vgc()
            sid, timer0 = m0.get("session_id"), sum((m0.get("timer") or {}).values())
            child = int(self.state("primary").get("child") or 0)
            log0 = len(self.log("primary"))
            if not child:
                self.check("killed_runtime_resumes_its_seats_at_once", False, why="no runtime pid in the state file")
                return
            t = time.time()
            os.kill(child, signal.SIGKILL)
            # the old runtime's workers live on until the run loop kills its group (it then waits RESTART_DELAY=3 s):
            # count the match's moves once they are gone, so the next one is the new runtime's
            self.wait(lambda: "agent exited with code" in self.log("primary")[log0:], timeout=30, step=0.2)
            n0 = self.settled_decisions(sid)
            self.event("kill_runtime", pid=child, decisions_before=n0, attempt=attempt)
            if not self.match(sid).get("terminal"):
                break
            self.event("match_ended_at_the_kill", attempt=attempt)
        new = self.wait(lambda: (self.state("primary").get("child") or str(child)) != str(child), timeout=120, step=0.5)
        t_new = round(time.time() - t, 1) if new else None
        resumed = self.wait(lambda: self.match(sid).get("decisions", 0) > n0 or self.match(sid).get("terminal"),
                            timeout=180, step=1)
        t_resume = round(time.time() - t, 1) if resumed else None
        after = self.match(sid)
        off1 = self.stats().get("official") or {}
        seats1 = off1.get("seats") or {}
        tail = self.log("primary")[log0:]
        row = {"new_runtime_s": t_new, "next_decision_s": t_resume,
               "timer_decisions_delta": sum((after.get("timer") or {}).values()) - timer0,
               "seat_busy_delta": off1.get("seat_busy", 0) - off0.get("seat_busy", 0),
               "new_execution_grants": sum(len((seats1.get(k) or {}).get("grants") or []) - len(g)
                                           for k, g in grants0.items()),
               "seat_busy_lines": tail.count("Another runtime is playing this match"),
               "ignored_execution_id": "Ignoring ARENA_EXECUTION_ID" in tail}
        self.event("runtime_restarted", **row)
        self.check("killed_runtime_resumes_its_seats_at_once",
                   bool(new and resumed) and row["timer_decisions_delta"] == 0 and row["seat_busy_delta"] == 0
                   and row["new_execution_grants"] == 0 and not row["seat_busy_lines"]
                   and not row["ignored_execution_id"], next_decision_s=t_resume)

    def step_auth_outage(self) -> None:
        time.sleep(15)
        child0 = self.state("primary").get("child")
        n_workers0 = len(self.workers("primary"))
        dec0 = (self.stats().get("totals") or {}).get("decisions", 0)
        inj0 = (self.stats().get("official") or {}).get("authenticate_injected", 0)
        (self.out / "outage").write_text("auth503")
        self.event("auth503_on", seconds=self.a.outage_s)
        end = time.time() + self.a.outage_s
        while time.time() < end:
            self.sample()
            time.sleep(2)
        (self.out / "outage").unlink()
        self.event("auth503_off")
        time.sleep(25)                                               # a poll + a lease renewal after it
        st = self.stats()
        log = self.log("primary")
        injected = (st.get("official") or {}).get("authenticate_injected", 0) - inj0
        same_runtime = self.state("primary").get("child") == child0
        new_workers = len(self.workers("primary")) - n_workers0
        self.event("auth503_result", runtime_pid_unchanged=same_runtime, authenticate_503s=injected,
                   workers_started_since=new_workers, decisions_since=(st.get("totals") or {}).get("decisions", 0) - dec0,
                   renew_failed_lines=log.count("Could not renew your agent session"),
                   discovery_recovered=log.count("Assignment discovery recovered."),
                   stopped_all_workers=log.count("active match worker(s)"))
        self.check("reauthentication_503_keeps_the_runtime_and_its_matches",
                   injected > 0 and same_runtime and "active match worker(s)" not in log
                   and "Could not renew your agent session" in log)

    def step_standby(self) -> None:
        self.seats_at_standby = set((self.stats().get("official") or {}).get("seats") or {})
        r = self.run_agent("standby", "start")
        self.event("standby_started", code=r.returncode)
        self.check("second_runtime_starts_as_hot_standby", r.returncode == 0 and "mode=tournament(standby)" in r.stdout
                   and "HOT STANDBY" in self.log("standby"))
        busy = self.wait(lambda: "is held by another runtime (the primary)" in self.log("standby"), timeout=120)
        off = self.stats().get("official") or {}
        self.check("standby_waits_on_the_lease", bool(busy) and off.get("moves_by_non_holder", 1) == 0,
                   seat_busy=off.get("seat_busy"), moves_by_non_holder=off.get("moves_by_non_holder"))

    def new_seats(self) -> list[str]:
        """Seats assigned (and granted) after the standby started."""
        seats = (self.stats().get("official") or {}).get("seats") or {}
        return [sid for sid, s in seats.items() if sid not in self.seats_at_standby and s["grants"]]

    def step_takeover(self) -> None:
        # a seat assigned while both run (so the check below has one to look at), then stop the primary while a VGC
        # match is in its battle (the costly case: its 55 s turn clock runs on)
        self.wait(self.new_seats, timeout=240, step=1)
        self.wait(lambda: self.vgc().get("phase") == "moving" and not self.vgc().get("terminal")
                  and any(p == "moving" for p, *_ in self.vgc().get("lat") or []), timeout=180, step=0.5)
        seats0 = (self.stats().get("official") or {}).get("seats") or {}
        before = {sid: len(s["grants"]) for sid, s in seats0.items()}
        # while both run, every new seat is the primary's (the standby leaves new seats alone for ARENA_NEW_SEAT_DELAY_S
        # and then plays only a seat whose grant it gets), and the standby starts no worker at all
        primary = next((s["grants"][0]["execution"] for s in seats0.values() if s["grants"]), None)
        new = [sid for sid, s in seats0.items() if sid not in self.seats_at_standby and s["grants"]]
        taken_new = [sid for sid in new if seats0[sid]["grants"][0]["execution"] != primary]
        self.check("standby_leaves_new_seats_to_the_primary", bool(new) and not taken_new, new_seats=len(new),
                   claimed_by_the_standby=taken_new)
        self.check("standby_starts_no_worker_while_the_primary_plays", not self.workers("standby"),
                   standby_workers=len(self.workers("standby")))
        live = {m["session_id"] for m in self.stats().get("matches") or [] if not m.get("terminal")}
        vgc0 = self.vgc()
        timer0 = sum((vgc0.get("timer") or {}).values())
        t = time.time()
        r = self.run_agent("primary", "stop")
        self.event("primary_stopped", code=r.returncode, seconds=round(time.time() - t, 1), live_matches=len(live),
                   vgc_in_battle=vgc0.get("phase") == "moving" and not vgc0.get("terminal"))

        def taken():
            seats = (self.stats().get("official") or {}).get("seats") or {}
            open_seats = [sid for sid, s in seats.items() if s["match"] in live
                          and not next((m for m in self.stats()["matches"] if m["session_id"] == s["match"]), {}).get("terminal")]
            return open_seats and all(len(seats[sid]["grants"]) > before.get(sid, 0) for sid in open_seats) and open_seats
        got = self.wait(taken, timeout=180, step=1)
        self.event("standby_took_over", seconds=round(time.time() - t, 1) if got else None, seats=got)
        dec0 = (self.stats().get("totals") or {}).get("decisions", 0)
        moved = self.wait(lambda: (self.stats().get("totals") or {}).get("decisions", 0) > dec0, timeout=120, step=1)
        same = next((m for m in self.stats().get("matches") or [] if m["session_id"] == vgc0.get("session_id")), {})
        timer_delta = sum((same.get("timer") or {}).values()) - timer0
        self.event("vgc_match_across_the_takeover", decisions=[vgc0.get("decisions"), same.get("decisions")],
                   timer_decisions_delta=timer_delta, terminal=same.get("terminal"), slowest=same.get("slow"))
        self.check("standby_takes_the_seats_over_and_plays", bool(got and moved),
                   takeover_s=round(time.time() - t, 1))
        self.check("the_takeover_costs_no_turn", bool(got) and timer_delta == 0,
                   timer_decisions_delta=timer_delta)

    def step_finish(self) -> None:
        # the standby plays on: every VGC match live now plus --after-takeover more whole ones; then no new match starts
        # (the drain file) and the live ones (Werewolf tables too) end
        started = sum(1 for m in self.stats().get("matches") or [] if m.get("game_type") == VGC)
        self.wait(lambda: self.vgc_done() >= started + self.a.after_takeover, timeout=self.a.minutes * 60)
        (self.out / "drain").write_text("drain")
        time.sleep(STATS_EVERY_S + 1)                                # (a match started just before shows up first)
        self.wait(lambda: all(m.get("terminal") for m in self.stats().get("matches") or []),
                  timeout=self.a.minutes * 60)
        vgc = [m for m in self.stats().get("matches") or [] if m.get("game_type") == VGC]
        self.event("vgc_matches", done=self.vgc_done(), of=len(vgc),
                   rows=[{k: m.get(k) for k in ("terminal", "win", "turns", "decisions", "timer", "invalid_actions",
                                                "blind", "reason", "secs")} for m in vgc])

    # ---- main ---------------------------------------------------------------------------------------------------------
    def run(self) -> dict:
        self.start_platform()
        try:
            for step in (self.step_start, self.step_kill_worker, self.step_kill_runtime, self.step_auth_outage,
                         self.step_standby, self.step_takeover, self.step_finish):
                if time.time() - self.t0 > self.a.minutes * 60:
                    self.event("out_of_time", before=step.__name__)
                    break
                step()
        finally:
            for name in ("standby", "primary"):
                if (self.out / name).exists():
                    self.run_agent(name, "stop")
            time.sleep(3)
            left = self.mine()
            self.event("stopped", leftovers=[(p, _cmdline(p)[:80]) for p in left])
            self.check("nothing_left_running", not left)
            if self.platform:
                self.platform.send_signal(signal.SIGTERM)
                try:
                    self.platform.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    self.platform.kill()
        return self.report()

    def report(self) -> dict:
        st = self.stats()
        logs = {n: self.log(n) for n in ("primary", "standby")}
        everything = "".join(logs.values()) + "".join(p.read_text(errors="replace") for p in self.out.glob("*/agent-*.state"))
        self.check("key_never_logged", self.key not in everything)
        off = st.get("official") or {}
        seats = off.get("seats") or {}
        played = sorted({s.get("context") for s in seats.values() if s.get("grants") and s.get("context")})
        self.check("both_kinds_of_game_played", {"testing", "tournament"} <= set(played), contexts=played,
                   by_kind={k: sum(1 for s in seats.values() if s.get("context") == k and s.get("grants"))
                            for k in played})
        conn = sorted(s["connected_s"] for s in seats.values()               # tournament seats: they have the window
                      if s.get("connected_s") is not None and s.get("context") != "testing")
        testing = [s["connected_s"] for s in seats.values() if s.get("connected_s") is not None
                   and s.get("context") == "testing"]
        connect = {"seats": len(conn), "p50_s": conn[len(conn) // 2] if conn else None,
                   "max_s": conn[-1] if conn else None, "window_s": self.a.connect_window,
                   "no_shows": off.get("no_shows"), "testing_seats": len(testing),
                   "testing_max_s": max(testing) if testing else None}
        self.check("every_seat_connected_inside_its_window",
                   off.get("no_shows", 1) == 0 and bool(conn) and conn[-1] < self.a.connect_window, **connect)
        rep = {"out": str(self.out), "bundle": str(self.bundle), "minutes": round((time.time() - self.t0) / 60, 1),
               "connect": connect,
               "checks": self.checks, "all_ok": all(self.checks.values()),
               "platform": {k: (st.get("totals") or {}).get(k) for k in (
                   "matches_started", "matches_done", "wins", "decisions", "timer_decisions", "invalid_actions",
                   "match_invalid", "race_errors", "forfeits", "simulator_errors", "blind_states", "werewolf_tables",
                   "latency_p50", "latency_p99", "latency_max")},
               "official": {k: v for k, v in (st.get("official") or {}).items() if k != "seats"},
               "seats": (st.get("official") or {}).get("seats"),
               "matches": [{k: m.get(k) for k in ("game_type", "seat", "terminal", "win", "decisions", "timer",
                                                   "invalid_actions", "race_errors", "blind", "role", "chats", "turns",
                                                   "secs", "latency_max", "error")} for m in st.get("matches") or []],
               "battle_latency": self._lat(st, "moving"), "peak": self.peak,
               "logs": {n: {"tracebacks": t.count("Traceback"), "unexpected_errors": t.count("unexpected error"),
                            "runtime_restarts": t.count("[run_agent] agent exited"),
                            "workers_started": len(_START.findall(t)),
                            "seat_busy_exits": t.count("seat is held by another runtime; not playing it"),
                            "seat_checks_busy": t.count("is held by another runtime (the primary)")}
                        for n, t in logs.items()},
               "events": self.events}
        (self.out / "report.json").write_text(json.dumps(rep, indent=1))
        return rep

    @staticmethod
    def _lat(st: dict, phase: str) -> dict | None:
        xs = sorted(x for m in st.get("matches") or [] for p, x, _ in m.get("lat") or [] if p == phase)
        if not xs:
            return None
        return {"n": len(xs), "p50": xs[len(xs) // 2], "p99": xs[min(len(xs) - 1, int(0.99 * len(xs)))], "max": xs[-1]}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bundle", default=None, help="an extracted bundle with its .venv (default: this checkout)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--minutes", type=float, default=30.0)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--path", default=None, help="PATH for the agent side (default: this one; e.g. /usr/bin:/bin)")
    ap.add_argument("--outage-s", type=float, default=15.0)
    ap.add_argument("--connect-window", type=float, default=240.0,
                    help="each seat's connect window, as the event platform's 4 minutes (default 240 s)")
    ap.add_argument("--session-ttl", type=float, default=90.0)
    ap.add_argument("--token-ttl", type=float, default=150.0)
    ap.add_argument("--vgc-matches", type=int, default=60,
                    help="at most this many VGC matches, one after another (default 60: step 7 ends the run)")
    ap.add_argument("--after-takeover", type=int, default=1,
                    help="whole VGC matches the standby plays after the ones live at the takeover (default 1)")
    ap.add_argument("--blind-every", type=int, default=8, help="a blind battle state every Nth request (default 8)")
    ap.add_argument("--werewolf-tables", type=int, default=30, help="at most this many Werewolf tables (default 30)")
    ap.add_argument("--ww-think", type=float, default=3.0)
    ap.add_argument("--seed", type=int, default=21)
    ap.add_argument("--nice", type=int, default=10)
    a = ap.parse_args(argv)
    if not os.path.isdir("/proc"):
        sys.exit("rehearsal_official.py reads /proc: run it on Linux")

    def _term(*_):
        raise SystemExit("SIGTERM")                                  # run() then stops everything (finally)
    signal.signal(signal.SIGTERM, _term)
    rep = DryRun(a).run()
    print(json.dumps({k: rep[k] for k in ("checks", "all_ok", "platform", "official", "connect", "battle_latency",
                                          "peak", "logs")}, indent=1))


if __name__ == "__main__":
    main()
