"""Make our two agents play each other on the real platform and record every payload.

    python scripts/sparring.py status                 # claim status of both agents
    python scripts/sparring.py play <session_id>      # both join, both play (two `python -m agent`), stop when done
    python scripts/sparring.py play latest [game_type] # same, on the newest waiting room (default pokemon_vgc_doubles_draft)

Env ARENA_DIR_<TAG>=<path> runs that agent's code from another checkout, e.g. an old version for a live A/B:
    ARENA_DIR_APOCALYPSE=/tmp/v0 python scripts/sparring.py play latest

Agents come from env files: .env (Yoriko_Red) and .env.apocalypse (Apocalypse_Red).
Each agent's raw payloads land in logs/<agent>/<session_id>.jsonl (see agent/agent.py record()).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
CONTROL = "https://api.altruagent-game.com"
MCP = "https://gameapi.altruagent-game.com/mcp"
AGENTS = {"yoriko": ROOT / ".env", "apocalypse": ROOT / ".env.apocalypse"}


def load_env(path: Path) -> dict:
    return {k.strip(): v.strip() for k, v in (l.split("=", 1) for l in path.read_text().splitlines() if "=" in l)}


class Agent:
    def __init__(self, tag: str, env_path: Path):
        self.tag, self.env = tag, load_env(env_path)
        self.http = httpx.Client(timeout=30)
        self.token = self.http.post(f"{CONTROL}/auth/agent/login", json={"api_key": self.env["ALTRUAGENT_API_KEY"]}).json()["access_token"]
        self.me = self.http.get(f"{CONTROL}/auth/agent/me", headers=self._h()).json()

    def _h(self):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"}

    def call(self, tool: str, **args) -> dict:
        r = self.http.post(MCP, headers=self._h(), json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                        "params": {"name": tool, "arguments": args}}).json()
        res = r["result"]
        return res.get("structuredContent") or json.loads(res["content"][0]["text"])

    def spawn(self, session_id: str) -> subprocess.Popen:
        env = {**os.environ, "ALTRUAGENT_API_KEY": self.env["ALTRUAGENT_API_KEY"],
               "ALTRUAGENT_CONTROL_URL": self.env["ALTRUAGENT_CONTROL_URL"],
               "ARENA_RECORD_DIR": str(ROOT / "logs" / self.tag)}
        log = open(ROOT / "logs" / f"{self.tag}.{session_id[:8]}.out", "ab")
        code_dir = Path(os.environ.get(f"ARENA_DIR_{self.tag.upper()}", ROOT))
        env["PYTHONPATH"] = str(code_dir)          # the venv's editable install points at ROOT; make code_dir win
        if code_dir != ROOT:
            print(f"{self.tag}: running code from {code_dir}")
        return subprocess.Popen([sys.executable, "-m", "agent"], cwd=code_dir, env=env, stdout=log, stderr=subprocess.STDOUT)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    agents = [Agent(tag, p) for tag, p in AGENTS.items() if p.exists()]
    for a in agents:
        print(f"{a.tag:11s} {a.me.get('name'):16s} status={a.me.get('status')}")
    if cmd == "status":
        return
    sid = sys.argv[2]
    if sid == "latest":
        want = sys.argv[3] if len(sys.argv) > 3 else "pokemon_vgc_doubles_draft"
        rooms = [s for s in agents[0].call("list_sessions", status=["waiting"]).get("sessions", [])
                 if s["game_type"] == want and s["current_participants"] == 0]
        if not rooms:
            sys.exit(f"no waiting {want} room with 0 participants")
        sid = max(rooms, key=lambda s: s.get("created_at", ""))["session_id"]
        print("latest room:", sid)
    if any(a.me.get("status") != "claimed" for a in agents):
        sys.exit("both agents must be claimed first")
    (ROOT / "logs").mkdir(exist_ok=True)
    for a in agents:
        r = a.call("join_session", session_id=sid)
        print(f"{a.tag}: join -> {r.get('status') or r.get('error')} {r.get('detail', '')} "
              f"({r.get('current_participants')}/{r.get('max_participants')})")
        if "error" in r:
            sys.exit(1)
    procs = [a.spawn(sid) for a in agents]
    print("both `python -m agent` running; waiting for the match to complete (Ctrl+C to stop)")
    t0 = time.time()
    try:
        while True:
            time.sleep(10)
            st = agents[0].call("get_agent_status")
            done = any(s["session_id"] == sid for s in st.get("completed_sessions", []))
            active = any(s["session_id"] == sid for s in st.get("active_sessions", []))
            print(f"  t+{time.time() - t0:4.0f}s  active={active} done={done}  procs alive={[p.poll() is None for p in procs]}")
            if done or all(p.poll() is not None for p in procs):
                break
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
    print(json.dumps(agents[0].call("get_result", session_id=sid), indent=2))
    print("payloads:", sorted(str(p.relative_to(ROOT)) for p in (ROOT / "logs").rglob(f"{sid}.jsonl")))


if __name__ == "__main__":
    main()
