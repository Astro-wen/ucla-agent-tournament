"""One-step joint lookahead (agent/pokemon/lookahead.py): cell semantics on synthetic boards (always run),
plus calc-backed integration through PokemonPolicy (skipped without agent/js/node_modules)."""
import math
import time

import pytest

from agent.agent import doubles_ok
from agent.pokemon import dex
from agent.pokemon import lookahead as L
from agent.pokemon import policy as P
from agent.pokemon.calc import DamageCalc, _js_dir
from agent.pokemon.policy import PokemonPolicy

needs_calc = pytest.mark.skipif(_js_dir() is None, reason="agent/js has no node_modules")

CHIYU = {"species": "Chi-Yu", "item": "Choice Specs", "ability": "Beads of Ruin", "nature": "Timid", "level": 50,
         "evs": {"hp": 4, "spa": 252, "spe": 252}, "ivs": {}, "moves": ["Heat Wave", "Dark Pulse", "Overheat", "Snarl"]}
INC = {"species": "Incineroar", "item": "Safety Goggles", "ability": "Intimidate", "nature": "Careful", "level": 50,
       "evs": {"hp": 252, "atk": 4, "def": 100, "spd": 148, "spe": 4}, "ivs": {},
       "moves": ["Fake Out", "Flare Blitz", "Knock Off", "Protect"]}
RILL = {"species": "Rillaboom", "item": "Assault Vest", "ability": "Grassy Surge", "nature": "Adamant", "level": 50,
        "evs": {"hp": 252, "atk": 116, "def": 44, "spd": 92, "spe": 4}, "ivs": {},
        "moves": ["Fake Out", "Grassy Glide", "Wood Hammer", "U-turn"]}
AMOO = {"species": "Amoonguss", "item": "Rocky Helmet", "ability": "Regenerator", "nature": "Calm", "level": 50,
        "evs": {"hp": 252, "def": 100, "spd": 156}, "ivs": {}, "moves": ["Spore", "Rage Powder", "Pollen Puff", "Protect"]}
GAR = {"species": "Garchomp", "item": "Focus Sash", "ability": "Rough Skin", "nature": "Jolly", "level": 50,
       "evs": {"hp": 4, "atk": 252, "spe": 252}, "ivs": {}, "moves": ["Earthquake", "Rock Slide", "Iron Head", "Protect"]}
ALL = (CHIYU, INC, RILL, AMOO, GAR)


# ---- synthetic boards: no calculator ---------------------------------------------------------------------
def smon(key, species, hp=1.0, speed=100, fresh=False, ability="", item=""):
    side, idx = ("me", int(key[-1])) if key.startswith("me") else ("opp", int(key[-1]))
    view = {"species": species, "ability": ability, "item": item}
    return L._Mon(key, side, idx, species, hp, view, None, dex.types_of(species), speed, fresh)


def search(mons, dmg, trick_room=False, terrain=None, weights=None):
    """A _Search wired to a synthetic board. dmg: {(atk key, move, def key): fraction of max HP (one roll)}."""
    s = L._Search.__new__(L._Search)
    s.P, s.turn, s.sun, s.terrain, s.trick_room = P, 3, False, terrain, trick_room
    s.mons = {k: None for k in ("me0", "me1", "opp0", "opp1")}
    s.mons.update({m.key: m for m in mons})
    s.hp0 = {m.key: m.hp for m in mons}
    s.weight = weights or {"opp0": 1.0, "opp1": 1.0}
    s._dmg = {k: ([v], 1.0, False) for k, v in dmg.items()}
    for (a, mid, d) in list(dmg):
        for k in ("me0", "me1", "opp0", "opp1"):
            s._dmg.setdefault((a, mid, k), None)
    return s


def act(s, key, mid, tgt=None, static=0.0):
    m = s.mons[key]
    return s._finish(L._Act(key, m.side, mid, tgt, static), m)


def test_faster_ko_removes_the_victims_action_protect_blocks_and_trick_room_flips_order():
    me = [smon("me0", "chiyu", speed=200), smon("me1", "incineroar", speed=50)]
    opp = [smon("opp0", "rillaboom", hp=0.4, speed=100), smon("opp1", "amoonguss", speed=30)]
    dmg = {("me0", "heatwave", "opp0"): 0.5, ("me0", "heatwave", "opp1"): 0.3,
           ("opp0", "woodhammer", "me0"): 0.9, ("opp1", "pollenpuff", "me1"): 0.2}
    s = search(me + opp, dmg)
    row = (act(s, "me0", "heatwave"), L._Act(None, "me", None, None))
    col = (act(s, "opp0", "woodhammer", ("me", 0)), act(s, "opp1", "pollenpuff", ("me", 1)))
    # Chi-Yu KOs Rillaboom before Wood Hammer: we deal 0.4 + 0.3 (+KO), take 0.2; Chi-Yu and Incineroar
    # both outspeed the lone Amoonguss.
    v = s._simulate(row, col)
    assert v == pytest.approx(0.4 + 0.3 + L.KO_VALUE - 0.2 + 2 * L.SPEED_BONUS)
    # Rillaboom Protects: Heat Wave only hits Amoonguss; only Chi-Yu outspeeds both foes.
    col2 = (act(s, "opp0", "protect"), col[1])
    assert s._simulate(row, col2) == pytest.approx(0.3 - 0.2 + L.SPEED_BONUS)
    # Trick Room: Rillaboom (100) moves before Chi-Yu (200) and Wood Hammer lands first.
    s = search(me + opp, dmg, trick_room=True)
    row = (act(s, "me0", "heatwave"), L._Act(None, "me", None, None))
    col = (act(s, "opp0", "woodhammer", ("me", 0)), act(s, "opp1", "pollenpuff", ("me", 1)))
    assert s._simulate(row, col) == pytest.approx(0.4 + 0.3 + L.KO_VALUE - 0.9 - 0.2)


def test_fake_out_needs_a_fresh_user_and_sucker_punch_needs_an_attacking_target():
    me = [smon("me0", "incineroar", speed=80, fresh=True), smon("me1", "chiyu", speed=200)]
    opp = [smon("opp0", "rillaboom", speed=100), smon("opp1", "kingambit", speed=50)]
    dmg = {("me0", "fakeout", "opp0"): 0.1, ("opp0", "woodhammer", "me1"): 0.6, ("opp1", "suckerpunch", "me1"): 0.5,
           ("me1", "heatwave", "opp0"): 0.3, ("me1", "heatwave", "opp1"): 0.2}
    s = search(me + opp, dmg)
    row = (act(s, "me0", "fakeout", ("opp", 0)), act(s, "me1", "heatwave"))
    col = (act(s, "opp0", "woodhammer", ("me", 1)), act(s, "opp1", "suckerpunch", ("me", 1)))
    # Fake Out flinches Rillaboom; Sucker Punch (+1) lands before Heat Wave. Chi-Yu outspeeds both foes.
    assert s._simulate(row, col) == pytest.approx(0.1 + 0.3 + 0.2 - 0.5 + L.SPEED_BONUS)
    me[0].fresh = False                                  # stale Fake Out fails: Wood Hammer + Sucker Punch KO Chi-Yu
    assert s._simulate(row, col) == pytest.approx(0.3 + 0.2 - 1.0 - L.KO_VALUE)
    me[0].fresh = True
    row2 = (row[0], act(s, "me1", "protect"))            # Chi-Yu Protects (not attacking): Sucker Punch fails
    assert s._simulate(row2, col) == pytest.approx(0.1 - L.PROTECT_TEMPO + L.SPEED_BONUS)


def test_rage_powder_redirects_unless_grass_or_goggles_and_armor_tail_blocks_priority():
    me = [smon("me0", "chiyu", speed=200), smon("me1", "incineroar", speed=50, item="safetygoggles")]
    opp = [smon("opp0", "kingambit", hp=0.2, speed=60), smon("opp1", "amoonguss", speed=30)]
    opp[1].card = {"moves": ["Rage Powder"]}
    dmg = {("me0", "darkpulse", "opp0"): 0.5, ("me0", "darkpulse", "opp1"): 0.4,
           ("me1", "knockoff", "opp0"): 0.3, ("me1", "knockoff", "opp1"): 0.25}
    s = search(me + opp, dmg)
    col = (L._Act(None, "opp", None, None), act(s, "opp1", "ragepowder"))
    dp = (act(s, "me0", "darkpulse", ("opp", 0)), L._Act(None, "me", None, None))
    assert s._simulate(dp, col) == pytest.approx(0.4 + L.SPEED_BONUS)        # pulled into Amoonguss
    ko = (L._Act(None, "me", None, None), act(s, "me1", "knockoff", ("opp", 0)))
    assert s._simulate(ko, col) == pytest.approx(0.2 + L.KO_VALUE + 2 * L.SPEED_BONUS)  # Safety Goggles ignore Rage Powder
    me = [smon("me0", "incineroar", speed=50, fresh=True), smon("me1", "chiyu", speed=200)]
    opp = [smon("opp0", "farigiraf", speed=40, ability="armortail"), smon("opp1", "amoonguss", speed=30)]
    s = search(me + opp, {("me0", "fakeout", "opp1"): 0.1})
    fo = (act(s, "me0", "fakeout", ("opp", 1)), L._Act(None, "me", None, None))
    assert s._simulate(fo, (L._Act(None, "opp", None, None),) * 2) == pytest.approx(2 * L.SPEED_BONUS)


def test_expected_damage_keeps_accuracy_and_focus_sash_survives_at_full_hp():
    me = [smon("me0", "chiyu", speed=200), smon("me1", "incineroar", speed=50)]
    opp = [smon("opp0", "garchomp", speed=150, item="focussash"), smon("opp1", "amoonguss", hp=0.3, speed=30)]
    s = search(me + opp, {("me0", "overheat", "opp0"): 1.5, ("me0", "overheat", "opp1"): 1.0})
    s._dmg[("me0", "overheat", "opp1")] = ([1.0], 0.7, False)         # 70% accurate KO
    none = (L._Act(None, "opp", None, None),) * 2
    oh0 = (act(s, "me0", "overheat", ("opp", 0)), L._Act(None, "me", None, None))
    assert s._simulate(oh0, none) == pytest.approx(0.99 + L.SPEED_BONUS)      # Sash: left at 1%, no KO
    oh1 = (act(s, "me0", "overheat", ("opp", 1)), L._Act(None, "me", None, None))
    # the KO counts as happening (0.7 >= 0.5) but is scored at its expectation
    assert s._simulate(oh1, none) == pytest.approx(0.7 * (0.3 + L.KO_VALUE) + L.SPEED_BONUS)


def test_robust_rule_prefers_spread_when_a_protect_is_possible():
    me = [smon("me0", "chiyu", speed=200), smon("me1", "incineroar", speed=50)]
    opp = [smon("opp0", "rillaboom", speed=100), smon("opp1", "amoonguss", speed=30)]
    dmg = {("me0", "heatwave", "opp0"): 0.45, ("me0", "heatwave", "opp1"): 0.45, ("me0", "overheat", "opp0"): 0.7,
           ("me1", "knockoff", "opp1"): 0.2}
    s = search(me + opp, dmg)
    kn = act(s, "me1", "knockoff", ("opp", 1))
    rows = [(act(s, "me0", "heatwave"), kn), (act(s, "me0", "overheat", ("opp", 0)), kn)]
    cols = [(L._Act(None, "opp", None, None),) * 2, (act(s, "opp0", "protect"), L._Act(None, "opp", None, None))]
    scores = []
    for row in rows:
        vals = [s._simulate(row, c) for c in cols]
        scores.append(L.W_WORST * min(vals) + L.W_MEAN * sum(vals) / len(vals))
    assert scores[0] > scores[1]


# ---- attacks aimed at our own ally play out for real ------------------------------------------------------
def at_ally(s, key, mid, ally_idx):
    a = act(s, key, mid, ("me", ally_idx))
    a.at_ally = True                                     # as _our_acts builds a target -1 / -2 attack
    return a


PASS = L._Act(None, "me", None, None)
NONE2 = (L._Act(None, "opp", None, None),) * 2


def test_an_attack_on_our_ally_costs_its_damage_and_ko_instead_of_being_a_free_pass():
    me = [smon("me0", "incineroar", speed=80), smon("me1", "chiyu", hp=0.5, speed=90)]
    opp = [smon("opp0", "rillaboom", speed=100), smon("opp1", "amoonguss", speed=120)]   # nobody outspeeds
    s = search(me + opp, {("me0", "knockoff", "me1"): 0.3, ("me0", "knockoff", "opp0"): 0.3})
    hit = (at_ally(s, "me0", "knockoff", 1), PASS)
    assert s._simulate((PASS, PASS), NONE2) == pytest.approx(0.0)
    assert s._simulate(hit, NONE2) == pytest.approx(-0.3)                  # damage to our ally is a loss
    s.mons["me1"].hp = s.hp0["me1"] = 0.2
    assert s._simulate(hit, NONE2) == pytest.approx(-(0.2 + L.KO_VALUE))   # ... and so is knocking it out
    s.hp0["me1"] = s.mons["me1"].hp = 0.5
    assert s._simulate((hit[0], act(s, "me1", "protect")), NONE2) == pytest.approx(-L.PROTECT_TEMPO)  # blocked
    foe = (at_ally(s, "me0", "knockoff", 1), PASS)
    assert s._simulate(foe, NONE2) < s._simulate((act(s, "me0", "knockoff", ("opp", 0)), PASS), NONE2)


def test_an_attack_on_a_fainted_or_missing_ally_fails_and_fake_out_on_it_flinches_it():
    me = [smon("me0", "incineroar", speed=80, fresh=True), smon("me1", "chiyu", speed=90)]
    opp = [smon("opp0", "farigiraf", speed=40, ability="armortail"), smon("opp1", "amoonguss", speed=30)]
    s = search(me + opp, {("me0", "fakeout", "me1"): 0.1, ("me1", "heatwave", "opp0"): 0.3,
                          ("me1", "heatwave", "opp1"): 0.3})
    fo = at_ally(s, "me0", "fakeout", 1)
    # Armor Tail only stops priority aimed at its own side: our Fake Out lands on our Chi-Yu, which flinches
    assert s._simulate((fo, act(s, "me1", "heatwave")), NONE2) == pytest.approx(-0.1 + 2 * L.SPEED_BONUS)
    s2 = search([me[0]] + opp, {("me0", "knockoff", "me1"): 0.3})
    assert s2._simulate((at_ally(s2, "me0", "knockoff", 1), PASS), NONE2) == pytest.approx(L.SPEED_BONUS)


def test_pollen_puff_on_our_ally_heals_half_its_hp_in_move_order():
    me = [smon("me0", "amoonguss", speed=60), smon("me1", "incineroar", hp=0.3, speed=50)]
    opp = [smon("opp0", "rillaboom", speed=100), smon("opp1", "chiyu", speed=400)]    # nobody of ours outspeeds
    s = search(me + opp, {("opp0", "woodhammer", "me1"): 0.6})
    puff = (at_ally(s, "me0", "pollenpuff", 1), PASS)
    assert s._simulate(puff, NONE2) == pytest.approx(0.5)                  # the heal is worth the HP it restores
    wh = (act(s, "opp0", "woodhammer", ("me", 1)), L._Act(None, "opp", None, None))
    # Rillaboom is faster: Wood Hammer KOs Incineroar first, the heal then fails
    assert s._simulate(puff, wh) == pytest.approx(-(0.3 + L.KO_VALUE))
    s.mons["me0"].speed = 300                           # heal first: 0.3 -> 0.8, survives the 0.6 hit
    puff = (at_ally(s, "me0", "pollenpuff", 1), PASS)
    assert s._simulate(puff, wh) == pytest.approx(0.5 - 0.6)
    s.mons["me1"].hp = s.hp0["me1"] = 0.8               # capped at full HP
    assert s._simulate(puff, NONE2) == pytest.approx(0.2)
    s.mons["me1"].hp = s.hp0["me1"] = 1.0               # a full-HP partner: Pollen Puff does nothing
    assert s._simulate(puff, NONE2) == pytest.approx(0.0)
    s.mons["me1"].hp = s.hp0["me1"] = 0.3
    s.mons["me1"].ability = "bulletproof"               # Pollen Puff is a bullet move
    assert s._simulate(puff, NONE2) == pytest.approx(0.0)
    s.mons["me1"].ability = ""
    assert s._simulate((puff[0], act(s, "me1", "protect")), NONE2) == pytest.approx(-L.PROTECT_TEMPO)


def test_our_acts_flags_ally_attacks_and_the_ally_fix_switch_restores_the_old_free_pass(monkeypatch):
    me = [smon("me0", "incineroar", speed=80), smon("me1", "chiyu", hp=0.5, speed=90)]
    opp = [smon("opp0", "rillaboom", speed=100), smon("opp1", "amoonguss", speed=120)]
    s = search(me + opp, {("me0", "knockoff", "me1"): 0.3})
    s.pol = PokemonPolicy()
    cands = [{"cand": {"type": "move", "move_id": "knockoff", "target": t}, "base": b, "solo": b}
             for t, b in ((1, 30.0), (-2, -100.0))]
    cands.append({"cand": {"type": "move", "move_id": "tailwind", "target": 0}, "base": 70.0, "solo": 70.0})
    monkeypatch.delenv("LOOKAHEAD_ALLY_FIX", raising=False)
    foe_hit, ally_hit, tw = s._our_acts(0, cands)
    assert not foe_hit.at_ally and foe_hit.kind == "attack" and foe_hit.tgt == ("opp", 0)
    assert ally_hit.at_ally and ally_hit.kind == "attack" and ally_hit.tgt == ("me", 1)
    assert tw.kind == "status" and not tw.at_ally and tw.static == pytest.approx(0.7)
    assert s._simulate((ally_hit, PASS), NONE2) == pytest.approx(-0.3)
    monkeypatch.setenv("LOOKAHEAD_ALLY_FIX", "0")
    _, old, _ = s._our_acts(0, cands)
    assert old.at_ally and old.kind == "none"
    assert s._simulate((old, PASS), NONE2) == pytest.approx(0.0)       # the old bug: attacking our ally was free


# ---- knobs (env) ------------------------------------------------------------------------------------------
KNOBS = ("LOOKAHEAD_ALPHA", "LOOKAHEAD_FOE_WEIGHT", "LOOKAHEAD_FOE_TEMP", "LOOKAHEAD_K_OURS", "LOOKAHEAD_K_THEIRS")


def test_knob_defaults_and_bad_values_fall_back_to_the_current_behaviour(monkeypatch):
    for k in KNOBS:
        monkeypatch.delenv(k, raising=False)
    # built-in default alpha 0.4 (the panel winner; v2 / v2.1 shipped 0.6)
    assert L.alpha() == L.W_WORST == 0.4 and 1.0 - L.alpha() == L.W_MEAN == 0.6
    assert L.foe_weight_mode() == "equal" and L.foe_temp() == 1.0 and L.k_limits() == (L.K_OURS, L.K_THEIRS) == (5, 4)
    for bad in ("", "abc", "nan", "inf", "-0.1", "1.5"):
        monkeypatch.setenv("LOOKAHEAD_ALPHA", bad)
        assert L.alpha() == 0.4
    monkeypatch.setenv("LOOKAHEAD_ALPHA", "0.6")                   # the env var still overrides the default
    assert L.alpha() == 0.6
    for bad in ("", "0", "-3", "x", "nan", "inf"):
        monkeypatch.setenv("LOOKAHEAD_K_OURS", bad)
        monkeypatch.setenv("LOOKAHEAD_K_THEIRS", bad)
        monkeypatch.setenv("LOOKAHEAD_FOE_TEMP", bad)
        assert L.k_limits() == (5, 4) and L.foe_temp() == 1.0
    monkeypatch.setenv("LOOKAHEAD_FOE_WEIGHT", "bogus")
    assert L.foe_weight_mode() == "equal"
    monkeypatch.setenv("LOOKAHEAD_ALPHA", " 1 ")
    monkeypatch.setenv("LOOKAHEAD_FOE_WEIGHT", "SoftMax")
    monkeypatch.setenv("LOOKAHEAD_FOE_TEMP", "0.25")
    monkeypatch.setenv("LOOKAHEAD_K_OURS", "3")
    monkeypatch.setenv("LOOKAHEAD_K_THEIRS", "2")
    assert L.alpha() == 1.0 and L.foe_weight_mode() == "softmax" and L.foe_temp() == 0.25 and L.k_limits() == (3, 2)


def test_row_score_alpha_and_softmax_weights():
    vals = [0.2, -0.4, 1.0]
    assert L.row_score(vals) == pytest.approx(0.4 * -0.4 + 0.6 * 0.8 / 3)
    assert L.row_score(vals) == L.W_WORST * min(vals) + L.W_MEAN * sum(vals) / len(vals)   # bit-identical default
    assert L.row_score(vals, 1.0, 0.0) == pytest.approx(-0.4)                 # alpha = 1: pure worst case
    assert L.row_score(vals, 0.0, 1.0) == pytest.approx(0.8 / 3)              # alpha = 0: pure mean
    w = L.softmax_weights([0.5, 0.5, 0.5], 1.0)
    assert w == pytest.approx([1 / 3] * 3)
    assert L.row_score(vals, 0.4, 0.6, w) == pytest.approx(L.row_score(vals))
    w = L.softmax_weights([0.0, 1.0, 0.0], 1.0)                             # the threatening reply counts more
    assert w[1] == pytest.approx(math.e / (2 + math.e)) and w[0] == w[2]
    assert L.row_score(vals, 0.0, 1.0, w) == pytest.approx(sum(v * x for v, x in zip(vals, w)))
    assert L.softmax_weights([0.0, 1.0, 0.0], 1e-3)[1] == pytest.approx(1.0)   # cold: the biggest threat only
    assert L.softmax_weights([0.0, 1.0, 0.0], 1e6) == pytest.approx([1 / 3] * 3, abs=1e-5)
    assert L.softmax_weights([500.0, 0.0], 1e-3) == pytest.approx([1.0, 0.0])   # no overflow
    for bad in ([], [0.1, float("nan")], [float("inf")]):
        assert L.softmax_weights(bad, 1.0) is None


# ---- calc-backed integration -------------------------------------------------------------------------------
def view(card, hp=1.0, foe=False, **kw):
    sp = dex.to_id(card["species"])
    v = {"species": sp, "name": card["species"], "current_hp_fraction": hp, "status": None, "fainted": False,
         "types": dex.types_of(sp), "boosts": {}, "ability": None if foe else dex.to_id(card["ability"]),
         "item": "unknown_item" if foe else dex.to_id(card["item"]), "moves": [] if foe else
         [dex.to_id(m) for m in card["moves"]]}
    if foe:
        v.update(current_hp=int(round(100 * hp)), max_hp=100)
    v.update(kw)
    return v


def options(card, slot, switches=()):
    other = -(2 - slot)
    out = []
    for mv in card["moves"]:
        mid = dex.to_id(mv)
        kind = dex.move(mid).get("target")
        tg = [other, 1, 2] if kind in ("normal", "any") else [1, 2] if kind == "adjacentFoe" else [0]
        out.append({"type": "move", "move_id": mid, "targets": tg})
    return out + [{"type": "switch", "species": sp} for sp in switches]


def state(me, opp, turn=3, opts=None, force=(False, False), switches=()):
    slots = []
    for i in range(2):
        if opts is not None:
            o = opts[i]
        elif me[i] is None:
            o = [{"type": "pass"}]
        else:
            o = options(next(c for c in ALL if dex.to_id(c["species"]) == me[i]["species"]), i, switches)
        slots.append({"slot": i, "board_position": -(i + 1), "active": me[i], "force_switch": force[i], "options": o})
    tmpl = {"type": "doubles_turn", "slots": slots}
    obs = {"is_doubles": True, "turn": turn, "weather": {}, "fields": {}, "side_conditions": {},
           "opponent_side_conditions": {}, "active_pokemon": list(me), "opponent_active_pokemon": list(opp),
           "force_switch": list(force)}
    return {"phase": "moving", "state_version": 3, "session_id": "s", "game_type": "pokemon_vgc_doubles_draft",
            "observation": obs, "legal_actions": {"actions": [{"action_id": "doubles_turn", "input": {"action": tmpl}}]}}


def policy(mine=(CHIYU, INC, GAR), theirs=(RILL, AMOO)):
    pol = PokemonPolicy()
    for c in mine:
        pol.cards[dex.to_id(c["species"])] = c
    for c in theirs:
        pol.opp_cards[dex.to_id(c["species"])] = c
    pol.prev_active = [dex.to_id(mine[0]["species"]), dex.to_id(mine[1]["species"])] if len(mine) > 1 else [None, None]
    pol.opp_prev_active = [dex.to_id(theirs[0]["species"]), dex.to_id(theirs[1]["species"])] if len(theirs) > 1 \
        else [None, None]
    return pol


@needs_calc
def test_lookahead_runs_is_legal_fast_and_deterministic():
    st = state([view(CHIYU), view(INC)], [view(RILL, 0.5, foe=True), view(AMOO, foe=True)])
    pol = policy()
    pol.act(st)                                            # warm the node process up
    t = time.perf_counter()
    out = pol.act(st)["action"]
    assert time.perf_counter() - t < L.TIME_CAP
    assert doubles_ok(st, out) and pol.last_exact and pol.last_lookahead is not None
    assert pol.last_lookahead["rows"] * pol.last_lookahead["cols"] <= L.MAX_CELLS
    assert out == policy().act(st)["action"]


@needs_calc
def test_odd_shapes_locked_move_none_foe_missing_boosts_unknown_set():
    pol = policy(theirs=(RILL,))                        # Garchomp's set never seen: guessed STAB attacks
    me0 = view(CHIYU)
    del me0["boosts"]
    foe = view(GAR, foe=True, status="PAR")
    del foe["boosts"]
    opts = [[{"type": "move", "move_id": "overheat"}], options(INC, 1)]       # locked: no "targets"
    st = state([me0, view(INC, 0.3)], [foe, None], opts=opts)
    out = pol.act(st)["action"]
    assert doubles_ok(st, out) and out["slot_0"] == {"type": "move", "move_id": "overheat", "target": 0}
    assert "earthpower" in pol.foe_moves(foe) or "highhorsepower" in pol.foe_moves(foe)


@needs_calc
def test_forced_switch_is_left_to_the_exact_scorer():
    pol = policy()
    opts = [[{"type": "switch", "species": "garchomp"}], [{"type": "pass"}]]
    st = state([view(INC, 0.0, fainted=True), view(CHIYU)], [view(RILL, foe=True), view(AMOO, foe=True)],
               opts=opts, force=(True, False))
    out = pol.act(st)["action"]
    assert doubles_ok(st, out) and pol.last_lookahead is None
    assert out == {"type": "doubles_turn", "slot_0": {"type": "switch", "species": "garchomp"}, "slot_1": {"type": "pass"}}


@needs_calc
def test_no_protect_twice_in_a_row():
    st = state([view(CHIYU), view(INC, 0.2)], [view(RILL, foe=True), view(AMOO, foe=True)], turn=5)
    pol = policy()
    pol.last_protect["incineroar"] = 4
    out = pol.act(st)["action"]
    assert doubles_ok(st, out) and out["slot_1"]["move_id"] != "protect"


@needs_calc
def test_time_cap_and_crashes_keep_the_exact_pick(monkeypatch):
    st = state([view(CHIYU), view(INC)], [view(RILL, foe=True), view(AMOO, foe=True)])
    exact = policy()
    monkeypatch.setattr(P, "joint_lookahead", lambda *a, **k: None)
    want = exact.act(st)["action"]
    monkeypatch.undo()
    monkeypatch.setattr(L, "TIME_CAP", 0.0)
    pol = policy()
    assert pol.act(st)["action"] == want and pol.last_lookahead is None

    def boom(self):
        raise RuntimeError("bug")
    monkeypatch.setattr(L, "TIME_CAP", 2.5)
    monkeypatch.setattr(L._Search, "run", boom)
    pol = policy()
    assert pol.act(st)["action"] == want


def boost_exact(monkeypatch, wins):
    """Make the exact scorer pick wins[slot](cand) in each listed slot (500 points each); the context the
    policy scored with is left on pol.test_ctx."""
    orig = PokemonPolicy._exact_context

    def patched(self, *a, **k):
        ctx = orig(self, *a, **k)
        for i, cands in ctx["per_slot"]:
            for c in cands:
                if i in wins and wins[i](c["cand"]):
                    c["base"] = c["solo"] = 500.0
        self.test_ctx = ctx
        return ctx
    monkeypatch.setattr(PokemonPolicy, "_exact_context", patched)


def is_switch(c):
    return c.get("type") == "switch"


def is_protect(c):
    return c.get("type") == "move" and c.get("move_id") in L.PROTECT_MOVES


def is_puff_on_ally(c):
    return c.get("type") == "move" and c.get("move_id") == "pollenpuff" and c.get("target", 0) < 0


@needs_calc
def test_lookahead_keeps_a_voluntary_switch(monkeypatch):
    """The rows hold no switches, so an exact pick that switches has no row to compare against: it must stay."""
    monkeypatch.delenv("LOOKAHEAD_SWITCH_FIX", raising=False)     # the default, whatever the shell has set
    st = state([view(CHIYU), view(INC)], [view(RILL, foe=True), view(AMOO, foe=True)], switches=("garchomp",))
    boost_exact(monkeypatch, {0: is_switch})             # e.g. a choice-locked mon with nothing useful to click
    sw = {"type": "switch", "species": "garchomp"}
    pol, kept = policy(), L.STATS["kept_unmodelled"]
    out = pol.act(st)["action"]
    assert doubles_ok(st, out) and out["slot_0"] == sw and pol.last_lookahead is None
    assert L.STATS["kept_unmodelled"] == kept + 1
    monkeypatch.setenv("LOOKAHEAD_SWITCH_FIX", "0")      # the old behaviour: the switch is silently replaced
    out = policy().act(st)["action"]
    assert doubles_ok(st, out) and out["slot_0"]["type"] == "move"


@needs_calc
def test_kept_switch_never_carries_a_protect_chain(monkeypatch):
    """Switch + a repeat Protect: the switch stays, the Protect slot is re-picked by the exact pair score."""
    monkeypatch.delenv("LOOKAHEAD_SWITCH_FIX", raising=False)
    st = state([view(CHIYU), view(INC, 0.3)], [view(RILL, foe=True), view(AMOO, foe=True)], switches=("garchomp",))
    boost_exact(monkeypatch, {0: is_switch, 1: is_protect})
    pol = policy()
    pol.last_protect["incineroar"] = 2                   # turn 3: Incineroar Protected last turn
    repicked = L.STATS.get("kept_repicked", 0)
    out = pol.act(st)["action"]
    assert doubles_ok(st, out) and out["slot_0"] == {"type": "switch", "species": "garchomp"}
    assert out["slot_1"]["type"] == "move" and not is_protect(out["slot_1"])
    assert L.STATS.get("kept_repicked", 0) == repicked + 1 and pol.last_protect["incineroar"] == 2
    # the replacement is the exact scorer's best partner for the kept switch
    ctx = pol.test_ctx
    (_, c0s), (_, c1s) = ctx["per_slot"]
    sw = next(c for c in c0s if c["cand"] == out["slot_0"])
    alts = [c for c in c1s if not is_protect(c["cand"]) and pol._pair_value(ctx, sw, c) is not None]
    assert out["slot_1"] == max(alts, key=lambda c: pol._pair_value(ctx, sw, c))["cand"]


@needs_calc
def test_ally_pollen_puff_is_kept_and_a_protect_chain_next_to_it_is_not(monkeypatch):
    monkeypatch.delenv("LOOKAHEAD_SWITCH_FIX", raising=False)
    st = state([view(INC, 0.3), view(AMOO)], [view(RILL, foe=True), view(CHIYU, foe=True)])
    mine, theirs = (INC, AMOO, GAR), (RILL, CHIYU)
    puff = {"type": "move", "move_id": "pollenpuff", "target": -1}
    with monkeypatch.context() as m:                     # the heal alone: kept, the lookahead steps aside
        boost_exact(m, {1: is_puff_on_ally})
        pol = policy(mine, theirs)
        out = pol.act(st)["action"]
        assert doubles_ok(st, out) and out["slot_1"] == puff and pol.last_lookahead is None
    boost_exact(monkeypatch, {0: is_protect, 1: is_puff_on_ally})
    pol = policy(mine, theirs)
    pol.last_protect["incineroar"] = 2                   # the heal next to a repeat Protect: heal kept, no chain
    out = pol.act(st)["action"]
    assert doubles_ok(st, out) and out["slot_1"] == puff
    assert out["slot_0"]["type"] == "move" and not is_protect(out["slot_0"])


@needs_calc
def test_exact_table_carries_our_single_target_attacks_into_the_partner():
    st = state([view(CHIYU), view(INC)], [view(RILL, foe=True), view(AMOO, foe=True)])
    pol, obs = policy(), st["observation"]
    tmpl = st["legal_actions"]["actions"][0]["input"]["action"]
    table = pol._exact_table(obs, tmpl, obs["active_pokemon"], obs["opponent_active_pokemon"])
    for key in (("me", 0, "darkpulse", "me", 1), ("me", 0, "overheat", "me", 1), ("me", 1, "knockoff", "me", 0),
                ("me", 1, "flareblitz", "me", 0), ("me", 1, "fakeout", "me", 0)):
        assert table[key] and max(table[key]["rolls"]) > 0
    assert table[("me", 1, "knockoff", "me", 0)]["defender_hp"] == table[("opp", 0, "woodhammer", "me", 0)]["defender_hp"]
    assert ("me", 0, "heatwave", "me", 1) not in table and ("me", 0, "snarl", "me", 1) not in table   # foes only


@needs_calc
def test_knobs_reach_the_lookahead_and_explicit_defaults_change_nothing(monkeypatch):
    st = state([view(CHIYU), view(INC)], [view(RILL, 0.5, foe=True), view(AMOO, foe=True)])
    for k in KNOBS:
        monkeypatch.delenv(k, raising=False)
    pol = policy()
    want = pol.act(st)["action"]
    la = dict(pol.last_lookahead)
    assert la["k"] == (5, 4) and la["alpha"] == 0.4 and la["foe_weight"] == "equal"
    for k, v in zip(KNOBS, ("0.4", "equal", "1.0", "5", "4")):
        monkeypatch.setenv(k, v)
    pol = policy()
    assert pol.act(st)["action"] == want and pol.last_lookahead["score"] == la["score"]
    monkeypatch.setenv("LOOKAHEAD_K_OURS", "2")
    monkeypatch.setenv("LOOKAHEAD_K_THEIRS", "1")
    pol = policy()
    out = pol.act(st)["action"]
    assert doubles_ok(st, out) and pol.last_lookahead["k"] == (2, 1)
    assert pol.last_lookahead["rows"] < la["rows"] and pol.last_lookahead["cols"] < la["cols"]
    monkeypatch.delenv("LOOKAHEAD_K_OURS")
    monkeypatch.delenv("LOOKAHEAD_K_THEIRS")
    monkeypatch.setenv("LOOKAHEAD_ALPHA", "1")
    monkeypatch.setenv("LOOKAHEAD_FOE_WEIGHT", "softmax")
    monkeypatch.setenv("LOOKAHEAD_FOE_TEMP", "0.5")
    pol = policy()
    out = pol.act(st)["action"]
    assert doubles_ok(st, out) and pol.last_lookahead["alpha"] == 1.0 and pol.last_lookahead["foe_weight"] == "softmax"
    assert out == policy().act(st)["action"]


# ---- Nash mixing (LOOKAHEAD_MIX) ---------------------------------------------------------------------------
def test_rm_plus_rock_paper_scissors_is_uniform():
    rps = [[0, -1, 1], [1, 0, -1], [-1, 1, 0]]
    for A in (rps, [[5 + 2 * v for v in r] for r in rps]):
        x, y, gap = L.solve_rm_plus(A)
        assert x == pytest.approx([1 / 3] * 3, abs=1e-3) and y == pytest.approx([1 / 3] * 3, abs=1e-3)
        assert gap < 1e-3
    x, y, gap = L.solve_rm_plus([[3, -1], [-1, 1]])     # mixed, not uniform: p(row 0) = q(col 0) = 1/3
    assert x == pytest.approx([1 / 3, 2 / 3], abs=0.01) and y == pytest.approx([1 / 3, 2 / 3], abs=0.01)
    assert gap < 0.01


def test_rm_plus_plays_the_dominant_row_and_rejects_bad_matrices():
    A = [[2.0, 3.0, 1.5, 0.2], [1.0, 2.5, 0.5, 0.1], [0.0, 1.0, -1.0, -3.0]]
    x, y, gap = L.solve_rm_plus(A)
    assert x[0] > 0.99 and y[3] > 0.99 and gap < 0.01
    assert L.solve_rm_plus(A) == (x, y, gap)             # deterministic
    for bad in ([], [[]], [[1.0, 2.0], [3.0]], [[1.0, float("nan")], [0.0, 1.0]], [[float("inf")]]):
        assert L.solve_rm_plus(bad) is None
    assert L.solve_rm_plus([[1, 0], [0, 1]], deadline=time.monotonic() - 1) is None


def mix_board():
    me = [smon("me0", "chiyu", speed=200), smon("me1", "incineroar", speed=50)]
    opp = [smon("opp0", "rillaboom", speed=100), smon("opp1", "amoonguss", speed=30)]
    s = search(me + opp, {})
    s.t0, s.seed_key = time.monotonic(), ("sess", 7, 3)
    kn = act(s, "me1", "knockoff", ("opp", 1))
    rows = [(act(s, "me0", "darkpulse", ("opp", 0)), kn), (act(s, "me0", "darkpulse", ("opp", 1)), kn),
            (act(s, "me0", "heatwave"), kn)]
    none = L._Act(None, "opp", None, None)
    cols = [(act(s, "opp0", "protect"), none), (none, act(s, "opp1", "protect"))]
    # guessing which foe Protects: rows 0/1 mixed 50/50 are worth 0.5; the robust rule takes the safe row 2
    matrix = [[0.0, 1.0], [1.0, 0.0], [0.3, 0.3]]
    keys = [(0.2, 0.0, 0), (0.2, 0.0, -1), (0.3, 0.0, -2)]
    return s, rows, cols, matrix, keys


def test_mix_modes_draw_from_the_equilibrium_reproducibly():
    s, rows, cols, matrix, keys = mix_board()
    n, info = s._mix("argmax", rows, cols, matrix, keys, rows[2])
    assert n == 0 and not info["pure"] and info["p"][0][0] == pytest.approx(0.5, abs=0.01)
    draws = set()
    for seed in range(40):
        s.seed_key = ("sess", seed, 3)
        n, _ = s._mix("sample", rows, cols, matrix, keys, rows[2])
        assert n in (0, 1) and s._mix("sample", rows, cols, matrix, keys, rows[2])[0] == n
        draws.add(n)
    assert draws == {0, 1}
    # a saddle point needs no solver: the pure maximin row; maximin ties go to the robust key, never to the dice
    n, info = s._mix("sample", rows, cols, [[0.1, 0.2], [0.0, 0.5], [0.3, 0.4]], keys, rows[0])
    assert n == 2 and info["pure"]
    for seed in range(10):
        s.seed_key = ("sess", seed, 3)
        assert s._mix("sample", rows, cols, [[0.3, 0.9], [0.3, 0.4], [0.1, 0.5]], keys, rows[0])[0] == 0


def test_mix_fallbacks_keep_the_robust_row(monkeypatch):
    s, rows, cols, matrix, keys = mix_board()
    monkeypatch.setattr(L, "solve_rm_plus", lambda *a, **k: None)
    assert s._mix("argmax", rows, cols, matrix, keys, rows[2]) is None
    monkeypatch.setattr(L, "solve_rm_plus", lambda A, *a, **k: ([float("nan")] * len(A), [], 0.0))
    assert s._mix("sample", rows, cols, matrix, keys, rows[2]) is None
    monkeypatch.undo()
    monkeypatch.setattr(L, "MIX_TIME", 0.0)
    assert s._mix("argmax", rows, cols, matrix, keys, rows[2]) is None
    monkeypatch.undo()
    assert s._mix("argmax", [], cols, [], [], None) is None
    nan = [[float("nan")] * 2] * 3                      # no finite row left: nothing to mix
    assert s._mix("argmax", rows, cols, nan, keys, rows[2]) is None


def test_mix_keeps_the_exact_pick_in_the_subgame():
    s, rows, cols, _, _ = mix_board()
    a0, kn = rows[0]
    rows = [(a0, kn) for _ in range(L.MIX_K + 1)]        # the pick (last) is the worst row by the robust key ...
    keys = [(0.5, 0.0, -n) for n in range(L.MIX_K)] + [(0.1, 0.0, -L.MIX_K)]
    matrix = [[0.0, 0.0]] * L.MIX_K + [[1.0, 1.0]]         # ... but the only one worth playing in the game
    for mode in ("argmax", "sample"):
        n, info = s._mix(mode, rows, cols, matrix, keys, rows[-1])
        assert n == L.MIX_K and info["rows"] == L.MIX_K


def test_mix_argmax_breaks_near_ties_by_the_robust_key_and_sample_drops_residue(monkeypatch):
    s, rows, cols, matrix, keys = mix_board()           # subgame rows by robust key: 2, 0, 1
    monkeypatch.setattr(L, "solve_rm_plus", lambda A, *a, **k: ([0.0, 0.4999, 0.5001], [0.5, 0.5], 0.001))
    assert s._mix("argmax", rows, cols, matrix, keys, rows[2])[0] == 0      # 0.5 each at 3 decimals: key decides
    keys = [(0.2, 0.0, 0), (0.25, 0.0, -1), (0.3, 0.0, -2)]                 # now row 1 has the better key
    assert s._mix("argmax", rows, cols, matrix, keys, rows[2])[0] == 1
    x = [0.049, 0.4755, 0.4755]                          # row 2 (first in the subgame) is solver residue
    monkeypatch.setattr(L, "solve_rm_plus", lambda A, *a, **k: (list(x), [0.5, 0.5], 0.001))
    draws = set()
    for seed in range(200):
        s.seed_key = ("sess", seed, 3)
        draws.add(s._mix("sample", rows, cols, matrix, keys, rows[2])[0])
    assert draws == {0, 1}


@needs_calc
def test_mix_modes_are_legal_deterministic_and_fall_back_to_the_robust_row(monkeypatch):
    st = state([view(CHIYU), view(INC)], [view(RILL, 0.5, foe=True), view(AMOO, foe=True)])
    monkeypatch.setenv("LOOKAHEAD_MIX", "off")
    pol = policy()
    want = pol.act(st)["action"]
    assert pol.last_lookahead["mix"] is None
    for mode in ("sample", "argmax"):
        monkeypatch.setenv("LOOKAHEAD_MIX", mode)
        pol = policy()
        out = pol.act(st)["action"]
        assert doubles_ok(st, out) and pol.last_lookahead["mix_mode"] == mode and pol.last_lookahead["mix"]
        assert out == policy().act(st)["action"]
        with monkeypatch.context() as m:
            def boom(self, *a, **k):
                raise RuntimeError("bug")
            m.setattr(L._Search, "_mix", boom)
            pol = policy()
            assert pol.act(st)["action"] == want and pol.last_lookahead["mix"] is None
        with monkeypatch.context() as m:
            m.setattr(L, "MIX_TIME", 0.0)
            assert policy().act(st)["action"] == want


def test_no_calculator_means_heuristic(monkeypatch):
    monkeypatch.setattr(DamageCalc.get(), "enabled", False)
    st = state([view(CHIYU), view(INC)], [view(RILL, foe=True), view(AMOO, foe=True)])
    pol = policy()
    out = pol.act(st)["action"]
    assert doubles_ok(st, out) and not pol.last_exact and pol.last_lookahead is None
