"""Install check for a deployment machine or a bundle: no network, no platform, no key needed.

    python scripts/selfcheck.py                  # everything below; exit 0 = ready (the calculator runs)
    python scripts/selfcheck.py --no-replay      # skip the recorded-decision replay (e.g. inside a Docker build)
    python scripts/selfcheck.py --allow-no-calc  # emergency only: a calculator that cannot run is a warning

Checks: Python >= 3.11 and the pinned packages import; the agent's data files (data/*.json, the draft model) load;
the damage calculator (node + agent/js/node_modules) answers a real calc in a fresh node process: when it cannot,
selfcheck FAILS (exit 1) unless --allow-no-calc or ARENA_ALLOW_NO_CALC=1 (without it the agent wins only ~12%
against itself with the calculator; --require-calc is the default and still accepted);
recorded real platform states (logs/*/*.jsonl) replay through the agent with and without the calculator,
every answer passing the official runner's own validation; the agent key is configured (only its presence and a
short hash are printed, never the key).
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["ARENA_RECORD"] = "0"               # a replay must never write logs/<session>.jsonl

FAILS: list[str] = []
WARNS: list[str] = []


def ok(msg: str) -> None:
    print(f"OK    {msg}")


def warn(msg: str) -> None:
    WARNS.append(msg)
    print(f"WARN  {msg}")


def fail(msg: str) -> None:
    FAILS.append(msg)
    print(f"FAIL  {msg}")


def check_python() -> None:
    v = sys.version_info
    (ok if v >= (3, 11) else fail)(f"python {v.major}.{v.minor}.{v.micro} ({sys.executable})")
    try:
        from importlib.metadata import version
        pins = {}
        lock = ROOT / "requirements.lock"
        if lock.exists():
            for line in lock.read_text().splitlines():
                if "==" in line and not line.startswith("#"):
                    name, ver = line.strip().split("==", 1)
                    pins[name] = ver
        bad = []
        for name, want in pins.items():
            try:
                have = version(name)
            except Exception:  # noqa: BLE001
                have = None
            if have != want:
                bad.append(f"{name} {have or 'missing'} (lock {want})")
        if not pins:
            warn("requirements.lock not found: package versions not checked")
        elif bad:
            warn("packages differ from requirements.lock: " + ", ".join(bad[:6]))
        else:
            ok(f"{len(pins)} packages match requirements.lock")
    except Exception as e:  # noqa: BLE001
        warn(f"could not compare package versions: {e!r}")
    try:
        import altruagent  # noqa: F401
        import agent.agent  # noqa: F401
        import mcp  # noqa: F401
        ok("imports: agent.agent, altruagent, mcp")
    except Exception as e:  # noqa: BLE001
        fail(f"import failed: {e!r}")


def check_data() -> None:
    names = ["pokedex", "moves", "items", "abilities", "vgc_cards", "singles_sets"]
    for n in names:
        p = ROOT / "data" / f"{n}.json"
        try:
            json.loads(p.read_text())
        except Exception as e:  # noqa: BLE001
            fail(f"data/{n}.json unreadable: {e!r}")
            return
    model = ROOT / "agent" / "pokemon" / "draft_model.json"
    try:
        json.loads(model.read_text())
        ok(f"data files ({len(names)}) and the draft model load")
    except Exception as e:  # noqa: BLE001
        fail(f"draft model {model} unreadable: {e!r}")


def no_calc_allowed() -> bool:
    return str(os.environ.get("ARENA_ALLOW_NO_CALC") or "").strip().lower() in ("1", "yes", "true", "on")


NO_CALC_BANNER = """
      ************************************************************************************************
      *  THE DAMAGE CALCULATOR CANNOT RUN: {why}
      *  Without it the agent plays a heuristic that wins only ~12% of games against itself with the
      *  calculator. Fix: bundles ship node/bin/node; otherwise install Node >= 18 or set ARENA_NODE,
      *  and run (cd agent/js && npm ci --omit=dev) when node_modules is missing.
      *  {what}
      ************************************************************************************************"""


def check_calc(allow_no_calc: bool) -> bool:
    """The calculator must answer a real calc (a fresh node process); otherwise FAIL, or with ARENA_ALLOW_NO_CALC=1
    / --allow-no-calc a loud warning (emergency only: the heuristic still beats an agent that is not running)."""
    try:
        from agent.pokemon import calc
        good, why = calc.probe()
    except Exception as e:  # noqa: BLE001
        good, why = False, f"the check raised {e!r}"
    if good:
        ok(f"damage calculator answers: {why}")
        return True
    if allow_no_calc:
        print(NO_CALC_BANNER.format(why=why, what="ARENA_ALLOW_NO_CALC=1 / --allow-no-calc: allowed (emergency only)."))
        warn(f"damage calculator OFF ({why}): allowed by ARENA_ALLOW_NO_CALC=1 / --allow-no-calc")
    else:
        print(NO_CALC_BANNER.format(why=why, what="REFUSED. Only to run without it anyway: ARENA_ALLOW_NO_CALC=1."))
        fail(f"damage calculator OFF ({why})")
    return False


def replay(limit: int, with_calc: bool) -> None:
    from altruagent import DecisionContext, GameState, WithReasoning
    from altruagent.runner import _validate_decision

    from agent.agent import Contestant
    from agent.pokemon.calc import DamageCalc

    files = sorted(glob.glob(str(ROOT / "logs" / "*" / "*.jsonl")))
    if not files:
        warn("no recorded states under logs/*/*.jsonl: replay skipped")
        return
    dc = DamageCalc.get()
    was = dc.enabled
    dc.cache.clear()                            # (cached answers would hide the heuristic path)
    if not with_calc:
        dc.enabled = False
    n = bad = 0
    worst = 0.0
    phases: dict = {}
    try:
        for f in files:
            c = Contestant()
            for line in open(f):
                if n >= limit:
                    break
                rec = json.loads(line)
                if rec.get("kind") != "action":
                    continue
                raw = rec["raw"]
                gs = GameState.from_mcp_state(raw)
                ctx = DecisionContext(session_id=raw.get("session_id", "x"), tournament_id=None,
                                      game_type=raw.get("game_type"), agent_id="selfcheck")
                t = time.perf_counter()
                try:
                    out = c.choose_action(gs, ctx)
                    _validate_decision(out, gs.legal_actions)
                    if isinstance(out, WithReasoning) and len(out.reasoning_summary or "") > 280:
                        raise ValueError("public reasoning over 280 characters")
                except Exception as e:  # noqa: BLE001
                    bad += 1
                    print(f"      {Path(f).name} {raw.get('phase')}: {e!r}")
                worst = max(worst, time.perf_counter() - t)
                n += 1
                phases[raw.get("phase")] = phases.get(raw.get("phase"), 0) + 1
            if n >= limit:
                break
    finally:
        dc.enabled = was
    label = "with the calculator" if with_calc else "calculator disabled"
    (fail if bad else ok)(f"replayed {n} recorded decisions ({label}; {phases}): {bad} rejected, slowest {worst:.2f}s")


def check_key() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:  # noqa: BLE001
        pass
    key = os.environ.get("ALTRUAGENT_API_KEY") or ""
    url = os.environ.get("ALTRUAGENT_CONTROL_URL") or "(unset: run_agent.sh uses https://api.altruagent-game.com)"
    if not key or key == "sk_agent_your_key_here":
        warn(f"no agent key configured (.env or ALTRUAGENT_API_KEY); control plane {url}")
    else:
        ok(f"agent key configured (#{hashlib.sha256(key.encode()).hexdigest()[:10]}), control plane {url}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--require-calc", action="store_true",
                    help="(the default; still accepted) fail when the damage calculator cannot run")
    ap.add_argument("--allow-no-calc", action="store_true",
                    help="warn instead of failing when the calculator cannot run (same as ARENA_ALLOW_NO_CALC=1)")
    ap.add_argument("--no-replay", action="store_true", help="skip the recorded-decision replay")
    ap.add_argument("--replay-limit", type=int, default=80, help="recorded decisions to replay per pass (default 80)")
    a = ap.parse_args(argv)
    os.chdir(ROOT)
    check_python()
    if not FAILS:
        check_data()
        calc_ok = check_calc(a.allow_no_calc or no_calc_allowed())
        if not a.no_replay:
            replay(a.replay_limit, with_calc=calc_ok)
            if calc_ok:
                replay(a.replay_limit, with_calc=False)
        check_key()
    print(f"\nselfcheck: {'FAILED' if FAILS else 'ready'} ({len(FAILS)} failed, {len(WARNS)} warnings)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
