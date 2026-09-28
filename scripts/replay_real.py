"""Replay every recorded real platform state through the current agent: no crash, legal answers, decision time.

    python scripts/replay_real.py            # all logs/*/*.jsonl
    python scripts/replay_real.py --no-calc  # same, with the damage calculator disabled (heuristic fallback path)
    python scripts/replay_real.py --show-reasoning   # also print every public reasoning line

Each move is first put through the official runner's own `_validate_decision` (the shapes it accepts, including
the WithReasoning wrapper), and each public reasoning line is checked (length cap, ASCII whitelist, leak guard);
the time to build the line is reported on its own.
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time

os.environ["ARENA_RECORD"] = "0"          # replaying must not re-record the sessions it reads

from altruagent import DecisionContext, GameState, LegalAction, WithReasoning
from altruagent.runner import _validate_decision

from agent import reasoning
from agent.agent import Contestant, doubles_ok
from agent.pokemon.calc import DamageCalc


def _p99(xs: list[float]) -> float:
    return xs[int(len(xs) * 0.99) - 1] if xs else 0.0


def main() -> int:
    if "--no-calc" in sys.argv:
        DamageCalc.get().enabled = False
    show = "--show-reasoning" in sys.argv
    files = sorted(glob.glob("logs/*/*.jsonl"))
    n = fails = with_reason = 0
    times: list[float] = []
    reason_times: list[float] = []
    for f in files:
        c = Contestant()                                        # one instance per match, like the runner
        for line in open(f):
            rec = json.loads(line)
            raw = rec["raw"]
            ctx = DecisionContext(session_id=raw.get("session_id", "x"), tournament_id=None,
                                  game_type=raw.get("game_type"), agent_id="replay")
            gs = GameState.from_mcp_state(raw)
            t = time.perf_counter()
            try:
                out = c.choose_action(gs, ctx) if rec["kind"] == "action" else c.choose_message(gs, ctx)
            except Exception as e:                              # noqa: BLE001
                fails += 1
                print(f"CRASH {f} {raw.get('phase')}: {e!r}")
                continue
            finally:
                times.append(time.perf_counter() - t)
            n += 1
            bad = None
            if rec["kind"] == "action":
                try:
                    _validate_decision(out, gs.legal_actions)   # exactly what the official runner does first
                except Exception as e:                          # noqa: BLE001
                    bad = f"runner rejects {out!r}: {e}"
                if isinstance(out, WithReasoning):
                    with_reason += 1
                    text = out.reasoning_summary
                    if not isinstance(text, str) or text != reasoning.sanitize(text) \
                            or reasoning.leaks(text, raw.get("game_type") or ""):
                        bad = f"bad public reasoning {text!r}"
                    if show:
                        print(f"REASON {raw.get('phase')}: {text}")
                    out = out.action
                    t = time.perf_counter()                     # the line alone, same inputs as in choose_action
                    reasoning.attach(out, raw, ctx.game_type or "", c.reasoning_mode)
                    reason_times.append(time.perf_counter() - t)
            obs = raw.get("observation") if isinstance(raw.get("observation"), dict) else {}
            if bad:
                pass
            elif raw.get("phase") == "moving" and obs.get("is_doubles"):
                if not (isinstance(out, dict) and doubles_ok(raw, out)):
                    bad = f"illegal doubles answer {out}"
            elif raw.get("phase") == "team_preview":
                roster = {p["species"] for p in obs.get("your_roster", [])}
                ok = isinstance(out, dict) and len(set(out.get("bring", []))) == 4 and set(out["bring"]) <= roster \
                    and len(out.get("leads", [])) == 2 and set(out["leads"]) <= set(out["bring"])
                bad = None if ok else f"bad lineup {out}"
            elif raw.get("phase") in ("draft", "moving") and rec["kind"] == "action":
                ids = {a["action_id"] for a in (raw.get("legal_actions") or {}).get("actions", [])}
                aid = out.action_id if isinstance(out, LegalAction) else None
                if aid is not None and aid not in ids:
                    bad = f"action {aid} not legal"
            if bad:
                fails += 1
                print(f"ILLEGAL {f} {raw.get('phase')}: {bad}")
    times.sort()
    reason_times.sort()
    print(f"replayed {n} real decisions from {len(files)} files: {fails} failures | "
          f"decision time avg {1000 * sum(times) / max(1, len(times)):.1f} ms, p99 {1000 * _p99(times):.1f} ms, "
          f"max {1000 * (times[-1] if times else 0):.0f} ms")
    print(f"public reasoning (mode {reasoning.mode_from_env()}) on {with_reason} decisions: build avg "
          f"{1000 * sum(reason_times) / max(1, len(reason_times)):.3f} ms, p99 {1000 * _p99(reason_times):.3f} ms, "
          f"max {1000 * (reason_times[-1] if reason_times else 0):.3f} ms")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
