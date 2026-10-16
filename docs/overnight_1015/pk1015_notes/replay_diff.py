"""Replay real recordings through this tree's agent with ARENA_SEARCH_SUCKER off / on and list the battle answers that
change, plus Sucker Punch counts.  usage: python _notes/replay_diff.py Q file.jsonl ..."""
import json, os, sys
from pathlib import Path
os.environ["ARENA_RECORD"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from altruagent import DecisionContext, GameState, WithReasoning  # noqa: E402
from agent.agent import Contestant  # noqa: E402
import agent  # noqa: E402
print("agent from", agent.__file__, flush=True)

q = sys.argv[1]


def run(f, val):
    if val is None:
        os.environ.pop("ARENA_SEARCH_SUCKER", None)
    else:
        os.environ["ARENA_SEARCH_SUCKER"] = val
    c = Contestant()
    outs = []
    for line in open(f):
        rec = json.loads(line)
        raw = rec["raw"]
        ctx = DecisionContext(session_id=raw.get("session_id", "x"), tournament_id=None,
                              game_type=raw.get("game_type"), agent_id="replay")
        gs = GameState.from_mcp_state(raw)
        try:
            out = c.choose_action(gs, ctx) if rec["kind"] == "action" else c.choose_message(gs, ctx)
        except Exception as e:  # noqa: BLE001
            out = f"CRASH {e!r}"
        if isinstance(out, WithReasoning):
            out = out.action
        obs = raw.get("observation") if isinstance(raw.get("observation"), dict) else {}
        outs.append((raw.get("phase"), obs.get("turn"), out))
    return outs


tot = {"dec": 0, "diff": 0, "sp_off": 0, "sp_on": 0}
for f in sys.argv[2:]:
    a, b = run(f, None), run(f, q)
    for (ph, t, x), (_, _, y) in zip(a, b):
        if ph != "moving" or not isinstance(x, dict):
            continue
        tot["dec"] += 1
        sx = json.dumps(x, sort_keys=True)
        sy = json.dumps(y, sort_keys=True)
        tot["sp_off"] += sx.count("suckerpunch")
        tot["sp_on"] += sy.count("suckerpunch")
        if sx != sy:
            tot["diff"] += 1
            print(f"{Path(f).name[:8]} T{t}\n   off {sx}\n   on  {sy}")
print(tot)
