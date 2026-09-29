"""The engine-search battle policy (ARENA_BATTLE=search): protocol-log parsing (agent/pokemon/battlelog.py), the
platform-legal battle description (agent/pokemon/battlestate.py), the Node engine (agent/js/search.js via
agent/pokemon/engine.py), its policy integration and fallbacks, and the local-sim protocol log (ARENA_PLOG=1).
Engine-backed tests skip without node or pokemon-showdown."""
import json
import os
import random
import shutil

import pytest

from agent.pokemon import battlelog, battlestate, engine
from agent.pokemon import policy as pol
from agent.pokemon.dex import card_catalog, to_id

HAVE_ENGINE = shutil.which("node") is not None and engine._paths() is not None
needs_engine = pytest.mark.skipif(not HAVE_ENGINE, reason="node / pokemon-showdown missing")
FAST = {"samples": 1, "keep": 4, "slot_keep": 2, "probes": 1, "rollout": 0, "iters": 200, "budget_ms": 1500}

# A real platform log (our own match 141b61de, we are p2), up to the start of turn 4.
REAL = """|init|battle
|gametype|doubles
|player|p1|able-p0|266|
|player|p2|able-p1|169|
|gen|9
|poke|p1|Iron Valiant, L50|
|poke|p1|Archaludon, L50, M|
|poke|p1|Chien-Pao, L50|
|poke|p1|Iron Hands, L50|
|poke|p1|Gyarados, L50, M|
|poke|p1|Arcanine, L50, M|
|poke|p2|Incineroar, L50, M|
|poke|p2|Tornadus, L50, M|
|poke|p2|Flutter Mane, L50|
|poke|p2|Rillaboom, L50, M|
|poke|p2|Garchomp, L50, M|
|poke|p2|Tyranitar, L50, F|
|teampreview|4
|inactive|Time left: 90 sec this turn | 420 sec total | 90 sec grace
|teamsize|p1|4
|teamsize|p2|4
|start
|switch|p1a: Iron Hands|Iron Hands, L50|100/100
|switch|p1b: Gyarados|Gyarados, L50, M|100/100
|switch|p2a: Incineroar|Incineroar, L50, M|201/201
|switch|p2b: Rillaboom|Rillaboom, L50, M|205/205
|-fieldstart|move: Grassy Terrain|[from] ability: Grassy Surge|[of] p2b: Rillaboom
|-ability|p1b: Gyarados|Intimidate|boost
|-unboost|p2a: Incineroar|atk|1
|-unboost|p2b: Rillaboom|atk|1
|-ability|p2a: Incineroar|Intimidate|boost
|-unboost|p1a: Iron Hands|atk|1
|-unboost|p1b: Gyarados|atk|1
|turn|1
|inactive|Time left: 55 sec this turn | 420 sec total
|
|move|p2b: Rillaboom|Fake Out|p1a: Iron Hands
|-damage|p1a: Iron Hands|94/100
|move|p2a: Incineroar|Fake Out|p1b: Gyarados
|-damage|p1b: Gyarados|93/100
|cant|p1b: Gyarados|flinch
|cant|p1a: Iron Hands|flinch
|
|-heal|p1a: Iron Hands|100/100|[from] Grassy Terrain
|upkeep
|turn|2
|
|move|p2b: Rillaboom|Wood Hammer|p1b: Gyarados
|-damage|p1b: Gyarados|39/100
|-enditem|p1b: Gyarados|Sitrus Berry|[eat]
|-heal|p1b: Gyarados|63/100|[from] item: Sitrus Berry
|-damage|p2b: Rillaboom|169/205|[from] Recoil
|move|p1b: Gyarados|Waterfall|p2a: Incineroar
|-supereffective|p2a: Incineroar
|-damage|p2a: Incineroar|93/201
|move|p2a: Incineroar|Knock Off|p1b: Gyarados
|-damage|p1b: Gyarados|44/100
|move|p1a: Iron Hands|Close Combat|p2a: Incineroar
|-supereffective|p2a: Incineroar
|-damage|p2a: Incineroar|0 fnt
|-unboost|p1a: Iron Hands|def|1
|-unboost|p1a: Iron Hands|spd|1
|faint|p2a: Incineroar
|
|-heal|p2b: Rillaboom|181/205|[from] Grassy Terrain
|upkeep
|
|switch|p2a: Flutter Mane|Flutter Mane, L50|130/130
|-enditem|p2a: Flutter Mane|Booster Energy
|-activate|p2a: Flutter Mane|ability: Protosynthesis|[fromitem]
|-start|p2a: Flutter Mane|protosynthesisspe
|turn|3
|
|move|p2a: Flutter Mane|Moonblast|p1a: Iron Hands
|-supereffective|p1a: Iron Hands
|-damage|p1a: Iron Hands|0 fnt
|faint|p1a: Iron Hands
|-end|p1a: Iron Hands|Quark Drive|[silent]
|move|p2b: Rillaboom|Wood Hammer|p1b: Gyarados
|-damage|p1b: Gyarados|0 fnt
|faint|p1b: Gyarados
|-damage|p2b: Rillaboom|152/205|[from] Recoil
|
|-heal|p2b: Rillaboom|164/205|[from] Grassy Terrain
|upkeep
|
|switch|p1b: Arcanine|Arcanine, L50, M|100/100
|switch|p1a: Archaludon|Archaludon, L50, M|100/100
|-ability|p1b: Arcanine|Intimidate|boost
|-unboost|p2a: Flutter Mane|atk|1
|-unboost|p2b: Rillaboom|atk|1
|turn|4""".splitlines()


def _view(sp, hp, mx, **kw):
    v = {"species": sp, "current_hp": hp, "max_hp": mx, "current_hp_fraction": hp / mx if mx else 0,
         "status": None, "fainted": hp == 0, "boosts": {}}
    v.update(kw)
    return v


def real_obs():
    """The platform observation of that state (shapes as in docs/real_payloads.md)."""
    fm = _view("fluttermane", 130, 130, item="", ability="protosynthesis", boosts={"atk": -1})
    rb = _view("rillaboom", 164, 205, item="assaultvest", ability="grassysurge", boosts={"atk": -2})
    team = {"p2: Incineroar": _view("incineroar", 0, 201, status="FNT", item="safetygoggles"),
            "p2: Tornadus": _view("tornadus", 186, 186, item="mentalherb"),
            "p2: Flutter Mane": fm, "p2: Rillaboom": rb}
    ar = _view("archaludon", 100, 100, item="unknown_item")
    ac = _view("arcanine", 100, 100, item="unknown_item", ability="intimidate")
    opp = {"p1: Iron Hands": _view("ironhands", 0, 100, status="FNT", item="unknown_item"),
           "p1: Gyarados": _view("gyarados", 0, 100, status="FNT", item=None),
           "p1: Arcanine": ac, "p1: Archaludon": ar}
    return {"phase": "moving", "is_doubles": True, "turn": 4, "active_pokemon": [fm, rb],
            "opponent_active_pokemon": [ar, ac], "team": team, "opponent_team": opp,
            "fields": {"GRASSY_TERRAIN": 0}, "weather": {}, "side_conditions": {}, "opponent_side_conditions": {},
            "force_switch": [False, False], "protocol_log": list(REAL)}


def real_tmpl():
    return {"type": "doubles_turn", "slots": [
        {"slot": 0, "options": [{"type": "move", "move_id": "moonblast", "targets": [-2, 1, 2]},
                                {"type": "move", "move_id": "shadowball", "targets": [-2, 1, 2]},
                                {"type": "move", "move_id": "icywind", "targets": [0]},
                                {"type": "move", "move_id": "protect", "targets": [0]},
                                {"type": "switch", "species": "tornadus"}]},
        {"slot": 1, "options": [{"type": "move", "move_id": "fakeout", "targets": [-1, 1, 2]},
                                {"type": "move", "move_id": "woodhammer", "targets": [-1, 1, 2]},
                                {"type": "move", "move_id": "grassyglide", "targets": [-1, 1, 2]},
                                {"type": "move", "move_id": "uturn", "targets": [-1, 1, 2]},
                                {"type": "switch", "species": "tornadus"}]}]}


def cards(*names):
    by = {to_id(c["species"]): c for c in card_catalog().values()}
    return {to_id(n): by[to_id(n)] for n in names}


MINE = ("Incineroar", "Tornadus", "Flutter Mane", "Rillaboom", "Garchomp", "Tyranitar")
THEIRS = ("Iron Valiant", "Archaludon", "Chien-Pao", "Iron Hands", "Gyarados", "Arcanine")


def real_state(**kw):
    return battlestate.build(real_obs(), real_tmpl(), cards(*MINE), cards(*THEIRS),
                             opp_roster=[to_id(x) for x in THEIRS], **kw)


# ---------------------------------------------------------------- the protocol log
def test_real_log_parses_items_volatiles_terrain_and_actions():
    lg = battlelog.parse(REAL)
    assert lg.turn == 4 and lg.upkeeps == 3 and not lg.after_upkeep and lg.errors == 0
    assert lg.revealed["p1"] == ["ironhands", "gyarados", "arcanine", "archaludon"]
    assert lg.active["p2a"] == ("p2", "fluttermane") and lg.active["p1b"] == ("p1", "arcanine")
    assert lg.mons[("p1", "gyarados")].item_lost and lg.mons[("p1", "gyarados")].fainted
    fm = lg.mons[("p2", "fluttermane")]
    assert fm.item_lost and fm.volatiles["protosynthesis"]["stat"] == "spe" and fm.volatiles["protosynthesis"]["booster"]
    assert fm.actions == 1 and fm.switch_up == 2
    rb = lg.mons[("p2", "rillaboom")]
    assert rb.actions == 3 and rb.last_move == "woodhammer"                     # no more Fake Out
    assert lg.mons[("p1", "archaludon")].actions == 0                           # fresh: Fake Out would work
    assert lg.terrain == {"id": "grassyterrain", "up": 0, "setter": ("p2", "rillaboom")}
    assert battlelog.my_side(real_obs()) == "p2"


def _log(*lines):
    return battlelog.parse(["|switch|p1a: A|Incineroar, L50|100/100", "|switch|p1b: B|Rillaboom, L50|100/100",
                            "|switch|p2a: C|Amoonguss, L50|100/100", "|switch|p2b: D|Tornadus, L50|100/100",
                            "|turn|1", *lines])


def test_protect_chain_counts_successes_and_a_failure_resets_it():
    lg = _log("|move|p1a: A|Protect||[still]", "|-singleturn|p1a: A|Protect", "|upkeep", "|turn|2",
              "|move|p1a: A|Protect||[still]", "|-singleturn|p1a: A|Protect", "|upkeep", "|turn|3")
    m = lg.mons[("p1", "incineroar")]
    assert m.stall == 2 and m.stall_up == 1 and lg.upkeeps == 2
    lg = _log("|move|p1a: A|Protect||[still]", "|-singleturn|p1a: A|Protect", "|upkeep", "|turn|2",
              "|move|p1a: A|Protect|", "|-fail|p1a: A", "|upkeep", "|turn|3")
    assert lg.mons[("p1", "incineroar")].stall == 0


def test_side_and_field_conditions_remember_when_they_started():
    lg = _log("|move|p2b: D|Tailwind|p2b: D", "|-sidestart|p2: x|move: Tailwind", "|upkeep", "|turn|2",
              "|move|p1a: A|Trick Room|p1a: A", "|-fieldstart|move: Trick Room|[of] p1a: A", "|upkeep", "|turn|3")
    assert lg.sides["p2"]["tailwind"]["up"] == 0 and lg.sides["p2"]["tailwind"]["setter"] == ("p2", "tornadus")
    assert lg.pseudo["trickroom"]["up"] == 1 and lg.upkeeps == 2
    lg = _log("|-sidestart|p2: x|move: Tailwind", "|upkeep", "|-sideend|p2: x|move: Tailwind")
    assert "tailwind" not in lg.sides["p2"]


def test_taunt_sleep_toxic_wish_and_glaive_rush():
    lg = _log("|move|p2a: C|Spore|p1b: B", "|-status|p1b: B|slp|[from] move: Spore",
              "|move|p1a: A|Taunt|p2a: C", "|-start|p2a: C|move: Taunt", "|upkeep", "|turn|2",
              "|cant|p1b: B|slp", "|move|p2b: D|Wish|p2b: D", "|upkeep", "|turn|3")
    c = lg.mons[("p2", "amoonguss")]
    assert c.volatiles["taunt"]["moved"] and c.volatiles["taunt"]["up"] == 0      # it had moved: 4 turns
    assert lg.mons[("p1", "rillaboom")].slp_cants == 1 and lg.wish["p2b"]["up"] == 1
    hit = _log("|move|p1a: A|Glaive Rush|p2a: C", "|-damage|p2a: C|50/100")
    assert hit.mons[("p1", "incineroar")].glaive_up == 0
    blocked = _log("|move|p1a: A|Glaive Rush|p2a: C", "|-activate|p2a: C|move: Protect")
    assert blocked.mons[("p1", "incineroar")].glaive_up < 0


def test_the_parser_survives_garbage():
    lg = battlelog.parse(["garbage", None, 3, "|switch|", "|move|", "|-start|p9z: ?|x", "|turn|x", "|-damage|p1a"])
    assert lg.turn == 0 and not lg.mons
    assert battlelog.parse("not a list").lines == 0


# ---------------------------------------------------------------- the battle description
def test_state_from_the_real_observation():
    st = real_state()
    assert st["me"] == "p2" and st["request"] == "move" and st["turn"] == 4
    mine = st["sides"]["p2"]["mons"]
    assert [to_id(m["set"]["species"]) for m in mine][:2] == ["fluttermane", "rillaboom"]
    inc = next(m for m in mine if to_id(m["set"]["species"]) == "incineroar")
    assert inc["fainted"]
    fm, rb = mine[0], mine[1]
    assert fm["hp"] == 130 and fm["item"] == "" and fm["boosts"] == {"atk": -1}
    assert {"id": "protosynthesis", "fromBooster": True, "bestStat": "spe"} in fm["volatiles"]
    assert rb["move_actions"] == 3 and rb["hp"] == 164 and rb["item"] == "assaultvest"
    foe = st["sides"]["p1"]["mons"]
    assert [to_id(m["set"]["species"]) for m in foe] == ["archaludon", "arcanine", "ironhands", "gyarados"]
    assert foe[0]["move_actions"] == 0 and foe[3]["fainted"] and foe[3]["item"] == ""
    assert st["meta"]["filled"] == [] and st["field"]["terrain"] == {"id": "grassyterrain", "duration": 2}
    assert [len(o) for o in st["options"]] == [5, 5]


def test_unrevealed_foes_come_from_the_preview_prior():
    obs = real_obs()
    lines = REAL[:REAL.index("|switch|p1b: Arcanine|Arcanine, L50, M|100/100")]   # end of turn 3: they must replace
    obs["protocol_log"] = lines
    obs["opponent_active_pokemon"] = [_view("ironhands", 0, 100, status="FNT"), _view("gyarados", 0, 100, status="FNT")]
    prior = [(("ironhands", "gyarados", "chienpao", "ironvaliant"), 0.7),
             (("ironhands", "gyarados", "arcanine", "archaludon"), 0.3)]
    obs["force_switch"] = [True, True]                              # (their switch; ours is only for the test)
    st = battlestate.build(obs, real_tmpl(), cards(*MINE), cards(*THEIRS), opp_roster=[to_id(x) for x in THEIRS],
                           lineup_prior=prior)
    foe = [to_id(m["set"]["species"]) for m in st["sides"]["p1"]["mons"]]
    assert foe[:2] == ["ironhands", "gyarados"] and sorted(foe[2:]) == ["chienpao", "ironvaliant"]
    assert st["meta"]["filled"] == ["chienpao", "ironvaliant"] and st["request"] == "switch"
    assert st["sides"]["p1"]["mons"][0].get("switch_flag")          # their fainted actives are replaced too
    # the local simulator lists all four in opponent_team: only revealed ones may be used
    obs["opponent_team"]["p1: Chien-Pao"] = _view("chienpao", 50, 100)
    st2 = battlestate.build(obs, real_tmpl(), cards(*MINE), cards(*THEIRS), opp_roster=[to_id(x) for x in THEIRS],
                            lineup_prior=prior)
    cp = next(m for m in st2["sides"]["p1"]["mons"] if to_id(m["set"]["species"]) == "chienpao")
    assert cp["hp_frac"] == 1.0


def test_without_a_log_conditions_get_default_turns_and_fake_out_comes_from_the_trackers():
    obs = real_obs()
    obs.pop("protocol_log")
    obs["side_conditions"] = {"TAILWIND": 3}
    st = battlestate.build(obs, real_tmpl(), cards(*MINE), cards(*THEIRS), cond_left={"my_tailwind": 3},
                           prev_mine=["fluttermane", "rillaboom"], prev_foe=[None, None])
    assert st["sides"]["p2"]["conditions"] == [{"id": "tailwind", "duration": 3}]
    assert st["field"]["terrain"]["id"] == "grassyterrain"
    assert st["sides"]["p2"]["mons"][1]["move_actions"] == 1 and st["sides"]["p1"]["mons"][0]["move_actions"] == 0


def test_inconsistent_observations_raise_value_error():
    with pytest.raises(ValueError):
        battlestate.build({"turn": 1}, real_tmpl(), {}, {})
    obs = real_obs()
    obs["team"] = {}
    with pytest.raises(ValueError):
        battlestate.build(obs, real_tmpl(), {}, {})


# ---------------------------------------------------------------- the engine
@pytest.fixture(scope="module")
def eng():
    if not HAVE_ENGINE:
        pytest.skip("engine unavailable")
    e = engine.SearchEngine()
    assert e.warm(60)
    yield e
    e._kill()


@needs_engine
def test_engine_rebuilds_the_real_state(eng):
    s = eng.inspect(state=real_state())
    assert s is not None
    me, foe = s["sides"]["p2"], s["sides"]["p1"]
    assert me["active"] == ["fluttermane", "rillaboom"] and foe["active"] == ["archaludon", "arcanine"]
    mons = {m["species"]: m for m in me["mons"]}
    assert mons["fluttermane"]["hp"] == 130 and mons["rillaboom"]["hp"] == 164 and mons["incineroar"]["fainted"]
    assert mons["fluttermane"]["volatiles"]["protosynthesis"]["bestStat"] == "spe"
    assert mons["rillaboom"]["activeMoveActions"] == 3 and mons["fluttermane"]["boosts"]["atk"] == -1
    assert me["pokemonLeft"] == 3 and foe["pokemonLeft"] == 2 and me["request"] == "move"
    assert s["field"]["terrain"] == "grassyterrain" and s["field"]["terrain_d"] == 2 and s["turn"] == 4
    assert not s["field"]["pseudo"]                                       # no format-rule pseudo weathers left


@needs_engine
def test_engine_search_answers_a_template_move_and_keeps_the_classic_row(eng):
    classic = {"slot_0": {"type": "move", "move_id": "moonblast", "target": 1},
               "slot_1": {"type": "move", "move_id": "woodhammer", "target": 1}}
    ans = eng.search(real_state(classic=classic), dict(FAST, rng=5), 20.0)
    assert ans is not None, eng.last_error
    assert pol._template_ok(real_tmpl(), ans["action"])
    info = ans["info"]
    assert info["sims"] > 0 and info["errors"] == 0 and info["classic_value"] is not None
    assert info["rows"] >= 2 and info["build_ms"] < 2000


@needs_engine
@pytest.mark.parametrize("extra", [{"guard": 1}, {"guard": 1, "guard_delta": -99}, {"guard": 1, "free_delta": -99},
                                   {"mode": "blend"}, {"delta": 99}])
def test_engine_modes_answer_legal_moves(eng, extra):
    classic = {"slot_0": {"type": "move", "move_id": "moonblast", "target": 1},
               "slot_1": {"type": "move", "move_id": "woodhammer", "target": 1}}
    ans = eng.search(real_state(classic=classic), dict(FAST, rng=7, **extra), 20.0)
    assert ans is not None, eng.last_error
    assert pol._template_ok(real_tmpl(), ans["action"])
    info = ans["info"]
    if extra.get("delta") == 99:
        assert info["is_classic"]                                      # never worth 99 more: classic kept
    if extra.get("guard"):
        want = {-99: "guard"}.get(extra.get("guard_delta"), None) or \
            {-99: "free"}.get(extra.get("free_delta"), None)
        assert info["guard"] in ("classic", "guard", "free") and (want is None or info["guard"] == want)
        if info["guard"] == "classic":
            assert ans["action"] == classic
        if info["guard"] == "guard":        # one slot kept, the other a switch or Protect
            kept = [ans["action"][k] == classic[k] for k in classic]
            assert kept.count(True) == 1
            other = ans["action"]["slot_0" if not kept[0] else "slot_1"]
            assert other["type"] == "switch" or other.get("move_id") == "protect"


@needs_engine
def test_fixed_work_ignores_the_time_budget_and_replays_exactly(eng):
    classic = {"slot_0": {"type": "move", "move_id": "moonblast", "target": 1},
               "slot_1": {"type": "move", "move_id": "woodhammer", "target": 1}}
    work = dict(FAST, samples=2, rng=11, mode="blend", budget_ms=1, hard_ms=60000)
    a = eng.search(real_state(classic=classic), dict(work, fixed=1), 60.0)
    b = eng.search(real_state(classic=classic), dict(work, fixed=1), 60.0)
    assert a is not None and b is not None, eng.last_error
    # a 1 ms time budget does not cut a fixed-work search: every sample runs and the answer is reproducible
    assert a["info"]["samples"] == 2 and not a["info"].get("prune_cut") and not a["info"]["cut"]
    assert a["action"] == b["action"] and a["info"]["sims"] == b["info"]["sims"]
    assert a["info"]["pick_value"] == b["info"]["pick_value"]
    t = eng.search(real_state(classic=classic), dict(work, fixed=0), 60.0)      # the time budget: one sample
    assert t is not None and t["info"]["samples"] == 1


@needs_engine
def test_fixed_work_still_stops_at_the_safety_net(eng):
    slow = dict(FAST, samples=50, keep=12, slot_keep=6, rng=2, fixed=1, budget_ms=60000, hard_ms=300)
    ans = eng.search(real_state(), slow, 60.0)
    assert ans is not None, eng.last_error
    assert ans["info"]["samples"] < 50 and ans["info"]["ms"] < 5000
    assert pol._template_ok(real_tmpl(), ans["action"])


@needs_engine
def test_engine_timeout_kills_and_restarts(eng):
    e = engine.SearchEngine()
    assert e.warm(60)
    slow = dict(FAST, samples=50, budget_ms=60000, hard_ms=60000, keep=12, slot_keep=6)
    assert e.search(real_state(), slow, 0.05) is None
    assert e.timeouts == 1 and e.errors == 1
    assert e.warm(60)                                                       # a fresh process answers again
    assert e.search(real_state(), dict(FAST, rng=3), 20.0) is not None
    e._kill()


# ---------------------------------------------------------------- the real platform's six-entry `team`
# Three recorded real forced switches (Yoriko_Red, v4, 09-29): the platform's `team` lists all six of our roster, and
# the old reconstruction put a Pokemon left at team preview on the engine's bench instead of one we brought. Exactly
# what the policy handed battlestate.build there (observation, template, the draft's cards, the preview prior, seen
# foes) plus our team-preview `bring` and the answer played live.
#   slot1_single_bench (39a033d9 #15): slot 1 fainted, template [switch rillaboom]; the engine had Amoonguss
#     -> "no engine option for slot 1", classic fallback
#   double_faint_two_bench (2a30f417 #8): both fainted, template [ironhands, arcanine] twice; the engine had Milotic
#     instead of Arcanine -> only "switch 3, switch 3" rows -> "empty matrix", classic fallback
#   double_faint_one_bench (f525d6ac #10): both fainted, template [irontreads, pass] twice; the engine had Chi-Yu
#     instead of Iron Treads -> the only row left was "pass, pass", played live: INVALID_ACTION
with open(os.path.join(os.path.dirname(__file__), "data", "real_six_roster_team.json")) as _fh:
    SIX = json.load(_fh)
SIX_CLASSIC = {"slot1_single_bench": SIX["slot1_single_bench"]["played"],       # (the classic fallback played live)
               "double_faint_two_bench": SIX["double_faint_two_bench"]["played"], "double_faint_one_bench": None}


def six_state(name, **kw):
    f = SIX[name]
    st = battlestate.build(f["observation"], f["template"], f["my_cards"], f["opp_cards"], opp_roster=f["opp_roster"],
                           lineup_prior=f["lineup_prior"], seen_foes=f["seen_foes"], **kw)
    return f, st


@pytest.mark.parametrize("name", sorted(SIX))
def test_only_the_four_we_brought_are_rebuilt(name):
    f, st = six_state(name)
    team = [to_id(v["species"]) for v in f["observation"]["team"].values()]
    bring = sorted(to_id(s) for s in f["bring"])
    assert len(team) == 6 and len(bring) == 4                      # the platform lists the whole roster
    mine = [to_id(m["set"]["species"]) for m in st["sides"][st["me"]]["mons"]]
    assert sorted(mine) == bring and sorted(st["meta"]["benched"]) == sorted(set(team) - set(bring))
    for o in (x for opts in st["options"] for x in opts):
        assert o["type"] != "switch" or to_id(o["species"]) in mine[2:]     # every template switch has its Pokemon


def test_the_battle_team_comes_from_the_observation_alone():
    obs = real_obs()                                                # four entries (the local simulator's shape)
    assert battlestate._in_battle(obs, real_tmpl(), {to_id(v["species"]): v for v in obs["team"].values()},
                                  battlelog.parse(obs["protocol_log"]), "p2") == \
        {"incineroar", "tornadus", "fluttermane", "rillaboom"}
    obs["team"]["p2: Garchomp"] = _view("garchomp", 183, 183, revealed=False)      # left at team preview, listed
    obs["team"] = dict(reversed(list(obs["team"].items())))                        # ... ahead of the others
    st = battlestate.build(obs, real_tmpl(), cards(*MINE), cards(*THEIRS), opp_roster=[to_id(x) for x in THEIRS])
    assert [to_id(m["set"]["species"]) for m in st["sides"]["p2"]["mons"]][2:] in (["tornadus", "incineroar"],
                                                                                    ["incineroar", "tornadus"])
    assert st["meta"]["benched"] == ["garchomp"]


def test_a_battle_team_it_cannot_name_is_never_guessed():
    """Six listed but not exactly four known: build raises (the classic pick plays) instead of filling the gap in
    `team` order with a Pokemon left at team preview."""
    def six(obs):
        obs["team"]["p2: Garchomp"] = _view("garchomp", 183, 183, revealed=False, active=True)   # roster[0]/[1]
        obs["team"]["p2: Tyranitar"] = _view("tyranitar", 207, 207, revealed=False)
        obs["team"] = dict(reversed(list(obs["team"].items())))                            # ... listed first
        return obs

    def build(obs, tmpl):
        return battlestate.build(obs, tmpl, cards(*MINE), cards(*THEIRS), opp_roster=[to_id(x) for x in THEIRS])

    def no_switch():                   # both actives trapped / locked into a move: Showdown offers no switch
        tmpl = real_tmpl()
        for s in tmpl["slots"]:
            s["options"] = [o for o in s["options"] if o["type"] != "switch"]
        return tmpl

    assert sorted(build(six(real_obs()), real_tmpl())["meta"]["benched"]) == ["garchomp", "tyranitar"]
    # Tornadus (brought, never on the field) is named nowhere: the old fill put Tyranitar in its place
    with pytest.raises(ValueError, match="our four are not known"):
        build(six(real_obs()), no_switch())
    # one too many flagged: never drop one of the four silently
    obs = six(real_obs())
    obs["team"]["p2: Garchomp"]["revealed"] = True
    with pytest.raises(ValueError, match="our four are not known"):
        build(obs, real_tmpl())
    # four listed (the local simulator's shape): those are the four, nothing to guess
    st = build(real_obs(), no_switch())
    assert sorted(to_id(m["set"]["species"]) for m in st["sides"]["p2"]["mons"]) == \
        ["fluttermane", "incineroar", "rillaboom", "tornadus"]


@needs_engine
@pytest.mark.parametrize("name", sorted(SIX))
def test_a_real_forced_switch_is_searched(eng, name):
    f, st = six_state(name, classic=SIX_CLASSIC[name])
    ans = eng.search(st, dict(FAST, rng=3), 20.0)
    assert ans is not None, eng.last_error
    assert pol._template_ok(f["template"], ans["action"])
    parts = [ans["action"][f"slot_{i}"] for i in (0, 1)]
    if all(p["type"] == "switch" for p in parts):
        assert parts[0]["species"] != parts[1]["species"]
    if name == "slot1_single_bench":                                # one legal answer: the search returns it
        assert ans["action"] == f["played"] and ans["info"]["n_my"] == 1
    elif name == "double_faint_two_bench":
        assert ans["info"]["n_my"] == 2 and ans["info"]["forced"] and ans["info"]["errors"] == 0
    else:                                                           # one bench Pokemon: it comes in, the other passes
        assert sorted(p["type"] for p in parts) == ["pass", "switch"]
        assert next(p for p in parts if p["type"] == "switch")["species"] == "irontreads"


@needs_engine
def test_a_template_switch_the_engine_cannot_make_is_never_searched(eng):
    _, st = six_state("slot1_single_bench")
    st["options"][1] = [{"type": "switch", "species": "amoonguss"}]           # left at team preview: not in battle
    assert eng.search(st, dict(FAST), 20.0) is None
    assert eng.last_error == "no engine Pokemon for slot 1's switch to amoonguss"
    # the live INVALID_ACTION's shape: the one bench switch unmappable, so only "pass, pass" was left
    _, st = six_state("double_faint_one_bench")
    st["options"] = [[{"type": "switch", "species": "chiyu"}, {"type": "pass"}]] * 2
    assert eng.search(st, dict(FAST), 20.0) is None
    assert eng.last_error == "no engine Pokemon for slot 0's switch to chiyu"
    _, st = six_state("double_faint_two_bench")
    st["options"] = [[{"type": "switch", "species": "ironhands"}]] * 2         # both slots into the same Pokemon
    assert eng.search(st, dict(FAST), 20.0) is None
    assert eng.last_error.startswith("no legal joint engine option")


# ---------------------------------------------------------------- policy integration
class FakeEngine:
    enabled = True
    last_error = None

    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    def search(self, state, params, timeout, battle_json=None):
        self.calls += 1
        self.state, self.params = state, params
        return self.answer


def _policy(monkeypatch, answer):
    p = pol.PokemonPolicy()
    p.battle_mode = "search"
    p.cards, p.opp_cards = cards(*MINE), cards(*THEIRS)
    p.opp_roster = [to_id(x) for x in THEIRS]
    fake = FakeEngine(answer)
    monkeypatch.setattr(engine.SearchEngine, "_inst", fake)
    return p, fake


CLASSIC = {"slot_0": {"type": "move", "move_id": "moonblast", "target": 1},
           "slot_1": {"type": "move", "move_id": "woodhammer", "target": 2}}


def test_default_mode_is_search_and_classic_is_the_rollback(monkeypatch):
    monkeypatch.delenv("ARENA_BATTLE", raising=False)
    assert pol.battle_mode() == "search" and pol.PokemonPolicy().battle_mode == "search"
    monkeypatch.setenv("ARENA_BATTLE", "classic")
    assert pol.battle_mode() == "classic" and pol.PokemonPolicy().battle_mode == "classic"
    monkeypatch.setenv("ARENA_BATTLE", "bogus")
    assert pol.battle_mode() == "classic"


def test_default_search_config_is_blend_with_fixed_work(monkeypatch):
    for var, _ in pol.SEARCH_KNOBS.values():
        monkeypatch.delenv(var, raising=False)
    assert pol.search_params() == {"mode": "blend", "fixed": 1}
    monkeypatch.setenv("ARENA_SEARCH_MODE", "nash")
    monkeypatch.setenv("ARENA_SEARCH_FIXED", "0")
    monkeypatch.setenv("ARENA_SEARCH_SAMPLES", "x")                  # unparsable: ignored
    assert pol.search_params() == {"mode": "nash", "fixed": 0}


def test_search_pick_uses_a_legal_answer(monkeypatch):
    for var, _ in pol.SEARCH_KNOBS.values():
        monkeypatch.delenv(var, raising=False)
    other = {"slot_0": {"type": "move", "move_id": "protect", "target": 0},
             "slot_1": {"type": "switch", "species": "tornadus"}}
    p, fake = _policy(monkeypatch, {"action": other, "info": {"sims": 10}})
    out = p._search_pick({"session_id": "s", "state_version": 3}, real_obs(), real_tmpl(), CLASSIC, 4)
    assert out == other and p.search_stats["used"] == 1 and fake.calls == 1
    assert fake.state["classic"] == CLASSIC and fake.params["budget_ms"] <= pol.SEARCH_MS
    assert fake.params["mode"] == "blend" and fake.params["fixed"] == 1
    assert fake.params["hard_ms"] <= pol.SEARCH_CAP_MS and fake.params["hard_ms"] < 1000 * pol.SEARCH_HARD_S


@pytest.mark.parametrize("answer", [None, {"action": {"slot_0": {"type": "move", "move_id": "moonblast", "target": 0},
                                                      "slot_1": {"type": "pass"}}, "info": {}},
                                    {"action": "garbage"}])
def test_search_pick_falls_back_to_classic(monkeypatch, answer):
    p, _ = _policy(monkeypatch, answer)
    assert p._search_pick({"session_id": "s"}, real_obs(), real_tmpl(), CLASSIC, 4) is None
    assert p.search_stats["fallback"] == 1 and p.search_stats["used"] == 0


def test_search_pick_never_raises_on_a_bad_state(monkeypatch):
    p, fake = _policy(monkeypatch, None)
    assert p._search_pick({}, {"turn": 2}, real_tmpl(), CLASSIC, 2) is None
    assert fake.calls == 0 and any(k.startswith("state") for k in p.search_stats["reasons"])


def test_search_pick_skips_when_no_time_is_left(monkeypatch):
    from agent import timecontrol
    p, fake = _policy(monkeypatch, None)
    with timecontrol.decision(timecontrol.Deadline(0.1)):
        assert p._search_pick({}, real_obs(), real_tmpl(), CLASSIC, 4) is None
    assert fake.calls == 0 and p.search_stats["skipped_time"] == 1


def test_the_platform_budget_sets_the_safety_net_not_search_cap_ms(monkeypatch):
    """The platform's timer line (55 s this turn, 420 s bank + 90 s grace) never gives a battle decision more than
    timecontrol.BATTLE_S = 10 s (every recorded real state: 10 s), so the search's safety net is ~3.9 s and
    SEARCH_CAP_MS (6 s) does not bind on the platform (policy.py SEARCH_CAP_MS comment, docs/local_sim.md)."""
    from agent import timecontrol
    for var, _ in pol.SEARCH_KNOBS.values():
        monkeypatch.delenv(var, raising=False)
    for k in ("BATTLE_S", "LATENCY_S", "TURN_SHARE", "BANK_RESERVE_S", "BANK_SPREAD", "MIN_S", "HARD_GRACE_S"):
        monkeypatch.delenv(f"ARENA_TC_{k}", raising=False)
    p, fake = _policy(monkeypatch, None)
    raw = {"game_type": "pokemon_vgc", "phase": "moving", "observation": {
        "protocol_log": ["|turn|1", "|inactive|Time left: 55 sec this turn | 420 sec total | 90 sec grace"]}}
    d = timecontrol.budget_for(raw)
    assert d.source == "timer" and d.seconds == timecontrol.BATTLE_S == 10.0
    with timecontrol.decision(d):
        assert p._search_pick({"session_id": "s"}, real_obs(), real_tmpl(), CLASSIC, 4) is None
    assert fake.calls == 1
    assert 3500 <= fake.params["hard_ms"] <= 4000 < pol.SEARCH_CAP_MS


# ---------------------------------------------------------------- the local simulator's protocol log
@needs_engine
def test_local_protocol_log_is_each_players_view(monkeypatch):
    from local import pokemon_env as pe
    from local.showdown import Showdown
    monkeypatch.setenv("ARENA_PLOG", "1")
    seen = []

    class Rec(pe.RandomPlayer):
        def choose(self, raw):
            if raw["phase"] == "moving":
                seen.append((raw["current_actor"]["position"], raw["observation"]))
            return super().choose(raw)
    random.seed(1)
    with Showdown() as sd:
        pe.play_match(sd, Rec(), Rec(), seed=4700)
    assert seen
    for seat, obs in seen:
        log = obs["protocol_log"]
        me, foe = ("p1", "p2") if seat == 0 else ("p2", "p1")
        assert not any(x.startswith("|split|") for x in log)
        for x in log:
            if x.startswith(f"|switch|{foe}"):
                assert x.split("|")[4].split()[0].endswith("/100")
    monkeypatch.setenv("ARENA_PLOG", "0")
    seen.clear()
    with Showdown() as sd:
        pe.play_match(sd, Rec(), Rec(), seed=4700)
    assert seen and all("protocol_log" not in o for _, o in seen)


@needs_engine
def test_a_search_game_is_legal_and_uses_the_engine(monkeypatch):
    from local import pokemon_env as pe
    from local.showdown import Showdown
    import agent.agent as ag
    monkeypatch.setenv("ARENA_PLOG", "1")
    monkeypatch.setenv("ARENA_BATTLE", "search")
    for k, v in {"ARENA_SEARCH_SAMPLES": "1", "ARENA_SEARCH_KEEP": "4", "ARENA_SEARCH_PROBES": "1",
                 "ARENA_SEARCH_ROLLOUT": "0"}.items():
        monkeypatch.setenv(k, v)
    cand = pe.ContestantPlayer(ag.create_agent, "cand")
    with Showdown() as sd:
        r = pe.play_match(sd, cand, pe.RandomPlayer(), seed=4800)
    st = cand.agent.pk.search_stats
    assert r.invalid[0] == 0 and st["used"] > 0 and st["fallback"] == 0, st


def test_panel_knows_the_champion_by_name():
    from local import panel
    assert panel.resolve_opps("champion") == ["champion"] and panel.opponent_package("champion") == "champion"
    assert "champion" not in panel.PANEL
