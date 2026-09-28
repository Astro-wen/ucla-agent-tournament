"""Exact damage via Smogon's calculator (agent/js/calc.js), batched: one round trip per turn.

`DamageCalc.batch()` returns None when node or node_modules are missing, so callers fall back to the
base-stat heuristic instead of crashing (a crash = the platform random-plays our seat).

The node process starts lazily and its first answer pays for node + @smogon/calc start-up (0.3 s here, seconds on
a cold machine). Every new process is sent a tiny dummy calc right away, so the start-up runs in the background;
`DamageCalc.warm()` waits for that answer; the policy calls it in the phases before the first battle decision
(draft / teambuild / team preview), so a fresh match process does not spend its first turn's lookahead budget on it.

Time control and crash-proofing (agent/timecontrol.py): a round trip of n calcs may take its allowance
CALC_BASE_S + n x CALC_PER_CALC_S (at most CALC_TIMEOUT_S; ~2.3 s for a battle turn's ~50 calcs, which take ~20 ms,
~0.2 s with 16 agents on 16 loaded threads), and never runs past the current decision's deadline. The pipe I/O runs
in a helper thread, so a hung or stalled node (a full stdin pipe included) cannot block the decision: the batch then
answers None (the caller plays its heuristic) and the process keeps its request. The next round trip first collects
that owed answer; a process that owes one for longer than STALL_FACTOR x its allowance (COLD_START_S for a new
process's warm-up) is hung and killed. A failed round trip (the process died, EOF, a malformed or mismatched answer, a
hung process) no longer disables the calculator for the match: the process is killed and restarted, immediately after
an isolated failure and with RESTART_BACKOFF_S between consecutive ones, at most MAX_STARTS processes per match; only
then is the calculator off for good (heuristic play, exactly as without node).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .. import timecontrol
from . import dex
from .dex import to_id

log = logging.getLogger("agent.calc")

CALC_BASE_S = 2.0           # a round trip of n calcs may take CALC_BASE_S + n x CALC_PER_CALC_S (its allowance) ...
CALC_PER_CALC_S = 0.006     # (~5x the per-calc time of the draft's 1,200-calc batch with 16 agents on 16 loaded threads)
CALC_TIMEOUT_S = 8.0        # ... but never more than this
COLD_START_S = 20.0         # a new process that has not answered its warm-up calc within this is hung
STALL_FACTOR = 3.0          # an answer still owed after 3x its allowance: the process is hung
MIN_ROUNDTRIP_S = 0.02      # less time left than this: don't even ask (the caller plays its heuristic)
RESTART_BACKOFF_S = (0.0, 1.0, 2.0, 4.0, 8.0)   # wait before a restart after the n-th consecutive failure
MAX_STARTS = 6              # node processes per match (1 + 5 restarts); then the calculator is off for the match
_TIMEOUT = object()

# One cheap calc that makes node load @smogon/calc and the gen-9 data; its answer is never cached or used.
WARM_CALC = {"attacker": {"species": "Pikachu", "level": 50}, "defender": {"species": "Pikachu", "level": 50},
             "move": "Thunderbolt", "field": {"gameType": "Doubles"}}


def _js_paths() -> tuple[Path, Path] | None:
    """(directory of the calc.js to run, node_modules directory to resolve @smogon/calc from).

    A champion copy (champion/agent/js) keeps its OWN calc.js but has no node_modules (freeze_champion.sh drops
    them): it must still run its own calc.js, borrowing the repo's node_modules via NODE_PATH. Falling back to the
    repo's agent/js/calc.js instead would silently give the champion the candidate's calculator on a ladder."""
    here = Path(__file__).resolve().parent.parent / "js"          # agent/js next to this package
    root = dex.repo_root() / "agent" / "js"                         # the real one when we run as a champion copy
    script = next((d for d in (here, root) if (d / "calc.js").exists()), None)
    modules = next((d / "node_modules" for d in (here, root) if (d / "node_modules").exists()), None)
    if script is None or modules is None:
        return None
    return script, modules


def _js_dir() -> Path | None:
    """Directory of the calc.js this package runs (None = calculator unavailable)."""
    p = _js_paths()
    return p[0] if p else None


CACHE_MAX = 20000                 # calc results kept across decisions (cleared, never partially evicted)


class DamageCalc:
    _inst: "DamageCalc | None" = None

    def __init__(self):
        self.proc = None
        self.enabled = shutil.which("node") is not None and _js_dir() is not None
        self._id = 0
        self.cache: dict[str, dict] = {}
        self.ready = False          # the running node process has answered at least once (start-up paid)
        self.starts = 0             # node processes spawned
        self.startup_s = None       # seconds the last spawn took until its first answer (None: never measured)
        self.failures = 0           # consecutive failures (a good answer to a real request, not the warm-up, resets
                                    # it; drives the backoff)
        self.errors = 0             # failed round trips / dead or hung processes, whole match
        self.timeouts = 0           # round trips that ran out of time (the process kept the request)
        self.last_error: str | None = None
        self.next_start = 0.0       # time.monotonic() before which no new process is started (backoff)
        self._pending = None        # (queue, request id, sent at, allowance) of an answer the process still owes us
        self._spawned_at = None
        self.slowest = (0.0, 0)     # (seconds, calcs) of the slowest answered round trip (diagnostics)
        self.tightest = (0.0, 0)    # (share of its allowance, calcs) of the answered round trip closest to its limit

    @classmethod
    def get(cls) -> "DamageCalc":
        if cls._inst is None:
            cls._inst = cls()
        return cls._inst

    def stats(self) -> dict:
        return {"enabled": self.enabled, "starts": self.starts, "errors": self.errors, "timeouts": self.timeouts,
                "ready": self.ready, "startup_s": self.startup_s, "slowest": self.slowest, "last_error": self.last_error}

    # ---- process lifecycle ---------------------------------------------------------------------------------------
    @staticmethod
    def _command(script: Path) -> list[str]:
        return ["node", str(script / "calc.js")]

    def _start(self) -> bool:
        """True while a node process is running (a new one is started when needed, its warm-up calc sent at once so
        the start-up runs in the background). False when the calculator is off for good or backing off after a
        failure."""
        if self.proc is not None and self.proc.poll() is None:
            return True
        if not self.enabled:
            return False
        if self.proc is not None:                         # it died since the last round trip (crash, OOM kill ...)
            code = self.proc.poll()
            self._failed(f"node process exited (code {code})")
            if not self.enabled:
                return False
        if time.monotonic() < self.next_start:
            return False
        if self.starts >= MAX_STARTS:
            self._disable(f"{self.starts} node processes failed")
            return False
        try:
            paths = _js_paths()
            if paths is None:
                self._disable("calc.js / node_modules missing")
                return False
            script, modules = paths
            env = dict(os.environ)
            env["NODE_PATH"] = os.pathsep.join(x for x in (str(modules), env.get("NODE_PATH", "")) if x)
            self.proc = subprocess.Popen(self._command(script), stdin=subprocess.PIPE, env=env,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except Exception as e:
            log.warning("could not start the damage calc: %r", e)
            self.proc = None
            self.starts += 1                              # a failed spawn counts against MAX_STARTS too
            self._failed(f"spawn failed: {e!r}")
            return False
        self.starts += 1
        self.ready = False
        self._spawned_at = time.monotonic()
        self._id += 1
        self._pending = (self._send(json.dumps({"id": self._id, "calcs": [WARM_CALC]})), self._id, self._spawned_at,
                         None)                            # (no allowance: a warm-up, COLD_START_S applies)
        return True

    def _kill(self) -> None:
        proc, self.proc = self.proc, None
        self._pending = None
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
            log.error("damage calculator off for the rest of this match (%s); playing the heuristic", reason)
        self.enabled = False
        self._kill()

    def _failed(self, reason: str) -> None:
        """A round trip failed or the process died / hung: kill it and schedule a restart (backoff, capped)."""
        self.errors += 1
        self.failures += 1
        self.last_error = reason
        self._kill()
        if self.starts >= MAX_STARTS:
            self._disable(f"{reason}; {self.starts} node processes used")
            return
        delay = RESTART_BACKOFF_S[min(self.failures, len(RESTART_BACKOFF_S)) - 1]
        self.next_start = time.monotonic() + delay
        log.warning("damage calc: %s; restarting %s (process %d of at most %d)", reason,
                    "now" if delay <= 0 else f"in {delay:.0f}s", self.starts + 1, MAX_STARTS)

    # ---- I/O ------------------------------------------------------------------------------------------------------
    def _send(self, payload: str) -> "queue.Queue":
        """Write one request and read one answer line in a helper thread (so neither a full stdin pipe nor a silent
        process can block the decision). The queue receives the line ('' = EOF, None = I/O error)."""
        q: queue.Queue = queue.Queue(maxsize=1)
        proc = self.proc

        def io():
            try:
                proc.stdin.write(payload + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
            except Exception:
                line = None
            q.put(line)

        threading.Thread(target=io, name="calc-io", daemon=True).start()
        return q

    @staticmethod
    def _wait(q: "queue.Queue", end: float):
        try:
            return q.get(timeout=max(0.0, end - time.monotonic()))
        except queue.Empty:
            return _TIMEOUT

    @staticmethod
    def _parse(line, rid: int, n: int | None) -> list | None:
        """The results of answer `line` to request `rid` (n results expected), or None when it is not one."""
        if not line:
            return None
        try:
            msg = json.loads(line)
            res = msg["results"]
        except Exception:
            return None
        if msg.get("id") != rid or not isinstance(res, list) or (n is not None and len(res) != n):
            return None
        return res

    @staticmethod
    def allowance(n: int) -> float:
        """Seconds a round trip of n calcs may take (before any decision deadline)."""
        return min(CALC_TIMEOUT_S, CALC_BASE_S + CALC_PER_CALC_S * max(0, n))

    def _answered(self, real: bool) -> None:
        """A good answer arrived. Only an answer to a real request (not the warm-up calc) ends a run of consecutive
        failures: a node that loads fine and then dies on every real batch must still back off between restarts."""
        if real:
            self.failures = 0
        if not self.ready:
            self.ready = True
            if self._spawned_at is not None:
                self.startup_s = time.monotonic() - self._spawned_at

    def _drain(self, end: float) -> bool:
        """Collect the answer the process still owes (a timed-out request, or its warm-up), waiting until `end` at
        most. True when the process is free; a process that owes it for too long is hung: killed and restarted."""
        q, rid, sent, allow = self._pending
        limit = COLD_START_S if allow is None else STALL_FACTOR * allow
        line = self._wait(q, min(end, sent + limit))
        if line is _TIMEOUT:
            age = time.monotonic() - sent
            if age >= limit:
                self._failed(f"no answer for {age:.1f}s")
                self._start()                             # restart now (unless backing off): warm by the next call
            return False
        self._pending = None
        if self._parse(line, rid, None) is None:
            self._failed("EOF or a malformed answer" if line else "node process died")
            self._start()
            return False
        self._answered(real=allow is not None)           # (allow None: the warm-up calc)
        return True

    def _roundtrip(self, calcs: list[dict], real: bool = True) -> list | None:
        """One request / response with the node process (started if needed); None on any failure or when the
        decision's time is up (never raises, never blocks past the deadline). `real=False`: a warm-up request."""
        try:
            if not self._start() or self.proc is None:
                return None
            if self._pending is not None and \
                    not self._drain(time.monotonic() + timecontrol.cap(COLD_START_S if not self.ready else CALC_TIMEOUT_S)):
                return None
            allow = self.allowance(len(calcs))
            sent = time.monotonic()
            end = sent + timecontrol.cap(allow)
            if end - sent < MIN_ROUNDTRIP_S:
                return None
            self._id += 1
            rid = self._id
            q = self._send(json.dumps({"id": rid, "calcs": calcs}))
            line = self._wait(q, end)
            if line is _TIMEOUT:
                self.timeouts += 1
                self._pending = (q, rid, sent, allow)
                log.warning("damage calc: no answer to %d calcs in %.2fs; heuristic for this decision", len(calcs),
                            time.monotonic() - sent)
                return None
            res = self._parse(line, rid, len(calcs))
            if res is None:
                self._failed("EOF or a malformed answer" if line else "node process died")
                self._start()
                return None
            self._answered(real)
            took = time.monotonic() - sent
            if took > self.slowest[0]:
                self.slowest = (round(took, 3), len(calcs))
            if took / allow > self.tightest[0]:
                self.tightest = (round(took / allow, 3), len(calcs))
            return res
        except Exception as e:                           # never let the calculator take a decision down
            log.warning("damage calc round trip failed: %r", e)
            try:
                self._failed(f"round trip raised {e!r}")
            except Exception:
                pass
            return None

    def warm(self) -> bool:
        """Start node and load the calculator now with one dummy calc, so the next real batch is a plain round trip.
        Idempotent (a no-op while the process is up and has answered); False when the calculator is unavailable or
        still starting when the decision's time is up. The dummy answer never enters the cache (batch()'s
        clear-before-insert cache order is untouched)."""
        if not self.enabled:
            return False
        if self.ready and self.proc is not None and self.proc.poll() is None:
            return True
        try:
            if not self._start() or self.proc is None:
                return False
            if self._pending is not None:
                return self._drain(time.monotonic() + timecontrol.cap(COLD_START_S))
            return self._roundtrip([WARM_CALC], real=False) is not None
        except Exception as e:
            log.warning("damage calc warm-up failed: %r", e)
            return False

    def batch(self, calcs: list[dict]) -> list[dict | None] | None:
        """Each calc: {attacker, defender, move, field?}. Returns per-calc dicts (or None on per-calc error)."""
        if not calcs:
            return []
        keys = [json.dumps(c, sort_keys=True) for c in calcs]
        # This batch's answers are collected here, not re-read from the cache: the old code cleared an overflowing
        # cache AFTER inserting the batch and then read the batch back from the empty cache, so every calc of that
        # decision (a battle turn, the lookahead's table, a whole team-preview batch) silently came back None.
        got = {k: self.cache[k] for k in keys if k in self.cache}
        todo = [(i, k) for i, k in enumerate(keys) if k not in got]
        if todo:
            res = self._roundtrip([calcs[i] for i, _ in todo])
            if res is None:
                return None
            if len(self.cache) + len(todo) > CACHE_MAX:
                self.cache.clear()                       # clear BEFORE inserting the current batch
            for (i, k), r in zip(todo, res):
                self.cache[k] = r
                got[k] = r
        out = []
        for k in keys:
            r = got.get(k)
            out.append(None if not r or "error" in r else r)
        return out


# ---------------------------------------------------------------- building calc inputs from platform data
# Consumed items whose effect stays while the holder is in: the calculator only applies Protosynthesis / Quark Drive's
# Booster Energy boost while it sees the item, so a spent Booster Energy is still passed on (main's behaviour).
_EFFECT_OUTLIVES_ITEM = frozenset({"boosterenergy"})
# Major statuses @smogon/calc understands. The platform sends them upper case ("BRN", "PAR", ...), the local sim lower
# case; calc.js silently ignores anything else, so a platform burn used to leave physical damage un-halved.
CALC_STATUSES = frozenset({"brn", "par", "psn", "tox", "slp", "frz"})


def view_item(mon: dict | None, card: dict | None):
    """The item a battle view's Pokémon holds, for every model that reads items (calc input, lookahead, Sash checks).

    The platform hides a foe's item ("unknown_item") until revealed: the drafted card's item then. A view that
    explicitly shows an empty slot (foe None / ours "": the platform clears it on |-enditem|) has lost the card's
    item: a used Focus Sash, a knocked-off Loaded Dice (else still scored at 4-5 hits), a popped Air Balloon, a spent
    Electric Seed. It is not brought back, except an item whose effect outlives it (Booster Energy)."""
    mon = mon or {}
    item = mon.get("item")
    if not item or item == "unknown_item":
        card_item = (card or {}).get("item")
        gone = "item" in mon and item in (None, "")
        if not gone or to_id(card_item or "") in _EFFECT_OUTLIVES_ITEM:
            item = card_item
    return item if item and item != "unknown_item" else None


def calc_status(status) -> str | None:
    """A view's status as the calculator's id ("BRN" -> "brn"); None for no / unknown status (e.g. "fnt")."""
    s = to_id(str(status or ""))
    return s if s in CALC_STATUSES else None


def calc_pokemon(mon: dict | None, card: dict | None, species: str | None = None,
                 allies_fainted: int | None = None) -> dict:
    """Merge a battle-state Pokémon view with its drafted card into a calculator input.

    `allies_fainted`: the side's fainted count Supreme Overlord locked in when this Pokémon came in (only passed
    on for a Supreme Overlord holder)."""
    mon = mon or {}
    sp = species or mon.get("species") or (card or {}).get("species") or ""
    entry = dex.species(sp)
    out = {"species": entry.get("name") or sp, "level": (card or {}).get("level", 50)}
    # The platform hides a foe's item ("unknown_item") and ability (None) until revealed: use its drafted card then
    # (an item the view shows as gone stays gone, see view_item).
    item = view_item(mon, card)
    if item:
        out["item"] = dex.item_name(item)
    ability = mon.get("ability") or (card or {}).get("ability")
    if ability:
        out["ability"] = dex.ability_name(ability)
    if allies_fainted and to_id(ability or "") == "supremeoverlord":
        out["alliesFainted"] = max(0, min(5, int(allies_fainted)))
    if card:
        out["nature"] = card.get("nature", "Hardy")
        out["evs"] = card.get("evs") or {}
        out["ivs"] = card.get("ivs") or {}
    boosts = {k: v for k, v in (mon.get("boosts") or {}).items() if k in ("atk", "def", "spa", "spd", "spe") and v}
    if boosts:
        out["boosts"] = boosts
    frac = mon.get("current_hp_fraction")
    if frac is None and mon.get("max_hp"):
        frac = (mon.get("current_hp") or 0) / mon["max_hp"]
    if frac is not None:
        # Fraction, not raw HP: the platform reports foe HP out of 100, ours in real HP; calc.js rescales.
        out["hpFraction"] = round(float(frac), 4)
    status = calc_status(mon.get("status"))
    if status:
        out["status"] = status                   # lower case: calc.js ignores the platform's "BRN" / "PAR"
    return out


def calc_field(obs: dict, attacker_is_me: bool) -> dict:
    ours = {to_id(k) for k in (obs.get("side_conditions") or {})}
    theirs = {to_id(k) for k in (obs.get("opponent_side_conditions") or {})}
    my_side = {"isTailwind": "tailwind" in ours, "isReflect": "reflect" in ours, "isLightScreen": "lightscreen" in ours,
               "isAuroraVeil": "auroraveil" in ours}
    their_side = {"isTailwind": "tailwind" in theirs, "isReflect": "reflect" in theirs,
                  "isLightScreen": "lightscreen" in theirs, "isAuroraVeil": "auroraveil" in theirs}
    weather = obs.get("weather")
    weather = next(iter(weather), None) if isinstance(weather, dict) else weather
    fields = obs.get("fields")
    fields = list(fields) if isinstance(fields, dict) else (fields or [])
    terrain = next((f for f in fields if "terrain" in to_id(str(f))), None)
    return {"gameType": "Doubles" if obs.get("is_doubles") else "Singles",
            "weather": _weather_name(weather), "terrain": _terrain_name(terrain),
            "attackerSide": my_side if attacker_is_me else their_side,
            "defenderSide": their_side if attacker_is_me else my_side}


def _weather_name(w) -> str | None:
    w = to_id(str(w or ""))
    return {"sunnyday": "Sun", "sun": "Sun", "raindance": "Rain", "rain": "Rain", "sandstorm": "Sand", "sand": "Sand",
            "snow": "Snow", "hail": "Hail", "desolateland": "Harsh Sunshine", "primordialsea": "Heavy Rain"}.get(w)


def _terrain_name(t) -> str | None:
    t = to_id(str(t or ""))
    for name in ("electric", "grassy", "psychic", "misty"):
        if name in t:
            return name.capitalize()
    return None


def move_name(move_id: str) -> str:
    return dex.move(move_id).get("name") or move_id
