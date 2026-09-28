"""Speed-aware KO races, Protect / Tailwind / Trick Room valuation and Focus Sash handling (policy.py).

Pure-math tests always run; scenario tests need the damage calculator (agent/js/node_modules)."""
import glob
import json
import os

import pytest

from agent.agent import doubles_ok
from agent.pokemon import policy as P
from agent.pokemon.calc import _js_dir
from agent.pokemon.policy import (PokemonPolicy, _breaks_sash, _cond_ids, _first, _hit_stats, _pair_ko, _pair_points,
                                  _pko, _points, _sashed, _speed_control_value, _speed_view)
from agent.pokemon.stats import effective_speed, move_priority

needs_calc = pytest.mark.skipif(_js_dir() is None, reason="agent/js has no node_modules")


def _recorded_sessions():
    root = os.environ.get("ARENA_LOG_DIR", "logs")
    return sorted(glob.glob(os.path.join(root, "*", "*.jsonl")))


def _id(s):
    return s.lower().replace("-", "").replace(" ", "")


GARCHOMP = {"species": "Garchomp", "item": "Focus Sash", "ability": "Rough Skin", "nature": "Jolly", "level": 50,
            "evs": {"hp": 4, "atk": 252, "spe": 252}, "ivs": {}, "moves": ["Earthquake", "Rock Slide", "Iron Head", "Protect"]}
WHIMSI = {"species": "Whimsicott", "item": "Focus Sash", "ability": "Prankster", "nature": "Timid", "level": 50,
          "evs": {"hp": 4, "spa": 252, "spe": 252}, "ivs": {}, "moves": ["Tailwind", "Moonblast", "Encore", "Light Screen"]}
GHOLD = {"species": "Gholdengo", "item": "Air Balloon", "ability": "Good as Gold", "nature": "Timid", "level": 50,
         "evs": {"spa": 252, "spd": 4, "spe": 252}, "ivs": {}, "moves": ["Make It Rain", "Shadow Ball", "Nasty Plot", "Protect"]}
ARCHA = {"species": "Archaludon", "item": "Leftovers", "ability": "Stamina", "nature": "Bold", "level": 50,
         "evs": {"hp": 252, "def": 252, "spa": 4}, "ivs": {}, "moves": ["Body Press", "Draco Meteor", "Flash Cannon", "Protect"]}
KINGAMBIT = {"species": "Kingambit", "item": "Black Glasses", "ability": "Supreme Overlord", "nature": "Adamant", "level": 50,
             "evs": {"hp": 252, "atk": 252, "spe": 4}, "ivs": {}, "moves": ["Sucker Punch", "Iron Head", "Kowtow Cleave", "Protect"]}
CHIYU = {"species": "Chi-Yu", "item": "Choice Specs", "ability": "Beads of Ruin", "nature": "Timid", "level": 50,
         "evs": {"hp": 4, "spa": 252, "spe": 252}, "ivs": {}, "moves": ["Heat Wave", "Dark Pulse", "Overheat", "Snarl"]}
AMOO = {"species": "Amoonguss", "item": "Rocky Helmet", "ability": "Regenerator", "nature": "Calm", "level": 50,
        "evs": {"hp": 252, "def": 100, "spd": 156}, "ivs": {}, "moves": ["Spore", "Rage Powder", "Pollen Puff", "Protect"]}
LANDO = {"species": "Landorus-Therian", "item": "Choice Scarf", "ability": "Intimidate", "nature": "Jolly", "level": 50,
         "evs": {"atk": 252, "def": 4, "spe": 252}, "ivs": {}, "moves": ["Earthquake", "Rock Slide", "U-turn", "Protect"]}
INCIN = {"species": "Incineroar", "item": "Safety Goggles", "ability": "Intimidate", "nature": "Careful", "level": 50,
         "evs": {"hp": 252, "atk": 4, "spd": 252}, "ivs": {}, "moves": ["Fake Out", "Flare Blitz", "Knock Off", "Parting Shot"]}
IRONHANDS = {"species": "Iron Hands", "item": "Assault Vest", "ability": "Quark Drive", "nature": "Adamant", "level": 50,
             "evs": {"hp": 252, "atk": 252, "spd": 4}, "ivs": {},
             "moves": ["Fake Out", "Close Combat", "Wild Charge", "Drain Punch"]}
TORNADUS = {"species": "Tornadus", "item": "Covert Cloak", "ability": "Prankster", "nature": "Timid", "level": 50,
            "evs": {"hp": 4, "spa": 252, "spe": 252}, "ivs": {}, "moves": ["Bleakwind Storm", "Taunt", "Tailwind", "Protect"]}


def mon(card, hp=1.0, foe=False, **kw):
    v = {"species": _id(card["species"]), "name": card["species"], "current_hp_fraction": hp, "current_hp": int(100 * hp),
         "max_hp": 100, "status": None, "fainted": False, "boosts": {}, **kw}
    if foe:
        v.update(item="unknown_item", ability=None, moves=[])
    return v


def opts(card, slot):
    """Move options for a card the way the platform sends them (single-target: [-ally, 1, 2]; spread/self: [0])."""
    from agent.pokemon import dex
    ally = -2 if slot == 0 else -1
    out = []
    for mv in card["moves"]:
        mid = _id(mv)
        t = dex.move(mid).get("target")
        out.append({"type": "move", "move_id": mid,
                    "targets": [ally, 1, 2] if t in ("normal", "any") else [1, 2] if t == "adjacentFoe" else [0]})
    return out


def state(slots, actives, opps, turn=3, **obs):
    o = {"is_doubles": True, "turn": turn, "force_switch": [False, False], "active_pokemon": actives,
         "opponent_active_pokemon": opps, "weather": {}, "fields": {}, "side_conditions": {},
         "opponent_side_conditions": {}, **obs}
    return {"phase": "moving", "state_version": 3, "session_id": "s", "game_type": "pokemon_vgc_doubles_draft",
            "observation": o, "legal_actions": {"actions": [{"action_id": "doubles_turn",
                                                             "input": {"action": {"type": "doubles_turn", "slots": slots}}}]}}


def policy(cards, opp_cards, prev=None, prev_opp=None):
    pol = PokemonPolicy()
    for c in cards:
        pol.cards[_id(c["species"])] = c
    for c in opp_cards:
        pol.opp_cards[_id(c["species"])] = c
    pol.prev_active = list(prev or [_id(c["species"]) for c in cards][:2])   # not fresh: no Fake Out
    pol.prev_opp = list(prev_opp or [_id(c["species"]) for c in opp_cards][:2])
    return pol


# ---------------------------------------------------------------- Focus Sash / Sturdy math
def test_sash_stops_a_lone_hit_but_not_two():
    rolls = [150] * 16
    assert _hit_stats(rolls, 100) == (1.0, 1.0)
    useful, pko = _hit_stats(rolls, 100, sash=True)
    assert pko == 0.0 and useful == pytest.approx(0.99)            # left on 1 HP
    assert _points(rolls, 100, sash=True) == pytest.approx(99.0)
    assert _points(rolls, 100) == pytest.approx(100 + P.KO_BONUS)
    assert _pko(rolls, 1.0, 100, sash=True) == 0.0 and _pko(rolls, 0.9, 100) == pytest.approx(0.9)
    two = _pair_points((rolls, 1.0), ([30] * 16, 1.0), 100, True, True)
    assert two == pytest.approx(100 + P.KO_BONUS)                  # the second hit breaks through the Sash
    assert _pair_ko((rolls, 1.0), ([30] * 16, 1.0), 100, True, True) == pytest.approx(1.0)
    # The other attacker does 0 (immune): the lone hit is still stopped.
    assert _pair_ko((rolls, 1.0), ([0] * 16, 1.0), 100, True, True) == 0.0
    assert _pair_ko((rolls, 1.0), ([0] * 16, 1.0), 100) == pytest.approx(1.0)
    # Accuracy mixes the lone-hit branches: 50% the partner misses -> Sash holds.
    assert _pair_ko((rolls, 1.0), ([30] * 16, 0.5), 100, True, True) == pytest.approx(0.5)
    # Without a Sash nothing changes (regression guard for the base scale).
    assert _pair_points((rolls, 1.0), ([30] * 16, 1.0), 100) == pytest.approx(100 + P.KO_BONUS)


def test_sashed_needs_full_hp_and_reads_the_drafted_card():
    assert _sashed(mon(GARCHOMP, foe=True), GARCHOMP)               # item hidden in battle: the draft card says Sash
    assert not _sashed(mon(GARCHOMP, hp=0.99, foe=True), GARCHOMP)
    assert not _sashed(mon(CHIYU, foe=True), CHIYU)
    assert _sashed({"species": "skarmory", "current_hp_fraction": 1.0, "ability": "Sturdy"}, None)
    assert _sashed({"species": "garchomp"}, GARCHOMP)               # no HP key: assume full
    assert _breaks_sash("surgingstrikes") and _breaks_sash("iciclespear") and not _breaks_sash("closecombat")


# ---------------------------------------------------------------- speed helpers
def test_speed_helpers():
    assert (_first(True), _first(False), _first(None)) == (1.0, 0.0, 0.5)
    assert _cond_ids({"TAILWIND": 3, "TRICK_ROOM": 1}) == {"tailwind", "trickroom"}
    assert _cond_ids(["tailwind"]) == {"tailwind"} and _cond_ids(None) == set() and _cond_ids("x") == set()
    v = _speed_view({"species": "chiyu", "item": "unknown_item", "boosts": None})
    assert v["item"] is None and v["boosts"] == {}
    assert move_priority("grassyglide", {}, None, "Grassy") == 1 and move_priority("grassyglide", {}, None, None) == 0
    assert move_priority("tailwind", {"ability": "Prankster"}) == 1
    assert move_priority("bravebird", {"ability": "Gale Wings", "current_hp_fraction": 1.0}) == 1
    assert move_priority("bravebird", {"ability": "Gale Wings", "current_hp_fraction": 0.5}) == 0


def test_speed_control_value_counts_flipped_matchups():
    sm = {"mine": {0, 1}, "foes": {0, 1}, "my_spe": {0: 100.0, 1: 90.0}, "foe_spe": {0: 150.0, 1: 120.0}, "tr": False}
    obs = {"side_conditions": {}}
    assert _speed_control_value("tailwind", sm, obs, 70.0) == pytest.approx(70.0)      # flips all 4 matchups
    fast = dict(sm, my_spe={0: 200.0, 1: 190.0})
    assert _speed_control_value("tailwind", fast, obs, 70.0) == pytest.approx(20.0)    # already faster
    assert _speed_control_value("tailwind", sm, {"side_conditions": {"TAILWIND": 2}}, 70.0) == -10.0
    assert _speed_control_value("trickroom", sm, obs, 20.0) > 60                       # slow team: set it
    assert _speed_control_value("trickroom", fast, obs, 20.0) < 0                      # fast team: don't
    assert _speed_control_value("trickroom", dict(fast, tr=True), obs, 20.0) == 40.0   # ending their TR helps us
    assert _speed_control_value("trickroom", dict(sm, tr=True), obs, 20.0) == -60.0    # never end our own TR


def test_condition_turns_left_and_protect_memory():
    pol = PokemonPolicy()
    pol._track_conditions({"turn": 3, "opponent_side_conditions": {"TAILWIND": 2}})
    assert pol._cond_left("opp_tailwind", 3) == 3 and pol._cond_left("opp_tailwind", 4) == 2
    assert pol._cond_left("trickroom", 3) is None
    pol._track_conditions({"turn": 6, "opponent_side_conditions": {}})
    assert pol._cond_left("opp_tailwind", 6) is None
    # Protect twice in a row is refused; a forced-switch state in between does not reset the memory.
    pick = {"slot_0": {"type": "move", "move_id": "protect", "target": 0}, "slot_1": {"type": "move", "move_id": "bodypress", "target": 1}}
    pol._remember({"force_switch": [False, False]}, pick, [{"species": "gholdengo"}, {"species": "archaludon"}])
    assert pol._protected_last(0, "gholdengo") and not pol._protected_last(1, "archaludon")
    pol._remember({"force_switch": [False, True]}, {"slot_0": {"type": "pass"}, "slot_1": {"type": "switch", "species": "garchomp"}},
                  [{"species": "gholdengo"}, None])
    assert pol._protected_last(0, "gholdengo") and not pol._protected_last(1, "garchomp")
    assert not pol._protected_last(0, "kingambit")                     # a different mon in that slot
    c = {"slot": 0, "species": "gholdengo", "choice": False}
    assert pol._protect_value(c, {}, None, {}, 3) == P.PROTECT_AGAIN
    assert pol._protect_value({"slot": 1, "species": "landorustherian", "choice": True}, {}, None, {}, 3) == P.PROTECT_AGAIN


def test_paralysis_halves_speed_whatever_the_status_case():
    """Regression: the platform sends "PAR" (real logs carry "BRN" / "PSN"), the local sim "par"."""
    full = effective_speed(_speed_view({"species": "garchomp"}), GARCHOMP)
    assert full == pytest.approx(169.0)                                     # Jolly 252 Spe Garchomp at level 50
    for s in ("PAR", "par", "Par"):
        assert effective_speed(_speed_view({"species": "garchomp", "status": s}), GARCHOMP) == pytest.approx(full / 2)
        assert effective_speed({"species": "garchomp", "status": s}, GARCHOMP) == pytest.approx(full / 2)
    for s in ("BRN", "psn", "FNT", None, ""):
        assert effective_speed(_speed_view({"species": "garchomp", "status": s}), GARCHOMP) == pytest.approx(full)
    assert _speed_view({"status": "PAR"})["status"] == "par" and _speed_view({"status": None})["status"] is None


def test_speed_control_first_seen_in_a_forced_switch_state():
    """Regression (real apocalypse/cc03745a): the foe's Tailwind goes up on turn 4 and our slot 0 faints that turn,
    so the first state that shows it is our forced-switch state of turn 4 -- it lasts turns 4-7, not 3-6."""
    pol = PokemonPolicy()
    pol._track_conditions({"turn": 4, "force_switch": [False, False], "opponent_side_conditions": {}})
    pol._track_conditions({"turn": 4, "force_switch": [True, False], "opponent_side_conditions": {"TAILWIND": 4}})
    assert pol.cond_set["opp_tailwind"] == 4
    pol._track_conditions({"turn": 5, "force_switch": [False, False], "opponent_side_conditions": {"TAILWIND": 4}})
    assert [pol._cond_left("opp_tailwind", t) for t in (5, 6, 7)] == [3, 2, 1]
    # The other seat of the same match first sees its own Tailwind at the start of turn 5: the same count.
    other = PokemonPolicy()
    other._track_conditions({"turn": 5, "force_switch": [False, False], "side_conditions": {"TAILWIND": 4}})
    assert [other._cond_left("my_tailwind", t) for t in (5, 6, 7)] == [3, 2, 1]
    # Trick Room (5 turns including the one it is set on), set in a turn that ended with our forced switch.
    tr = PokemonPolicy()
    tr._track_conditions({"turn": 2, "force_switch": [False, True], "fields": {"TRICK_ROOM": 2}})
    assert [tr._cond_left("trickroom", t) for t in (3, 4, 5, 6)] == [4, 3, 2, 1]
    # Local-sim shapes (fields as a list, side conditions {name: 1}) take the same path.
    loc = PokemonPolicy()
    loc._track_conditions({"turn": 7, "force_switch": [True, False], "fields": ["trickroom"],
                           "opponent_side_conditions": {"tailwind": 1}})
    assert loc._cond_left("trickroom", 8) == 4 and loc._cond_left("opp_tailwind", 8) == 3


@pytest.mark.skipif(not _recorded_sessions(), reason="no recorded platform sessions (logs/*/*.jsonl)")
def test_tracked_speed_control_start_turn_matches_the_real_payloads():
    """On every recorded platform session the start turn we infer equals the platform's own value ({"TAILWIND": 4})."""
    checked = 0
    for f in _recorded_sessions():
        pol = PokemonPolicy()
        for line in open(f):
            d = json.loads(line)
            raw = d.get("raw") or {}
            obs = raw.get("observation") or {}
            if d.get("kind") != "action" or raw.get("phase") != "moving" or not obs.get("is_doubles"):
                continue
            pol._track_conditions(obs)
            for key, src, name in (("opp_tailwind", "opponent_side_conditions", "TAILWIND"),
                                   ("my_tailwind", "side_conditions", "TAILWIND"), ("trickroom", "fields", "TRICK_ROOM")):
                v = obs.get(src).get(name) if isinstance(obs.get(src), dict) else None
                if isinstance(v, int):
                    assert pol.cond_set.get(key) == v, (f, obs.get("turn"), key)
                    checked += 1
    if not checked:
        pytest.skip("no Tailwind / Trick Room in the recorded sessions")


def test_protect_value_parts(monkeypatch):
    """Chip damage absorbed only counts while the partner acts; a lethal hit is the KO-save part; never twice."""
    monkeypatch.setattr(P, "PROTECT_ABSORB", 40.0)
    pol = PokemonPolicy()
    chip = {"mine": {0, 1}, "foes": {0}, "focus": {(0, 0): 1.0}, "stall": {},
            "threat": {(0, 0): (0.0, 0.6, 0, False, 60.0, {0: 0.0})}}
    me = {"slot": 0, "species": "garchomp", "hp": 1.0}
    attacker = {"acts": True, "nb": {}, "ko": {}, "use": {}, "pa1": 1.0, "mid": "earthquake", "target": 0}
    assert pol._protect_value(me, attacker, chip, {}, 3) == pytest.approx(P.PROTECT_IDLE + 40.0 * 0.6)
    assert pol._protect_value(me, {"acts": False}, chip, {}, 3) == pytest.approx(P.PROTECT_IDLE)
    lethal = dict(chip, threat={(0, 0): (1.0, 1.0, 0, False, 150.0, {0: 1.0})})
    v_alone = pol._protect_value(me, attacker, lethal, {}, 3)
    assert v_alone == pytest.approx(P.PROTECT_SAVE * P.PROTECT_ALONE)       # no absorb term for a lethal hit
    killer = dict(attacker, ko={0: 1.0}, pa1=1.0)                             # partner KOs the threat (after it acts)
    assert pol._protect_value(me, killer, lethal, {}, 3) == pytest.approx(P.PROTECT_SAVE)
    first = dict(killer, nb={0: 1.0})                                         # ... or before it acts: no need
    assert pol._protect_value(me, first, lethal, {}, 3) == pytest.approx(P.PROTECT_IDLE)
    stall = dict(lethal, stall={0: True})
    assert pol._protect_value(me, attacker, stall, {}, 3) == pytest.approx(P.PROTECT_SAVE * P.PROTECT_ALONE + P.STALL_BONUS)


# ---------------------------------------------------------------- scenarios on the real calculator
@needs_calc
def test_speed_model_knows_sash_and_priority():
    pol = policy([CHIYU, AMOO], [WHIMSI, KINGAMBIT])
    slots = [{"slot": 0, "options": opts(CHIYU, 0)}, {"slot": 1, "options": opts(AMOO, 1)}]
    st = state(slots, [mon(CHIYU), mon(AMOO)], [mon(WHIMSI, foe=True), mon(KINGAMBIT, foe=True)])
    a = pol.act(st)["action"]
    assert pol.last_exact and doubles_ok(st, a)
    sm = pol.last_speed
    assert sm["foe_sash"] == {0: True, 1: False}
    assert sm["foe_spe"][0] > sm["my_spe"][1]                          # Whimsicott outspeeds Amoonguss
    assert sm["foe_prio"][1] in (0, 1)
    assert any(p == 1 for p in sm["threat"][(1, 0)][5])               # Sucker Punch tracked in its own bracket


@needs_calc
def test_protect_when_ko_is_certain_and_partner_handles_the_threat_but_never_twice():
    # Kingambit's Sucker Punch (+1) OHKOs Gholdengo; Archaludon's Body Press hits Kingambit hard.
    pol = policy([GHOLD, ARCHA], [CHIYU, KINGAMBIT])
    slots = [{"slot": 0, "options": opts(GHOLD, 0)}, {"slot": 1, "options": opts(ARCHA, 1)}]
    opps = [None, mon(KINGAMBIT, foe=True)]
    st = state(slots, [mon(GHOLD), mon(ARCHA, hp=0.8)], opps, turn=5)
    a = pol.act(st)["action"]
    assert doubles_ok(st, a)
    assert a["slot_0"] == {"type": "move", "move_id": "protect", "target": 0}
    assert a["slot_1"]["type"] == "move" and a["slot_1"]["target"] == 2
    a = pol.act(state(slots, [mon(GHOLD), mon(ARCHA, hp=0.6)], opps, turn=6))["action"]
    assert a["slot_0"]["move_id"] != "protect"                         # the second Protect would fail 2/3


@needs_calc
def test_no_idle_protect_without_danger():
    pol = policy([ARCHA, GARCHOMP], [AMOO, WHIMSI])
    slots = [{"slot": 0, "options": opts(ARCHA, 0)}, {"slot": 1, "options": opts(GARCHOMP, 1)}]
    st = state(slots, [mon(ARCHA), mon(GARCHOMP)], [mon(AMOO, foe=True), mon(WHIMSI, foe=True)])
    a = pol.act(st)["action"]
    assert doubles_ok(st, a)
    assert a["slot_0"]["move_id"] != "protect" and a["slot_1"]["move_id"] != "protect"


@needs_calc
def test_partner_protect_refunds_earthquake_ally_damage(monkeypatch):
    slots = [{"slot": 0, "options": opts(GARCHOMP, 0)}, {"slot": 1, "options": opts(ARCHA, 1)}]
    st = state(slots, [mon(GARCHOMP), mon(ARCHA)], [mon(CHIYU, foe=True), mon(KINGAMBIT, foe=True)])

    def values():
        pol = policy([GARCHOMP, ARCHA], [CHIYU, KINGAMBIT])
        pol.debug_pairs = []
        assert doubles_ok(st, pol.act(st)["action"])
        return {(x["move_id"], x["target"], y["move_id"], y["target"]): v for v, x, y in pol.debug_pairs}

    hurt = values()
    monkeypatch.setattr(P, "ALLY_HIT_WEIGHT", 0.0)                    # as if Earthquake never touched our partner
    free = values()
    # Next to a Protecting Archaludon the Earthquake costs nothing; next to an attacking one it hurts our partner.
    assert hurt[("earthquake", 0, "protect", 0)] == pytest.approx(free[("earthquake", 0, "protect", 0)], abs=0.2)
    assert hurt[("earthquake", 0, "flashcannon", 1)] < free[("earthquake", 0, "flashcannon", 1)] - 10
    assert hurt[("rockslide", 0, "flashcannon", 1)] == pytest.approx(free[("rockslide", 0, "flashcannon", 1)], abs=0.2)


@needs_calc
def test_sashed_foe_is_not_counted_as_a_one_hit_ko():
    pol = policy([CHIYU, AMOO], [WHIMSI, KINGAMBIT])
    pol.debug_pairs = []
    slots = [{"slot": 0, "options": opts(CHIYU, 0)}, {"slot": 1, "options": opts(AMOO, 1)}]
    full = state(slots, [mon(CHIYU), mon(AMOO)], [mon(WHIMSI, foe=True), mon(KINGAMBIT, foe=True)])
    pol.act(full)
    v_full = {(x["move_id"], x["target"], y["move_id"], y["target"]): v for v, x, y in pol.debug_pairs}
    chip = state(slots, [mon(CHIYU), mon(AMOO)], [mon(WHIMSI, hp=0.99, foe=True), mon(KINGAMBIT, foe=True)])
    pol.act(chip)
    v_chip = {(x["move_id"], x["target"], y["move_id"], y["target"]): v for v, x, y in pol.debug_pairs}
    k = ("overheat", 1, "spore", 2)
    assert v_chip[k] > v_full[k] + P.KO_BONUS * 0.5                   # the KO bonus only exists once the Sash is gone


@needs_calc
def test_speed_model_reads_platform_paralysis():
    """Regression: an upper-case "PAR" (platform payload) halves speed inside the speed model, like "par" locally."""
    slots = [{"slot": 0, "options": opts(CHIYU, 0)}, {"slot": 1, "options": opts(AMOO, 1)}]

    def speeds(status):
        pol = policy([CHIYU, AMOO], [WHIMSI, KINGAMBIT])
        st = state(slots, [mon(CHIYU, status=status), mon(AMOO)], [mon(WHIMSI, foe=True, status=status),
                                                                    mon(KINGAMBIT, foe=True)])
        assert doubles_ok(st, pol.act(st)["action"]) and pol.last_exact
        return pol.last_speed["my_spe"], pol.last_speed["foe_spe"]

    (me0, foe0), (me_up, foe_up), (me_lo, foe_lo) = speeds(None), speeds("PAR"), speeds("par")
    assert me_up[0] == me_lo[0] == pytest.approx(me0[0] / 2) and me_up[1] == me0[1]
    assert foe_up[0] == foe_lo[0] == pytest.approx(foe0[0] / 2) and foe_up[1] == foe0[1]


@needs_calc
def test_foe_first_seen_in_our_forced_switch_state_keeps_its_fake_out():
    """Regression: Incineroar comes in mid-turn 5 (Parting Shot / Eject Button), then our Archaludon faints, so our
    forced-switch state of turn 5 already shows it. On turn 6 it is still fresh and its Fake Out works."""
    fs = [{"slot": 0, "options": [{"type": "pass"}]},
          {"slot": 1, "options": [{"type": "switch", "species": "garchomp"}]}]
    slots = [{"slot": 0, "options": opts(GHOLD, 0)}, {"slot": 1, "options": opts(GARCHOMP, 1)}]
    turn6 = state(slots, [mon(GHOLD), mon(GARCHOMP)], [mon(INCIN, foe=True), mon(AMOO, foe=True)], turn=6)

    pol = policy([GHOLD, ARCHA, GARCHOMP], [KINGAMBIT, AMOO, INCIN], prev=["gholdengo", "archaludon"],
                 prev_opp=["kingambit", "amoonguss"])
    forced = state(fs, [mon(GHOLD), None], [mon(INCIN, foe=True), mon(AMOO, foe=True)], turn=5,
                   force_switch=[False, True])
    a = pol.act(forced)["action"]
    assert doubles_ok(forced, a) and a["slot_1"] == {"type": "switch", "species": "garchomp"}
    assert pol.prev_opp == ["kingambit", "amoonguss"] and pol.prev_active == ["gholdengo", "archaludon"]
    assert doubles_ok(turn6, pol.act(turn6)["action"])
    fo = pol.last_speed["fake_out"]
    assert fo.get((0, 0), 0.0) > 0 and fo.get((0, 1), 0.0) > 0 and not any(j == 1 for j, _ in fo)
    assert pol.prev_opp == ["incineroar", "amoonguss"]                 # a normal state is remembered

    # Control: Incineroar was already on the field at the start of turn 5 -> no Fake Out threat on turn 6.
    stale = policy([GHOLD, GARCHOMP], [INCIN, AMOO])
    assert doubles_ok(turn6, stale.act(turn6)["action"])
    assert stale.last_speed["fake_out"] == {}


@needs_calc
def test_a_slot_that_is_surely_koed_first_never_attacks_its_own_partner():
    """Regression (real yoriko/cc03745a turn 3): a 10% Iron Hands is outsped and KO'd by Scarf Landorus' Earthquake
    (spread, 100% accurate), so P(it acts) = 0 and every option scores the same. The tie used to go to the first
    listed target, -2 = our own Tornadus: Close Combat into the partner whenever the foe does something else."""
    lando = dict(LANDO, moves=["Earthquake", "Rock Slide", "Stone Edge", "Protect"])
    pol = policy([IRONHANDS, TORNADUS], [lando, AMOO])
    slots = [{"slot": 0, "options": opts(IRONHANDS, 0)}, {"slot": 1, "options": opts(TORNADUS, 1)}]
    st = state(slots, [mon(IRONHANDS, hp=0.1), mon(TORNADUS)], [mon(lando, foe=True), mon(AMOO, foe=True)])
    pol.debug_pairs = []
    a = pol.act(st)["action"]
    assert doubles_ok(st, a) and pol.last_exact
    sm = pol.last_speed
    assert sm["threat"][(0, 0)][0] == pytest.approx(1.0) and sm["threat"][(0, 0)][3]    # a sure spread KO ...
    assert sm["foe_spe"][0] > sm["my_spe"][0]                                            # ... before it moves
    vals = {(x["move_id"], x["target"]): v for v, x, y in pol.debug_pairs if y == a["slot_1"]}
    assert vals[("closecombat", -2)] == vals[("closecombat", 1)]            # ... so the options tie on value
    assert a["slot_0"]["type"] == "move" and a["slot_0"]["target"] in (1, 2)


@pytest.mark.skipif(not _recorded_sessions(), reason="no recorded platform sessions (logs/*/*.jsonl)")
def test_no_attack_into_our_own_live_partner_on_real_states():
    """Every recorded platform doubles state: no damaging / status move aimed at our own live partner (only the
    ally support moves Helping Hand / Pollen Puff / Decorate / Coaching may target it)."""
    from altruagent import DecisionContext, GameState
    from agent.agent import Contestant
    from agent.pokemon.dex import to_id
    bad = []
    for f in _recorded_sessions():
        c = Contestant()
        for line in open(f):
            d = json.loads(line)
            if d.get("kind") != "action":
                continue
            raw = d["raw"]
            obs = raw.get("observation") or {}
            ctx = DecisionContext(session_id="replay", tournament_id=None,
                                  game_type=raw.get("game_type") or "pokemon_vgc_doubles_draft", agent_id="me")
            out = c.choose_action(GameState.from_mcp_state(raw), ctx)
            if raw.get("phase") != "moving" or not obs.get("is_doubles") or not isinstance(out, dict):
                continue
            acts = list(obs.get("active_pokemon") or []) + [None, None]
            for i in (0, 1):
                s = out.get(f"slot_{i}") or {}
                ally = acts[1 - i]
                if s.get("type") == "move" and isinstance(s.get("target"), int) and s["target"] < 0 \
                        and ally and not ally.get("fainted") and to_id(s.get("move_id", "")) not in P.ALLY_SUPPORT_MOVES:
                    bad.append((f, obs.get("turn"), i, s))
    assert not bad, bad
