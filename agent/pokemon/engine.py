"""The engine-search battle policy's Node process (agent/js/search.js): start, warm, ask, time out, restart.

ARENA_BATTLE=search (the default) makes PokemonPolicy ask this process for every VGC doubles battle decision (see
policy `_search_pick`); the rollback ARENA_BATTLE=classic never starts it. The process runs the Pokemon Showdown simulator
(the same engine and format the platform uses) and answers one JSON line per request.

Everything here fails safe, like calc.py: a missing node / search.js / pokemon-showdown, a process that dies, a
malformed answer or an answer that does not arrive within the request's timeout makes `search()` return None and the
policy plays its classic pick. Pipe I/O runs in a helper thread so a hung process can never block a decision.

A slow or contended CPU (deploy-2) must not turn into a kill / restart / timeout loop: killing a process that is merely
slow throws away its loaded simulator, and its replacement loads on the same busy CPU, so the next decision is "not
warm in time" and the one after times out again. So:
  * a search that misses its timeout is left to finish in the background (search.js stops at its safety net anyway);
    its late answer is read and dropped before the next request (`_drain_owed`), like calc.py's owed answers. Only a
    process that still owes it after STALL_FACTOR x its timeout (at least STALL_MIN_S) is hung: killed and restarted;
  * when a request had to wait (a new process loading, a late answer draining), the safety net it sends is shrunk to
    what is left of its time, and a request with less than MIN_NET_MS left is not sent (the process stays, warm);
  * a restart delayed by the backoff is started in the background when the backoff ends, not on the next decision;
  * a dead process (crash, OOM kill), a malformed answer or a hang is still killed and restarted (RESTART_BACKOFF_S);
    at most MAX_STARTS processes per match, then the search is off for the match.

Memory (deploy-10): every search process costs 230-340 MB and every concurrent match has one, so a machine-wide cap
holds: before a process starts it takes one of `search_cap()` slots (flock on a file under ARENA_LOCK_DIR, released
when the process is killed or the worker exits in any way) and checks that MIN_FREE_MB is available (MemAvailable and
the cgroup's headroom). Without a slot or the memory, the match plays the classic pick and says so (ERROR, once per
match); it asks again every ADMIT_RETRY_S, so it gets the search back once a slot frees up. This admission applies to
the deployed agent only (`admission_on()`): local simulators, ladders, panels, replays and tests (ARENA_RECORD=0) run
every search process they start, so a local game never depends on what else runs on the machine.

Where pokemon-showdown comes from: agent/js/node_modules (scripts/bundle.sh vendors a trimmed copy there), else the
development checkout's local/js/node_modules (the local simulator's own copy), else ARENA_SHOWDOWN. It is not in
agent/js/package.json, so `npm ci` there does not install it. A missing simulator must not go unnoticed (every battle
decision would quietly play the classic policy): `probe()` backs scripts/selfcheck.py (FAIL) and scripts/run_agent.sh
(refuses to start) whenever ARENA_BATTLE=search.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import dex
from .calc import find_node as _node_bin      # (not the calculator's own: ARENA_CALC=off turns off only the calculator)
from .calc import node_env, prefer_for_oom_kill

log = logging.getLogger("agent.engine")

COLD_START_S = 30.0         # a new process must answer its warm-up within this (loads the engine: ~1 s idle)
MAX_STARTS = 6              # processes per match (1 + 5 restarts), then the search is off for the match
RESTART_BACKOFF_S = (0.0, 1.0, 3.0, 8.0, 15.0)
SEARCH_OOM_SCORE = 800      # oom_score_adj of the search process: the first to go under memory pressure
_TIMEOUT = object()

STALL_FACTOR, STALL_MIN_S = 3.0, 15.0   # a late answer still owed after this long: the process is hung
DRAIN_MAX_S = 1.5           # a request waits at most this long (and a quarter of its time) for a late answer
MIN_NET_MS = 300            # a request left with a smaller safety net than this is not sent


def _env_int(name: str, default: int | None) -> int | None:
    try:
        v = str(os.environ.get(name) or "").strip()
        return default if v in ("", "auto") else int(v)
    except ValueError:
        return default


# ---- machine-wide admission (deploy-10) ---------------------------------------------------------------------------
ENGINE_MB = 600             # memory per concurrent match with a search: search process 230-340 MB, damage calc ~100,
                            # match worker ~80, and headroom (docs/deploy.md: 2 matches ~1.1 GB)
BASE_MB = 768               # kept for the supervisor, the system and non-search matches (Werewolf ~80 MB each)
MIN_FREE_MB = _env_int("ARENA_SEARCH_MIN_FREE_MB", 500)     # a new search process needs this much available memory
ADMIT_RETRY_S = 5.0         # a match without a slot asks again after this long


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def _inactive_file_mb(d: Path, v1: bool) -> int:
    """Reclaimable page cache of a cgroup (memory.stat inactive_file): counted in its usage, yet free for the taking."""
    for line in (_read(str(d / "memory.stat")) or "").splitlines():
        k, _, v = line.partition(" ")
        if k == ("total_inactive_file" if v1 else "inactive_file"):
            try:
                return int(v) // 2**20
            except ValueError:
                return 0
    return 0


def _cgroup_mem() -> tuple[int | None, int | None]:
    """(limit, headroom) in MB of the tightest memory cgroup this process is in (v2 memory.max, v1 limit_in_bytes;
    headroom = limit - usage + reclaimable page cache), (None, None) when there is none or it cannot be read."""
    limit = room = None
    try:
        rel = None
        for line in (_read("/proc/self/cgroup") or "").splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3 and (parts[0] == "0" or "memory" in parts[1].split(",")):
                rel = parts[2]
                v1 = parts[1] != ""
                break
        if rel is None:
            return None, None
        base = Path("/sys/fs/cgroup/memory" if v1 else "/sys/fs/cgroup")
        d = base / rel.lstrip("/")
        while True:
            mx = _read(str(d / ("memory.limit_in_bytes" if v1 else "memory.max")))
            cur = _read(str(d / ("memory.usage_in_bytes" if v1 else "memory.current")))
            if mx and mx.strip() != "max" and cur:
                m, c = int(mx) // 2**20, int(cur) // 2**20
                if m < 2**40 // 2**20:                     # v1 "no limit" is a huge number
                    r = m - c + _inactive_file_mb(d, v1)
                    limit = m if limit is None else min(limit, m)
                    room = r if room is None else min(room, r)
            if d == base or base not in d.parents:
                break
            d = d.parent
    except (OSError, ValueError):
        return limit, room
    return limit, room


def _meminfo() -> dict:
    out = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        try:
            out[k.strip()] = int(v.split()[0]) // 1024
        except (IndexError, ValueError):
            pass
    return out


def mem_available_mb() -> int | None:
    """Memory a new process can get now: min(MemAvailable, the cgroup's limit - usage) in MB; None when unknown."""
    avail = _meminfo().get("MemAvailable")
    room = _cgroup_mem()[1]
    vals = [v for v in (avail, room) if v is not None]
    return min(vals) if vals else None


def search_cap() -> int:
    """How many search processes may run at once on this machine: ARENA_SEARCH_MAX_PROCS (0 = no cap), else from the
    memory this machine / container has: (min(MemTotal, cgroup limit) - BASE_MB) // ENGINE_MB, at least 1 (1 GB -> 1,
    2 GB -> 2, 4 GB -> 5, 8 GB -> 12)."""
    n = _env_int("ARENA_SEARCH_MAX_PROCS", None)
    if n is not None:
        return max(0, n)
    total = [v for v in (_meminfo().get("MemTotal"), _cgroup_mem()[0]) if v is not None]
    if not total:
        return 0
    return max(1, (min(total) - BASE_MB) // ENGINE_MB)


def admission_on() -> bool:
    """Do the machine-wide cap and the free-memory check apply to this process? Yes for the deployed agent (decisions
    recorded: ARENA_RECORD != 0) and wherever ARENA_SEARCH_MAX_PROCS / ARENA_SEARCH_MIN_FREE_MB is set; no for local
    simulators, ladders, panels, replays and tests (ARENA_RECORD=0): their games must play the same whatever else runs
    on the machine (a panel with --jobs 6 and a champion of this code runs 12 search processes, and a denied one would
    play a whole worker's games with the classic pick)."""
    if any(str(os.environ.get(k) or "").strip() not in ("", "auto")
           for k in ("ARENA_SEARCH_MAX_PROCS", "ARENA_SEARCH_MIN_FREE_MB")):
        return True
    return os.environ.get("ARENA_RECORD", "1") != "0"


def _slot_dir() -> Path:
    try:
        uid = os.getuid()
    except AttributeError:
        uid = "u"
    return Path(os.environ.get("ARENA_LOCK_DIR") or tempfile.gettempdir()) / f"arena-search-slots-{uid}"


class _Slot:
    """One of the machine's search slots, held (an exclusive flock on its file) while our search process lives. The
    kernel drops the lock when the file is closed, so a killed worker never leaks its slot."""

    def __init__(self, f, n: int, cap: int):
        self.f, self.n, self.cap = f, n, cap

    def release(self) -> None:
        f, self.f = self.f, None
        if f is not None:
            try:
                f.close()
            except OSError:
                pass

    @staticmethod
    def take() -> "tuple[_Slot | None, str]":
        """(a slot, '') or (None, why not). No cap (0) or no flock (not Linux / macOS): an unnumbered slot."""
        cap = search_cap()
        try:
            import fcntl
        except ImportError:
            return _Slot(None, -1, 0), ""
        if cap <= 0:
            return _Slot(None, -1, 0), ""
        try:
            d = _slot_dir()
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as e:
            return _Slot(None, -1, 0), f"(no slot directory: {e!r})"
        for n in range(cap):
            try:
                f = open(d / f"slot-{n}.lock", "a")
            except OSError:
                continue
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return _Slot(f, n, cap), ""
            except OSError:
                f.close()
        return None, (f"no search slot free: {cap} search process{'es' if cap != 1 else ''} already run on this "
                      f"machine (the cap for its memory; ARENA_SEARCH_MAX_PROCS overrides)")


def _paths() -> tuple[Path, list[Path]] | None:
    """(directory of search.js, node_modules directories to put on NODE_PATH) or None when unavailable."""
    here = Path(__file__).resolve().parent.parent / "js"
    if not (here / "search.js").exists():
        return None
    mods = []
    for d in (here / "node_modules", dex.repo_root() / "agent" / "js" / "node_modules",
              dex.repo_root() / "local" / "js" / "node_modules"):
        if (d / "pokemon-showdown").exists() and d not in mods:
            mods.append(d)
    if not mods and not os.environ.get("ARENA_SHOWDOWN"):
        return None
    return here, mods


def available() -> tuple[bool, str]:
    """Can this machine run the engine search? (for selfcheck / diagnostics)"""
    if _node_bin() is None:
        return False, "no node"
    p = _paths()
    if p is None:
        return False, ("agent/js/search.js or pokemon-showdown missing (a bundle vendors it in agent/js/node_modules; "
                       "a git checkout needs: cd local/js && npm ci)")
    return True, f"search.js in {p[0]}, pokemon-showdown from {[str(m) for m in p[1]] or os.environ.get('ARENA_SHOWDOWN')}"


PROBE_FILE = "search_probe.json"   # data/: one real VGC battle decision (its search.js request), for probe(search=True)
PROBE_SLOW_S = 2.5                 # a probe search slower than this: the machine is too slow / busy for the search


def probe(timeout: float = 60.0, search: bool = False) -> tuple[bool, str]:
    """Can this machine run the engine search? For scripts/selfcheck.py, scripts/run_agent.sh and agent/health.py,
    never used in play: a fresh search process loads the simulator (its warm-up answer) and, with `search`, answers one
    real battle decision (data/search_probe.json, the deployed search settings), then is stopped. It never takes a
    machine-wide slot. (True, 'simulator loads in 1.2s; ...') or (False, the reason). Never raises."""
    try:
        good, why = available()
        if not good:
            return False, why
        e = SearchEngine(admit=False)
        t = time.monotonic()
        took = None
        try:
            ready = e.warm(timeout)
            load_s = time.monotonic() - t
            if ready and search:
                req = json.loads((dex.DATA / PROBE_FILE).read_text())
                t1 = time.monotonic()
                ans = e.search(req["state"], req["params"], max(1.0, min(20.0, timeout - load_s)))
                took = time.monotonic() - t1
                if not ans or not isinstance(ans.get("action"), dict):
                    return False, (f"the simulator loads ({load_s:.1f}s) but a search did not answer "
                                   f"({e.last_error or 'no answer'}); {why}")
        finally:
            e.close()
        if not ready:
            return False, f"the search process did not load the simulator ({e.last_error or 'no warm-up answer'}); {why}"
        if took is not None:
            slow = f" SLOW (> {PROBE_SLOW_S:.1f}s: a busy or slow CPU)" if took > PROBE_SLOW_S else ""
            return True, f"simulator loads in {load_s:.1f}s, a search answered in {took:.1f}s{slow}; {why}"
        return True, f"simulator loads in {time.monotonic() - t:.1f}s; {why}"
    except Exception as ex:  # noqa: BLE001 - diagnostics only
        return False, f"probe raised {ex!r}"


class SearchEngine:
    _inst: "SearchEngine | None" = None

    def __init__(self, admit: bool = True):
        self.proc = None
        self.enabled = _node_bin() is not None and _paths() is not None
        self._id = 0
        self.ready = False
        self.starts = 0
        self.failures = 0
        self.errors = 0
        self.timeouts = 0
        self.last_error: str | None = None
        self.next_start = 0.0
        self._warm_q = None            # (queue, id, sent) of the warm-up answer still owed
        self._owed = None              # (queue, id, sent, timeout) of a search answer that came too late (still coming)
        self.late = 0                  # late answers read and dropped (a search that overran; its process was kept)
        self.busy = 0                  # requests not sent: the process was still finishing a late search
        self.hung = 0                  # processes killed for owing an answer too long
        self.denied = 0                # starts refused for want of a machine-wide slot or memory
        self._admit = admit            # False: probes (never take a slot)
        self._slot: _Slot | None = None
        self._admit_retry = 0.0
        self._restart_timer = None
        self.startup_s = None
        self.lock = threading.Lock()
        if not self.enabled:              # (selfcheck and run_agent.sh refuse such a setup; the log says it too)
            log.error("engine search unavailable (%s): every battle decision plays the classic policy",
                      available()[1])

    @classmethod
    def get(cls) -> "SearchEngine":
        if cls._inst is None:
            cls._inst = cls()
        return cls._inst

    def stats(self) -> dict:
        slot = self._slot
        return {"enabled": self.enabled, "starts": self.starts, "errors": self.errors, "timeouts": self.timeouts,
                "late": self.late, "busy": self.busy, "hung": self.hung, "denied": self.denied,
                "slot": None if slot is None else f"{slot.n + 1}/{slot.cap}" if slot.cap else "-",
                "ready": self.ready, "startup_s": self.startup_s, "last_error": self.last_error}

    def _admitted(self) -> bool:
        """A machine-wide slot and enough free memory for a new search process (see the module docstring). A refusal is
        logged loudly once per match and retried after ADMIT_RETRY_S. Local runs (`admission_on()` False: ARENA_RECORD=0
        and no knob set) are always admitted."""
        if not self._admit or self._slot is not None or not admission_on():
            return True
        now = time.monotonic()
        if now < self._admit_retry:
            return False
        slot, why = _Slot.take()
        if slot is not None:
            avail = mem_available_mb()
            if avail is None or MIN_FREE_MB is None or avail >= MIN_FREE_MB:
                self._slot = slot
                if why:
                    log.warning("engine search: %s; starting without a machine-wide cap", why)
                return True
            slot.release()
            why = f"low memory: {avail} MB available (a search process needs {MIN_FREE_MB} MB)"
        self._admit_retry = now + ADMIT_RETRY_S
        self.denied += 1
        self.last_error = why
        if self.denied == 1:
            log.error("ENGINE SEARCH NOT STARTED: %s; this match plays the classic pick until that changes", why)
        return False

    # ---- process lifecycle ---------------------------------------------------------------------------------------
    def _start(self) -> bool:
        if self.proc is not None and self.proc.poll() is None:
            return True
        if not self.enabled:
            return False
        if self.proc is not None:
            self._failed(f"search process exited (code {self.proc.poll()})")
            if not self.enabled:
                return False
        if time.monotonic() < self.next_start:
            return False
        if self.starts >= MAX_STARTS:
            self._disable(f"{self.starts} search processes failed")
            return False
        if not self._admitted():
            return False
        try:
            p = _paths()
            if p is None:
                self._disable("search.js / pokemon-showdown missing")
                return False
            script, mods = p
            self.proc = subprocess.Popen([_node_bin() or "node", str(script / "search.js")], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=node_env(mods),
                                         text=True, bufsize=1)
            prefer_for_oom_kill(self.proc.pid, SEARCH_OOM_SCORE)
        except Exception as e:
            self.proc = None
            self.starts += 1
            self._failed(f"spawn failed: {e!r}")
            return False
        self.starts += 1
        self.ready = False
        self._id += 1
        self._warm_q = (self._send({"id": self._id, "cmd": "warm"}), self._id, time.monotonic())
        return True

    def _kill(self) -> None:
        proc, self.proc = self.proc, None
        self._warm_q = None
        self._owed = None
        self.ready = False
        slot, self._slot = self._slot, None
        try:
            if proc is None:
                return
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
        finally:
            if slot is not None:                         # (after the process is gone: its memory is free again)
                slot.release()

    def _disable(self, reason: str) -> None:
        if self.enabled:
            log.error("engine search off for the rest of this match (%s); playing the classic policy", reason)
        self.enabled = False
        self._kill()

    def _failed(self, reason: str) -> None:
        self.errors += 1
        self.failures += 1
        self.last_error = reason
        self._kill()
        if self.starts >= MAX_STARTS:
            self._disable(f"{reason}; {self.starts} processes used")
            return
        delay = RESTART_BACKOFF_S[min(self.failures, len(RESTART_BACKOFF_S)) - 1]
        self.next_start = time.monotonic() + delay
        log.warning("engine search: %s; restarting %s", reason, "now" if delay <= 0 else f"in {delay:.0f}s")
        if delay > 0:
            self._restart_later(delay)

    def _restart_later(self, delay: float) -> None:
        """Start the next process in the background once the backoff ends, so the next decision after it finds the
        process loading or warm instead of paying the start-up itself. Never raises."""
        try:
            if self._restart_timer is not None:
                self._restart_timer.cancel()
            t = threading.Timer(delay + 0.05, self.start_background)
            t.daemon = True
            t.start()
            self._restart_timer = t
        except Exception:
            log.debug("engine search: background restart not scheduled", exc_info=True)

    def _send(self, msg: dict) -> "queue.Queue":
        q: queue.Queue = queue.Queue(maxsize=1)
        proc = self.proc
        payload = json.dumps(msg, separators=(",", ":"))

        def io():
            try:
                proc.stdin.write(payload + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
            except Exception:
                line = None
            q.put(line)

        threading.Thread(target=io, name="search-io", daemon=True).start()
        return q

    @staticmethod
    def _wait(q, end: float):
        try:
            return q.get(timeout=max(0.0, end - time.monotonic()))
        except queue.Empty:
            return _TIMEOUT

    def _collect_warm(self, end: float) -> bool:
        """Wait (until `end` at most) for the warm-up answer of a new process. True when it is ready."""
        if self.ready:
            return True
        if self._warm_q is None:
            return False
        q, rid, sent = self._warm_q
        line = self._wait(q, min(end, sent + COLD_START_S))
        if line is _TIMEOUT:
            if time.monotonic() - sent >= COLD_START_S:
                self._failed(f"no warm-up answer in {COLD_START_S:.0f}s")
                self._start()
            return False
        self._warm_q = None
        try:
            msg = json.loads(line) if line else None
        except Exception:
            msg = None
        if not msg or msg.get("id") != rid or not msg.get("ok"):
            self._failed(f"warm-up failed: {(msg or {}).get('error') or 'no answer'}")
            self._start()
            return False
        self.ready = True
        self.startup_s = round(time.monotonic() - sent, 3)
        return True

    # ---- API ------------------------------------------------------------------------------------------------------
    def warm(self, timeout: float = COLD_START_S) -> bool:
        """Start the process and wait for its warm-up (the engine data load, ~1 s) at most `timeout` seconds."""
        try:
            with self.lock:
                if not self._start():
                    return False
                return self._collect_warm(time.monotonic() + max(0.0, timeout))
        except Exception as e:
            log.warning("engine search warm-up failed: %r", e)
            return False

    def start_background(self) -> None:
        """Spawn the process now (its warm-up runs while we do other things). Never raises."""
        try:
            with self.lock:
                self._start()
        except Exception:
            log.debug("engine search spawn failed", exc_info=True)

    def close(self) -> None:
        """Stop the process and any scheduled restart (probes, tests). Never raises."""
        try:
            if self._restart_timer is not None:
                self._restart_timer.cancel()
            self._kill()
        except Exception:
            pass

    def search(self, state: dict | None, params: dict, timeout: float, battle_json: str | None = None,
               grace: float = 0.0) -> dict | None:
        """One search; the answer dict ({"action", "info"}) or None (unavailable, error, timeout). `timeout` is what
        the request's safety net (params["hard_ms"]) was sized for; its answer is awaited up to `grace` seconds longer
        (a slow CPU overruns the net). Never raises, never blocks past timeout + grace (plus a thread join)."""
        msg = {"cmd": "search", "state": state, "params": params}
        if battle_json is not None:
            msg["battle_json"] = battle_json
        return self._request(msg, timeout, grace)

    def inspect(self, state: dict | None = None, battle_json: str | None = None, timeout: float = 10.0) -> dict | None:
        """Diagnostics (scripts/search_fidelity.py): the summary of the battle `state` rebuilds to (or of a
        serialized battle). None on any failure."""
        msg = {"cmd": "inspect", "state": state}
        if battle_json is not None:
            msg["battle_json"] = battle_json
        ans = self._request(msg, timeout)
        return (ans or {}).get("summary")

    def _drain_owed(self, end: float) -> bool:
        """Read the late answer of a search we stopped waiting for (waiting until `end` at most). True when the process
        is free for a new request. One that owes it for too long is hung: killed and restarted."""
        q, rid, sent, timeout = self._owed
        stall = sent + max(STALL_FACTOR * timeout, STALL_MIN_S)
        line = self._wait(q, min(end, stall))
        if line is _TIMEOUT:
            if time.monotonic() >= stall:
                self.hung += 1
                self._failed(f"no answer for {time.monotonic() - sent:.1f}s (hung)")
                self._start()
            return False
        self._owed = None
        try:
            ans = json.loads(line) if line else None
        except Exception:
            ans = None
        if not ans or ans.get("id") != rid:
            self._failed("EOF or a malformed late answer" if line else "search process died")
            self._start()
            return False
        self.late += 1
        return True

    def _request(self, msg: dict, timeout: float, grace: float = 0.0) -> dict | None:
        t0 = time.monotonic()
        end = t0 + max(0.0, timeout)
        if not self.lock.acquire(timeout=max(0.0, min(1.0, timeout))):
            return None
        try:
            if not self._start():
                return None
            waited = False
            if self._owed is not None:               # the last search overran: let it finish, never kill it for that
                waited = True
                if not self._drain_owed(min(end, t0 + min(DRAIN_MAX_S, 0.25 * timeout))):
                    if self._owed is not None:           # (else it was hung or died: restarted, last_error says so)
                        self.busy += 1
                        self.last_error = "busy: the last search is still finishing"
                    return None
            cold = not self.ready
            if not self._collect_warm(end):
                self.last_error = "not warm in time"
                return None
            if time.monotonic() >= end:
                self.last_error = "not warm in time"
                return None
            params = msg.get("params")
            if (cold or waited) and isinstance(params, dict) and isinstance(params.get("hard_ms"), (int, float)):
                # This request's time went partly to a new process loading or a late answer draining, but its safety
                # net was sized for all of it: shrink the net to what is left, so the answer comes in time instead of
                # the search overrunning. (A warm, free process: unchanged.) Too little left: not sent, and the process
                # stays up and warm for the next decision.
                left_ms = int(800 * (end - time.monotonic()))
                if left_ms < MIN_NET_MS:
                    self.last_error = "not warm in time (the warm-up used the time)"
                    return None
                msg = {**msg, "params": {**params, "hard_ms": min(int(params["hard_ms"]), left_ms)}}
            self._id += 1
            rid = self._id
            sent = time.monotonic()
            q = self._send({**msg, "id": rid})
            line = self._wait(q, end + max(0.0, grace))
            if line is _TIMEOUT:
                # Slow, not dead: the search stops at its safety net and answers; that answer is read and dropped
                # before the next request (`_drain_owed`). Killing it would throw the loaded simulator away and make
                # the next decisions pay a start-up on the same busy CPU.
                self.timeouts += 1
                self._owed = (q, rid, sent, max(0.1, timeout))
                self.last_error = f"no answer in {time.monotonic() - t0:.2f}s"
                log.warning("engine search: no answer in %.2fs (safety net %s ms); it finishes in the background, "
                            "this decision plays the classic pick", time.monotonic() - t0,
                            (msg.get("params") or {}).get("hard_ms"))
                return None
            try:
                ans = json.loads(line) if line else None
            except Exception:
                ans = None
            if not ans or ans.get("id") != rid:
                self._failed("EOF or a malformed answer" if line else "search process died")
                self._start()
                return None
            if ans.get("error"):
                self.errors += 1
                self.last_error = str(ans.get("error"))[:300]
                log.warning("engine search error: %s", self.last_error)
                return None
            self.failures = 0
            return ans
        except Exception as e:
            log.warning("engine search round trip failed: %r", e)
            try:
                self._failed(f"round trip raised {e!r}")
            except Exception:
                pass
            return None
        finally:
            self.lock.release()
