"""Per-match memory that survives a restart of the match's worker process.

The official runtime (`python -m agent --tournament`) and the discovery supervisor play every match in its own process
with its own fresh `create_agent()` instance. When that worker dies mid-match (a platform error it does not retry, an
OOM kill, a restart of the whole runtime, INVALID_ACTION), a NEW worker resumes the match with an empty Contestant: the
opponent's drafted sets (visible only during the draft), our own sets, the team-preview game's prior over the foe's
four, the battle trackers (Fake Out freshness, Protect chains, speed-control turns) and the Werewolf transcript
(`new_messages` only covers the current discussion window) would all be gone. So the Contestant saves that memory here
after every decision, and a fresh Contestant reloads it at its first decision of the match (agent/agent.py):

  * one JSON file per match and seat, keyed by (agent id, session id, our seat when known), in a private directory
    (0700, files 0600): ARENA_STATE_DIR, else <ARENA_RECORD_DIR or logs>/state, else <tmp>/arena-agent-<uid>/state
    when that one is not writable. The seat keeps two seats of the same agent in one match apart (self-play over
    claimed Testing seats, one process per seat): without it, a seat's first decision would take over the memory the
    other seat saved (its chat quota and queue, its private messages);
  * atomic: written to a temporary file in the same directory, then renamed over the old one, so a worker killed
    mid-write leaves the previous state intact;
  * no secrets: game memory only (cards, species, our choices, the chat we could read), never the environment;
  * bounded: a state larger than MAX_BYTES is not written (the previous file stays; logged once);
  * cleaned up: the platform never tells an agent that its match ended, so the first store use of every process
    removes files older than TTL_S and keeps at most MAX_FILES (newest first). Only the store's own files (named like
    `key()` makes them) are ever removed, so a state directory set to a shared place (ARENA_STATE_DIR=data) loses
    nothing else.
It is on where decisions are recorded (ARENA_RECORD != 0: the deployed agent) and off for local simulators, replays and
tests, which set ARENA_RECORD=0; ARENA_STATE=1 / 0 overrides either way.

The same directory holds `health/<key>.json`: the live battle-decision counters of every match (agent/health.py reads
them for run_agent.sh's health line).

Values keep their Python types through JSON (`encode` / `decode`): tuples, sets and dicts with non-string keys are
tagged, so a reloaded policy holds exactly what the one that saved it held.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path

log = logging.getLogger("agent.matchstate")

VERSION = 1
MAX_BYTES = 1_000_000        # one match's state (a VGC match: ~20-40 KB; a long Werewolf game: < 150 KB)
TTL_S = 3 * 3600.0           # files untouched this long belong to finished matches
MAX_FILES = 200              # per directory (state files, and health files), newest kept
_ON = {"1", "on", "true", "yes"}
_OFF = {"0", "off", "false", "no"}
_TAGS = ("__t", "__s", "__d")
_SAFE = re.compile(r"[^A-Za-z0-9_-]")
# The store's own files: `key()` stems (<agent>--<session>[--p<seat>]) as .json, and _write_atomic's temporaries.
_OURS = re.compile(r"^(?:[A-Za-z0-9_-]+--[A-Za-z0-9_-]+\.json"
                   r"|\.[A-Za-z0-9_-]+--[A-Za-z0-9_-]+\.json\.[A-Za-z0-9_]+\.tmp)$")
_dir: Path | None = None
_dir_for: tuple | None = None
_warned: set = set()


def enabled() -> bool:
    """ARENA_STATE=1 / 0, else on exactly when decisions are recorded (ARENA_RECORD != 0)."""
    v = str(os.environ.get("ARENA_STATE") or "").strip().lower()
    if v in _ON:
        return True
    if v in _OFF:
        return False
    return os.environ.get("ARENA_RECORD", "1") != "0"


def _warn_once(key, msg: str, *args) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(msg, *args)


def _private_dir(p: Path) -> Path | None:
    """`p` created (0700) and writable, or None."""
    try:
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            if p.stat().st_uid == os.getuid():
                os.chmod(p, 0o700)
        except (AttributeError, OSError):
            pass
        probe = p / f".probe-{os.getpid()}"
        probe.write_text("")
        probe.unlink()
        return p
    except OSError:
        return None


def state_dir() -> Path | None:
    """The private state directory (see the module docstring), created on first use; None when nothing is writable.
    Every new directory is pruned once per process."""
    global _dir, _dir_for
    want = (os.environ.get("ARENA_STATE_DIR"), os.environ.get("ARENA_RECORD_DIR"))
    if _dir is not None and _dir_for == want:
        return _dir
    cands = [Path(want[0])] if want[0] else [Path(want[1] or "logs") / "state"]
    try:
        uid = os.getuid()
    except AttributeError:
        uid = "u"
    cands.append(Path(tempfile.gettempdir()) / f"arena-agent-{uid}" / "state")
    for c in cands:
        d = _private_dir(c)
        if d is not None:
            if c is not cands[0]:
                _warn_once(("dir", str(cands[0])), "match state: %s is not writable; using %s", cands[0], d)
            _dir, _dir_for = d, want
            prune(d)
            prune(d / "health")
            return d
    _warn_once(("nodir",), "match state: no writable directory (%s): a restarted worker starts its match with empty "
                           "memory", ", ".join(str(c) for c in cands))
    return None


def _seat(seat) -> int | None:
    return seat if isinstance(seat, int) and not isinstance(seat, bool) and 0 <= seat < 100 else None


def key(agent_id, session_id, seat=None) -> str | None:
    """The file stem of a match (and of our seat in it, when known): sanitised ids (a platform id never becomes a
    path), None without a session id."""
    sid = _SAFE.sub("_", str(session_id or ""))[:96]
    if not sid.strip("_"):
        return None
    aid = _SAFE.sub("_", str(agent_id or "agent"))[:64] or "agent"
    seat = _seat(seat)
    return f"{aid}--{sid}" + (f"--p{seat}" if seat is not None else "")


# ---- typed JSON ------------------------------------------------------------------------------------------------
def encode(x):
    """JSON-safe form of x that `decode` turns back into the same types (tuples, sets, non-string dict keys)."""
    if x is None or isinstance(x, (str, bool, int, float)):
        return x
    if isinstance(x, dict):
        if all(isinstance(k, str) for k in x) and not any(t in x for t in _TAGS):
            return {k: encode(v) for k, v in x.items()}
        return {"__d": [[encode(k), encode(v)] for k, v in x.items()]}
    if isinstance(x, tuple):
        return {"__t": [encode(v) for v in x]}
    if isinstance(x, (set, frozenset)):
        items = list(x)
        try:
            items = sorted(items)                    # a deterministic file (the order of a set is not part of it)
        except TypeError:
            pass
        return {"__s": [encode(v) for v in items]}
    if isinstance(x, list):
        return [encode(v) for v in x]
    raise TypeError(f"cannot keep a {type(x).__name__} in the match state")


def decode(x):
    if isinstance(x, list):
        return [decode(v) for v in x]
    if isinstance(x, dict):
        if len(x) == 1:
            (k, v), = x.items()
            if k == "__t" and isinstance(v, list):
                return tuple(decode(i) for i in v)
            if k == "__s" and isinstance(v, list):
                return {decode(i) for i in v}
            if k == "__d" and isinstance(v, list):
                return {decode(p[0]): decode(p[1]) for p in v if isinstance(p, list) and len(p) == 2}
        return {k: decode(v) for k, v in x.items()}
    return x


# ---- files -----------------------------------------------------------------------------------------------------
def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))   # 0600
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save(agent_id, session_id, blob: dict, seat=None) -> bool:
    """Write one match's state (atomically). False when it was not written (off, no directory, too big, error)."""
    try:
        stem = key(agent_id, session_id, seat)
        d = state_dir() if stem else None
        if d is None:
            return False
        text = json.dumps({"v": VERSION, **encode(blob)}, separators=(",", ":"))
        if len(text) > MAX_BYTES:
            _warn_once(("big", stem), "match state of %s is %d bytes (> %d): not saved; a restart resumes from the "
                                      "last smaller state", stem, len(text), MAX_BYTES)
            return False
        _write_atomic(d / f"{stem}.json", text)
        return True
    except Exception as e:                            # noqa: BLE001 - a decision never fails because of this
        _warn_once(("save", type(e).__name__), "match state not saved: %r", e)
        return False


def load(agent_id, session_id, seat=None) -> dict | None:
    """The saved state of a match (decoded), or None (none, unreadable, another format version)."""
    try:
        stem = key(agent_id, session_id, seat)
        d = state_dir() if stem else None
        if d is None:
            return None
        p = d / f"{stem}.json"
        if not p.is_file() or p.stat().st_size > MAX_BYTES:
            return None
        raw = json.loads(p.read_text())
        if not isinstance(raw, dict) or raw.get("v") != VERSION:
            return None
        out = decode(raw)
        out.pop("v", None)
        return out
    except Exception as e:                            # noqa: BLE001
        _warn_once(("load", type(e).__name__), "match state unreadable: %r", e)
        return None


def remove(agent_id, session_id, seat=None) -> None:
    """Forget a match (test harnesses): the files of `seat`, or of every seat when it is None."""
    try:
        stem = key(agent_id, session_id, seat)
        d = state_dir() if stem else None
        if d is not None:
            for base in (d, d / "health"):
                paths = [base / f"{stem}.json"]
                if _seat(seat) is None and base.is_dir():
                    paths += list(base.glob(f"{stem}--p*.json"))
                for p in paths:
                    if p.exists():
                        p.unlink()
    except OSError:
        pass


def prune(d: Path, now: float | None = None) -> int:
    """Remove state files older than TTL_S, then all but the newest MAX_FILES (and stale temporaries). Never raises."""
    n = 0
    try:
        if not d.is_dir():
            return 0
        now = time.time() if now is None else now
        files = []
        for p in d.iterdir():
            if not _OURS.match(p.name) or not p.is_file():   # never anything the store did not write
                continue
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if now - m > TTL_S or (p.name.endswith(".tmp") and now - m > 600):
                try:
                    p.unlink()
                    n += 1
                except OSError:
                    pass
            elif p.name.endswith(".json"):
                files.append((m, p))
        for _, p in sorted(files, reverse=True)[MAX_FILES:]:
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
    except OSError:
        pass
    return n


# ---- live health counters --------------------------------------------------------------------------------------
def write_health(agent_id, session_id, counters: dict, seat=None) -> None:
    """The match's live battle-decision counters (agent/health.py aggregates them). Never raises."""
    try:
        stem = key(agent_id, session_id, seat)
        d = state_dir() if stem else None
        if d is None:
            return
        h = d / "health"
        if not h.is_dir() and _private_dir(h) is None:
            return
        _write_atomic(h / f"{stem}.json", json.dumps({"t": time.time(), **counters}, separators=(",", ":")))
    except Exception:                                 # noqa: BLE001
        log.debug("health counters not written", exc_info=True)


def read_health(window_s: float, now: float | None = None) -> list[dict]:
    """Every match's live counters written in the last `window_s` seconds. Never raises."""
    out = []
    try:
        d = state_dir()
        h = d / "health" if d is not None else None
        if h is None or not h.is_dir():
            return []
        now = time.time() if now is None else now
        for p in h.glob("*.json"):
            try:
                if now - p.stat().st_mtime > window_s:
                    continue
                v = json.loads(p.read_text())
                if isinstance(v, dict):
                    out.append(v)
            except (OSError, ValueError):
                continue
    except Exception:                                 # noqa: BLE001
        return out
    return out


# ---- how far a match is ----------------------------------------------------------------------------------------
_PHASE_RANK = {"draft": 1, "teambuild": 1, "team_preview": 2, "moving": 3}


def _int(x, default: int = 0) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


def progress(raw, game: str) -> list:
    """How far into its match a state is (lists compare in order). "pokemon": [phase rank, state_version] (the
    platform restarts state_version at team preview and at the battle, and raises it with every decision in between);
    anything else (Werewolf): [day]."""
    raw = raw if isinstance(raw, dict) else {}
    if game == "pokemon":
        return [_PHASE_RANK.get(str(raw.get("phase") or ""), 3), _int(raw.get("state_version"))]
    gs = raw.get("game_state")
    day = _int(gs.get("day")) if isinstance(gs, dict) else 0
    if not day:
        m = re.search(r"Day\s+(\d+)\s*\|", str(raw.get("observation") or ""))
        day = int(m.group(1)) if m else 0
    return [day]


def resumable(saved: dict | None, raw, game: str, seat=None) -> bool:
    """Does a saved state apply to this decision? It must come from the same game and seat (when both are known) and
    not from a later point than this state (a real session id is unique to its match; the local simulators reuse one
    for every game, and a new game must never pick up the last one's memory), and this must not be a Pokemon draft
    before our first pick (a match's first decision)."""
    try:
        if not isinstance(saved, dict) or saved.get("game") != game:
            return False
        if _seat(seat) is not None and _seat(saved.get("seat")) is not None and saved.get("seat") != seat:
            return False
        if game == "pokemon" and isinstance(raw, dict) and raw.get("phase") == "draft":
            obs = raw.get("observation") if isinstance(raw.get("observation"), dict) else {}
            me = (raw.get("current_actor") or {}).get("agent_id") or obs.get("current_seat")
            rosters = obs.get("rosters") if isinstance(obs.get("rosters"), dict) else {}
            if not rosters.get(me):
                return False
        was = saved.get("progress")
        return isinstance(was, list) and list(was) <= progress(raw, game)
    except Exception:                                 # noqa: BLE001
        return False
