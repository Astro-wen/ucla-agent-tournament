"""HoK delivery contracts, including checks that must also run on macOS.

The full export and bundle integration tests need Linux. These checks read their
allowlists without executing deployment scripts, and never read a key file,
start an agent, contact a service, or install a dependency.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from altruagent import WAIT, DecisionContext, GameState
from agent.agent import Contestant, _is_hok

ROOT = Path(__file__).resolve().parents[1]
DELIVERY = [("scripts/publish_sync.sh", "EXPORT_PATHS"), ("scripts/bundle.sh", "INCLUDE")]


def _allowlist(script, variable):
    source = (ROOT / script).read_text(encoding="utf-8")
    declaration = re.search(rf"^{variable}=\((.*?)\)", source, re.M | re.S)
    assert declaration, f"{script}: missing {variable} allowlist"
    return shlex.split(declaration[1], comments=True)


def _covered(path, allowlist):
    return any(path == entry or path.startswith(entry.rstrip("/") + "/") for entry in allowlist)


def _payload(game_type="honor_of_kings_5v5", mode="realtime"):
    # Deliberately independent of the existing HoK fixture and any LLM example.
    return {
        "session_id": "delivery", "game_type": game_type, "status": "in_progress",
        "phase": "moving", "is_current_actor": True, "state_version": 1,
        "pacing": {"mode": mode}, "seats": [0], "camp": 1,
        "observation": {"seats": {"0": {"summary": {
            "my_hero": {"hp": 3000, "max_hp": 3000, "camp": 1},
        }}}},
        "simultaneous": {"your_pending_seats": [0], "standing_orders": {}},
        "legal_actions": {"actions": [
            {"action_id": "s0:button_0", "input": {"action": {"which_button": 0, "seat": 0}}},
            {"action_id": "s0:macro_advance", "label": "Advance",
             "input": {"action": {"which_button": 3, "seat": 0}}},
        ]},
    }


def test_export_readme_source_survives_the_public_readme_rename():
    source = (ROOT / "scripts/publish_sync.sh").read_text(encoding="utf-8")
    assignment = re.search(r"^PUBLIC_README=(.+)$", source, re.M)
    assert assignment, "publish_sync.sh must identify the committed README source"
    readme = shlex.split(assignment[1], comments=True)[0]
    assert (ROOT / readme).is_file(), f"export still requires missing {readme}; current public file is README.md"


@pytest.mark.parametrize("script,variable", DELIVERY)
def test_both_delivery_allowlists_include_the_entire_hok_package(script, variable):
    allowlist = _allowlist(script, variable)
    assert "agent" in allowlist and "altruagent" in allowlist
    files = sorted((ROOT / "agent/hok").glob("*.py"))
    assert files and any(path.name == "policy.py" for path in files)
    assert all(_covered(path.relative_to(ROOT).as_posix(), allowlist) for path in files)
    assert not any(path == "examples" or path.startswith("examples/") for path in allowlist)
    assert not any(path.startswith(".env") and path != ".env.example" for path in allowlist)


@pytest.mark.parametrize("script,variable", DELIVERY)
def test_delivery_preserves_readme_image_assets(script, variable):
    images = re.findall(r"!\[[^\]]*\]\((assets/[^)\s]+)\)", (ROOT / "README.md").read_text(encoding="utf-8"))
    assert images, "the public README's image assets must be checked, not silently skipped"
    assert all((ROOT / image).is_file() for image in images)
    missing = [image for image in images if not _covered(image, _allowlist(script, variable))]
    assert not missing, f"{script} omits public README assets: {missing}"


@pytest.mark.parametrize("script,variable", DELIVERY)
def test_delivery_preserves_third_party_notices(script, variable):
    assert _covered("NOTICE", _allowlist(script, variable)), f"{script} omits third-party license notices"


@pytest.mark.parametrize("mode", ["realtime", "lockstep"])
def test_hok_rules_work_without_site_packages_or_credentials(mode):
    # -S excludes installed providers/SDKs; a minimal environment passes no keys.
    # This imports only the rules package, not the agent's live entry point.
    code = (
        "import json; from agent.hok import HokPolicy; "
        f"answer = HokPolicy().decide(json.loads({json.dumps(_payload(mode=mode))!r})); "
        "print(json.dumps(answer[0]))"
    )
    result = subprocess.run(
        [sys.executable, "-S", "-c", code],
        cwd=ROOT, env={"PYTHONPATH": str(ROOT), "ARENA_RECORD": "0", "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == "s0:macro_advance"


@pytest.mark.parametrize("game_type", ["red_alert", "pokemon_vgc_doubles_draft", "werewolf", "other_realtime_game"])
@pytest.mark.parametrize("source", ["context", "state", "raw"])
def test_an_explicit_other_game_type_cannot_be_overridden_by_hok_action_shape(game_type, source):
    state = GameState.from_mcp_state(_payload(None))
    context = SimpleNamespace(game_type=game_type if source == "context" else None)
    if source == "state":
        state.game_name = game_type
    elif source == "raw":
        state.raw["game_type"] = game_type
    assert not _is_hok(state, context)


@pytest.mark.parametrize("game_type", [None, "unknown", "none", "other_realtime_game"])
def test_unidentified_realtime_game_is_not_claimed_by_action_shape(monkeypatch, game_type):
    # The handoff requires HoK routing by explicit game type, not by action shape.
    # Unknown real-time games must retain their existing WAIT-only route.
    monkeypatch.setenv("ARENA_HOK", "on")
    monkeypatch.setenv("ARENA_RECORD", "0")
    state = GameState.from_mcp_state(_payload(game_type))
    context = DecisionContext(session_id="delivery", tournament_id=None, game_type=game_type, agent_id="test")
    contestant = Contestant()
    assert contestant.choose_action(state, context) is WAIT
    assert contestant._hok is None
