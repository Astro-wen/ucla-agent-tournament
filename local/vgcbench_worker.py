"""VGC-Bench policy worker: runs under the VGC-Bench venv (NOT the repo's), driven by local/vgcbench_opp.py.

    <vgc-bench venv>/bin/python local/vgcbench_worker.py --vgcbench-root /mnt/d/altruagent/vgc-bench-sv

Imports only the standard library, poke_env and vgc_bench (and what they need: numpy, torch); never our agent.
JSON lines on stdin / stdout, one answer per request:
    {"cmd": "hello"}                                                   -> {"ok": true, "pid", "versions"}
    {"cmd": "open", "game": id, "model": path, "side": "p1"|"p2", "deterministic": false}
    {"cmd": "feed", "game": id, "lines": [..], "request": {..}|null, "seed": int, "debug": bool}
        -> {"choice": "/choose ..." | "/team ...", "sent": [..], "actions": [[a1, a2], ..], "ms": .., "vocab": ..}
    {"cmd": "select", "game": id, "order": [1-based team indices]}      (mark the Pokemon brought at team preview)
    {"cmd": "close", "game": id}
    {"cmd": "quit"}
Errors come back as {"error": text}; the process never exits on a bad request.

How a game is fed (what a player connected to a Showdown server receives): every `feed` becomes the websocket
messages the server would send - `>battle-<format>-<n>` followed by that side's protocol lines, then a separate
`>battle-..` + `|request|<json>` message - and goes through poke-env's own PSClient._handle_message ->
Player._handle_battle_message, so battle tracking, Open Team Sheets (accepted, as VGC-Bench always plays), team
preview and move choice run exactly as in VGC-Bench's PolicyPlayer (embed_battle + DoublesEnv.get_action_mask +
policy.forward + DoublesEnv.action_to_order; teampreview through the policy since both checkpoints have
choose_on_teampreview). Whatever the player sends to the "server" is captured and returned.

Deviations, all counted and returned: move / item / ability ids missing from VGC-Bench's vocab lists map to index 0
(their "null" / "no move" entry) instead of raising ("vocab"). Sampling is seeded per decision from the caller's
`seed` (torch, numpy, random) so a panel game replays exactly.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
import traceback
from collections import Counter

FORMAT = "gen9vgc2025regi"


class Vocab(list):
    """VGC-Bench's id list with a lenient index(): unknown -> 0 (null / no move), counted."""

    def __init__(self, items, kind: str, counts: Counter):
        super().__init__(items)
        self.kind, self.counts = kind, counts

    def index(self, x, *args):  # noqa: D401 - list API
        self.counts["lookups"] += 1
        try:
            return super().index(x, *args)
        except ValueError:
            self.counts["unknown"] += 1
            self.counts[f"{self.kind}:{x}"] += 1
            return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vgcbench-root", required=True, help="VGC-Bench checkout (its data/*.json load from the cwd)")
    ap.add_argument("--threads", type=int, default=1, help="torch CPU threads")
    a = ap.parse_args()
    proto = sys.stdout
    sys.stdout = sys.stderr                  # the protocol owns stdout; library prints go to stderr

    def send(obj) -> None:
        proto.write(json.dumps(obj) + "\n")
        proto.flush()

    try:
        os.chdir(a.vgcbench_root)            # vgc_bench/src/utils.py opens data/*.json relative to the cwd
        if a.vgcbench_root not in sys.path:
            sys.path.insert(0, a.vgcbench_root)
        import numpy as np
        import torch
        torch.set_num_threads(max(1, a.threads))
        import poke_env
        from poke_env.concurrency import POKE_LOOP
        from poke_env.environment import DoublesEnv
        from poke_env.ps_client import AccountConfiguration
        from stable_baselines3 import PPO

        from vgc_bench.src import policy_player as pp
        from vgc_bench.src import utils as vu
    except Exception:  # noqa: BLE001 - report start-up failures on the protocol channel
        send({"ready": False, "error": traceback.format_exc()[-3000:]})
        return

    counts: Counter = Counter()
    pp.abilities = Vocab(vu.abilities, "ability", counts)
    pp.items = Vocab(vu.items, "item", counts)
    pp.moves = Vocab(vu.moves, "move", counts)

    last: dict = {"actions": [], "masks": []}
    orig_a2o, orig_mask = DoublesEnv.action_to_order, DoublesEnv.get_action_mask

    def rec_a2o(action, battle, fake=False, strict=True):
        last["actions"].append([int(x) for x in action])
        return orig_a2o(action, battle, fake, strict)

    def rec_mask(battle):
        m = orig_mask(battle)
        last["masks"].append([i for i, v in enumerate(m) if v])
        return m

    DoublesEnv.action_to_order = staticmethod(rec_a2o)      # recorders only: the policy's own code path is unchanged
    DoublesEnv.get_action_mask = staticmethod(rec_mask)

    policies: dict = {}
    seats: dict = {}                                        # (model, side, deterministic) -> PolicyPlayer
    games: dict = {}
    counter = [0]

    class Seat(pp.PolicyPlayer):
        """VGC-Bench's PolicyPlayer with the websocket replaced: what it sends is captured."""

        def __init__(self, policy, side: str, deterministic: bool):
            super().__init__(policy=policy, deterministic=deterministic,
                             account_configuration=AccountConfiguration(side, None), battle_format=FORMAT,
                             start_listening=False, accept_open_team_sheet=True, max_concurrent_battles=0,
                             log_level=50)
            self.sent: list[str] = []

            async def capture(message, room="", message_2=None):
                self.sent.append(message if message_2 is None else f"{message}|{message_2}")

            self.ps_client.send_message = capture

    def run(coro, timeout=120.0):
        return asyncio.run_coroutine_threadsafe(coro, POKE_LOOP).result(timeout)

    def policy_for(path: str):
        if path not in policies:
            pol = PPO.load(path, device="cpu").policy
            pol.eval()
            policies[path] = pol
        return policies[path]

    def view(battle) -> dict:
        """The DoubleBattle as the policy sees it (the faithfulness check compares it with the engine's truth)."""
        def mon(p, own):
            v = {"species": p.species, "name": p.name, "hp": p.current_hp, "max_hp": p.max_hp,
                 "hp_frac": round(p.current_hp_fraction, 4), "status": p.status.name.lower() if p.status else None,
                 "fainted": p.fainted, "active": p.active, "item": p.item, "ability": p.ability,
                 "moves": {m: mv.current_pp for m, mv in p.moves.items()}, "boosts": dict(p.boosts),
                 "revealed": p.revealed}
            if own:
                v["selected"] = p.selected_in_teampreview
            return v
        act = battle.active_pokemon if battle.player_role else [None, None]
        oact = battle.opponent_active_pokemon if battle.player_role else [None, None]
        return {"role": battle.player_role, "turn": battle.turn, "teampreview": battle.teampreview,
                "format": battle.format,
                "weather": {w.name.lower(): t for w, t in battle.weather.items()},
                "fields": {f.name.lower(): t for f, t in battle.fields.items()},
                "side_conditions": {s.name.lower(): v for s, v in battle.side_conditions.items()},
                "opp_side_conditions": {s.name.lower(): v for s, v in battle.opponent_side_conditions.items()},
                "team": [mon(p, True) for p in battle.team.values()],
                "opp_team": [mon(p, False) for p in battle.opponent_team.values()],
                "active": [p.species if p else None for p in act],
                "opp_active": [p.species if p else None for p in oact],
                "force_switch": list(battle.force_switch), "trapped": list(battle.trapped),
                "can_tera": list(battle.can_tera),
                "available_moves": [[m.id for m in ms] for ms in battle.available_moves],
                "available_switches": [[p.species for p in ps] for ps in battle.available_switches]}

    def handle(msg: dict) -> dict:
        cmd = msg.get("cmd")
        if cmd == "hello":
            import stable_baselines3
            return {"ok": True, "pid": os.getpid(),
                    "versions": {"torch": torch.__version__, "poke_env": getattr(poke_env, "__version__", "?"),
                                 "sb3": stable_baselines3.__version__, "numpy": np.__version__}}
        if cmd == "load":
            t0 = time.perf_counter()
            pol = policy_for(msg["model"])
            return {"ok": True, "ms": round(1000 * (time.perf_counter() - t0), 1),
                    "choose_on_teampreview": bool(getattr(pol, "choose_on_teampreview", False))}
        if cmd == "open":
            det = bool(msg.get("deterministic"))
            key = (msg["model"], msg["side"], det)
            if key not in seats:
                seats[key] = Seat(policy_for(msg["model"]), msg["side"], det)
            counter[0] += 1
            games[msg["game"]] = {"seat": seats[key], "tag": f"battle-{FORMAT}-{counter[0]}", "started": False}
            return {"ok": True, "tag": games[msg["game"]]["tag"]}
        if cmd == "feed":
            g = games[msg["game"]]
            seat, tag = g["seat"], g["tag"]
            seed = int(msg.get("seed") or 0)
            random.seed(seed)
            np.random.seed(seed % (2 ** 32))
            torch.manual_seed(seed)
            seat.sent.clear()
            last["actions"], last["masks"] = [], []
            counts.clear()
            lines = list(msg.get("lines") or [])
            if not g["started"]:                 # a server opens the room with |init| / |title| before the battle
                lines = ["|init|battle", "|title|p1 vs. p2"] + lines
                g["started"] = True
            req = msg.get("request")
            t0 = time.perf_counter()
            if lines:
                run(seat.ps_client._handle_message(f">{tag}\n" + "\n".join(lines)))
            repaired = False
            battle = seat._battles.get(tag)
            if (req is not None and not req.get("teamPreview") and battle is not None
                    and not any(p.selected_in_teampreview for p in battle.team.values())):
                # Only after a team preview this worker never saw (the caller's preview fell back): mark the
                # Pokemon actually brought, as VGC-Bench's own teampreview() would have (embed_pokemon asserts it).
                battle.parse_request(req)
                brought = {p["ident"] for p in req["side"]["pokemon"]}
                for ident, p in battle.team.items():
                    p._selected_in_teampreview = ident in brought
                repaired = True
            if req is not None:
                run(seat.ps_client._handle_message(f">{tag}\n|request|" + json.dumps(req)))
            ms = 1000 * (time.perf_counter() - t0)
            choice = next((s for s in reversed(seat.sent) if s.startswith(("/choose", "/team"))), None)
            out = {"choice": choice, "sent": list(seat.sent), "actions": list(last["actions"]), "ms": round(ms, 2),
                   "vocab": {k: v for k, v in counts.items()}, "repaired": repaired}
            if msg.get("debug"):
                battle = seat._battles.get(tag)
                out["masks"] = list(last["masks"])
                out["view"] = view(battle) if battle is not None else None
            return out
        if cmd == "select":
            g = games[msg["game"]]
            battle = g["seat"]._battles[g["tag"]]
            team = list(battle.team.values())
            chosen = {int(i) for i in msg["order"]}
            for i, p in enumerate(team, 1):
                p._selected_in_teampreview = i in chosen
            return {"ok": True, "team": [p.species for p in team]}
        if cmd == "close":
            g = games.pop(msg.get("game"), None)
            if g is not None:
                g["seat"]._battles.pop(g["tag"], None)
                g["seat"].ps_client._battle_locks.pop(g["tag"], None)
            return {"ok": True}
        return {"error": f"unknown cmd {cmd!r}"}

    send({"ready": True, "pid": os.getpid()})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send({"error": "bad json"})
            continue
        if msg.get("cmd") == "quit":
            break
        try:
            send(handle(msg))
        except Exception as e:  # noqa: BLE001 - one bad request never kills the worker
            send({"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-3000:]})


if __name__ == "__main__":
    main()
