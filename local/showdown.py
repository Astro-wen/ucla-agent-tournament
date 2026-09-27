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
