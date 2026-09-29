"""The VGC-Bench opponents (local/vgcbench_opp.py, local/vgcbench_worker.py) and the bridge's side views
(local/js/bridge.js `sideview` / `preview_view`). Only test_vgcbench_plays_legal_matches needs the VGC-Bench install
(VGCBENCH_HOME, default /mnt/d/altruagent) and skips without it; the bridge tests skip without local/js/node_modules;
everything else is pure Python."""
import json
import random
import re
import shutil
import stat

import pytest

from local import panel
from local import pokemon_env as pe
from local import vgcbench_opp as vb
from local.showdown import JS_DIR

NODE = shutil.which("node") is not None and (JS_DIR / "node_modules").exists()
needs_node = pytest.mark.skipif(not NODE, reason="node / npm install in local/js missing")
HAVE_VGCB, WHY = vb.available()
needs_vgcb = pytest.mark.skipif(not HAVE_VGCB, reason=f"VGC-Bench install missing: {WHY}")


# ---------------------------------------------------------------- registry / availability
def test_vgcbench_is_opt_in():
    assert set(panel.VGCB) == set(vb.NAMES) == set(vb.CHECKPOINTS)
    assert all(n not in g for n in vb.NAMES for g in panel.GROUPS.values())
    assert set(vb.EXTRA_KEYS) == set(panel.VGCB_KEYS)


def test_missing_install_is_reported_not_crashed(tmp_path, monkeypatch):
    for var in ("VGCBENCH_CODE", "VGCBENCH_PYTHON", "VGCBENCH_MODELS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("VGCBENCH_HOME", str(tmp_path / "nowhere"))
    ok, why = vb.available()
    assert not ok and "docs/local_sim.md" in why
    with pytest.raises(SystemExit, match="VGC-Bench is not installed"):
        panel.resolve_opps("v1,vgcbench_sp")
    with pytest.raises(RuntimeError):
        vb.VGCBench("vgcbench_bc")
    assert panel.resolve_opps("v1") == ["v1"]                  # everything else still resolves


# ---------------------------------------------------------------- choice conversion
def _req():
    side = [{"ident": "p1: Incineroar", "details": "Incineroar, L50, M", "condition": "100/202"},
            {"ident": "p1: Chien-Pao", "details": "Chien-Pao, L50", "condition": "150/155"},
            {"ident": "p1: Amoonguss", "details": "Amoonguss, L50, F", "condition": "220/220"},
            {"ident": "p1: Urshifu", "details": "Urshifu-Rapid-Strike, L50, M", "condition": "175/175"}]
    return {"active": [{"moves": [{"id": "fakeout"}, {"id": "flareblitz"}]}, {"moves": [{"id": "recharge"}]}],
            "side": {"id": "p1", "pokemon": side}}


def _raw(slot0=None, slot1=None):
    s0 = slot0 or [{"type": "move", "move_id": "fakeout", "targets": [1, 2, -2]},
                   {"type": "move", "move_id": "flareblitz", "targets": [1, 2, -2]},
                   {"type": "switch", "species": "amoonguss"}, {"type": "switch", "species": "urshifurapidstrike"}]
    s1 = slot1 or [{"type": "move", "move_id": "recharge", "targets": [0]}]
    tmpl = {"type": "doubles_turn", "slots": [{"slot": 0, "options": s0}, {"slot": 1, "options": s1}]}
    return {"phase": "moving", "legal_actions": {"actions": [{"input": {"action": tmpl}}]}}


def test_choice_strings_become_platform_turns():
    st = {}
    out = vb.choice_to_turn("/choose move fakeout 2, move 1", _req(), _raw(), st)
    assert out == {"type": "doubles_turn", "slot_0": {"type": "move", "move_id": "fakeout", "target": 2},
                   "slot_1": {"type": "move", "move_id": "recharge", "target": 0}}
    out = vb.choice_to_turn("/choose switch Urshifu, move recharge", _req(), _raw(), st)
    assert out["slot_0"] == {"type": "switch", "species": "urshifurapidstrike"}
    out = vb.choice_to_turn("/choose move flareblitz terastallize -2, move 1", _req(), _raw(), st)
    assert out["slot_0"] == {"type": "move", "move_id": "flareblitz", "target": -2} and st["gimmicks"] == 1
    assert st.get("retargeted", 0) == 0
    for c in ("/choose default", "/choose move fakeout 1", "/choose move closecombat 1, move 1",
              "/choose switch Pikachu, move 1", "/team 1234", "", "/choose move fakeout 0, move 1"):
        with pytest.raises(ValueError):
            vb.choice_to_turn(c, _req(), _raw(), {})


def test_targets_showdown_would_redirect_are_normalised_and_counted():
    one_foe = [{"type": "move", "move_id": "fakeout", "targets": [1, -2]}]
    st = {}
    out = vb.choice_to_turn("/choose move fakeout 2, move 1", _req(), _raw(slot0=one_foe), st)
    assert out["slot_0"]["target"] == 1 and st["retargeted"] == 1       # fainted foe slot -> the foe left
    locked = [{"type": "move", "move_id": "fakeout", "targets": [0]}]
    out = vb.choice_to_turn("/choose move fakeout 1, move 1", _req(), _raw(slot0=locked), st)
    assert out["slot_0"]["target"] == 0 and st["retargeted"] == 2       # a locked move ignores its target
    with pytest.raises(ValueError):                                     # an ally target is never redirected
        vb.choice_to_turn("/choose move fakeout -1, move 1", _req(), _raw(slot0=one_foe), {})


def test_team_orders():
    assert vb.team_order("/team 3142") == [3, 1, 4, 2]
    ids = ["a", "b", "c", "d", "e", "f"]
    lu = vb.lineup_from_order([3, 1, 4, 2], ids)
    assert lu == {"type": "select_lineup", "bring": ["c", "a", "d", "b"], "leads": ["c", "a"]}
    assert pe.lineup_ok(lu, ids)
    for bad in ("/team 1123", "/team 123", "/team 1237", "team 1234"):
        with pytest.raises(ValueError):
            vb.team_order(bad)


# ---------------------------------------------------------------- bridge side views
def _teams(seed):
    from local.pool import POOL, to_showdown_set
    rng = random.Random(seed)
    cards = rng.sample(POOL, 12)
    return [to_showdown_set(c) for c in cards[:6]], [to_showdown_set(c) for c in cards[6:]]


HP = re.compile(r"^\|[^|]+\|(p[12])[ab]: [^|]*\|(?:[^|]*\|)?(\d+)/(\d+)")


def _channel(omni, side):
    """The protocol's rule, written out independently: a |split|<owner> block is followed by the owner's secret line
    and the public line; the owner sees the first, everybody else the second (empty lines are dropped)."""
    out, i = [], 0
    while i < len(omni):
        x = omni[i]
        if x.startswith("|split|"):
            line = omni[i + 1] if x.split("|")[2] == side else omni[i + 2]
            i += 3
        else:
            line, i = x, i + 1
        if line:
            out.append(line)
    return out


def _check_side(lines, side, omni):
    """`lines` (one side's view) is exactly that side's channel: no omniscient line, no secret of the other side."""
    other = "p2" if side == "p1" else "p1"
    assert not any(x.startswith(("|split|", "|request|")) for x in lines)
    assert lines == _channel(omni, side)
    secrets = {omni[i + 1] for i, x in enumerate(omni) if x.startswith(f"|split|{other}") and omni[i + 1] != omni[i + 2]}
    public = {x for x in _channel(omni, side)}
    assert not (secrets - public) & set(lines)
    for x in lines:                                            # the other side's HP only ever out of 100
        m = HP.match(x)
        if m and m.group(1) == other and m.group(3) != "0":
            assert m.group(3) == "100", x


@needs_node
def test_sideview_is_one_players_view_and_read_only():
    from local.showdown import Showdown
    t1, t2 = _teams(5)
    rnd = random.Random(1)
    with Showdown() as sd:
        m = pe.Match(sd, [pe.RandomPlayer(), pe.RandomPlayer()], random.Random(0))   # for its _battle_state only
        omni = list(sd.start(t1, t2, seed=[1, 2, 3, 4])["log"])
        snap = sd.choose("team 1234", "team 3412")
        omni += snap["log"]
        pos = {"p1": 0, "p2": 0}
        seen = {"p1": [], "p2": []}
        for _ in range(12):
            if snap["ended"]:
                break
            for side in ("p1", "p2"):
                v = sd.sideview(side, pos[side])
                seen[side] += v["lines"]
                pos[side] = v["pos"]
                req = v["request"]
                assert req is None or req["side"]["id"] == side
                assert req == snap["requests"][side] or (req or {}).get("wait")
            again = sd.choose()                                # sideview changed nothing in the live battle
            assert again["log"] == [] and again["requests"] == snap["requests"]
            choices = {}
            for side, req in snap["requests"].items():
                if req:
                    raw, tmpl = m._battle_state(0 if side == "p1" else 1, side, req, snap["state"], 0)
                    random.seed(rnd.random())
                    choices[side] = pe._to_choice(pe.random_action(raw), req, tmpl)
            snap = sd.choose(**choices)
            omni += snap["log"]
        for side in ("p1", "p2"):
            full = sd.sideview(side, 0)
            assert full["lines"] == seen[side] + sd.sideview(side, pos[side])["lines"]
            _check_side(full["lines"], side, omni)
            head = sd.sideview(side, full["header_end"])
            assert any(x.startswith("|teampreview") for x in full["lines"][:len(full["lines"]) - len(head["lines"])])
            assert not any(x.startswith("|teampreview") for x in head["lines"])
        assert any(x.startswith("|split|") for x in omni)      # the check above had secrets to hide


@needs_node
def test_preview_view_matches_the_live_header_and_leaves_the_battle_alone():
    from local.showdown import Showdown
    t1, t2 = _teams(8)
    with Showdown() as sd:
        sd.start(t1, t2, seed=[9, 8, 7, 6])
        before = sd.choose()
        for side in ("p1", "p2"):
            pv = sd.preview_view(side, t1, t2, [9, 8, 7, 6])
            live = sd.sideview(side, 0)
            no_t = [x for x in live["lines"] if not x.startswith("|t:|")]
            assert [x for x in pv["lines"] if not x.startswith("|t:|")] == no_t    # same genders: same seed
            assert pv["request"]["teamPreview"] and pv["request"]["side"]["id"] == side
            assert pv["request"] == live["request"]
            assert [x.split("|")[1:3] for x in pv["showteam"]] == [["showteam", "p1"], ["showteam", "p2"]]
            _check_side(pv["lines"], side, pv["lines"])
        after = sd.choose()
        assert after["log"] == [] and after["requests"] == before["requests"]
        assert after["state"]["turn"] == before["state"]["turn"] == 0


# ---------------------------------------------------------------- failure handling (no VGC-Bench needed)
@pytest.fixture
def fresh_worker_slot():
    vb.shutdown()
    vb._W.update(worker=None, restarts=0, failed=None)
    yield
    vb.shutdown()
    vb._W.update(worker=None, restarts=0, failed=None)


def _fake_install(tmp_path, monkeypatch, python_body: str):
    home = tmp_path / "vgcb"
    for f in ("vgc-bench-sv/vgc_bench/src/policy_player.py", "vgc-bench-sv/data/moves.json",
              *(f"models-sv/{c}" for c in vb.CHECKPOINTS.values())):
        (home / f).parent.mkdir(parents=True, exist_ok=True)
        (home / f).write_text("")
    py = home / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text(python_body)
    py.chmod(py.stat().st_mode | stat.S_IEXEC)
    for var in ("VGCBENCH_CODE", "VGCBENCH_PYTHON", "VGCBENCH_MODELS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("VGCBENCH_HOME", str(home))
    assert vb.available()[0]


@needs_node
def test_a_dead_worker_means_counted_random_fallbacks_never_a_crash(tmp_path, monkeypatch, fresh_worker_slot):
    from local.showdown import Showdown
    _fake_install(tmp_path, monkeypatch, "#!/bin/sh\nexit 3\n")        # a "python" that dies at once
    bot = vb.VGCBench("vgcbench_sp")
    with pytest.raises(vb.WorkerError, match="exit 3"):
        bot.start()
    rnd = pe.RandomPlayer()
    with Showdown() as sd:
        r = pe.play_match(sd, bot, rnd, seed=4600)
    assert r.invalid == [0, 0]
    st = bot.stats
    assert st["preview_fallbacks"] == 1 and st["fallbacks"] == st["decisions"] + 1 and st["decisions"] > 0
    assert 1 <= vb._W["restarts"] <= vb.MAX_RESTARTS and st["errors"] >= 2
    assert all("fallback" in t for t in bot.trace)


def test_a_hung_worker_times_out(tmp_path, monkeypatch, fresh_worker_slot):
    _fake_install(tmp_path, monkeypatch, "#!/bin/sh\nsleep 30\n")
    monkeypatch.setattr(vb, "START_TIMEOUT", 0.5)
    with pytest.raises(vb.WorkerError, match="timed out"):
        vb.Worker()


# ---------------------------------------------------------------- the real thing
@needs_vgcb
@needs_node
def test_vgcbench_plays_legal_matches(fresh_worker_slot, monkeypatch):
    from local.showdown import Showdown
    monkeypatch.setenv("VGCBENCH_DEBUG", "1")
    bot = vb.VGCBench("vgcbench_sp")
    info = bot.start()
    assert info["choose_on_teampreview"]
    rnd = pe.RandomPlayer()
    with Showdown() as sd:
        for g in range(2):
            players = [bot, rnd] if g % 2 == 0 else [rnd, bot]
            r = pe.play_match(sd, players[0], players[1], seed=4700 + g)
            st = bot.stats
            assert r.invalid[g % 2] == 0 and st["fallbacks"] == 0 and st["header_mismatch"] == 0
            assert st["decisions"] > 0 and st["vocab_lookups"] > 0 and st["restarts"] == 0
            pv = bot.trace[0]
            assert pv["phase"] == "team_preview" and pv["choice"].startswith("/team ")
            moves = [t for t in bot.trace if t["phase"] == "moving"]
            assert all(t["choice"].startswith("/choose ") and t["view"]["turn"] == t["turn"] for t in moves)
            json.dumps(bot.trace)                              # --save-logs writes it
