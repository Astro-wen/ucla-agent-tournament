"""The engine-search battle policy's Node process (agent/js/search.js): start, warm, ask, time out, restart.

ARENA_BATTLE=search (the default) makes PokemonPolicy ask this process for every VGC doubles battle decision (see
policy `_search_pick`); the rollback ARENA_BATTLE=classic never starts it. The process runs the Pokemon Showdown simulator
(the same engine and format the platform uses) and answers one JSON line per request.

Everything here fails safe, like calc.py: a missing node / search.js / pokemon-showdown, a process that dies, a
malformed answer or an answer that does not arrive within the request's timeout makes `search()` return None and the
policy plays its classic pick. A timed-out process is killed at once (a search never owes an answer we still want) and
a new one is started in the background, so the next decision finds it warm; at most MAX_STARTS processes per match,
then the search is off for the match. Pipe I/O runs in a helper thread so a hung process can never block a decision.

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
import threading
import time
from pathlib import Path

from . import dex
from .calc import _node_bin

log = logging.getLogger("agent.engine")

COLD_START_S = 30.0         # a new process must answer its warm-up within this (loads the engine: ~1 s idle)
MAX_STARTS = 6              # processes per match (1 + 5 restarts), then the search is off for the match
RESTART_BACKOFF_S = (0.0, 1.0, 3.0, 8.0, 15.0)
_TIMEOUT = object()


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


def probe(timeout: float = 60.0) -> tuple[bool, str]:
    """Can this machine run the engine search? For scripts/selfcheck.py and scripts/run_agent.sh, never used in play:
    a fresh search process loads the simulator (its warm-up answer) and is stopped. (True, 'simulator loads in 1.2s;
    search.js in ...') or (False, the reason). Never raises."""
    try:
        good, why = available()
        if not good:
            return False, why
        e = SearchEngine()
        t = time.monotonic()
        try:
            ready = e.warm(timeout)
        finally:
            e._kill()
        if not ready:
            return False, f"the search process did not load the simulator ({e.last_error or 'no warm-up answer'}); {why}"
        return True, f"simulator loads in {time.monotonic() - t:.1f}s; {why}"
    except Exception as ex:  # noqa: BLE001 - diagnostics only
        return False, f"probe raised {ex!r}"


class SearchEngine:
    _inst: "SearchEngine | None" = None

    def __init__(self):
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
        return {"enabled": self.enabled, "starts": self.starts, "errors": self.errors, "timeouts": self.timeouts,
                "ready": self.ready, "startup_s": self.startup_s, "last_error": self.last_error}

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
        try:
            p = _paths()
            if p is None:
                self._disable("search.js / pokemon-showdown missing")
                return False
            script, mods = p
            env = dict(os.environ)
            env["NODE_PATH"] = os.pathsep.join([str(m) for m in mods] + ([env["NODE_PATH"]] if env.get("NODE_PATH") else []))
            self.proc = subprocess.Popen([_node_bin() or "node", str(script / "search.js")], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, text=True,
                                         bufsize=1)
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
        self.ready = False
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

    def search(self, state: dict | None, params: dict, timeout: float, battle_json: str | None = None) -> dict | None:
        """One search; the answer dict ({"action", "info"}) or None (unavailable, error, timeout). Never raises,
        never blocks past `timeout` seconds (plus a thread join of a few ms)."""
        msg = {"cmd": "search", "state": state, "params": params}
        if battle_json is not None:
            msg["battle_json"] = battle_json
        return self._request(msg, timeout)

    def inspect(self, state: dict | None = None, battle_json: str | None = None, timeout: float = 10.0) -> dict | None:
        """Diagnostics (scripts/search_fidelity.py): the summary of the battle `state` rebuilds to (or of a
        serialized battle). None on any failure."""
        msg = {"cmd": "inspect", "state": state}
        if battle_json is not None:
            msg["battle_json"] = battle_json
        ans = self._request(msg, timeout)
        return (ans or {}).get("summary")

    def _request(self, msg: dict, timeout: float) -> dict | None:
        t0 = time.monotonic()
        end = t0 + max(0.0, timeout)
        if not self.lock.acquire(timeout=max(0.0, min(1.0, timeout))):
            return None
        try:
            if not self._start():
                return None
            if not self._collect_warm(end):
                self.last_error = "not warm in time"
                return None
            if time.monotonic() >= end:
                return None
            self._id += 1
            rid = self._id
            line = self._wait(self._send({**msg, "id": rid}), end)
            if line is _TIMEOUT:
                self.timeouts += 1
                self._failed(f"no answer in {time.monotonic() - t0:.2f}s")
                self._start()                        # warm again in the background for the next decision
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
