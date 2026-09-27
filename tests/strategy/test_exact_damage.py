"""Exact-damage move scoring (calc-backed tests skip without agent/js/node_modules; fallbacks are tested always)."""
import glob
import json
import os
import time

import pytest

from agent.agent import Contestant, doubles_ok
from agent.pokemon.calc import DamageCalc, _js_dir, calc_pokemon
from agent.pokemon.policy import KO_BONUS, PokemonPolicy, _hit_stats, _pair_points, _points

needs_calc = pytest.mark.skipif(_js_dir() is None, reason="agent/js has no node_modules")

CHIYU = {"species": "Chi-Yu", "item": "Choice Specs", "ability": "Beads of Ruin", "nature": "Timid", "level": 50,
         "evs": {"hp": 4, "spa": 252, "spe": 252}, "ivs": {}, "moves": ["Heat Wave", "Dark Pulse", "Overheat", "Snarl"]}
INC = {"species": "Incineroar", "item": "Safety Goggles", "ability": "Intimidate", "nature": "Careful", "level": 50,
       "evs": {"hp": 252, "atk": 4, "def": 100, "spd": 148, "spe": 4}, "ivs": {},
       "moves": ["Fake Out", "Flare Blitz", "Knock Off", "Parting Shot"]}
RILL = {"species": "Rillaboom", "item": "Assault Vest", "ability": "Grassy Surge", "nature": "Adamant", "level": 50,
        "evs": {"hp": 252, "atk": 116, "def": 44, "spd": 92, "spe": 4}, "ivs": {},
        "moves": ["Fake Out", "Grassy Glide", "Wood Hammer", "U-turn"]}
AMOO = {"species": "Amoonguss", "item": "Rocky Helmet", "ability": "Regenerator", "nature": "Calm", "level": 50,
        "evs": {"hp": 252, "def": 100, "spd": 156}, "ivs": {}, "moves": ["Spore", "Rage Powder", "Pollen Puff", "Protect"]}


def mon(card, hp=1.0, **kw):
    sp = card["species"].lower().replace("-", "").replace(" ", "")
    return {"species": sp, "name": card["species"], "current_hp_fraction": hp, "current_hp": int(100 * hp),
            "max_hp": 100, "status": None, "fainted": False, "boosts": {}, **kw}


def state(slots, actives, opps, turn=3, **obs):
    o = {"is_doubles": True, "turn": turn, "force_switch": [False, False], "active_pokemon": actives,
         "opponent_active_pokemon": opps, "weather": {}, "fields": {}, "side_conditions": {},
         "opponent_side_conditions": {}, **obs}
    tmpl = {"type": "doubles_turn", "slots": slots}
    return {"phase": "moving", "state_version": 3, "session_id": "s", "game_type": "pokemon_vgc_doubles_draft",
            "observation": o, "legal_actions": {"actions": [{"action_id": "doubles_turn", "input": {"action": tmpl}}]}}


def policy(cards=(CHIYU, INC), opp_cards=(RILL, AMOO), prev=("chiyu", "incineroar")):
    pol = PokemonPolicy()
    for c in cards:
        pol.cards[c["species"].lower().replace("-", "").replace(" ", "")] = c
    for c in opp_cards:
        pol.opp_cards[c["species"].lower().replace("-", "").replace(" ", "")] = c
    pol.prev_active = list(prev)          # not "fresh": Fake Out is off the table
    return pol


def single_target_slots():
    return [{"slot": 0, "options": [{"type": "move", "move_id": "darkpulse", "targets": [-2, 1, 2]},
                                    {"type": "move", "move_id": "overheat", "targets": [-2, 1, 2]}]},
            {"slot": 1, "options": [{"type": "move", "move_id": "flareblitz", "targets": [-1, 1, 2]},
                                    {"type": "move", "move_id": "knockoff", "targets": [-1, 1, 2]}]}]


def test_roll_math_fraction_of_current_hp_and_ko_probability():
    useful, pko = _hit_stats([90] * 8 + [110] * 8, 100)
    assert useful == pytest.approx(0.95) and pko == 0.5           # overkill rolls are capped at the current HP
    assert _points([250] * 16, 10) == _points([10] * 16, 10)       # a 250% roll on a 10% target is worth a 10% roll
    a, b = ([60] * 16, 1.0), ([60] * 16, 0.9)
    assert _pair_points(a, b, 100) > _points(a[0], 100) + 0.9 * _points(b[0], 100)   # together they KO: worth more
    assert _pair_points(a, b, 100) <= 100 + KO_BONUS                                  # but never more than one KO
    assert _pair_points(([100] * 16, 1.0), ([100] * 16, 1.0), 100) == 100 + KO_BONUS  # second hit on a KO adds nothing


@needs_calc
def test_doubles_does_not_double_target_a_nearly_dead_foe():
    pol = policy()
    st = state(single_target_slots(), [mon(CHIYU), mon(INC)], [mon(RILL, hp=0.05), mon(AMOO)])
    a = pol.act(st)["action"]
    assert pol.last_exact and doubles_ok(st, a)
    assert {a["slot_0"]["target"], a["slot_1"]["target"]} == {1, 2}   # one finishes Rillaboom, one hits Amoonguss
    # With Amoonguss gone both may pile onto Rillaboom.
    st = state(single_target_slots(), [mon(CHIYU), mon(INC)], [mon(RILL, hp=0.05), None])
    a = pol.act(st)["action"]
    assert doubles_ok(st, a) and a["slot_0"]["target"] == 1 and a["slot_1"]["target"] == 1


@needs_calc
def test_spread_move_uses_calc_spread_modifier_only_with_two_targets():
    pol = policy()
    tmpl = {"slots": [{"slot": 0, "options": [{"type": "move", "move_id": "heatwave", "targets": [0]}]}]}
    obs = state([], [], [])["observation"]
    two = pol._exact_table(obs, tmpl, [mon(CHIYU), mon(INC)], [mon(RILL), mon(AMOO)])
    one = pol._exact_table(obs, tmpl, [mon(CHIYU), mon(INC)], [mon(RILL), None])
    assert two[("me", 0, "heatwave", "opp", 0)]["max"] < one[("me", 0, "heatwave", "opp", 0)]["max"]
    assert ("me", 0, "heatwave", "opp", 1) in two and ("me", 0, "heatwave", "opp", 1) not in one
    assert ("opp", 0, "woodhammer", "me", 0) in two                 # foes' known moves feed the threat weights


@needs_calc
def test_earthquake_counts_ally_damage_and_singles_uses_the_calc():
    pol = policy(cards=(INC, CHIYU), prev=("incineroar", "chiyu"))
    slots = [{"slot": 0, "options": [{"type": "move", "move_id": "earthquake", "targets": [0]},
                                     {"type": "move", "move_id": "flareblitz", "targets": [-2, 1, 2]}]},
             {"slot": 1, "options": [{"type": "move", "move_id": "snarl", "targets": [0]}]}]
    st = state(slots, [mon(INC), mon(CHIYU)], [mon(RILL), mon(AMOO)])
    a = pol.act(st)["action"]
    assert a["slot_0"]["move_id"] == "flareblitz"                     # Earthquake would nearly KO our own Chi-Yu
    # Singles: Chi-Yu vs Rillaboom -> the calc picks Fire over Dark.
    la = [{"action_id": f"move:{k}", "input": {"action_id": f"move:{k}", "action": {"type": "move", "slot": k, "move_id": m}}}
          for k, m in enumerate(["darkpulse", "overheat", "snarl"])]
    ss = {"phase": "moving", "observation": {"is_doubles": False, "active_pokemon": mon(CHIYU), "turn": 2,
                                             "opponent_active_pokemon": mon(RILL), "weather": {}, "fields": {}},
          "legal_actions": {"actions": la}}
    out = pol.act(ss)
    assert pol.last_exact and out["action"]["move_id"] == "overheat"


@needs_calc
def test_calc_unavailable_or_crashing_falls_back_to_heuristic(monkeypatch):
    pol = policy()
    st = state(single_target_slots(), [mon(CHIYU), mon(INC)], [mon(RILL, hp=0.05), mon(AMOO)])
    monkeypatch.setattr(DamageCalc.get(), "batch", lambda calcs: None)
    a = pol.act(st)["action"]
    assert not pol.last_exact and doubles_ok(st, a)

    def boom(calcs):
        raise RuntimeError("bridge died")
    monkeypatch.setattr(DamageCalc.get(), "batch", boom)
    a = pol.act(st)["action"]
    assert not pol.last_exact and doubles_ok(st, a)


def test_defensive_payload_shapes_locked_move_none_and_fainted_foes_missing_boosts():
    pol = policy()
    slots = [{"slot": 0, "options": [{"type": "move", "move_id": "overheat"}]},                 # locked: no targets
             {"slot": 1, "options": [{"type": "move", "move_id": "flareblitz", "targets": [-1, 1, 2]},
                                     {"type": "move", "move_id": "partingshot", "targets": [-1, 1, 2]}]}]
    opps = [None, {"species": "amoonguss", "fainted": True, "current_hp_fraction": 0.0}]
    actives = [{"species": "chiyu"}, {"species": "incineroar", "current_hp_fraction": 0.4}]   # no boosts / hp keys
    st = state(slots, actives, opps)
    a = pol.act(st)["action"]
    assert a["slot_0"] == {"type": "move", "move_id": "overheat", "target": 0} and doubles_ok(st, a)
    pol.opp_cards.clear()                                          # unknown opponent sets
    a = pol.act(st)["action"]
    assert doubles_ok(st, a)


def test_calc_pokemon_uses_drafted_item_when_platform_hides_it():
    p = calc_pokemon({"species": "chiyu", "item": "unknown_item", "ability": None}, CHIYU)
    assert p["item"] == "Choice Specs" and p["ability"] == "Beads of Ruin" and p["evs"]["spa"] == 252
    assert "item" not in calc_pokemon({"species": "chiyu", "item": "unknown_item"}, None)


@needs_calc
def test_decision_time_is_small():
    pol = policy()
    st = state(single_target_slots(), [mon(CHIYU), mon(INC)], [mon(RILL, hp=0.6), mon(AMOO)])
    pol.act(st)                                                    # warm up the node process
    t = time.perf_counter()
    pol.act(state(single_target_slots(), [mon(CHIYU), mon(INC)], [mon(RILL, hp=0.5), mon(AMOO, hp=0.9)]))
    assert time.perf_counter() - t < 1.0


def _recorded_sessions():
    root = os.environ.get("ARENA_LOG_DIR", "logs")
    return sorted(glob.glob(os.path.join(root, "*", "*.jsonl")))


@pytest.mark.skipif(not _recorded_sessions(), reason="no recorded platform sessions (logs/*/*.jsonl)")
def test_replay_real_recorded_states():
    """Every real platform state we ever acted on still yields a legal action, in well under the 300 s budget."""
    from altruagent import DecisionContext, GameState
    worst = 0.0
    for f in _recorded_sessions():
        c = Contestant()
        for line in open(f):
            d = json.loads(line)
            if d.get("kind") != "action":
                continue
            raw = d["raw"]
            gs = GameState.from_mcp_state(raw)
            ctx = DecisionContext(session_id="replay", tournament_id=None, game_type=d.get("game_type") or "pokemon_vgc_doubles_draft", agent_id="me")
            t = time.perf_counter()
            out = c.choose_action(gs, ctx)
            worst = max(worst, time.perf_counter() - t)
            obs = raw.get("observation") or {}
            if raw.get("phase") == "moving" and isinstance(obs, dict) and obs.get("is_doubles"):
                assert isinstance(out, dict) and out["type"] == "doubles_turn" and doubles_ok(raw, out), f
                assert c.pk.last_exact or _js_dir() is None, f
    assert worst < 5.0
