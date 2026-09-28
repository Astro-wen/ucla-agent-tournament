"""Regression tests for the semantic conflicts resolved while merging v3 (lookahead + calc-fixes + speed-protect
+ preview-matrix + draft). Calc-backed tests skip without agent/js/node_modules."""
import pytest

from agent.agent import doubles_ok
from agent.pokemon import dex
from agent.pokemon import policy as P
from agent.pokemon.calc import _js_dir
from agent.pokemon.policy import PokemonPolicy, _sashed

needs_calc = pytest.mark.skipif(_js_dir() is None, reason="agent/js has no node_modules")

GARCHOMP = {"species": "Garchomp", "item": "Focus Sash", "ability": "Rough Skin", "nature": "Jolly", "level": 50,
            "evs": {"hp": 4, "atk": 252, "spe": 252}, "ivs": {}, "moves": ["Earthquake", "Rock Slide", "Iron Head", "Protect"]}
GHOLD = {"species": "Gholdengo", "item": "Air Balloon", "ability": "Good as Gold", "nature": "Timid", "level": 50,
         "evs": {"spa": 252, "spd": 4, "spe": 252}, "ivs": {}, "moves": ["Make It Rain", "Shadow Ball", "Nasty Plot", "Protect"]}
ARCHA = {"species": "Archaludon", "item": "Leftovers", "ability": "Stamina", "nature": "Bold", "level": 50,
         "evs": {"hp": 252, "def": 252, "spa": 4}, "ivs": {}, "moves": ["Body Press", "Draco Meteor", "Flash Cannon", "Protect"]}
AMOO = {"species": "Amoonguss", "item": "Rocky Helmet", "ability": "Regenerator", "nature": "Calm", "level": 50,
        "evs": {"hp": 252, "def": 100, "spd": 156}, "ivs": {}, "moves": ["Spore", "Rage Powder", "Pollen Puff", "Protect"]}
KINGAMBIT = {"species": "Kingambit", "item": "Black Glasses", "ability": "Supreme Overlord", "nature": "Adamant", "level": 50,
             "evs": {"hp": 252, "atk": 252, "spe": 4}, "ivs": {}, "moves": ["Sucker Punch", "Iron Head", "Kowtow Cleave", "Protect"]}
INCIN = {"species": "Incineroar", "item": "Safety Goggles", "ability": "Intimidate", "nature": "Careful", "level": 50,
         "evs": {"hp": 252, "atk": 4, "spd": 252}, "ivs": {}, "moves": ["Fake Out", "Flare Blitz", "Knock Off", "Parting Shot"]}


def sid(card):
    return dex.to_id(card["species"])


def mon(card, hp=1.0, foe=False, **kw):
    v = {"species": sid(card), "name": card["species"], "current_hp_fraction": hp, "current_hp": int(100 * hp),
         "max_hp": 100, "status": None, "fainted": False, "boosts": {}, **kw}
    if foe:
        v.update(item="unknown_item", ability=None, moves=[])
    return v


def opts(card, slot):
    ally = -2 if slot == 0 else -1
    out = []
    for mv in card["moves"]:
        mid = dex.to_id(mv)
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


def policy(cards, opp_cards):
    pol = PokemonPolicy()
    for c in cards:
        pol.cards[sid(c)] = c
    for c in opp_cards:
        pol.opp_cards[sid(c)] = c
    return pol


# ---------------------------------------------------------------- step 2: lookahead x speed-protect
def test_a_foes_earthquake_into_its_own_partner_is_not_a_threat_to_us():
    """The lookahead's exact table also holds ("opp", j, move, "opp", 1 - j) (a foe's Earthquake hitting its own
    partner). speed-protect's threat loop keyed only on k[4], so that entry became a sure KO of OUR slot 1 - j."""
    pol = policy([GHOLD, ARCHA], [GARCHOMP, AMOO])
    actives = [mon(GHOLD), mon(ARCHA)]
    opps = [mon(GARCHOMP, foe=True), mon(AMOO, foe=True)]
    table = {("opp", 0, "earthquake", "opp", 1): {"rolls": [999] * 16, "defender_hp": 220}}
    sm = pol._speed_model({"turn": 3}, actives, opps, table)
    assert sm["threat"] == {}
    table[("opp", 0, "earthquake", "me", 1)] = {"rolls": [999] * 16, "defender_hp": 200}
    sm = pol._speed_model({"turn": 3}, actives, opps, table)
    assert set(sm["threat"]) == {(0, 1)} and sm["threat"][(0, 1)][0] == pytest.approx(1.0)


def test_one_protect_move_set_serves_the_lookahead_and_speed_protect():
    from agent.pokemon import lookahead as L
    assert P.PROTECT_MOVES is L.PROTECT_MOVES
    assert {"burningbulwark", "kingsshield", "obstruct"} <= P.PROTECT_MOVES <= P.STALL_MOVES


def test_a_used_sash_is_gone_for_speed_protect_too():
    """speed-protect's _sashed used to bring the card's Sash back when the view shows an empty slot (calc-fixes
    stopped doing that in calc.py): both now read calc.view_item."""
    assert _sashed({"species": "garchomp", "item": "unknown_item", "current_hp_fraction": 1.0}, GARCHOMP)
    assert not _sashed({"species": "garchomp", "item": None, "current_hp_fraction": 1.0}, GARCHOMP)
    assert not _sashed({"species": "garchomp", "item": "", "current_hp_fraction": 1.0}, GARCHOMP)


@needs_calc
def test_exact_pick_is_the_argmax_of_the_shared_pair_value():
    """The pair scorer the lookahead reuses (`_pair_value`) is speed-protect's joint scoring: the exact pick is its
    argmax (tie-break included) and a Protect pair carries `_protect_value`."""
    pol = policy([GHOLD, ARCHA], [INCIN, KINGAMBIT])
    slots = [{"slot": 0, "options": opts(GHOLD, 0)}, {"slot": 1, "options": opts(ARCHA, 1)}]
    st = state(slots, [mon(GHOLD), mon(ARCHA, hp=0.8)], [None, mon(KINGAMBIT, foe=True)], turn=5)
    obs = st["observation"]
    tmpl = st["legal_actions"]["actions"][0]["input"]["action"]
    table = pol._exact_table(obs, tmpl, obs["active_pokemon"], obs["opponent_active_pokemon"])
    ctx = pol._exact_context(obs, slots, obs["active_pokemon"], obs["opponent_active_pokemon"], 5, table)
    assert ctx["sm"] is not None and pol.last_speed is ctx["sm"]
    pick = pol._doubles_exact(ctx)
    (_, c0s), (_, c1s) = ctx["per_slot"]
    best = max(((c0, c1) for c0 in c0s for c1 in c1s if pol._pair_value(ctx, c0, c1) is not None),
               key=lambda p: pol._pair_value(ctx, *p))
    assert pick == {"slot_0": best[0]["cand"], "slot_1": best[1]["cand"]}
    prot = next(c for c in c0s if c.get("kind") == "protect")
    atk = next(c for c in c1s if c["cand"].get("target") == 2 and c.get("mid") == "bodypress")
    raw = pol._pair_value(ctx, prot, atk, tie_break=False)
    without = P._p_act(atk, prot) * atk["solo"] + P._p_act(prot, atk) * prot["solo"]
    assert raw - without == pytest.approx(
        pol._protect_value(prot, atk, ctx["sm"], obs, 5) + P._p_act(atk, prot) * atk.get("ally_pen", 0.0)
        + sum((P.REMOVE_BASE + P.REMOVE_DANGER * ctx["danger"].get(j, 0.5))
              * min(1.0, 1 - (1 - P._p_act(atk, prot) * atk["kob"].get(j, 0.0))) for j in atk["kob"]), abs=1e-6)


@needs_calc
def test_lookahead_foe_memory_skips_forced_switch_states():
    """speed-protect's fix #3 applies to the lookahead's own foe memory (opp_prev_active) too: a foe first seen in our
    forced-switch state (it came in mid-turn) is still fresh - Fake Out works - on the next turn."""
    fs = [{"slot": 0, "options": [{"type": "pass"}]},
          {"slot": 1, "options": [{"type": "switch", "species": "garchomp"}]}]
    pol = policy([GHOLD, ARCHA, GARCHOMP], [KINGAMBIT, AMOO, INCIN])
    pol.prev_active, pol.prev_opp = ["gholdengo", "archaludon"], ["kingambit", "amoonguss"]
    pol.opp_prev_active = ["kingambit", "amoonguss"]
    forced = state(fs, [mon(GHOLD), None], [mon(INCIN, foe=True), mon(AMOO, foe=True)], turn=5,
                   force_switch=[False, True])
    a = pol.act(forced)["action"]
    assert doubles_ok(forced, a)
    assert pol.opp_prev_active == ["kingambit", "amoonguss"] and pol.prev_opp == ["kingambit", "amoonguss"]
    slots = [{"slot": 0, "options": opts(GHOLD, 0)}, {"slot": 1, "options": opts(GARCHOMP, 1)}]
    turn6 = state(slots, [mon(GHOLD), mon(GARCHOMP)], [mon(INCIN, foe=True), mon(AMOO, foe=True)], turn=6)
    assert doubles_ok(turn6, pol.act(turn6)["action"])
    assert pol.opp_prev_active == ["incineroar", "amoonguss"]          # a normal state is remembered


# ---------------------------------------------------------------- step 3: calc-fixes x preview-matrix
def _dirty(pol):
    pol._active_sets = {"me": {"kingambit"}, "opp": {"garchomp"}}
    pol._entry_fallen = {("me", "kingambit"): 3}
    pol.prev_active, pol.prev_opp, pol.opp_prev_active = ["kingambit", "amoonguss"], ["garchomp", None], ["garchomp", None]
    pol.last_protect, pol.last_move = {"amoonguss": 7}, {1: ("amoonguss", "protect")}
    pol.cond_set = {"opp_tailwind": 6}


def _preview_obs():
    mine = [GHOLD, ARCHA, GARCHOMP, AMOO, KINGAMBIT, INCIN]
    return {"your_roster": [{"species": sid(c), "types": dex.types_of(sid(c))} for c in mine],
            "opponent_roster": [{"species": sid(c), "types": dex.types_of(sid(c))} for c in mine]}


def _clean(pol):
    return (not pol._active_sets and not pol._entry_fallen and pol.prev_active == [None, None]
            and pol.prev_opp == [None, None] and pol.opp_prev_active == [None, None] and not pol.last_protect
            and not pol.last_move and not pol.cond_set)


def test_team_preview_resets_battle_memory_when_the_matrix_game_answers(monkeypatch):
    """calc-fixes cleared its Supreme Overlord entry counts at the top of the old `_preview`; preview-matrix renamed
    that body to `_preview_heuristic`, which only runs when the matrix game fails -- so on most previews the counts
    leaked into the next battle. The reset now sits in the `_preview` wrapper (with the other per-battle trackers)."""
    canned = {"action": {"type": "select_lineup", "bring": ["gholdengo", "archaludon", "garchomp", "amoonguss"],
                         "leads": ["gholdengo", "archaludon"]}, "reasoning_summary": "preview matrix game: canned"}
    monkeypatch.setattr(PokemonPolicy, "_preview_matrix", lambda self, obs, session_id: canned)
    pol = PokemonPolicy()
    _dirty(pol)
    assert pol._preview(_preview_obs(), "s") is canned
    assert _clean(pol)


def test_team_preview_resets_battle_memory_on_the_heuristic_path_too(monkeypatch):
    monkeypatch.setattr(PokemonPolicy, "_preview_matrix", lambda self, obs, session_id: None)
    pol = PokemonPolicy()
    _dirty(pol)
    out = pol.act({"phase": "team_preview", "session_id": "s", "observation": _preview_obs()})
    assert out["action"]["type"] == "select_lineup" and "matrix" not in out["reasoning_summary"]
    assert _clean(pol)
    # the heuristic body itself no longer touches battle memory (the lineup guess runs it on a scratch policy)
    _dirty(pol)
    pol._preview_heuristic(_preview_obs())
    assert pol._entry_fallen == {("me", "kingambit"): 3}


# ---------------------------------------------------------------- step 4: calc-fixes x draft
def test_threat_table_mean_reads_calc_fixes_multi_hit_totals():
    """calc-fixes' calc.js returns 32 equally likely totals for a multi-hit move (marked `hits`); the Drafter's
    threat table divided their sum by 16, doubling every multi-hit move's damage."""
    from agent.pokemon.matchup import _hits, _mean_damage
    totals = [60 + i for i in range(32)]
    assert _mean_damage(totals, "surgingstrikes", {"hits": 3}) == pytest.approx(sum(totals) / 32)
    assert _mean_damage(totals, "iciclespear", {"hits": "2/3/4/5"}) == pytest.approx(sum(totals) / 32)
    assert _mean_damage([16 * 30, 16 * 31, 16 * 29], "surgingstrikes") == pytest.approx(90)   # main's output
    baxcal = {"species": "Baxcalibur", "item": "Loaded Dice", "ability": "Thermal Exchange"}
    assert _hits(baxcal, "iciclespear") is None                     # calc.js mixes Loaded Dice's 4/5 itself
    assert _hits({"species": "Cinccino", "ability": "Skill Link"}, "tailslap") == 5


@needs_calc
def test_threat_table_multi_hit_damage_is_hits_times_one_hit():
    from agent.pokemon.calc import DamageCalc, calc_pokemon
    from agent.pokemon.matchup import _mean_damage
    urshifu = {"species": "Urshifu-Rapid-Strike", "item": "Mystic Water", "ability": "Unseen Fist", "nature": "Jolly",
               "level": 50, "evs": {"hp": 4, "atk": 252, "spe": 252}, "ivs": {}, "moves": ["Surging Strikes"]}
    q = {"attacker": calc_pokemon(None, urshifu), "defender": calc_pokemon(None, ARCHA), "move": "Surging Strikes",
         "field": {"gameType": "Doubles"}}
    (three,) = DamageCalc.get().batch([q])
    assert three.get("hits") == 3 and len(three["rolls"]) == 32
    m3 = _mean_damage(three["rolls"], "surgingstrikes", three)
    assert three["min"] <= m3 <= three["max"]                          # an expected TOTAL, inside the exact range
    assert m3 == pytest.approx((three["min"] + three["max"]) / 2, rel=0.05)   # (the convolution is ~symmetric)
    assert sum(three["rolls"]) / 16 > 1.9 * m3                         # what the old sum / 16 read: double


def test_draft_merges_preview_warm_up_pool_memory_and_drafter_summary(monkeypatch):
    """_draft keeps preview-matrix's calc warm-up, the Drafter's per-match pool memory and its `why` summary."""
    from agent.pokemon.calc import DamageCalc
    started = []
    monkeypatch.setattr(DamageCalc, "_start", lambda self: started.append(1) or True)
    monkeypatch.setattr(PokemonPolicy, "_drafter_pick", lambda self, obs, me, legal: "c2")
    cards = [{"card_id": f"c{k}", **c} for k, c in enumerate((GHOLD, ARCHA, GARCHOMP))]
    st = {"phase": "draft", "current_actor": {"agent_id": "me"},
          "observation": {"phase": "draft", "current_seat": "me", "available_cards": cards, "rosters": {}},
          "legal_actions": {"actions": [{"action_id": f"draft_pick:{c['card_id']}",
                                         "input": {"action": {"type": "draft_pick", "card_id": c["card_id"]}}}
                                        for c in cards]}}
    pol = PokemonPolicy()
    out = pol.act(st)
    assert started and set(pol.draft_pool) == {"c0", "c1", "c2"}
    assert out["action"] == {"type": "draft_pick", "card_id": "c2"} and "drafter" in out["reasoning_summary"]
