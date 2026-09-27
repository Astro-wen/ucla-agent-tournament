"""Replay every recorded real platform state through the current agent: no crash, legal answers, decision time.

    python scripts/replay_real.py            # all logs/*/*.jsonl
    python scripts/replay_real.py --no-calc  # same, with the damage calculator disabled (heuristic fallback path)
"""
from __future__ import annotations

import glob
import json
import sys
import time

from altruagent import DecisionContext, GameState, LegalAction, SendMessage

from agent.agent import Contestant, doubles_ok
from agent.pokemon.calc import DamageCalc


def main() -> int:
    if "--no-calc" in sys.argv:
        DamageCalc.get().enabled = False
    files = sorted(glob.glob("logs/*/*.jsonl"))
    n = fails = 0
    times: list[float] = []
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
            obs = raw.get("observation") if isinstance(raw.get("observation"), dict) else {}
            bad = None
            if raw.get("phase") == "moving" and obs.get("is_doubles"):
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
    p99 = times[int(len(times) * 0.99) - 1] if times else 0
    print(f"replayed {n} real decisions from {len(files)} files: {fails} failures | "
          f"decision time avg {1000 * sum(times) / max(1, len(times)):.1f} ms, p99 {1000 * p99:.1f} ms, "
          f"max {1000 * (times[-1] if times else 0):.0f} ms")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
