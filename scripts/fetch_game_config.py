"""Save a game's full platform config (read-only MCP calls) for offline study.

    python scripts/fetch_game_config.py red_alert data/red_alert_config.json

Also prints what `list_games` and `list_sessions` say about that game right now.
Needs ALTRUAGENT_API_KEY in .env (any claimed agent); nothing is joined or played.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from altruagent.client import AltruAgentClient  # noqa: E402
from altruagent.mcp_transport import call_tool  # noqa: E402

MCP_URL = "https://gameapi.altruagent-game.com/mcp"


def main(argv: list[str]) -> int:
    game_type = argv[1] if len(argv) > 1 else "red_alert"
    out = Path(argv[2]) if len(argv) > 2 else Path(f"data/{game_type}_config.json")
    client = AltruAgentClient()
    try:
        games = call_tool(client, MCP_URL, "list_games", {})
        rows = [g for g in games.get("games", []) if g.get("game_type") == game_type]
        print("list_games:", json.dumps(rows))
        sessions = call_tool(client, MCP_URL, "list_sessions", {"game_type": game_type})
        print("list_sessions:", json.dumps(sessions)[:2000])
        config = call_tool(client, MCP_URL, "get_game_config", {"game_type": game_type})
    finally:
        client.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(config, indent=1, sort_keys=True) + "\n")
    print(f"saved {out} ({out.stat().st_size} bytes); top-level keys: {sorted(config)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
