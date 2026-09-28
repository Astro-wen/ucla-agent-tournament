"""local/panel.py statistics, verdict, opponent registry and candidate-only environment; the scripted bots of
local/panel_bots.py on synthetic boards; the frozen cautious package ignoring the candidate's environment. One
Showdown-backed bot-vs-bot match (skipped without local/js/node_modules)."""
import importlib
import os
import random

import pytest

from local import panel
from local import panel_bots as bots
from local.showdown import JS_DIR


# ---------------------------------------------------------------- statistics
def _rows(opp, wins, n, cand="c", seed=31, **extra):
    return [{"cand": cand, "opp": opp, "seed": seed, "g": g, "win": g < wins, "tie": False, "turns": 5,
             "inv_cand": 0, "inv_opp": 0, "secs": 0.1, "tmax_cand": 0.5, **extra} for g in range(n)]


def test_wilson_matches_known_values():
    lo, hi = panel.wilson(199, 200)
    assert round(100 * lo, 1) == 97.2 and round(100 * hi, 1) == 99.9
    assert panel.wilson(0, 0) == (0.0, 0.0)


def test_summary_is_unweighted_mean_and_min_over_opponents():
    rows = _rows("a", 90, 100) + _rows("b", 30, 50)
    s = panel.summarize(rows, ["b", "a"])
    assert s["opps"] == ["b", "a"]
    assert s["panel_mean"] == pytest.approx((0.9 + 0.6) / 2)          # not (90 + 30) / 150
    assert s["min_opp"] == "b" and s["min_rate"] == pytest.approx(0.6)
    assert s["panel_ci"][0] < s["panel_mean"] < s["panel_ci"][1]
    rows.append({**rows[0], "g": 999, "error": "boom", "win": False})
    assert panel.summarize(rows)["per_opp"]["a"]["errors"] == 1
    assert panel.summarize(rows)["per_opp"]["a"]["n"] == 100


def test_summary_sums_platform_counters_of_field_opponents():
    rows = _rows("basic", 10, 10, timer_opp=8, pf_requests=9, pf_crashes=1, pf_rejected=1, forfeit="opp")
    p = panel.summarize(rows)["per_opp"]["basic"]
    assert p["timer_opp"] == 80 and p["forfeit_opp"] == 10 and p["pf_crashed_games"] == 10 and p["pf_rejected"] == 10
    assert "pf_requests" not in panel.summarize(_rows("v1", 5, 10))["per_opp"]["v1"]


def test_compare_pairs_by_game_and_verdict():
    base = _rows("a", 50, 100) + _rows("b", 50, 100)
    better = _rows("a", 70, 100) + _rows("b", 60, 100)        # same games won + more: only-this wins, no losses
    s = panel.summarize(better)
    c = panel.compare(better, base)
    assert c["per_opp"]["a"]["diff"] == pytest.approx(0.2) and c["per_opp"]["a"]["only_base"] == 0
    assert c["panel_diff"] == pytest.approx(0.15) and c["panel_ci"][0] > 0
    assert panel.verdict(s, c) == "PROMOTE"
    worse = _rows("a", 70, 100) + _rows("b", 20, 100)
    c = panel.compare(worse, base)
    assert c["worse"] == ["b"] and panel.verdict(panel.summarize(worse), c) == "REJECT"
    dirty = [dict(r, inv_cand=1) if r["g"] == 0 else r for r in better]
    assert panel.verdict(panel.summarize(dirty), panel.compare(dirty, base)) == "REJECT"
    for bad in ({"timer_cand": 1}, {"forfeit": "cand"}):             # the candidate itself must never be timer-played
        dirty = [dict(r, **bad) if r["g"] == 0 else r for r in better]
        assert panel.verdict(panel.summarize(dirty), panel.compare(dirty, base)) == "REJECT"
    opp_side = [dict(r, timer_opp=3, forfeit="opp") if r["g"] == 0 else r for r in better]
    assert panel.verdict(panel.summarize(opp_side), panel.compare(opp_side, base)) == "PROMOTE"


def _pattern_rows(opp, won, n=100, **extra):
    return [{"cand": "c", "opp": opp, "seed": 31, "g": g, "win": g in won, "tie": False, "turns": 5, "inv_cand": 0,
             "inv_opp": 0, "secs": 0.1, "tmax_cand": 0.5, **extra} for g in range(n)]


def test_panel_ci_is_clustered_by_game_not_by_opponent():
    # review 2 finding 1: game g is the same seed and seat against every opponent, so its outcomes move together.
    # Here the candidate gains games 50-61 and loses games 0-5 against ALL four opponents: 4 x the same evidence.
    base_won, cand_won = set(range(50)), set(range(6, 62))
    opps = ("a", "b", "c", "d")
    base = [r for o in opps for r in _pattern_rows(o, base_won)]
    cand = [r for o in opps for r in _pattern_rows(o, cand_won)]
    c = panel.compare(cand, base)
    one = c["per_opp"]["a"]
    assert c["panel_diff"] == pytest.approx(0.06) and one["diff"] == pytest.approx(0.06)
    assert c["panel_se"] == pytest.approx(one["se"])              # one opponent's worth of information, not four
    naive = (sum(c["per_opp"][o]["se"] ** 2 for o in opps)) ** 0.5 / len(opps)   # the old formula: se / 2
    assert c["panel_diff"] - panel.Z * naive > 0                   # would have said PROMOTE ...
    assert c["panel_ci"][0] < 0 and panel.verdict(panel.summarize(cand), c) == "HOLD"   # ... it is not significant
    s = panel.summarize(cand)                                      # the level CI is clustered the same way
    p = one["rate"]
    assert s["panel_se"] == pytest.approx((p * (1 - p) / 99) ** 0.5)


def test_clustered_se_is_the_se_of_the_per_game_average():
    rng = random.Random(5)
    n, opps = 200, ("a", "b", "c")
    diffs = {o: {(31, g): rng.choice((-1, 0, 0, 1)) for g in range(n)} for o in opps}
    per_game = [sum(diffs[o][(31, g)] for o in opps) / len(opps) for g in range(n)]
    m = sum(per_game) / n
    want = (sum((x - m) ** 2 for x in per_game) / (n - 1) / n) ** 0.5
    assert panel.clustered_se(diffs) == pytest.approx(want)
    # games of different opponents that never overlap are independent clusters: ~ the old sum-of-variances
    a = {(31, g): float(rng.random() < 0.7) for g in range(200)}
    b = {(31, g): float(rng.random() < 0.9) for g in range(200, 700)}

    def se1(xs):
        k, mu = len(xs), sum(xs.values()) / len(xs)
        return (sum((x - mu) ** 2 for x in xs.values()) / (k - 1) / k) ** 0.5
    assert panel.clustered_se({"a": a, "b": b}) == pytest.approx((se1(a) ** 2 + se1(b) ** 2) ** 0.5 / 2, rel=0.01)
    assert panel.clustered_se({"a": a}) == pytest.approx(se1(a))
    assert panel.clustered_se({"a": {(31, 0): 1.0}}) == 0.0


def test_verdict_needs_the_candidates_decision_times():
    # review 2 finding 2: timer_cand / forfeit_cand used to be 0 by construction (the candidate had no clock), so
    # "clean" could not fail on them. Rows now carry the candidate's measured decision times; without them, no verdict.
    base = _rows("a", 50, 100)
    good = _rows("a", 70, 100)
    assert panel.unclean(panel.summarize(good)) == [] and panel.verdict(panel.summarize(good),
                                                                        panel.compare(good, base)) == "PROMOTE"
    untimed = [{k: v for k, v in r.items() if k != "tmax_cand"} for r in good]
    s = panel.summarize(untimed)
    assert s["untimed_cand"] == 100 and "without the candidate's decision times" in panel.unclean(s)[0]
    assert panel.verdict(s, panel.compare(untimed, base)) == "REJECT"
    slow = [dict(r, tmax_cand=70.0, timer_cand=7, forfeit="cand") if r["g"] == 3 else r for r in good]
    s = panel.summarize(slow)
    assert s["tmax_cand"] == 70.0 and s["timer_cand"] == 7 and s["forfeit_cand"] == 1
    assert len(panel.unclean(s)) == 2 and panel.verdict(s, panel.compare(slow, base)) == "REJECT"


def test_the_panel_candidate_plays_on_the_wall_clock():
    from local import field_opps as fo
    from local import pokemon_env as pe
    seat = panel.make_candidate(lambda: "the agent", {"PANEL_T_X": "1"})
    assert isinstance(seat.player, fo.WallClockSeat) and isinstance(seat.player.player, pe.ContestantPlayer)
    seat.reset()
    assert seat.agent == "the agent" and seat.player.stats["bank_left"] == 510.0 and seat.env == {"PANEL_T_X": "1"}


def test_default_seed_is_not_a_benchmark_or_ladder_seed():
    # review 2 finding 5: with seed 31 six of the 13 rows are local.benchmark's own games
    assert panel.DEFAULT_SEED not in (31, 7, 11)


def test_near_certain_opponents_do_not_blunt_the_verdict():
    base = _rows("a", 50, 200)
    cand = _rows("a", 62, 200)
    z = lambda c: c["panel_diff"] / ((c["panel_ci"][1] - c["panel_ci"][0]) / (2 * panel.Z))   # noqa: E731
    c1 = panel.compare(cand, base)
    extra_b = _rows("smoke", 200, 200) + _rows("basic", 199, 200)
    extra_c = _rows("smoke", 200, 200) + _rows("basic", 199, 200)
    c2 = panel.compare(cand + extra_c, base + extra_b)
    assert z(c2) == pytest.approx(z(c1))


def test_opponent_registry():
    assert panel.resolve_opps("all") == list(panel.PANEL)
    assert len(panel.PANEL) == 13 and panel.PANEL[:8] == ("official_baseline", "random", "v0", "v1", "v2", "cautious",
                                                          "greedy", "protect_spam")
    assert panel.resolve_opps("strength") == list(panel.STRENGTH)
    assert panel.resolve_opps("field") == ["basic", "messaging", "smoke", "llm_fallback", "llm_timeout"]
    assert panel.resolve_opps("v1, smoke,v1") == ["v1", "smoke"]
    assert panel.opponent_package("v1") == "versions.v1_exact_damage"
    assert panel.opponent_package("v2") == "versions.v2_joint_lookahead"
    assert panel.opponent_package("v3") == "versions.v3_draft_preview"
    assert panel.opponent_package("v0_heuristic") == "versions.v0_heuristic"
    assert panel.opponent_package("cautious") == "local.sparring.v2_cautious"
    with pytest.raises(SystemExit):
        panel.resolve_opps("v1,nonexistent")


def test_default_output_stays_out_of_the_real_recordings_glob():
    # scripts/replay_real.py, local.pool --harvest and tests read logs/*/*.jsonl as real platform recordings
    rel = panel.default_out("x", 31).relative_to(panel.ROOT / "logs")
    assert len(rel.parts) >= 3 and rel.suffix == ".jsonl"


def test_field_group_matches_the_field_registry():
    from local import field_opps
    assert tuple(field_opps.FIELD) == panel.FIELD_ALL
    assert set(panel.FIELD_PANEL) <= set(panel.FIELD_ALL)


# ---------------------------------------------------------------- the candidate's environment stays the candidate's
def test_scoped_env_sets_and_restores(monkeypatch):
    monkeypatch.setenv("PANEL_T_KEEP", "old")
    monkeypatch.delenv("PANEL_T_NEW", raising=False)
    with panel.scoped_env({"PANEL_T_KEEP": "cand", "PANEL_T_NEW": "x"}):
        assert os.environ["PANEL_T_KEEP"] == "cand" and os.environ["PANEL_T_NEW"] == "x"
    assert os.environ["PANEL_T_KEEP"] == "old" and "PANEL_T_NEW" not in os.environ


def test_only_the_scoped_player_sees_the_candidate_env(monkeypatch):
    monkeypatch.delenv("LOOKAHEAD_ALPHA", raising=False)

    class Probe:
        name = "probe"

        def __init__(self):
            self.seen = []

        def reset(self):
            self.seen.append(("reset", os.environ.get("LOOKAHEAD_ALPHA")))

        def choose(self, raw):
            self.seen.append(("choose", os.environ.get("LOOKAHEAD_ALPHA")))
            return None

    cand, opp = Probe(), Probe()
    scoped = panel.Scoped(cand, {"LOOKAHEAD_ALPHA": "0.9"})
    for p in (scoped, opp):
        p.reset()
        p.choose({})
    assert cand.seen == [("reset", "0.9"), ("choose", "0.9")]
    assert opp.seen == [("reset", None), ("choose", None)] and "LOOKAHEAD_ALPHA" not in os.environ
    assert scoped.name == "probe"


def test_warnings_are_counted_for_whoever_was_acting():
    import logging

    class Loud:
        name = "loud"

        def __init__(self, msg):
            self.msg = msg

        def reset(self):
            pass

        def choose(self, raw):
            # the candidate and a frozen copy of the same code share logger names (and maybe files)
            logging.getLogger("agent.policy").warning(self.msg, exc_info=False)
            return None

    root = logging.getLogger()
    root.addHandler(panel.WARNS)
    try:
        panel.WARNS.reset()
        cand = panel.Scoped(Loud("preview matrix failed; using the heuristic preview"), {})
        opp = panel.Scoped(Loud("joint lookahead hit the 2.5s cap after 3/9 rows"), {}, "opp")
        cand.choose({})
        cand.choose({})
        opp.choose({})
        logging.getLogger("local.harness").warning("between decisions")
        logging.getLogger("agent.policy").info("below WARNING: not counted")
        w = panel.WARNS
        assert w.warn == {"cand": 2, "opp": 1, "env": 1} and w.preview == {"cand": 2, "opp": 0, "env": 0}
        assert w.first["cand"].startswith("agent.policy: preview matrix failed") and w.side is None
    finally:
        root.removeHandler(panel.WARNS)
        panel.WARNS.reset()
    rows = _rows("v3", 5, 10, pvfb_cand=1, warn_cand=2, warn_opp=0)
    p = panel.summarize(rows)["per_opp"]["v3"]
    assert p["pvfb_cand"] == 10 and p["pvfb_opp"] == 0 and p["warn_cand"] == 20
    assert panel.summarize(_rows("v1", 5, 10))["per_opp"]["v1"]["pvfb_cand"] == 0     # rows from older runs


def test_preview_wall_cuts_are_counted_without_changing_the_answer(monkeypatch):
    import sys
    import types
    mod = types.ModuleType("panel_t_pkg.pokemon.preview")
    answers = iter([{"bring": [0, 1, 2, 3], "info": {"wall_hit": True}}, {"bring": [1, 2, 3, 4], "info": {}}, None])
    mod.choose = lambda *a, **k: next(answers)
    monkeypatch.setitem(sys.modules, "panel_t_pkg.pokemon.preview", mod)
    assert panel.instrument_preview("panel_t_pkg") and not panel.instrument_preview("panel_t_pkg")   # once
    assert not panel.instrument_preview("panel_t_missing")
    panel.WARNS.reset()
    with panel.WARNS.acting("opp"):
        assert mod.choose(1, x=2) == {"bring": [0, 1, 2, 3], "info": {"wall_hit": True}}
        assert mod.choose() == {"bring": [1, 2, 3, 4], "info": {}}
        assert mod.choose() is None
    assert panel.WARNS.preview_wall == {"cand": 0, "opp": 1, "env": 0}
    panel.WARNS.reset()
    # the real v3 policy calls preview.choose through the module, so the wrapper sees its games
    pv = importlib.import_module("versions.v3_draft_preview.agent.pokemon.preview")
    pol = importlib.import_module("versions.v3_draft_preview.agent.pokemon.policy")
    monkeypatch.setattr(pv, "choose", pv.choose)                     # restored after the test
    assert panel.instrument_preview("versions.v3_draft_preview.agent")
    assert pol.preview_matrix.choose is pv.choose and pv.choose.__wrapped__ is not None


def test_shell_knobs_are_the_names_the_frozen_versions_read():
    env = {"LOOKAHEAD_MIX": "argmax", "ARENA_DRAFT_TEMP": "1", "PREVIEW_MATRIX_KNOBS": "{}", "PATH": "/bin",
           "ARENA_RECORD": "0"}
    assert set(panel.shell_knobs(env)) == {"LOOKAHEAD_MIX", "ARENA_DRAFT_TEMP", "PREVIEW_MATRIX_KNOBS"}


def test_calc_origin_flags_a_foreign_calculator(tmp_path, monkeypatch):
    import sys
    import types
    pkg = tmp_path / "pkg" / "agent"
    (pkg / "pokemon").mkdir(parents=True)
    (pkg / "js").mkdir()
    (pkg / "js" / "calc.js").write_text("own")
    other = tmp_path / "other"
    other.mkdir()
    (other / "calc.js").write_text("candidate's")
    mod = types.ModuleType("fakepkg.agent.pokemon.calc")
    mod.__file__ = str(pkg / "pokemon" / "calc.py")
    monkeypatch.setitem(sys.modules, "fakepkg.agent.pokemon.calc", mod)
    assert panel.calc_origin("fakepkg.agent", {"calc_js": str(other), "calc_enabled": True})["foreign"] is True
    assert panel.calc_origin("fakepkg.agent", {"calc_js": str(pkg / "js"), "calc_enabled": True})["foreign"] is False
    (other / "calc.js").write_text("own")                                  # byte-identical copy: fine
    assert panel.calc_origin("fakepkg.agent", {"calc_js": str(other), "calc_enabled": True})["foreign"] is False


# ---------------------------------------------------------------- frozen packages
def test_cautious_ignores_the_candidates_env(monkeypatch):
    monkeypatch.setenv("LOOKAHEAD_MIX", "sample")
    monkeypatch.setenv("LOOKAHEAD_SWITCH_FIX", "0")
    cautious = importlib.import_module("local.sparring.v2_cautious.agent.pokemon.lookahead")
    assert cautious.mix_mode() == "argmax" and cautious.switch_fix() is True


# ---------------------------------------------------------------- bots on synthetic boards
ME = [{"species": "charizard", "current_hp_fraction": 1.0, "fainted": False},
      {"species": "venusaur", "current_hp_fraction": 1.0, "fainted": False}]
FOES = [{"species": "blastoise", "current_hp_fraction": 1.0, "fainted": False},
        {"species": "pikachu", "current_hp_fraction": 0.1, "fainted": False}]


def _hit(dmg, hp=200):
    return {"rolls": [dmg] * 16, "defender_hp": hp}


def _slot(i, *opts):
    return {"slot": i, "options": list(opts)}


def _mv(mid, targets):
    return {"type": "move", "move_id": mid, "targets": targets}


def test_greedy_takes_the_biggest_expected_damage_and_never_hits_its_ally():
    p = bots.GreedyPolicy()
    table = {("me", 0, "flamethrower", "opp", 0): _hit(100),          # 50% of blastoise
             ("me", 0, "flamethrower", "opp", 1): _hit(100),          # capped at pikachu's 10%
             ("me", 0, "airslash", "opp", 1): _hit(30)}
    slot = _slot(0, _mv("flamethrower", [1, 2, -2]), _mv("airslash", [1, 2, -2]), _mv("protect", [0]),
                 {"type": "switch", "species": "garchomp"})
    assert p._slot_choice(slot, {}, ME, FOES, table, set()) == {"type": "move", "move_id": "flamethrower", "target": 1}
    zero = {k: _hit(0) for k in table}                                  # nothing does damage: still never the ally
    c = p._slot_choice(slot, {}, ME, FOES, zero, set())
    assert c["type"] == "move" and c["target"] != -2


def test_greedy_fake_out_only_counts_on_the_first_turn_out():
    p = bots.GreedyPolicy()
    table = {("me", 0, "fakeout", "opp", 0): _hit(60), ("me", 0, "flamethrower", "opp", 0): _hit(40)}
    slot = _slot(0, _mv("fakeout", [1, 2, -2]), _mv("flamethrower", [1, 2, -2]))
    assert p._slot_choice(slot, {}, ME, FOES, table, set())["move_id"] == "fakeout"
    p.prev_active = ["charizard", "venusaur"]                          # it was already out last decision
    assert p._slot_choice(slot, {}, ME, FOES, table, set())["move_id"] == "flamethrower"


def test_greedy_switches_only_when_forced_and_passes_legally():
    p = bots.GreedyPolicy()
    forced = _slot(0, {"type": "switch", "species": "garchomp"}, {"type": "switch", "species": "rillaboom"})
    used = {"garchomp"}
    assert p._slot_choice(forced, {}, ME, FOES, {}, used) == {"type": "switch", "species": "rillaboom"}
    only_used = _slot(1, {"type": "switch", "species": "garchomp"}, {"type": "pass"})
    assert p._slot_choice(only_used, {}, ME, FOES, {}, used) == {"type": "pass"}


def test_protect_spam_protects_when_threatened_but_not_twice_in_a_row():
    p = bots.ProtectSpamPolicy()
    table = {("me", 0, "flamethrower", "opp", 0): _hit(100),
             ("opp", 0, "hydropump", "me", 0): _hit(150, 150)}            # a KO on every roll
    slot = _slot(0, _mv("flamethrower", [1, 2, -2]), _mv("protect", [0]))
    assert p._slot_choice(slot, {"turn": 3}, ME, FOES, table, set())["move_id"] == "protect"
    assert p._slot_choice(slot, {"turn": 4}, ME, FOES, table, set())["move_id"] == "flamethrower"
    assert p._slot_choice(slot, {"turn": 5}, ME, FOES, table, set())["move_id"] == "protect"
    calm = {("me", 0, "flamethrower", "opp", 0): _hit(100), ("opp", 0, "hydropump", "me", 0): _hit(10, 150)}
    assert bots.ProtectSpamPolicy()._slot_choice(slot, {"turn": 3}, ME, FOES, calm, set())["move_id"] == "flamethrower"


@pytest.mark.skipif(not (JS_DIR / "node_modules").exists(), reason="npm install not run in local/js")
def test_bots_play_legal_matches():
    from local.pokemon_env import ContestantPlayer, play_match
    from local.showdown import Showdown
    a = ContestantPlayer(lambda: bots.create_agent("greedy"), "greedy")
    b = ContestantPlayer(lambda: bots.create_agent("protect_spam"), "protect_spam")
    random.seed(0)
    with Showdown() as sd:
        for g in range(2):
            r = play_match(sd, a, b, seed=4200 + g)
            assert r.invalid == [0, 0] and r.timer_defaults == [0, 0] and r.forfeit is None
            assert a.agent.stats["fallbacks"] == 0 and b.agent.stats["fallbacks"] == 0
