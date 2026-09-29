"""Python side of local/js/bridge.js — drives a headless Showdown battle."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

JS_DIR = Path(__file__).resolve().parent / "js"
FORMAT = "gen9vgc2025regi@@@Terastal Clause"   # verified: doubles, Lv50 sets, Open Team Sheets, Tera disabled


class Showdown:
    def __init__(self):
        if not (JS_DIR / "node_modules").exists():
            raise RuntimeError(f"run `npm install` in {JS_DIR} first")
        self.proc = subprocess.Popen(["node", str(JS_DIR / "bridge.js")], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True, bufsize=1)

    def _rpc(self, msg: dict) -> dict:
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("bridge died")
        return json.loads(line)

    def start(self, p1: list[dict], p2: list[dict], seed: list[int] | None = None, fmt: str = FORMAT) -> dict:
        return self._rpc({"cmd": "start", "format": fmt, "p1": p1, "p2": p2, "seed": seed})

    def choose(self, p1: str | None = None, p2: str | None = None) -> dict:
        return self._rpc({"cmd": "choose", "p1": p1, "p2": p2})

    def validate(self, sets: list[dict], fmt: str = "gen9vgc2025regi") -> list:
        return self._rpc({"cmd": "validate", "sets": sets, "format": fmt})["problems"]

    def gen_team(self, fmt: str = "gen9randombattle", seed: list[int] | None = None) -> list[dict]:
        """A Showdown-generated team (random-battle sets: species, item, ability, moves, level, EVs...)."""
        return self._rpc({"cmd": "gen_team", "format": fmt, "seed": seed})["team"]

    def search(self, side: str, seed: list[int], params: dict | None = None) -> dict:
        """The `simbot` engine search (local/js/simbot.js) for `side` ('p1'/'p2') on clones of the current battle:
        {"choice": Showdown choice string, "slots": [per-slot doubles_turn entries], "info": {...}} or {"error"}.
        The live battle is not touched."""
        return self._rpc({"cmd": "search", "side": side, "seed": seed, "params": params or {}})

    def sideview(self, side: str, frm: int | None = None) -> dict:
        """What one player ('p1'/'p2') would receive from a Showdown server (the VGC-Bench opponent,
        local/vgcbench_opp.py): the live battle's protocol lines from log index `frm` (default 0) resolved for that
        side's channel, and that side's current request. {"lines", "request", "pos" (log length now; pass it back as
        `frm` next time), "header_end" (log length after the team-preview header), "turn", "ended"}. Read-only."""
        return self._rpc({"cmd": "sideview", "side": side, "from": frm})

    def preview_view(self, side: str, p1: list[dict], p2: list[dict], seed: list[int] | None = None) -> dict:
        """The same side view of a throwaway battle's team-preview phase with these teams: {"lines", "request",
        "showteam" (the Open Team Sheets lines)}. The live battle is not touched."""
        return self._rpc({"cmd": "preview_view", "side": side, "p1": p1, "p2": p2, "seed": seed})

    def dump(self) -> str:
        """The live battle serialized (Battle.toJSON text, logs emptied): the local oracle ablation of the agent's
        engine search (ARENA_SEARCH_ORACLE=1, local.panel). Never used by an agent on the platform."""
        return self._rpc({"cmd": "dump"})["battle"]

    def close(self):
        try:
            self._rpc({"cmd": "quit"})
        except Exception:
            pass
        self.proc.kill()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
