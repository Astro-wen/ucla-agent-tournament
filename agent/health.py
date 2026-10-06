"""The `search=` field of scripts/run_agent.sh's health line: is the engine search really answering?

    python -m agent.health [--window S] [--probe-every S] [--no-probe]

Checking that search.js and pokemon-showdown exist says nothing about a CPU so busy that every search times out, or a
memory cap that keeps the process from starting (deploy-9). So this reports what the matches saw:

  * live evidence first: the battle-decision counters every match worker keeps in the match-state directory
    (agent/matchstate.py) for the last WINDOW_S: `ok(used=38/40)` when at least OK_SHARE of them played a searched
    answer, else `DEGRADED(used=3/20,no_answer=10,not_warm=7)` with the machine reasons behind the rest;
  * no battle decision in that window: one real search, a fresh engine process loading the simulator and answering one
    recorded battle decision (engine.probe(search=True)), at most every PROBE_EVERY_S (cached in the state directory):
    `idle(probe=1.9s)`, `SLOW(probe=4.2s)` or `FAILED(<why>)`;
  * ARENA_BATTLE=classic: `classic(ARENA_BATTLE)`.
It prints one token without spaces and always exits 0.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

WINDOW_S = 1800.0
PROBE_EVERY_S = 900.0
OK_SHARE = 0.8


_SHORT = (("no answer", "no_answer"), ("not warm", "not_warm"), ("busy", "busy"), ("no search slot", "no_slot"),
          ("low memory", "low_memory"), ("hung", "hung"), ("died", "died"), ("exited", "died"), ("malformed", "died"),
          ("engine off", "engine_off"), ("no time", "no_time"), ("crash", "crash"))


def _short(reason: str) -> str:
    """A machine reason as a short key: 'engine: no answer in 4.83s' -> 'no_answer'."""
    r = str(reason).lower()
    for text, key in _SHORT:
        if text in r:
            return key
    r = r.split(":", 1)[-1] if r.startswith("engine:") else r
    return re.sub(r"[^a-z]+", "_", " ".join(r.split()[:2])).strip("_")[:24] or "other"


def live(window_s: float = WINDOW_S) -> str | None:
    """The live verdict from the matches' counters, or None when no battle decision happened in the window."""
    from . import matchstate
    rows = [r for r in matchstate.read_health(window_s) if isinstance(r.get("battle"), int) and r["battle"] > 0]
    if not rows:
        return None
    battle = sum(r["battle"] for r in rows)
    used = sum(int(r.get("used") or 0) for r in rows)
    if all(r.get("mode") == "classic" for r in rows):
        return f"classic(battle={battle})"
    why: dict = {}
    for r in rows:
        for k, v in (r.get("machine") or {}).items():
            why[_short(k)] = why.get(_short(k), 0) + int(v or 0)
    lite = sum(1 for r in rows if r.get("lite"))
    extra = "".join(f",{k}={v}" for k, v in sorted(why.items(), key=lambda kv: -kv[1])[:4])
    extra += f",lite={lite}" if lite else ""
    tag = "ok" if used >= OK_SHARE * battle else "DEGRADED"
    return f"{tag}(used={used}/{battle}{extra})"


def probe(every_s: float = PROBE_EVERY_S) -> str:
    """A real search, at most every `every_s` seconds (the last result is reused in between)."""
    from . import matchstate
    from .pokemon import engine
    d = matchstate.state_dir()
    cache = d / "probe.cache" if d is not None else None
    try:
        if cache is not None and cache.is_file() and time.time() - cache.stat().st_mtime < every_s:
            return json.loads(cache.read_text())["verdict"]
    except (OSError, ValueError, KeyError):
        pass
    good, why = engine.probe(timeout=60.0, search=True)
    m = re.search(r"a search answered in ([\d.]+)s", why)
    if good and m:
        verdict = f"{'SLOW' if float(m.group(1)) > engine.PROBE_SLOW_S else 'idle'}(probe={m.group(1)}s)"
    elif good:
        verdict = "idle(probe=loaded)"
    else:
        verdict = f"FAILED({re.sub(r'[^A-Za-z0-9_.=/-]+', '_', why)[:60].strip('_')})"
    try:
        if cache is not None:
            cache.write_text(json.dumps({"t": time.time(), "verdict": verdict}))
    except OSError:
        pass
    return verdict


def search_status(window_s: float = WINDOW_S, every_s: float = PROBE_EVERY_S, allow_probe: bool = True) -> str:
    try:
        from .pokemon.policy import battle_mode
        if battle_mode() != "search":
            return "classic(ARENA_BATTLE)"
        v = live(window_s)
        if v is not None:
            return v
        return probe(every_s) if allow_probe else "idle(no_battle)"
    except Exception as e:                            # noqa: BLE001 - a health line never fails
        return f"unknown({type(e).__name__})"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--window", type=float, default=WINDOW_S, help="seconds of live counters to read")
    ap.add_argument("--probe-every", type=float, default=PROBE_EVERY_S, help="seconds between two probe searches")
    ap.add_argument("--no-probe", action="store_true", help="never start a probe search")
    a = ap.parse_args(argv)
    os.environ.setdefault("ARENA_SUMMARY", "0")
    print(search_status(a.window, a.probe_every, not a.no_probe))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
