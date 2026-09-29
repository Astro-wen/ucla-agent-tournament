"""`vgcbench_bc` / `vgcbench_sp`: the open-source VGC-Bench policies (Angliss et al., AAMAS; MIT) as panel opponents.

Why: every other sparring partner is our own code, a scripted bot, or simbot (an engine search we wrote). VGC-Bench is
a LEARNED outside bot: behaviour cloning on Showdown ladder logs with Open Team Sheets (`vgcbench_bc`, checkpoint
saves_bc/seed1/100.zip) and that model fine-tuned by self-play (`vgcbench_sp`, saves_bc_sp/reg_all/seed1/
98304000.zip), i.e. what a competitor could download and enter.

Opt-in and optional: it needs the VGC-Bench install (their Scarlet/Violet release + its own venv with torch and
poke-env + the checkpoints; recipe in docs/local_sim.md). Without it `available()` says why, local.panel refuses
these names with that message and the tests skip. Nothing else imports this module.

How it plays (local/vgcbench_worker.py runs their code in their venv, one worker per panel process, reused across
games, both checkpoints loaded once, lazily):
  draft         the CHAMPION's code (champion/agent), exactly like simbot, so a normal-draft match measures preview
                and battle play on equally well drafted teams.
  team preview  their policy (both checkpoints choose on team preview). The env asks for the lineup before the live
                Showdown battle exists, so the bridge builds a throwaway battle with the same two teams and the live
                battle's seed (peeked from the match RNG without drawing from it, so the random genders in the
                header are the live battle's) and returns this side's view of its team-preview phase plus the Open
                Team Sheets lines; the policy's `/team abcd` becomes the platform's select_lineup.
  battle        each decision: the live battle's protocol lines since the last call resolved for this side's
                channel (bridge `sideview`: no omniscient lines, no opponent request) and this side's request go to
                the worker, which answers VGC-Bench's Showdown choice string; it is converted to the platform's
                doubles_turn dict (move id / Showdown target numbers = the template's target values / switch name
                -> species) and checked with ext_opp.template_ok.
Anything that goes wrong (worker start-up or crash: one restart, then fallback; an answer outside the template)
plays a random legal action and is COUNTED (stats). Also counted: vocab ids VGC-Bench does not know (mapped to its
null entry), gimmicks stripped (Terastal Clause: never expected, the request never offers tera), targets
normalised to what Showdown would do anyway (a single-target move aimed at a fainted foe's slot goes to the other
foe; a locked move ignores its target), and a header mismatch between the throwaway preview battle and the live one.

Environment: VGCBENCH_HOME (default /mnt/d/altruagent; holds vgc-bench-sv/, venv/, models-sv/) or VGCBENCH_CODE /
VGCBENCH_PYTHON / VGCBENCH_MODELS one by one; VGCBENCH_DETERMINISTIC=1 plays the argmax instead of sampling (their
play.py samples); VGCBENCH_THREADS (torch threads per worker, default 1); VGCBENCH_DEBUG=1 keeps the worker's view of
every decision in the trace (for scripts/vgcbench_check.py).
"""
from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import random
import re
import select
import subprocess
import time
from pathlib import Path

from .ext_opp import template_ok
from .pokemon_env import ContestantPlayer, Player, lineup_ok, random_action

log = logging.getLogger("local.vgcbench")

NAMES = ("vgcbench_bc", "vgcbench_sp")
CHECKPOINTS = {"vgcbench_bc": "results/saves_bc/seed1/100.zip",
               "vgcbench_sp": "results/saves_bc_sp/reg_all/seed1/98304000.zip"}
DRAFTER = "champion.agent"
WORKER = Path(__file__).resolve().parent / "vgcbench_worker.py"
DEFAULT_HOME = "/mnt/d/altruagent"
STAT_KEYS = ("decisions", "fallbacks", "errors", "sims", "ms_total", "ms_max")
EXTRA_KEYS = ("preview_fallbacks", "restarts", "vocab_lookups", "vocab_unknown", "unknown_decisions",
              "retargeted", "gimmicks", "header_mismatch", "policy_ms")
START_TIMEOUT = float(os.environ.get("VGCBENCH_START_TIMEOUT", "900"))   # cold imports from drvfs D: take ~1 min
CALL_TIMEOUT = float(os.environ.get("VGCBENCH_CALL_TIMEOUT", "120"))
MAX_RESTARTS = 5                          # per panel process; after that the worker stays down (every answer falls back)
GIMMICKS = ("mega", "zmove", "dynamax", "max", "terastallize", "tera")


def paths(environ=None) -> dict:
    env = os.environ if environ is None else environ
    home = Path(env.get("VGCBENCH_HOME", DEFAULT_HOME))
    return {"code": Path(env.get("VGCBENCH_CODE", home / "vgc-bench-sv")),
            "python": Path(env.get("VGCBENCH_PYTHON", home / "venv" / "bin" / "python")),
            "models": Path(env.get("VGCBENCH_MODELS", home / "models-sv"))}


def available(environ=None) -> tuple[bool, str]:
    """Is the VGC-Bench install there? (ok, reason)."""
    p = paths(environ)
    need = [p["python"], p["code"] / "vgc_bench" / "src" / "policy_player.py", p["code"] / "data" / "moves.json"]
    need += [p["models"] / c for c in CHECKPOINTS.values()]
    missing = [str(x) for x in need if not x.exists()]
    if missing:
        return False, ("VGC-Bench is not installed here (missing " + ", ".join(missing[:3])
                       + "); see docs/local_sim.md for the install recipe or set VGCBENCH_HOME")
    return True, "ok"


def _sid(x) -> str:
    return re.sub(r"[^a-z0-9]", "", str(x).lower())


def _no_t(lines) -> list[str]:
    """Protocol lines without the |t:| timestamps (wall-clock, differ between two battles built a moment apart)."""
    return [x for x in lines or [] if not x.startswith("|t:|")]


# ------------------------------------------------------------------------------------------ the worker process
class WorkerError(RuntimeError):
    pass


class Worker:
    """One local/vgcbench_worker.py process (JSON lines). Any failure raises WorkerError and kills the process."""

    def __init__(self, environ=None):
        p = paths(environ)
        threads = str(int(os.environ.get("VGCBENCH_THREADS", "1")))
        env = {**os.environ, "OMP_NUM_THREADS": threads, "MKL_NUM_THREADS": threads, "PYTHONHASHSEED": "0"}
        env.pop("PYTHONPATH", None)          # their venv only: our repo must not be importable from the worker
        err = os.environ.get("VGCBENCH_WORKER_STDERR")
        self.proc = subprocess.Popen([str(p["python"]), "-u", str(WORKER), "--vgcbench-root", str(p["code"]),
                                      "--threads", threads],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=open(err, "a") if err else subprocess.DEVNULL,
                                     text=True, bufsize=1, env=env, cwd=str(p["code"]))
        hello = self._read(START_TIMEOUT)
        if not hello.get("ready"):
            self.kill()
            raise WorkerError(f"worker failed to start: {hello.get('error')}")
        self.pid = hello.get("pid")

    def _read(self, timeout: float) -> dict:
        r, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not r:
            self.kill()
            raise WorkerError(f"worker timed out after {timeout:.0f} s")
        line = self.proc.stdout.readline()
        if not line:
            code = self.proc.poll()
            self.kill()
            raise WorkerError(f"worker died (exit {code})")
        return json.loads(line)

    def call(self, msg: dict, timeout: float = CALL_TIMEOUT) -> dict:
        if self.proc.poll() is not None:
            raise WorkerError(f"worker is dead (exit {self.proc.returncode})")
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            self.kill()
            raise WorkerError(f"worker pipe: {e}") from e
        res = self._read(timeout)
        if "error" in res:
            raise WorkerError(f"worker: {res['error']} {str(res.get('trace', ''))[-600:]}")
        return res

    def alive(self) -> bool:
        return self.proc.poll() is None

    def kill(self) -> None:
        try:
            self.proc.kill()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass


_W: dict = {"worker": None, "restarts": 0, "failed": None}


def worker(stats: dict | None = None) -> Worker:
    """This process's shared worker: started on first use; a dead one is restarted (at most MAX_RESTARTS times per
    process, each counted in stats["restarts"]); after that WorkerError every time."""
    w = _W["worker"]
    if w is not None and w.alive():
        return w
    if w is not None:
        if _W["restarts"] >= MAX_RESTARTS:
            raise WorkerError(f"worker down for good after {MAX_RESTARTS} restarts: {_W['failed']}")
        _W["restarts"] += 1
        if stats is not None:
            stats["restarts"] += 1
    elif _W["failed"] is not None and _W["restarts"] >= MAX_RESTARTS:
        raise WorkerError(f"worker down for good: {_W['failed']}")
    _W["worker"] = None
    try:
        _W["worker"] = Worker()
    except WorkerError as e:
        _W["failed"] = str(e)
        _W["worker"] = _Dead()
        raise
    return _W["worker"]


class _Dead:
    """Placeholder for a worker that failed to start (so the next call counts as a restart)."""

    def alive(self) -> bool:
        return False

    def kill(self) -> None:
        pass


def shutdown() -> None:
    w = _W.get("worker")
    if w is not None:
        try:
            if w.alive():
                w.call({"cmd": "quit"}, timeout=2)
        except Exception:  # noqa: BLE001
            pass
        w.kill()
    _W["worker"] = None


# ------------------------------------------------------------------------------------------ choice conversion
def team_order(choice: str) -> list[int]:
    """'/team 3142' -> [3, 1, 4, 2] (1-based indices into the roster, leads first)."""
    m = re.fullmatch(r"/team\s+(\d{4})", (choice or "").strip())
    if not m:
        raise ValueError(f"not a 4-Pokemon team order: {choice!r}")
    order = [int(c) for c in m.group(1)]
    if len(set(order)) != 4 or not all(1 <= i <= 6 for i in order):
        raise ValueError(f"bad team order {choice!r}")
    return order


def lineup_from_order(order: list[int], ids: list[str]) -> dict:
    bring = [ids[i - 1] for i in order]
    return {"type": "select_lineup", "bring": bring, "leads": bring[:2]}


def choice_to_turn(choice: str, req: dict, raw: dict, stats: dict | None = None) -> dict:
    """VGC-Bench's Showdown choice ('/choose move closecombat 1, switch Incineroar') -> the platform's doubles_turn
    dict for this request / template. Showdown's target numbers ARE the template's target values (1 / 2 = foe
    slots, -1 / -2 = our slots, 0 = none). Normalised (counted in stats["retargeted"]): a target the template does
    not list where Showdown would act the same anyway - a locked move (template [0]) ignores its target; a
    single-target move at an empty / fainted foe slot hits the one foe left. Gimmick words are dropped (counted).
    Raises ValueError on anything else."""
    stats = stats if stats is not None else {}
    s = (choice or "").strip()
    if not s.startswith("/choose "):
        raise ValueError(f"not a choice: {choice!r}")
    s = s[len("/choose "):].strip()
    if s == "default":
        raise ValueError("'default' choice")
    parts = [x.strip() for x in s.split(",")]
    slots = raw["legal_actions"]["actions"][0]["input"]["action"]["slots"]
    if len(parts) != len(slots):
        raise ValueError(f"{len(parts)} orders for {len(slots)} slots: {choice!r}")
    side = req["side"]["pokemon"]
    out = {"type": "doubles_turn"}
    for part, slot in zip(parts, slots):
        i = slot["slot"]
        toks = part.split()
        if not toks:
            raise ValueError(f"empty order in {choice!r}")
        if toks[0] == "pass":
            out[f"slot_{i}"] = {"type": "pass"}
        elif toks[0] == "switch":
            name = " ".join(toks[1:])
            mon = next((p for p in side if p["ident"].split(": ", 1)[-1] == name), None)
            if mon is None:
                mon = next((p for p in side if _sid(p["details"].split(",")[0]) == _sid(name)), None)
            if mon is None:
                raise ValueError(f"switch target {name!r} not in our side")
            out[f"slot_{i}"] = {"type": "switch", "species": _sid(mon["details"].split(",")[0])}
        elif toks[0] == "move":
            rest = toks[1:]
            if not rest:
                raise ValueError(f"move without id in {choice!r}")
            gim = [t for t in rest[1:] if t in GIMMICKS]
            if gim:
                stats["gimmicks"] = stats.get("gimmicks", 0) + len(gim)
            rest = [t for t in rest if t not in GIMMICKS]
            target = 0
            if len(rest) >= 2 and re.fullmatch(r"[+-]?\d+", rest[-1]):
                target = int(rest[-1])
                rest = rest[:-1]
            mid = "".join(rest)
            if re.fullmatch(r"\d+", mid):                    # 'move 1' (poke-env's recharge): an index
                moves = (req.get("active") or [{}] * (i + 1))[i].get("moves") or []
                k = int(mid) - 1
                if not 0 <= k < len(moves):
                    raise ValueError(f"move index {mid} out of range in {choice!r}")
                mid = moves[k]["id"]
            mid = _sid(mid)
            opt = next((o for o in slot["options"] if o["type"] == "move" and o["move_id"] == mid), None)
            if opt is None:
                raise ValueError(f"move {mid!r} not offered for slot {i}")
            if target not in opt["targets"]:
                foes = [t for t in opt["targets"] if t > 0]
                if opt["targets"] == [0]:
                    target = 0                               # locked / no-target move: Showdown ignores the target
                elif target > 0 and len(foes) == 1:
                    target = foes[0]                         # the other foe slot is empty / fainted: Showdown retargets
                else:
                    raise ValueError(f"target {target} not allowed for {mid} (template {opt['targets']})")
                stats["retargeted"] = stats.get("retargeted", 0) + 1
            out[f"slot_{i}"] = {"type": "move", "move_id": mid, "target": target}
        else:
            raise ValueError(f"unknown order {part!r}")
    return out


# ------------------------------------------------------------------------------------------ the player
class VGCBench(Player):
    """A VGC-Bench checkpoint as a panel opponent. Needs Match.bind (the live bridge and its seat)."""

    def __init__(self, name: str = "vgcbench_sp", environ=None):
        if name not in CHECKPOINTS:
            raise ValueError(f"unknown VGC-Bench variant {name!r}; known: {NAMES}")
        ok, why = available(environ)
        if not ok:
            raise RuntimeError(why)
        self.name = name
        self.model = str(paths(environ)["models"] / CHECKPOINTS[name])
        self.deterministic = os.environ.get("VGCBENCH_DETERMINISTIC", "") not in ("", "0")
        self.debug = os.environ.get("VGCBENCH_DEBUG", "") not in ("", "0")
        self.params = {"model": self.model, "deterministic": self.deterministic}
        self.calc_pkg = DRAFTER
        mod = importlib.import_module(f"{DRAFTER}.agent")
        self.inner = ContestantPlayer(mod.create_agent, f"{name}-drafter")
        self.match = None
        self.sid = None
        self.rng = random.Random(0)
        self.stats: dict = dict.fromkeys(STAT_KEYS + EXTRA_KEYS, 0)
        self.trace: list[dict] = []
        self.game = None              # this match's game id on the worker (None: not opened / lost with a worker)
        self.pos = None               # live battle log index up to which the worker has this side's lines
        self.preview = None           # the throwaway battle's side view (kept for a mid-game worker restart)
        self.order = None             # the team order we played (1-based roster indices, leads first)
        self._n = 0
        self.hook = None              # scripts/vgcbench_check.py: called with (player, raw, request, result, out)

    @property
    def agent(self):
        return None

    def start(self) -> dict:
        """Start the worker and load this checkpoint now (outside any decision): {"ms", "choose_on_teampreview"}."""
        return worker(self.stats).call({"cmd": "load", "model": self.model}, timeout=START_TIMEOUT)

    def reset(self) -> None:
        self.inner.reset()
        self.stats = dict.fromkeys(STAT_KEYS + EXTRA_KEYS, 0)
        self.stats["ms_max"] = 0.0
        self.trace = []
        self._close_game()
        self.pos = self.preview = self.order = None

    def bind(self, match, seat: int) -> None:
        """Called by Match.__init__: the live bridge, our side, and a per-match RNG derived from (not drawn from) the
        match's random state, so the match's own random stream is untouched."""
        self.match, self.sid = match, ("p1" if seat == 0 else "p2")
        h = hashlib.sha256(repr(match.rng.getstate()).encode()).hexdigest()
        self.rng = random.Random(int(h[:16], 16) ^ (seat + 1))

    # ---- worker plumbing ----------------------------------------------------------------------------------------
    def _close_game(self) -> None:
        if self.game is not None:
            w = _W.get("worker")
            if w is not None and w.alive():
                try:
                    w.call({"cmd": "close", "game": self.game}, timeout=10)
                except Exception:  # noqa: BLE001
                    pass
        self.game = None

    def _open(self, w: Worker) -> None:
        self._n += 1
        self.game = f"{os.getpid()}-{id(self)}-{self._n}"
        w.call({"cmd": "open", "game": self.game, "model": self.model, "side": self.sid,
                "deterministic": self.deterministic}, timeout=START_TIMEOUT)

    def _feed(self, w: Worker, lines, request, decide: bool) -> dict:
        res = w.call({"cmd": "feed", "game": self.game, "lines": lines, "request": request,
                      "seed": self.rng.randrange(1, 2 ** 31), "debug": self.debug})
        voc = res.get("vocab") or {}
        self.stats["vocab_lookups"] += int(voc.get("lookups", 0))
        self.stats["vocab_unknown"] += int(voc.get("unknown", 0))
        if decide and voc.get("unknown"):
            self.stats["unknown_decisions"] += 1
        self.stats["policy_ms"] += float(res.get("ms") or 0)
        return res

    def _with_worker(self, fn):
        """fn(worker) with one restart: a worker that dies or times out is restarted once for this call (the game
        is then rebuilt on the new worker, see _resync). A second failure raises (the caller falls back)."""
        try:
            return fn(worker(self.stats))
        except WorkerError as e:
            self.stats["errors"] += 1
            log.warning("%s worker failed (%s); restarting once", self.name, e)
            w = _W.get("worker")
            if w is not None:
                w.kill()
            self.game = None
            try:
                return fn(worker(self.stats))
            except WorkerError:
                self.stats["errors"] += 1
                self.game = None
                raise

    # ---- decisions -------------------------------------------------------------------------------------------------
    def choose(self, raw: dict):
        phase = raw.get("phase")
        if phase == "draft":
            return self.inner.choose(raw)
        if phase == "team_preview":
            return self._team_preview(raw)
        return self._battle(raw)

    def _team_preview(self, raw: dict):
        from .pool import to_showdown_set
        t0 = time.perf_counter()
        ids = [p["species"] for p in raw["observation"]["your_roster"]]
        info: dict = {}
        try:
            teams = [[to_showdown_set(c) for c in r] for r in self.match.rosters]
            peek = random.Random()
            peek.setstate(self.match.rng.getstate())          # the live battle's seed, without drawing from the rng
            seed = [peek.randrange(1, 65535) for _ in range(4)]
            pv = self.match.sd.preview_view(self.sid, teams[0], teams[1], seed)
            if "error" in pv:
                raise RuntimeError(f"preview_view: {pv.get('error')} {str(pv.get('detail'))[:300]}")
            self.preview = pv

            def go(w):
                self._close_game()
                self._open(w)
                self._feed(w, self.preview["lines"], self.preview["request"], decide=False)
                return self._feed(w, self.preview["showteam"], None, decide=True)

            res = self._with_worker(go)
            info = {"choice": res.get("choice"), "actions": res.get("actions"), "policy_ms": res.get("ms")}
            order = team_order(res.get("choice") or "")
            out = lineup_from_order(order, ids)
            if not lineup_ok(out, ids):
                raise RuntimeError(f"lineup {out} not legal")
            self.order = order
        except Exception as e:  # noqa: BLE001 - never crash the seat: a random legal lineup, counted
            self.stats["fallbacks"] += 1
            self.stats["preview_fallbacks"] += 1
            log.warning("%s team-preview fallback: %s", self.name, e)
            out = random_action(raw)
            self.order = [ids.index(s) + 1 for s in out["leads"]] + \
                         [ids.index(s) + 1 for s in out["bring"] if s not in out["leads"]]
            self._close_game()                                # the battle rebuilds it with this lineup (_resync)
            info = {"fallback": str(e)[:300]}
        ms = 1000 * (time.perf_counter() - t0)
        self.trace.append({"phase": "team_preview", "ms": round(ms, 1), **info})
        return out

    def _battle(self, raw: dict):
        t0 = time.perf_counter()
        self.stats["decisions"] += 1
        out, info = None, {}
        try:
            prev = self.pos
            sd = self.match.sd
            if prev is None:                                  # first battle decision: the team-preview header was
                sv0 = sd.sideview(self.sid, 0)                # already fed from the throwaway preview battle
                if "error" in sv0:
                    raise RuntimeError(f"sideview: {sv0.get('error')}")
                sv = sd.sideview(self.sid, sv0["header_end"])
                header = sv0["lines"][:len(sv0["lines"]) - len(sv["lines"])]
                if self.preview is not None and _no_t(header) != _no_t(self.preview["lines"]):
                    self.stats["header_mismatch"] += 1
            else:
                sv = sd.sideview(self.sid, prev)
            if "error" in sv:
                raise RuntimeError(f"sideview: {sv.get('error')}")
            lines, req = sv["lines"], sv["request"]

            def go(w):
                if self.game is None:                         # fresh or restarted worker: rebuild this game's view
                    self._resync(w, prev)
                r = self._feed(w, lines, req, decide=True)
                self.pos = sv["pos"]                          # the worker has these lines now
                return r

            res = self._with_worker(go)
            info = {"choice": res.get("choice"), "actions": res.get("actions"), "policy_ms": res.get("ms")}
            if self.debug:
                info.update(view=res.get("view"), masks=res.get("masks"), request=req)
            out = choice_to_turn(res.get("choice") or "", req, raw, self.stats)
            if not template_ok(raw, out):
                raise RuntimeError(f"answer {res.get('choice')!r} -> {out} not in the template")
            if self.hook is not None:
                self.hook(self, raw, req, res, out)
        except Exception as e:  # noqa: BLE001 - never crash the seat: a random legal action, counted
            self.stats["fallbacks"] += 1
            log.warning("%s fallback (turn %s): %s", self.name, (raw.get("observation") or {}).get("turn"), e)
            out = random_action(raw)
            info = {**info, "fallback": str(e)[:300]}
        ms = 1000 * (time.perf_counter() - t0)
        self.stats["ms_total"] += ms
        self.stats["ms_max"] = max(self.stats["ms_max"], ms)
        self.trace.append({"phase": "moving", "turn": (raw.get("observation") or {}).get("turn"), "ms": round(ms, 1),
                           "out": out, **info})
        return out

    def _resync(self, w: Worker, upto: int | None) -> None:
        """A worker that lost this game (restart, or a team preview that fell back before the game was opened):
        replay what this side has seen - the preview battle's header, request and Open Team Sheets (or, without
        them, the live header), the lineup actually played - then the live lines up to log index `upto` (the caller
        feeds the rest)."""
        sd = self.match.sd
        self._open(w)
        allv = sd.sideview(self.sid, 0)
        body = sd.sideview(self.sid, allv["header_end"])
        if self.preview is not None and "lines" in self.preview:
            self._feed(w, self.preview["lines"], self.preview["request"], decide=False)
            self._feed(w, self.preview["showteam"], None, decide=False)
        else:
            self._feed(w, allv["lines"][:len(allv["lines"]) - len(body["lines"])], None, decide=False)
        if self.order:
            w.call({"cmd": "select", "game": self.game, "order": list(self.order)})
        if upto is not None:
            rest = sd.sideview(self.sid, upto)
            seen = body["lines"][:len(body["lines"]) - len(rest["lines"])]
            if seen:
                self._feed(w, seen, None, decide=False)


def make(name: str) -> VGCBench:
    return VGCBench(name)
