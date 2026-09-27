"""One-step joint lookahead (agent/pokemon/lookahead.py): cell semantics on synthetic boards (always run),
plus calc-backed integration through PokemonPolicy (skipped without agent/js/node_modules)."""
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


def test_no_calculator_means_heuristic(monkeypatch):
    monkeypatch.setattr(DamageCalc.get(), "enabled", False)
    st = state([view(CHIYU), view(INC)], [view(RILL, foe=True), view(AMOO, foe=True)])
    pol = policy()
    out = pol.act(st)["action"]
    assert doubles_ok(st, out) and not pol.last_exact and pol.last_lookahead is None
