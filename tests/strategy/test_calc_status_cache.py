"""Calculator input / bridge fixes found in the league review (merge step 1):

* statuses reach calc.js lower case: the platform sends "BRN" / "PAR" / ..., calc.js only knows "brn" / "par" / ...,
  so a platform burn used to leave physical damage un-halved;
* DamageCalc.batch() clears an overflowing cache BEFORE inserting the current batch (it used to clear after inserting
  and then read the batch back from the empty cache: every calc of that decision came back None);
* an item the view shows as gone stays gone in the lookahead too (calc.view_item), matching the calc's own input.

Calc-backed tests skip without agent/js/node_modules."""
import json

import pytest

import agent.pokemon.calc as calc_mod
from agent.agent import doubles_ok
from agent.pokemon import dex
from agent.pokemon.calc import DamageCalc, _js_dir, calc_pokemon, calc_status, view_item
from agent.pokemon.lookahead import _norm_view
from agent.pokemon.policy import PokemonPolicy

needs_calc = pytest.mark.skipif(_js_dir() is None, reason="agent/js has no node_modules")

GARCHOMP = {"species": "Garchomp", "item": "Focus Sash", "ability": "Rough Skin", "nature": "Jolly", "level": 50,
            "evs": {"hp": 4, "atk": 252, "spe": 252}, "ivs": {}, "moves": ["Earthquake", "Rock Slide", "Iron Head", "Protect"]}
KINGAMBIT = {"species": "Kingambit", "item": "Black Glasses", "ability": "Supreme Overlord", "nature": "Adamant",
             "level": 50, "evs": {"hp": 252, "atk": 252, "spe": 4}, "ivs": {},
             "moves": ["Sucker Punch", "Iron Head", "Kowtow Cleave", "Protect"]}
CHIYU = {"species": "Chi-Yu", "item": "Choice Specs", "ability": "Beads of Ruin", "nature": "Timid", "level": 50,
         "evs": {"hp": 4, "spa": 252, "spe": 252}, "ivs": {}, "moves": ["Heat Wave", "Dark Pulse", "Overheat", "Snarl"]}
AMOO = {"species": "Amoonguss", "item": "Rocky Helmet", "ability": "Regenerator", "nature": "Calm", "level": 50,
        "evs": {"hp": 252, "def": 100, "spd": 156}, "ivs": {}, "moves": ["Spore", "Rage Powder", "Pollen Puff", "Protect"]}
ARCHA = {"species": "Archaludon", "item": "Leftovers", "ability": "Stamina", "nature": "Bold", "level": 50,
         "evs": {"hp": 252, "def": 252, "spa": 4}, "ivs": {}, "moves": ["Body Press", "Draco Meteor", "Flash Cannon", "Protect"]}
BOOSTER = {"species": "Iron Hands", "item": "Booster Energy", "ability": "Quark Drive", "nature": "Adamant", "level": 50,
           "evs": {"hp": 252, "atk": 252}, "ivs": {}, "moves": ["Fake Out", "Close Combat", "Wild Charge", "Drain Punch"]}


def sid(card):
    return dex.to_id(card["species"])


def one(attacker, defender, move):
    r = DamageCalc.get().batch([{"attacker": attacker, "defender": defender, "move": move,
                                 "field": {"gameType": "Singles"}}])
    assert r is not None and r[0] is not None, r
    return r[0]


# ---------------------------------------------------------------- status case
def test_calc_status_is_lower_cased_and_unknown_statuses_are_dropped():
    for raw, want in (("BRN", "brn"), ("brn", "brn"), ("PAR", "par"), ("Psn", "psn"), ("TOX", "tox"), ("SLP", "slp"),
                      ("FRZ", "frz")):
        assert calc_status(raw) == want
        assert calc_pokemon({"species": "garchomp", "status": raw}, GARCHOMP)["status"] == want
    for raw in (None, "", "FNT", "fnt", "confusion"):
        assert calc_status(raw) is None
        assert "status" not in calc_pokemon({"species": "garchomp", "status": raw}, GARCHOMP)


@needs_calc
def test_a_platform_burn_halves_physical_damage():
    """Regression: calc.js ignored the platform's "BRN", so a burned attacker hit as hard as a healthy one."""
    target = calc_pokemon({"species": "archaludon", "current_hp_fraction": 1.0}, ARCHA)
    healthy = one(calc_pokemon({"species": "garchomp"}, GARCHOMP), target, "Earthquake")
    burned = one(calc_pokemon({"species": "garchomp", "status": "BRN"}, GARCHOMP), target, "Earthquake")
    lower = one(calc_pokemon({"species": "garchomp", "status": "brn"}, GARCHOMP), target, "Earthquake")
    assert burned["rolls"] == lower["rolls"]                              # same answer whatever the case
    assert len(burned["rolls"]) == len(healthy["rolls"]) == 16
    for b, h in zip(burned["rolls"], healthy["rolls"]):
        assert abs(b - h / 2) <= 1, (b, h)                               # physical: halved (Showdown rounds per roll)
    assert burned["max"] <= healthy["max"] / 2 + 1
    # A special attack does not care about burn.
    spec_ok = one(calc_pokemon({"species": "chiyu"}, CHIYU), target, "Dark Pulse")
    spec_brn = one(calc_pokemon({"species": "chiyu", "status": "BRN"}, CHIYU), target, "Dark Pulse")
    assert spec_ok["rolls"] == spec_brn["rolls"]


# ---------------------------------------------------------------- cache order
class _EchoNode:
    """Stands in for the node process: answers every calc with {"rolls": [len(move)], "defender_hp": 100}."""

    def __init__(self):
        self.stdin = self
        self.stdout = self
        self.pending = []
        self.sent = 0

    def poll(self):
        return None

    def write(self, s):
        msg = json.loads(s)
        self.sent += len(msg["calcs"])
        self.pending.append(json.dumps({"id": msg["id"], "results": [
            {"rolls": [len(c["move"])], "defender_hp": 100, "move": c["move"]} for c in msg["calcs"]]}) + "\n")

    def flush(self):
        pass

    def readline(self):
        return self.pending.pop(0)


def _calc(move):
    return {"attacker": {"species": "Garchomp"}, "defender": {"species": "Archaludon"}, "move": move}


def test_an_overflowing_cache_is_cleared_before_the_batch_not_after(monkeypatch):
    """Regression: the batch that crossed the cap used to come back all None (cleared after inserting, then read
    back from the empty cache) - in battle the whole exact table / lookahead then fell back to the heuristic."""
    monkeypatch.setattr(calc_mod, "CACHE_MAX", 10)
    dc = DamageCalc()
    dc.enabled, dc.proc = True, _EchoNode()
    first = dc.batch([_calc("Earthquake")])
    assert first[0]["move"] == "Earthquake"
    for k in range(8):                                                    # 9 entries: one more batch overflows
        dc.cache[f"dummy{k}"] = {"rolls": [1]}
    batch = [_calc("Earthquake"), _calc("Rock Slide"), _calc("Iron Head"), _calc("Stomping Tantrum")]
    out = dc.batch(batch)
    assert [r and r["move"] for r in out] == ["Earthquake", "Rock Slide", "Iron Head", "Stomping Tantrum"]
    assert dc.proc.sent == 1 + 3                                          # the cached Earthquake was not re-sent
    assert not any(k.startswith("dummy") for k in dc.cache)               # the cache was cleared ...
    assert len(dc.cache) == 3                                             # ... before this batch went in
    again = dc.batch(batch)                                               # and the next decision reuses it
    assert [r and r["move"] for r in again] == ["Earthquake", "Rock Slide", "Iron Head", "Stomping Tantrum"]
    assert dc.proc.sent == 4 + 1                                          # only the evicted Earthquake re-sent


def test_errors_and_duplicates_in_a_batch_keep_their_positions(monkeypatch):
    dc = DamageCalc()
    dc.enabled, dc.proc = True, _EchoNode()
    orig = dc.proc.write

    def write(s):
        orig(s)
        msg = json.loads(dc.proc.pending[-1])
        msg["results"][1] = {"error": "bad move"}
        dc.proc.pending[-1] = json.dumps(msg) + "\n"

    dc.proc.write = write
    out = dc.batch([_calc("Earthquake"), _calc("Nonsense"), _calc("Earthquake")])
    assert out[0]["move"] == "Earthquake" and out[1] is None and out[2]["move"] == "Earthquake"


# ---------------------------------------------------------------- items the view shows as gone
def test_view_item_keeps_hidden_items_and_drops_gone_ones():
    assert view_item({"item": "unknown_item"}, GARCHOMP) == "Focus Sash"         # foe item hidden: the card's
    assert view_item({}, GARCHOMP) == "Focus Sash"                               # no item key at all: the card's
    assert view_item({"item": None}, GARCHOMP) is None                           # foe slot cleared: Sash used
    assert view_item({"item": ""}, GARCHOMP) is None                             # our slot cleared
    assert view_item({"item": "lifeorb"}, GARCHOMP) == "lifeorb"                  # revealed item wins
    assert view_item({"item": None}, BOOSTER) == "Booster Energy"                # spent, but its boost stays
    assert view_item(None, None) is None


def test_lookahead_view_agrees_with_the_calc_on_a_used_sash():
    used = {"species": "garchomp", "item": None, "current_hp_fraction": 1.0}
    hidden = {"species": "garchomp", "item": "unknown_item", "current_hp_fraction": 1.0}
    assert _norm_view(used, GARCHOMP)["item"] is None and "item" not in calc_pokemon(used, GARCHOMP)
    assert dex.to_id(_norm_view(hidden, GARCHOMP)["item"]) == "focussash"
    assert calc_pokemon(hidden, GARCHOMP)["item"] == "Focus Sash"
    assert _norm_view({"species": "garchomp", "status": "BRN"}, GARCHOMP)["status"] == "brn"


# ---------------------------------------------------------------- the lookahead on platform-shaped statuses
def _opts(card, slot):
    ally = -2 if slot == 0 else -1
    out = []
    for mv in card["moves"]:
        mid = dex.to_id(mv)
        t = dex.move(mid).get("target")
        out.append({"type": "move", "move_id": mid,
                    "targets": [ally, 1, 2] if t in ("normal", "any") else [1, 2] if t == "adjacentFoe" else [0]})
    return out


def _mon(card, hp=1.0, foe=False, **kw):
    v = {"species": sid(card), "name": card["species"], "current_hp_fraction": hp, "current_hp": int(100 * hp),
         "max_hp": 100, "status": None, "fainted": False, "boosts": {}, **kw}
    if foe:
        v.update(item="unknown_item", ability=None, moves=[])
    return v


@needs_calc
def test_exact_table_and_lookahead_see_a_platform_burn():
    """Our burned Garchomp ("BRN", as the platform sends it): the exact table's Earthquake rolls are halved and the
    joint lookahead still runs on that table and answers legally."""
    def decide(status):
        pol = PokemonPolicy()
        for c in (GARCHOMP, ARCHA):
            pol.cards[sid(c)] = c
        for c in (KINGAMBIT, AMOO):
            pol.opp_cards[sid(c)] = c
        pol.prev_active = [sid(GARCHOMP), sid(ARCHA)]
        pol.opp_prev_active = [sid(KINGAMBIT), sid(AMOO)]
        slots = [{"slot": 0, "options": _opts(GARCHOMP, 0)}, {"slot": 1, "options": _opts(ARCHA, 1)}]
        obs = {"is_doubles": True, "turn": 3, "force_switch": [False, False],
               "active_pokemon": [_mon(GARCHOMP, status=status), _mon(ARCHA)],
               "opponent_active_pokemon": [_mon(KINGAMBIT, foe=True), _mon(AMOO, foe=True)],
               "weather": {}, "fields": {}, "side_conditions": {}, "opponent_side_conditions": {}}
        st = {"phase": "moving", "state_version": 3, "session_id": "s", "game_type": "pokemon_vgc_doubles_draft",
              "observation": obs, "legal_actions": {"actions": [{"action_id": "doubles_turn", "input": {
                  "action": {"type": "doubles_turn", "slots": slots}}}]}}
        tmpl = st["legal_actions"]["actions"][0]["input"]["action"]
        table = pol._exact_table(obs, tmpl, obs["active_pokemon"], obs["opponent_active_pokemon"])
        a = pol.act(st)["action"]
        assert doubles_ok(st, a) and pol.last_exact
        return table, pol.last_lookahead

    healthy, _ = decide(None)
    burned, la = decide("BRN")
    k = ("me", 0, "earthquake", "opp", 0)
    assert max(burned[k]["rolls"]) <= max(healthy[k]["rolls"]) / 2 + 1
    assert la is not None and la["rows"] > 0 and la["cols"] > 0
