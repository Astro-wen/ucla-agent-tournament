"""Play local Red Alert matches between two agent factories through the official runner (altruagent.runner.run_game,
the deployed agent's loop) against the simulator (local/ra_sim.py world, local/ra_server.py GameAPI). Never the real
platform; nothing is recorded (ARENA_RECORD=0).

    python -m local.ra_platform --a rusher --b turtle --games 4                 # deterministic lockstep clock
    python -m local.ra_platform --a ours --b llm --games 2 --clock realtime --speed 10 --max-minutes 10
    python -m local.ra_platform --a agent.redalert.bot:create --b idle,rusher,llm,turtle --games 40 --jobs 4 \
        --json out.json                                     # (`python -m local.ra_sim` takes the same arguments)

Situations: --pacing windows (the operator's 5-second windows), --default-stance attack_anything (if the platform's
agent seats turn out to be OpenRA bot players), --place-limit N (place is `invalid` after N buildings), --lost-rate,
--latency-ticks, --cash, --fault-rate (flaky connection: calls fail with HTTP 502 / 429 and the runner retries).

Agents (--a / --b): idle | turtle | rusher | llm (local/ra_bots.py), ours (agent.agent:create_agent, the deployed
contestant), or module:attr for any factory (called with no arguments; or with seed= when it accepts one). Side A
plays seat Multi0 in even games and Multi1 in odd ones; maps cycle through --maps.

Per match: result for A, reason, game minutes, then per side: first refinery / war factory (game seconds), peak army
value, kills / deaths cost, assets, batches sent and per game second, INVALID_ACTION refusals, rejected / partial
orders, other tool errors, choose_action calls per game second, decision p99 / slowest (wall ms), seconds on low
power, the longest wait between a building being ready and placed, runner errors.
The last line sums it up: A's wins / losses / draws and score with its 95% Wilson interval.

Clocks: `lockstep` (default) is deterministic and runs as fast as the agents (a 20-minute match takes about 10 s),
but every agent gets one decision per 0.2 s view whatever its thinking time (--latency-ticks adds a fixed delay);
`realtime` runs a view every 0.2 s / --speed of wall time, so thinking time costs game time like on the platform.
"""
from __future__ import annotations

import argparse
import importlib
import inspect
import json
import os
import random
import statistics
import sys
import threading
import time
from pathlib import Path

from altruagent.models import DecisionContext
from altruagent.runner import run_game

from .ra_rules import COUNTRIES, SIDES, Rules
from .ra_server import MatchServer, SeatSession
from .ra_sim import TICKS_PER_SECOND, World

MAPS = ("sidestep", "trapped", "shadowfiend-ii")


def make_factory(spec: str):
    """A callable(seed) -> agent for --a / --b."""
    from . import ra_bots
    if spec in ra_bots.BOTS:
        return lambda seed: ra_bots.make_bot(spec, seed)
    if spec == "ours":
        spec = "agent.agent:create_agent"
    if ":" not in spec:
        raise SystemExit(f"unknown agent {spec!r}: one of {', '.join(ra_bots.STYLES)}, ours, or module:attr")
    mod, attr = spec.split(":", 1)
    fn = getattr(importlib.import_module(mod), attr)
    try:
        takes_seed = "seed" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        takes_seed = False
    return (lambda seed: fn(seed=seed)) if takes_seed else (lambda seed: fn())


class Timed:
    """Wrap a contestant: count and time every choose_action, forward on_action_result."""

    def __init__(self, agent):
        self.agent = agent
        self.calls = 0
        self.slowest = 0.0
        self.total = 0.0
        self.results = 0
        self.durations: list[float] = []
        hook = getattr(agent, "on_action_result", None)
        if callable(hook):
            self.on_action_result = self._hook

    def choose_action(self, state, context):
        t0 = time.perf_counter()
        try:
            fn = self.agent if callable(self.agent) and not hasattr(self.agent, "choose_action") \
                else self.agent.choose_action
            return fn(state, context)
        finally:
            dt = time.perf_counter() - t0
            self.calls += 1
            self.total += dt
            self.slowest = max(self.slowest, dt)
            self.durations.append(dt)

    def p99_ms(self) -> float:
        d = sorted(self.durations)
        return round(d[min(len(d) - 1, int(0.99 * len(d)))] * 1000, 1) if d else 0.0

    def _hook(self, result, context):
        self.results += 1
        return self.agent.on_action_result(result, context)


def play_match(agent_a, agent_b, *, seed: int, map_id: str, a_seat: int = 0, clock: str = "lockstep",
               speed: float = 1.0, max_minutes: float = 20.0, starting_cash: int = 5000, latency_ticks: int = 0,
               lost_rate: float = 0.0, countries=None, rules: Rules | None = None, timeout_s: float | None = None,
               names=("A", "B"), idle_s: float = 600.0, pacing: str = "realtime", default_stance: str = "defend",
               place_limit: int | None = None, fault_rate: float = 0.0) -> dict:
    """One match: agent_a in seat `a_seat`, agent_b in the other. Returns the metrics row."""
    world = World(rules, map_id, seed, starting_cash=starting_cash, countries=countries,
                  default_stance=default_stance, place_limit=place_limit)
    seats = [None, None]
    seats[a_seat], seats[1 - a_seat] = Timed(agent_a), Timed(agent_b)
    ids = [None, None]
    ids[a_seat], ids[1 - a_seat] = f"agent-{names[0]}", f"agent-{names[1]}"
    server = MatchServer(world, agent_ids=tuple(ids), names=tuple(ids), clock=clock, speed=speed,
                         max_minutes=max_minutes, latency_ticks=latency_ticks, lost_rate=lost_rate, idle_s=idle_s,
                         session_id=f"ra-sim-{seed}", pacing=pacing)
    errors: dict[int, str] = {}
    sessions = [SeatSession(server, i, fault_rate=fault_rate) for i in (0, 1)]

    def seat_main(i):
        ctx = DecisionContext(session_id=server.session_id, tournament_id=None, game_type="red_alert",
                              agent_id=ids[i], seat_position=i)
        try:
            run_game(sessions[i], ctx, seats[i], wait_seconds=0.05, sleep=lambda s: time.sleep(min(s, 0.2)))
        except BaseException as e:  # noqa: BLE001 - reported in the row
            errors[i] = f"{type(e).__name__}: {e}"[:300]
        finally:
            server.seat_done(i)

    t0 = time.monotonic()
    world_thread = threading.Thread(target=server.run, name="ra-world", daemon=True)
    world_thread.start()
    threads = [threading.Thread(target=seat_main, args=(i,), name=f"ra-seat-{i}", daemon=True) for i in (0, 1)]
    for t in threads:
        t.start()
    if timeout_s is None:
        timeout_s = max_minutes * 60 / (speed if clock == "realtime" else 1.0) * 3 + 120
    end = t0 + timeout_s
    for t in threads:
        t.join(max(0.0, end - time.monotonic()))
    hung = [i for i, t in enumerate(threads) if t.is_alive()]
    server.stop()
    world_thread.join(5)
    wall = time.monotonic() - t0
    return _row(server, world, seats, ids, a_seat, errors, hung, wall, map_id, seed, sessions)


def _row(server, world, seats, ids, a_seat, errors, hung, wall, map_id, seed, sessions=None) -> dict:
    game_s = max(0, world.tick - server.start_tick) / TICKS_PER_SECOND if server.started_wall else 0.0
    if server.failure:
        res_a = "no_result"
    elif server.winner is None:
        res_a = "draw" if server.terminal else "unfinished"
    else:
        res_a = "win" if server.winner == a_seat else "loss"

    def side(seat):
        p = world.players[seat]
        st = server.seats[seat].st
        t = seats[seat]
        first = {k: (round((v - server.start_tick) / TICKS_PER_SECOND, 1) if v is not None else None)
                 for k, v in (("proc", p.first_built.get("proc")), ("weap", p.first_built.get("weap")))}
        other_errors = {k: v for k, v in st["errors"].items() if k != "INVALID_ACTION"}
        return {"agent": ids[seat], "seat": seat, "country": p.country, "first_proc_s": first["proc"],
                "first_weap_s": first["weap"], "peak_army": p.peak_army, "kills_cost": p.mil["kills_cost"],
                "deaths_cost": p.mil["deaths_cost"], "assets": p.mil["assets_value"],
                "units_killed": p.mil["units_killed"], "buildings_killed": p.mil["buildings_killed"],
                "batches": st["batches"], "batches_per_s": round(st["batches"] / game_s, 3) if game_s else 0.0,
                "orders": st["orders"], "invalid_actions": st["invalid_actions"], "rejected": st["rejected"],
                "partial": st["partial"], "lost": st["lost"], "noop": st["noop"], "tool_errors": other_errors,
                "decisions": t.calls, "decisions_per_s": round(t.calls / game_s, 3) if game_s else 0.0,
                "slowest_ms": round(t.slowest * 1000, 1), "mean_ms": round(t.total / t.calls * 1000, 2) if t.calls
                else 0.0, "p99_ms": t.p99_ms(), "low_power_s": round(p.low_power_ticks / TICKS_PER_SECOND, 1),
                "place_delay_max_s": round(max(p.place_delays, default=0) / TICKS_PER_SECOND, 1),
                "place_delay_mean_s": round(sum(p.place_delays) / len(p.place_delays) / TICKS_PER_SECOND, 2)
                if p.place_delays else 0.0,
                "unplaced": sum(1 for q in ("Building", "Defense") if p.queues[q] and p.queues[q][0].done),
                "views": st["views"], "error": errors.get(seat), "hung": seat in hung,
                "faults": dict(sessions[seat].faults) if sessions else {},
                "invalid_details": list(server.seats[seat].invalid_details),
                "problems": server._problems(server.seats[seat])}

    b_seat = 1 - a_seat
    return {"seed": seed, "map": map_id, "a_seat": a_seat, "result_a": res_a, "reason": server.reason,
            "game_s": round(game_s, 1), "wall_s": round(wall, 1), "ticks": world.tick,
            "forced_advances": server.forced_advances, "a": side(a_seat), "b": side(b_seat)}


def _fmt_side(s: dict) -> str:
    def t(v):
        return "-" if v is None else f"{v:.0f}"
    err = f" ERR {s['error']}" if s["error"] else ""
    hung = " HUNG" if s["hung"] else ""
    te = f" errs {s['tool_errors']}" if s["tool_errors"] else ""
    return (f"{s['agent']}({s['country'][:3]}) proc {t(s['first_proc_s'])}s weap {t(s['first_weap_s'])}s "
            f"army^ {s['peak_army']} k/d {s['kills_cost']}/{s['deaths_cost']} assets {s['assets']} "
            f"batches {s['batches']} ({s['batches_per_s']:.2f}/s) invalid {s['invalid_actions']} "
            f"rej {s['rejected']} part {s['partial']} dec {s['decisions_per_s']:.2f}/s "
            f"p99 {s['p99_ms']:.0f}ms slow {s['slowest_ms']:.0f}ms lowpow {s['low_power_s']:.0f}s "
            f"place<= {s['place_delay_max_s']:.1f}s{te}{err}{hung}")


def wilson(score: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson interval of a score rate (draws count half)."""
    if n <= 0:
        return 0.0, 1.0
    p = score / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return max(0.0, c - h), min(1.0, c + h)


def _one(job: dict) -> dict:
    """One game in a worker process (--jobs): factories are rebuilt from their specs there."""
    os.environ["ARENA_RECORD"] = "0"
    fa, fb = make_factory(job["a"]), make_factory(job["b"])
    seed = job["seed"]
    countries = None
    if job.get("a_side") in ("allies", "soviet"):            # A's country from that side, B's from any (by seed)
        rng = random.Random(f"countries:{seed}")
        ca = rng.choice([c for c in COUNTRIES if SIDES[c] == job["a_side"]])
        cb = rng.choice(COUNTRIES)
        countries = (ca, cb) if job["a_seat"] == 0 else (cb, ca)
    row = play_match(fa(seed * 2 + 1), fb(seed * 2 + 2), seed=seed, map_id=job["map"], a_seat=job["a_seat"],
                     countries=countries,
                     clock=job["clock"], speed=job["speed"], max_minutes=job["max_minutes"],
                     starting_cash=job["cash"], latency_ticks=job["latency_ticks"], lost_rate=job["lost_rate"],
                     names=(job["a"].split(":")[-1], job["b"].split(":")[-1]), pacing=job["pacing"],
                     default_stance=job["stance"], place_limit=job["place_limit"],
                     fault_rate=job.get("fault_rate", 0.0))
    row["b_spec"] = job["b"]
    return row


def main(argv=None) -> None:
    os.environ["ARENA_RECORD"] = "0"
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--a", default="rusher", help="side A agent (default rusher)")
    ap.add_argument("--b", default="idle", help="side B agent, or a comma list cycled per game (default idle)")
    ap.add_argument("--games", type=int, default=2)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--maps", default=",".join(MAPS), help="maps cycled per game (default all three)")
    ap.add_argument("--clock", choices=("lockstep", "realtime"), default="lockstep")
    ap.add_argument("--speed", type=float, default=10.0, help="realtime clock: game seconds per wall second")
    ap.add_argument("--max-minutes", type=float, default=20.0)
    ap.add_argument("--cash", type=int, default=5000, help="match_rules.starting_cash (default 5000)")
    ap.add_argument("--latency-ticks", type=int, default=0,
                    help="lockstep: hold each batch until N ticks after its state (think + network; default 0)")
    ap.add_argument("--lost-rate", type=float, default=0.0, help="share of pending orders that come back lost")
    ap.add_argument("--no-swap", action="store_true", help="A always in seat Multi0")
    ap.add_argument("--pacing", choices=("realtime", "windows"), default="realtime",
                    help="windows: the operator's 5-second windows (turn-based for the runner)")
    ap.add_argument("--default-stance", choices=("defend", "attack_anything", "return_fire", "hold_fire"),
                    default="defend", help="new units' stance (OpenRA: defend for a human player, attack_anything "
                                           "for a bot player; the platform's choice is not documented)")
    ap.add_argument("--place-limit", type=int, default=None,
                    help="test switch: every place after this many placed buildings per side is invalid (no room)")
    ap.add_argument("--fault-rate", type=float, default=0.0,
                    help="share of game calls that fail like a flaky connection (HTTP 502 / 429; a play_action "
                         "before or after it was applied), which the runner rides out (SDK PR #7)")
    ap.add_argument("--a-side", choices=("any", "allies", "soviet"), default="any",
                    help="side A's faction (its country drawn from that side by seed; default any: both random)")
    ap.add_argument("--jobs", type=int, default=1, help="games in parallel processes (default 1)")
    ap.add_argument("--json", type=Path, default=None, help="write all rows here")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    opponents = [b for b in a.b.split(",") if b]
    for spec in [a.a] + opponents:
        make_factory(spec)                                     # fail fast on a bad spec
    maps = [m for m in a.maps.split(",") if m]
    jobs = [{"a": a.a, "b": opponents[g % len(opponents)], "seed": a.seed * 1000 + g, "map": maps[g % len(maps)],
             "a_seat": 0 if a.no_swap else g % 2, "clock": a.clock, "speed": a.speed, "max_minutes": a.max_minutes,
             "cash": a.cash, "latency_ticks": a.latency_ticks, "lost_rate": a.lost_rate, "pacing": a.pacing,
             "stance": a.default_stance, "place_limit": a.place_limit, "fault_rate": a.fault_rate,
             "a_side": a.a_side}
            for g in range(a.games)]
    if a.jobs > 1:
        from concurrent.futures import ProcessPoolExecutor
        pool = ProcessPoolExecutor(max_workers=a.jobs)
        results = pool.map(_one, jobs)
    else:
        pool, results = None, map(_one, jobs)
    rows = []
    try:
        for g, row in enumerate(results):
            rows.append(row)
            if not a.quiet:
                print(f"game {g} seed {row['seed']} {row['map']} A=Multi{row['a_seat']} vs {row['b_spec']}: "
                      f"{row['result_a'].upper()} ({row['reason']}) {row['game_s'] / 60:.1f} game-min in "
                      f"{row['wall_s']:.1f}s wall")
                print("   A " + _fmt_side(row["a"]))
                print("   B " + _fmt_side(row["b"]))
            sys.stdout.flush()
    finally:
        if pool is not None:
            pool.shutdown()
    res = [r["result_a"] for r in rows]
    w, l, d = res.count("win"), res.count("loss"), res.count("draw")
    lo, hi = wilson(w + 0.5 * d, len(rows))
    reasons: dict[str, int] = {}
    for r in rows:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    errs = sum(1 for r in rows for s in ("a", "b") if r[s]["error"] or r[s]["hung"])
    if len(opponents) > 1:
        for opp in opponents:
            mine = [r["result_a"] for r in rows if r["b_spec"] == opp]
            ow, ol, od = mine.count("win"), mine.count("loss"), mine.count("draw")
            print(f"   vs {opp}: {ow}W {ol}L {od}D")
    print(f"{len(rows)} games {a.a} vs {a.b}: A {w}W {l}L {d}D, score {100 * (w + 0.5 * d) / max(1, len(rows)):.0f}% "
          f"(95% CI {100 * lo:.0f}-{100 * hi:.0f}%), reasons {reasons}, runner errors/hangs {errs}, "
          f"A invalid {sum(r['a']['invalid_actions'] for r in rows)} rejected {sum(r['a']['rejected'] for r in rows)}, "
          f"B invalid {sum(r['b']['invalid_actions'] for r in rows)} rejected {sum(r['b']['rejected'] for r in rows)}, "
          f"median wall {statistics.median([r['wall_s'] for r in rows]) if rows else 0:.1f}s")
    if a.json:
        a.json.write_text(json.dumps({"args": vars(a) | {"json": str(a.json)}, "rows": rows}, indent=1, default=str))


if __name__ == "__main__":
    main()
