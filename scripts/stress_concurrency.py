"""Concurrency stress test: N simultaneous local matches, one fresh process each (like the live runner's
process-per-match supervisor), all starting at the same instant. Reports our agent's decision latency p50 / p99 /
max per decision type against its time budgets (agent/timecontrol.py) and the platform's limits.

    python scripts/stress_concurrency.py --procs 8 --games 2                     # vs random: 1 agent per process
    python scripts/stress_concurrency.py --procs 8 --games 2 --opponent champion  # 2 full agents per process

Each process: its own Showdown bridge (node), our full agent (agent/agent.py create_agent: its own node calculator)
and the opponent. Everything starts together (--start-at barrier), so N first draft picks (the heaviest calc batch
of a match: the drafter's threat table) and N node start-ups hit the machine at once: the worst case of a round
robin that creates all its matches together. Latency = wall time of Contestant.choose_action (what the platform's
clock sees on top of the network round trip).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIMITS = {"draft": None, "preview": 90.0, "battle": 55.0}      # platform limits (s); the draft has no idle timer


def worker(args) -> None:
    os.environ["ARENA_RECORD"] = "0"
    sys.path.insert(0, str(ROOT))
    from agent.agent import create_agent
    from agent.pokemon import lookahead as L
    from agent.pokemon.calc import DamageCalc
    from local.pokemon_env import ContestantPlayer, RandomPlayer, play_match
    from local.showdown import Showdown

    out = open(args.out, "a")
    degraded = []

    class Timed(ContestantPlayer):
        def choose(self, raw):
            t = time.monotonic()
            res = super().choose(raw)
            ms = 1000 * (time.monotonic() - t)
            d = self.agent.last_decision or {}
            dc = DamageCalc.get()
            pk = self.agent.pk
            if d.get("kind") == "draft" and (pk.draft_info or {}).get("wall_hit"):
                pk.degraded.append("draft:wall")
            degraded.extend(pk.degraded)
            pk.degraded.clear()
            out.write(json.dumps({"proc": args.worker, "phase": raw.get("phase"), "kind": d.get("kind"),
                                  "ms": round(ms, 2), "budget": d.get("budget"), "path": d.get("path"),
                                  "calc_errors": dc.errors, "calc_timeouts": dc.timeouts, "calc_starts": dc.starts,
                                  "calc_slowest": list(dc.slowest), "calc_tightest": list(dc.tightest),
                                  "la_skipped": pk.lookahead_skipped, "la_timeouts": L.STATS["timeouts"],
                                  "degraded": list(degraded)}) + "\n")
            out.flush()
            return res

    ours = Timed(create_agent, "candidate")
    if args.opponent == "champion":
        from champion.agent.agent import create_agent as champ
        opp = ContestantPlayer(champ, "champion")
    else:
        opp = RandomPlayer()
    with Showdown() as sd:
        wait = args.start_at - time.time()
        if wait > 0:
            time.sleep(wait)
        for g in range(args.games):
            seat = (args.worker + g) % 2
            players = [ours, opp] if seat == 0 else [opp, ours]
            t = time.monotonic()
            r = play_match(sd, players[0], players[1], seed=args.seed * 100000 + args.worker * 100 + g)
            out.write(json.dumps({"proc": args.worker, "game": g, "won": r.winner == seat, "turns": r.turns,
                                  "invalid": r.invalid[seat], "secs": round(time.monotonic() - t, 1)}) + "\n")
            out.flush()
    out.close()


def pct(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return xs[k]


def report(path: Path, procs: int, load: list) -> dict:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    dec = [r for r in rows if "ms" in r]
    games = [r for r in rows if "game" in r]
    out = {"procs": procs, "decisions": len(dec), "games": len(games), "load": load, "kinds": {}}
    print(f"\n== N={procs}: {len(games)} games, {len(dec)} decisions of ours, load avg {load}")
    print(f"{'type':8s} {'n':>5s} {'p50 ms':>8s} {'p99 ms':>8s} {'max ms':>8s} {'budget s':>9s} {'limit s':>8s}  paths")
    for kind in ("draft", "preview", "battle"):
        xs = [r["ms"] for r in dec if r.get("kind") == kind]
        if not xs:
            continue
        paths: dict = {}
        for r in dec:
            if r.get("kind") == kind:
                paths[r.get("path")] = paths.get(r.get("path"), 0) + 1
        budget = max((r.get("budget") or 0) for r in dec if r.get("kind") == kind)
        row = {"n": len(xs), "p50": pct(xs, 0.5), "p99": pct(xs, 0.99), "max": max(xs), "budget": budget,
               "limit": LIMITS[kind], "paths": paths}
        out["kinds"][kind] = row
        print(f"{kind:8s} {len(xs):5d} {row['p50']:8.1f} {row['p99']:8.1f} {row['max']:8.1f} {budget:9.1f} "
              f"{LIMITS[kind] if LIMITS[kind] else '-':>8}  {paths}")
    last = {}
    for r in dec:
        last[r["proc"]] = r
    out["calc_errors"] = sum(r["calc_errors"] for r in last.values())
    out["calc_timeouts"] = sum(r["calc_timeouts"] for r in last.values())
    out["calc_starts"] = sum(r["calc_starts"] for r in last.values())
    out["calc_slowest"] = max((tuple(r["calc_slowest"]) for r in last.values()), default=None)   # (s, calcs)
    out["calc_tightest"] = max((tuple(r["calc_tightest"]) for r in last.values()), default=None)  # (share, calcs)
    out["lookahead_skipped"] = sum(r["la_skipped"] for r in last.values())
    out["lookahead_timeouts"] = sum(r["la_timeouts"] for r in last.values())
    out["degraded"] = [x for r in last.values() for x in r["degraded"]]
    out["invalid"] = sum(g["invalid"] for g in games)
    out["wins"] = sum(g["won"] for g in games)
    out["game_secs_max"] = max((g["secs"] for g in games), default=None)
    print(f"calc: {out['calc_starts']} node starts, {out['calc_errors']} errors, {out['calc_timeouts']} timeouts, "
          f"slowest round trip {out['calc_slowest']} (s, calcs), closest to its allowance {out['calc_tightest']} "
          f"(share, calcs) | "
          f"lookahead skipped {out['lookahead_skipped']}, timed out {out['lookahead_timeouts']} | degraded steps "
          f"{out['degraded'] or 0} | invalid {out['invalid']} | wins {out['wins']}/{len(games)} | slowest game "
          f"{out['game_secs_max']} s")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--procs", type=int, default=4)
    ap.add_argument("--games", type=int, default=2)
    ap.add_argument("--seed", type=int, default=5)
    ap.add_argument("--opponent", choices=("random", "champion"), default="random")
    ap.add_argument("--out", default=None)
    ap.add_argument("--json", default=None, help="write the summary here")
    ap.add_argument("--worker", type=int, default=None)
    ap.add_argument("--start-at", type=float, default=0.0)
    a = ap.parse_args()
    if a.worker is not None:
        worker(a)
        return
    out = Path(a.out or Path(tempfile.gettempdir()) / f"stress_{a.procs}_{os.getpid()}.jsonl")
    out.write_text("")
    start_at = time.time() + 8.0 + 0.5 * a.procs            # every process imported and its bridge up by then
    env = dict(os.environ, ARENA_RECORD="0", PYTHONPATH=str(ROOT))
    err_files = [out.with_suffix(f".w{k}.err") for k in range(a.procs)]   # (a pipe could fill up and block a worker)
    ps = []
    for k in range(a.procs):
        with open(err_files[k], "w") as ef:
            ps.append(subprocess.Popen([sys.executable, __file__, "--worker", str(k), "--games", str(a.games),
                                        "--seed", str(a.seed), "--opponent", a.opponent, "--out", str(out),
                                        "--start-at", str(start_at)], cwd=ROOT, env=env, stderr=ef, text=True))
    load = []
    while any(p.poll() is None for p in ps):
        time.sleep(2.0)
        if time.time() > start_at and hasattr(os, "getloadavg"):
            load.append(round(os.getloadavg()[0], 1))
    errs = [f.read_text(errors="replace") for f in err_files]
    for k, (p, e) in enumerate(zip(ps, errs)):
        if p.returncode:
            print(f"worker {k} exited {p.returncode}:\n{e[-2000:]}")
    warn = sum(e.count("WARNING") + e.count("ERROR") for e in errs)
    summary = report(out, a.procs, [min(load or [0]), max(load or [0])])
    summary["stderr_warnings"] = warn
    print(f"stderr warning/error lines across workers: {warn}")
    if a.json:
        Path(a.json).write_text(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
