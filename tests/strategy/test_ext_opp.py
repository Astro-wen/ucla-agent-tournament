"""The `simbot` outside-style opponent (local/ext_opp.py, local/js/simbot.js), same-team matches
(Match.draft_same), protocol logs (play_match keep_log) and their panel wiring. Showdown-backed tests skip without
local/js/node_modules."""
import json
import random
import shutil
import subprocess

import pytest

from local import ext_opp, panel
from local import pokemon_env as pe
from local.showdown import JS_DIR

NODE = shutil.which("node") is not None and (JS_DIR / "node_modules").exists()
needs_node = pytest.mark.skipif(not NODE, reason="node / npm install in local/js missing")
FAST = {"samples": 1, "keep": 4, "slot_keep": 2, "probes": 1, "rollout": 0, "iters": 200}


# ---------------------------------------------------------------- registry
def test_simbot_is_opt_in_and_resolvable():
    assert "simbot" not in panel.PANEL and all("simbot" not in g for g in panel.GROUPS.values())
    assert panel.resolve_opps("simbot") == ["simbot"]
    assert panel.resolve_opps("v1,simbot_v1") == ["v1", "simbot_v1"]
    assert set(panel.EXT) == set(ext_opp.NAMES)


def test_only_parses_game_lists():
    assert panel.parse_only("12, 3,30-32,3") == [3, 12, 30, 31, 32]


def test_tap_records_preview_and_battle_answers_and_passes_everything_through():
    class P:
        name = "p"

        def __init__(self):
            self.resets, self.bound = 0, None

        def reset(self):
            self.resets += 1

        def bind(self, match, seat):
            self.bound = seat

        def choose(self, raw):
            return {"type": "doubles_turn", "slot_0": {"type": "pass"}} if raw["phase"] == "moving" else "draft_pick:x"

    p = P()
    t = panel.Tap(p)
    t.reset()
    t.bind(None, 1)
    assert t.choose({"phase": "draft"}) == "draft_pick:x"
    out = t.choose({"phase": "moving", "observation": {"turn": 3}})
    assert out["slot_0"] == {"type": "pass"} and p.resets == 1 and p.bound == 1 and t.name == "p"
    assert t.trace == [{"phase": "moving", "turn": 3, "out": out}]


def test_knobs_come_from_the_environment():
    k = ext_opp.knobs_from_env({"SIMBOT_SAMPLES": "3", "SIMBOT_MODE": "maximin", "SIMBOT_MIN_PROB": "0.1", "X": "1"})
    assert k == {"samples": 3, "mode": "maximin", "min_prob": 0.1}


# ---------------------------------------------------------------- template check
def _raw():
    tmpl = {"type": "doubles_turn", "slots": [
        {"slot": 0, "species": "incineroar", "options": [
            {"type": "move", "move_id": "fakeout", "targets": [1, 2, -2]},
            {"type": "switch", "species": "amoonguss"}]},
        {"slot": 1, "species": "rillaboom", "options": [
            {"type": "move", "move_id": "protect", "targets": [0]},
            {"type": "switch", "species": "amoonguss"}]}]}
    return {"phase": "moving", "legal_actions": {"actions": [{"input": {"action": tmpl}}]}}


def test_template_ok_matches_the_env_rules():
    ok = {"type": "doubles_turn", "slot_0": {"type": "move", "move_id": "fakeout", "target": 2},
          "slot_1": {"type": "move", "move_id": "protect", "target": 0}}
    assert ext_opp.template_ok(_raw(), ok)
    for bad in ({**ok, "slot_0": {"type": "move", "move_id": "fakeout", "target": 0}},
                {**ok, "slot_0": {"type": "switch", "species": "amoonguss"},
                 "slot_1": {"type": "switch", "species": "amoonguss"}},
                {**ok, "slot_1": {"type": "pass"}},
                {"type": "doubles_turn", "slot_0": ok["slot_0"]}):
        assert not ext_opp.template_ok(_raw(), bad)


# ---------------------------------------------------------------- same-team draft (no battle)
class Picker(pe.Player):
    """Takes the first legal action; remembers every draft state it saw."""
    name = "picker"

    def reset(self):
        self.seen = []

    def choose(self, raw):
        self.seen.append(raw)
        return raw["legal_actions"]["actions"][0]["input"]["action"]


def test_draft_same_gives_both_seats_the_same_six_and_shows_every_set():
    a, b = Picker(), Picker()
    for seed in range(5):
        m = pe.Match(None, [a, b], random.Random(seed))
        m.draft_same()
        sp = [sorted(c["species"] for c in r) for r in m.rosters]
        assert sp[0] == sp[1] and len(set(sp[0])) == 6
        assert m.invalid == [0, 0] and m.same_team
        pool_ids = {c["card_id"] for c in m.pool}
        ids = [{c["card_id"] for c in r} for r in m.rosters]
        orig = [i for i in (0, 1) if ids[i] <= pool_ids]
        assert len(orig) == 1                                  # the first drafter holds the real ids,
        twin = ids[1 - orig[0]]                                # the other seat the TWIN copies
        assert all(x.endswith(pe.TWIN) for x in twin) and not twin & pool_ids
        for p in (a, b):                                       # every state offers exactly one legal pick
            assert all(len(s["legal_actions"]["actions"]) == 1 for s in p.seen)
        first = a.seen[0] if len(a.seen[0]["observation"]["available_cards"]) == 12 else b.seen[0]
        assert len(first["observation"]["available_cards"]) == 12


def test_draft_same_counts_a_wrong_pick_as_invalid_but_deals_the_card():
    class Wrong(Picker):
        def choose(self, raw):
            super().choose(raw)
            return {"type": "draft_pick", "card_id": "nope"}
    m = pe.Match(None, [Wrong(), Picker()], random.Random(3))
    m.draft_same()
    assert m.invalid == [6, 0] and len(m.rosters[0]) == len(m.rosters[1]) == 6


# ---------------------------------------------------------------- the JS matrix solver
@needs_node
def test_regret_matching_solves_small_zero_sum_games():
    js = ("const s=require('./simbot');"
          "const r=s.solve([[0,-1,1],[1,0,-1],[-1,1,0]],3000);"
          "const d=s.solve([[3,1],[2,0]],500);"
          "console.log(JSON.stringify({rps:r.x, v:r.value, dom:d.x, dv:d.value}));")
    out = json.loads(subprocess.run(["node", "-e", js], cwd=JS_DIR, capture_output=True, text=True,
                                    timeout=60, check=True).stdout)
    assert all(abs(p - 1 / 3) < 0.05 for p in out["rps"]) and abs(out["v"]) < 0.05
    assert out["dom"][0] > 0.95 and abs(out["dv"] - 1) < 0.05     # row 0 dominates; column 1 is the best reply


# ---------------------------------------------------------------- whole matches
@needs_node
def test_simbot_plays_legal_matches_and_logs_them():
    from local.showdown import Showdown
    bot = ext_opp.SimBot("simbot_v1", params=FAST)      # v1's draft / preview: no champion calc start-up needed
    rnd = pe.RandomPlayer()
    with Showdown() as sd:
        for g in range(2):
            players = [bot, rnd] if g % 2 == 0 else [rnd, bot]
            r = pe.play_match(sd, players[0], players[1], seed=4400 + g, keep_log=True)
            assert r.invalid == [0, 0] and r.forfeit is None
            assert bot.stats["fallbacks"] == 0 and bot.stats["decisions"] > 0 and bot.stats["sims"] > 0
            assert any(line.startswith("|win|") for line in r.log) and r.log[0].startswith("|t:|")
            assert bot.trace and all("choice" in t for t in bot.trace)


@needs_node
def test_same_team_match_is_a_mirror():
    from local.showdown import Showdown
    a, b = pe.RandomPlayer(), pe.RandomPlayer()
    random.seed(0)
    with Showdown() as sd:
        r = pe.play_match(sd, a, b, seed=4500, same_team=True)
    assert r.same_team and sorted(r.rosters[0]) == sorted(r.rosters[1]) and r.invalid == [0, 0]
    assert r.log is None                                   # keep_log is opt-in


@needs_node
def test_search_leaves_the_live_battle_untouched():
    """The search clones: the live battle's turn, log and pending requests are exactly as before."""
    from local.pool import POOL, to_showdown_set
    from local.showdown import Showdown
    rng = random.Random(9)
    teams = [[to_showdown_set(c) for c in rng.sample(POOL, 4)] for _ in range(2)]
    with Showdown() as sd:
        sd.start(teams[0], teams[1], seed=[1, 2, 3, 4])
        snap = sd.choose("team 1234", "team 1234")
        before = snap["requests"]
        res = sd.search("p2", [5, 6, 7, 8], FAST)
        assert "error" not in res and len(res["slots"]) == 2
        again = sd.choose()                                # a no-op choose returns the current snapshot
        assert again["requests"] == before and again["log"] == [] and again["state"]["turn"] == snap["state"]["turn"]
