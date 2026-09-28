"""Cold start: the damage calculator is warmed before the first battle decision, and the lookahead's TIME_CAP only
counts lookahead work, so a fresh match process's first doubles turn never loses its lookahead to node start-up."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent.pokemon import lookahead as L
from agent.pokemon.calc import DamageCalc, _js_dir
from tests.strategy import test_lookahead as T

needs_calc = pytest.mark.skipif(_js_dir() is None or shutil.which("node") is None,
                                reason="agent/js has no node_modules / no node")
ROOT = Path(__file__).resolve().parents[2]


def preview_state():
    mine = [{"species": T.dex.to_id(c["species"])} for c in (T.CHIYU, T.INC, T.GAR, T.RILL)]
    theirs = [{"species": T.dex.to_id(c["species"])} for c in (T.RILL, T.AMOO, T.GAR, T.INC)]
    return {"phase": "team_preview", "session_id": "s", "state_version": 1,
            "observation": {"your_roster": mine, "opponent_roster": theirs},
            "legal_actions": {"actions": [{"action_id": "select_lineup", "input": {"action": {"type": "select_lineup"}}}]}}


def battle_state():
    return T.state([T.view(T.CHIYU), T.view(T.INC)], [T.view(T.RILL, foe=True), T.view(T.AMOO, foe=True)], turn=1)


@pytest.fixture
def fresh_calc(monkeypatch):
    """A brand-new DamageCalc singleton (no node process yet), as in a fresh match process."""
    dc = DamageCalc()
    monkeypatch.setattr(DamageCalc, "_inst", dc)
    yield dc
    if dc.proc is not None and dc.proc.poll() is None:
        dc.proc.kill()
        dc.proc.wait()


def slow_start(monkeypatch, seconds):
    """Node start-up that takes `seconds` longer (a cold machine)."""
    orig = DamageCalc._start

    def start(self):
        fresh = self.proc is None or self.proc.poll() is not None
        ok = orig(self)
        if ok and fresh:
            time.sleep(seconds)
        return ok
    monkeypatch.setattr(DamageCalc, "_start", start)


@needs_calc
def test_warm_is_idempotent_and_measures_the_start_up(fresh_calc):
    assert not fresh_calc.ready and fresh_calc.starts == 0
    assert fresh_calc.warm() and fresh_calc.ready and fresh_calc.starts == 1 and fresh_calc.startup_s > 0
    assert fresh_calc.warm() and fresh_calc.starts == 1                  # already warm: no second process
    assert fresh_calc.cache == {}                                        # the dummy calc is never cached
    fresh_calc.proc.kill()
    fresh_calc.proc.wait()
    assert fresh_calc.warm() and fresh_calc.starts == 2                  # a dead process is restarted


def test_warm_without_a_calculator_is_a_no_op(monkeypatch):
    dc = DamageCalc()
    dc.enabled = False
    monkeypatch.setattr(DamageCalc, "_inst", dc)
    assert dc.warm() is False and dc.proc is None
    pol = T.policy()
    out = pol.act(preview_state())
    assert out["action"]["type"] == "select_lineup" and pol.warmed and dc.proc is None


SLOW, CAP = 1.5, 1.2            # node start-up alone exceeds the (shrunk) lookahead cap; the cap leaves
                                # headroom for the K_THEIRS=6 search on a loaded machine


@needs_calc
def test_team_preview_warms_the_calc_before_the_first_battle_decision(fresh_calc, monkeypatch):
    slow_start(monkeypatch, SLOW)
    monkeypatch.setattr(L, "TIME_CAP", CAP)
    pol = T.policy()
    t = time.perf_counter()
    pol.act(preview_state())
    assert time.perf_counter() - t >= SLOW and fresh_calc.ready and fresh_calc.starts == 1
    timeouts = L.STATS["timeouts"]
    t = time.perf_counter()
    out = pol.act(battle_state())["action"]
    first = time.perf_counter() - t
    assert T.doubles_ok(battle_state(), out) and pol.last_lookahead is not None
    assert fresh_calc.starts == 1 and L.STATS["timeouts"] == timeouts and first < SLOW
    pol.act(preview_state())                              # every later pre-battle phase: no extra work
    assert fresh_calc.starts == 1


@needs_calc
def test_time_cap_does_not_count_calculator_start_up(fresh_calc, monkeypatch):
    """No warm-up (e.g. the process restarted mid-battle): the first decision pays the start-up, the lookahead
    still runs because its clock starts after the calc round trip."""
    slow_start(monkeypatch, SLOW)
    monkeypatch.setattr(L, "TIME_CAP", CAP)
    pol = T.policy()
    timeouts = L.STATS["timeouts"]
    out = pol.act(battle_state())["action"]
    assert T.doubles_ok(battle_state(), out) and fresh_calc.starts == 1
    assert pol.last_lookahead is not None and L.STATS["timeouts"] == timeouts
    # the decision as a whole took longer than TIME_CAP: measured from its start, the old cap would have fired
    assert pol.last_timing["table_ms"] >= 1000 * SLOW and pol.last_timing["total_ms"] > 1000 * L.TIME_CAP
    assert pol.last_timing["lookahead_ms"] < 1000 * L.TIME_CAP


CHILD = r"""
import json, sys, time
sys.path.insert(0, sys.argv[1])
from agent.pokemon import lookahead as L
from agent.pokemon.calc import DamageCalc
from tests.strategy import test_lookahead as T
from tests.strategy import test_cold_start as C
L.TIME_CAP = float(sys.argv[2])
pol = T.policy()
t = time.perf_counter()
if sys.argv[3] == "preview":
    pol.act(C.preview_state())
pre = time.perf_counter() - t
t = time.perf_counter()
out = pol.act(C.battle_state())["action"]
first = time.perf_counter() - t
print(json.dumps({"lookahead": pol.last_lookahead is not None, "timeouts": L.STATS["timeouts"], "pre_s": pre,
                  "first_s": first, "legal": T.doubles_ok(C.battle_state(), out), "starts": DamageCalc.get().starts,
                  "timing": pol.last_timing}))
"""


@needs_calc
@pytest.mark.skipif(os.name == "nt", reason="the slow-node shim is a POSIX shell script")
@pytest.mark.parametrize("flow", ["preview", "battle-only"])
def test_a_fresh_process_first_doubles_decision_keeps_its_lookahead(tmp_path, flow):
    """A real fresh interpreter + node whose start-up (1.0 s, via a PATH shim) exceeds TIME_CAP (0.8 s)."""
    shim = tmp_path / "node"
    shim.write_text(f"#!/bin/sh\nsleep 1.0\nexec '{shutil.which('node')}' \"$@\"\n")
    shim.chmod(0o755)
    env = dict(os.environ, PATH=f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}", ARENA_RECORD="0",
               PYTHONPATH=str(ROOT))
    r = subprocess.run([sys.executable, "-c", CHILD, str(ROOT), "0.8", flow], env=env, cwd=ROOT,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    res = json.loads(r.stdout.strip().splitlines()[-1])
    assert res["legal"] and res["lookahead"] and res["timeouts"] == 0 and res["starts"] == 1
    if flow == "preview":                                 # start-up paid during team preview
        assert res["pre_s"] >= 1.0 and res["first_s"] < 1.0
    else:                                                 # paid inside the decision, outside the lookahead's cap
        assert res["first_s"] >= 1.0 and res["timing"]["lookahead_ms"] < 800
