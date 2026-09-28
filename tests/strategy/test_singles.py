"""Singles game types (gen9same / gen9random / gen9ou_teambuild / gen9ou_draft): agent/pokemon/singles.py, the
teambuild fallbacks and the local singles env (local/singles_env.py)."""
import json
import shutil
from pathlib import Path

import pytest
from altruagent import DecisionContext, GameState, LegalAction

from agent.agent import Contestant
from agent.pokemon import singles as S
from agent.pokemon.calc import DamageCalc, _js_dir
from agent.pokemon.policy import ALT_OU_TEAM, DEFAULT_OU_TEAM, OU_TEAMS, PokemonPolicy
from local.showdown import JS_DIR

ROOT = Path(__file__).resolve().parents[2]
REAL_SAME = ROOT / "logs" / "yoriko" / "c8efbea9-7ef0-4b14-9843-9d3006c45101.jsonl"
needs_calc = pytest.mark.skipif(_js_dir() is None or shutil.which("node") is None,
                                reason="agent/js has no node_modules / no node")
needs_sim = pytest.mark.skipif(not (JS_DIR / "node_modules").exists() or shutil.which("node") is None,
                               reason="npm install not run in local/js")
needs_real = pytest.mark.skipif(not REAL_SAME.exists(), reason="recorded gen9same match not present")


@pytest.fixture(autouse=True)
def _no_public_reasoning(monkeypatch):
    """These tests look at the move itself; with the public line on, moves come back wrapped in WithReasoning (the
    simulator unwraps it like the runner: see test_singles_env_with_public_reasoning_on)."""
    monkeypatch.setenv("ARENA_REASONING", "off")


def real_states() -> list[dict]:
    return [json.loads(line)["raw"] for line in REAL_SAME.open()]


def ctx(game):
    return DecisionContext(session_id="sid", tournament_id=None, game_type=game, agent_id="me")


# ---------------------------------------------------------------- pure helpers
def test_mode_of_each_game_type():
    obs = {}
    assert S.mode_of({"game_type": "pokemon_gen9same"}, {"format": "gen9customgame"}) == "same"
    assert S.mode_of({"game_type": "pokemon_gen9random"}, obs) == "random"
    assert S.mode_of({}, {"format": "gen9randombattle"}) == "random"
    assert S.mode_of({"game_type": "pokemon_gen9ou_teambuild"}, obs) == "teambuild"
    assert S.mode_of({"game_type": "pokemon_gen9ou_draft"}, obs) == "draft"
    assert S.mode_of({}, {"format": "gen9ou"}) == "ou"


@pytest.mark.parametrize("game,mode", [("pokemon_gen9_same_team", "same"), ("pokemon_gen9_random", "random"),
                                       ("pokemon_gen9_ou_team_building", "teambuild"), ("pokemon_gen9_ou_draft", "draft")])
def test_other_spellings_of_the_game_types_still_map(game, mode):
    """The brief / platform UI also name them pokemon_gen9_same_team, ..._random, ..._ou_team_building."""
    assert S.mode_of({"game_type": game}, {}) == mode
    pol = PokemonPolicy()
    pol.game_type = game
    assert pol._singles_format({})


def test_levels_from_the_protocol_and_level_100_without_an_L():
    obs = {"team": {"p1: Hoopa": {}}, "protocol_log": [
        "|poke|p2|Garchomp, F|", "|switch|p1a: Hoopa|Hoopa-Unbound, L80|259/259",
        "|switch|p2a: Vaporeon|Vaporeon, L86, F|100/100"]}
    lv = S.log_levels(obs)
    assert lv[("me", "hoopaunbound")] == 80 and lv[("opp", "vaporeon")] == 86 and lv[("opp", "garchomp")] == 100


def test_cosmetic_formes_fall_back_to_a_species_the_calculator_knows():
    assert S.canon("sawsbucksummer") == "sawsbuck" and S.canon("minioryellow") == "minior"
    assert S.canon("rotomwash") == "rotomwash" and S.canon("garchomp") == "garchomp"
    assert S._cv({"species": "alcremierubycream", "types": ["FAIRY"]})["species"] == "alcremie"


def test_duel_is_in_hp_units_and_symmetric_enough():
    win = S.duel(1.0, 1.0, 0.6, 0.3, 0.0)                 # we 2HKO, they 4HKO: we win; slower, we take 2 hits
    assert abs(win - (1.0 + S.C_KO - 0.6)) < 1e-9
    assert abs(S.duel(1.0, 1.0, 0.6, 0.3, 1.0) - (1.0 + S.C_KO - 0.3)) < 1e-9     # faster: only one
    lose = S.duel(1.0, 1.0, 0.3, 0.6, 1.0)                # we move first twice, then fall
    assert abs(lose - (0.6 - 1.0 - S.C_KO)) < 1e-9
    assert S.duel(1.0, 1.0, 0.0, 0.0, 0.5) == 0.0 and S.duel(0.0, 1.0, 1.0, 1.0, 1.0) == 0.0


def test_race_counts_hits_and_speed_breaks_ties():
    assert S.race(1.0, 1.0, 0.6, 0.3, 0.0) > 0.5          # 2 hits vs 4 hits: we win even when slower
    assert S.race(1.0, 1.0, 0.5, 0.5, 1.0) > 0 > S.race(1.0, 1.0, 0.5, 0.5, 0.0)
    assert S.race(1.0, 1.0, 0.0, 0.3, 1.0) == -1.0 and S.race(1.0, 1.0, 0.3, 0.0, 0.0) == 1.0
    assert S.race(0.0, 1.0, 1.0, 0.0, 1.0) == -1.0


def test_foe_choice_lock_from_the_protocol():
    obs = {"team": {"p1: A": {}}, "protocol_log": ["|switch|p2a: Rotom|Rotom-Wash|100/100",
                                                   "|move|p2a: Rotom|Volt Switch|p1a: A",
                                                   "|move|p2a: Rotom|Hydro Pump|p1a: A"]}
    assert S.foe_last_move(obs) == "hydropump"
    obs["protocol_log"].append("|switch|p2a: Rotom|Rotom-Wash|100/100")
    assert S.foe_last_move(obs) is None


def test_same_team_foe_uses_our_exact_set():
    pol = PokemonPolicy()
    obs = {"team": {"p1: Garchomp": {"species": "garchomp", "item": "leftovers", "ability": "roughskin",
                                     "moves": ["earthquake", "dragonclaw", "swordsdance", "firefang"]}},
           "protocol_log": []}
    s, moves = S.foe_set(pol, "same", {"species": "garchomp", "item": "unknown_item", "ability": None, "moves": []},
                         {}, obs)
    assert s["item"] == "leftovers" and s["level"] == 100 and s["evs"]["atk"] == 252
    assert dict(moves) == {"earthquake": 1.0, "dragonclaw": 1.0, "swordsdance": 1.0, "firefang": 1.0}


def test_random_foe_movepool_is_probabilistic_and_revealed_moves_are_certain():
    pol = PokemonPolicy()
    s, moves = S.foe_set(pol, "random", {"species": "garchomp", "moves": ["earthquake"]}, {("opp", "garchomp"): 77},
                         {"team": {}})
    m = dict(moves)
    assert s["level"] == 77 and m["earthquake"] == 1.0
    assert 0 < max(p for k, p in m.items() if k != "earthquake") < 1.0


def test_hazards_stop_paying_once_maxed_out():
    val = lambda sc: S._hazard_value({"opponent_side_conditions": sc}, "spikes", 4)       # noqa: E731
    assert val({}) > val({"SPIKES": 1}) == val({"SPIKES": 2}) > 0
    assert val({"SPIKES": 3}) == 0.0
    tval = lambda sc: S._hazard_value({"opponent_side_conditions": sc}, "toxicspikes", 4)  # noqa: E731
    assert tval({"TOXIC_SPIKES": 1}) > 0 and tval({"TOXIC_SPIKES": 2}) == 0.0
    assert S._hazard_value({"opponent_side_conditions": {"STEALTH_ROCK": 7}}, "stealthrock", 4) == 0.0
    # our own Spikes layers raise the switch-in cost (poke-env: the value of a stackable condition = its layers)
    obs = lambda n: {"side_conditions": {"SPIKES": n}}                                    # noqa: E731
    mon = {"species": "garchomp", "types": ["DRAGON", "GROUND"]}
    assert S.entry_hazard(obs(1), mon, {}) < S.entry_hazard(obs(3), mon, {}) == 0.25


def test_env_side_conditions_carry_layers():
    from local.singles_env import _conditions
    assert _conditions({"side_conditions": ["spikes", "stealthrock"], "side_layers": {"spikes": 2, "stealthrock": 1}}) \
        == {"SPIKES": 2, "STEALTHROCK": 1}
    assert _conditions({"side_conditions": ["reflect"]}) == {"REFLECT": 1}


class _C:
    pass


def test_may_be_trapped_by_unrevealed_trapping_abilities():
    c = _C()
    c.mode, c.setA = "random", {}
    c.A = {"species": "heatran", "types": ["FIRE", "STEEL"], "ability": "flashfire", "item": "leftovers"}
    c.O, c.setO = {"species": "dugtrio", "types": ["GROUND"], "ability": None}, {}
    assert S.may_be_trapped(c)                                  # Arena Trap possible, Heatran grounded
    c.A = dict(c.A, item="airballoon")
    assert not S.may_be_trapped(c)
    c.A = {"species": "gengar", "types": ["GHOST", "POISON"], "ability": "cursedbody"}
    c.O = {"species": "gothitelle", "types": ["PSYCHIC"], "ability": None}
    assert not S.may_be_trapped(c)                              # Ghosts escape Shadow Tag
    c.A = {"species": "corviknight", "types": ["FLYING", "STEEL"], "ability": "pressure"}
    c.O = {"species": "magnezone", "types": ["ELECTRIC", "STEEL"], "ability": "sturdy"}
    assert not S.may_be_trapped(c)                              # revealed ability decides


def test_teambuild_switches_team_after_a_rejection():
    pol = PokemonPolicy()
    st = {"phase": "teambuild", "game_type": "pokemon_gen9ou_teambuild",
          "observation": {"phase": "teambuild", "submit_attempts_failed": 0}}
    assert pol.act(st)["action"]["team"] is DEFAULT_OU_TEAM
    st["observation"]["submit_attempts_failed"] = 1
    assert PokemonPolicy().act(st)["action"]["team"] is ALT_OU_TEAM
    assert pol.cards["greattusk"]["level"] == 100
    assert not {s["species"] for s in DEFAULT_OU_TEAM} & {s["species"] for s in ALT_OU_TEAM}


@needs_sim
def test_every_teambuild_team_passes_the_gen9ou_validator():
    from local.showdown import Showdown
    from local.singles_env import submitted_set
    with Showdown() as sd:
        for team in OU_TEAMS:
            assert sd.validate([submitted_set(s) for s in team], "gen9ou") == [None] * 6


def test_vgc_states_are_not_singles():
    pol = PokemonPolicy()
    pol.game_type = "pokemon_vgc_doubles_draft"
    assert not pol._singles_format({"battle_format": "gen9vgc2025regi"})
    pol.game_type = None
    assert not pol._singles_format({"battle_format": "gen9vgc2025regi"}) and not pol._singles_format({})
    pol.game_type = "pokemon_gen9same"
    assert pol._singles_format({"battle_format": "gen9customgame"})


def test_no_calc_forced_switch_takes_the_resist_not_the_first():
    from agent.pokemon.policy import _singles_switch_score
    obs = {"force_switch": True, "opponent_active_pokemon": {"species": "garchomp", "types": ["DRAGON", "GROUND"]},
           "available_switches": [{"species": "pikachu", "types": ["ELECTRIC"], "current_hp_fraction": 1.0},
                                  {"species": "clefable", "types": ["FAIRY"], "current_hp_fraction": 1.0}]}
    assert _singles_switch_score({"species": "clefable"}, obs) > _singles_switch_score({"species": "pikachu"}, obs)


# ---------------------------------------------------------------- the recorded real gen9same match
@needs_real
@needs_calc
def test_real_gen9same_decisions_are_legal_and_use_the_singles_policy():
    c = Contestant()
    n_singles = 0
    for raw in real_states():
        gs = GameState.from_mcp_state(raw)
        out = c.choose_action(gs, ctx(raw["game_type"]))
        if raw["phase"] == "team_preview":
            roster = set(raw["legal_actions"]["actions"][0]["input"]["action"]["roster"])
            assert out["type"] == "select_lineup" and len(set(out["bring"])) == 4 and set(out["bring"]) <= roster
            assert len(out["leads"]) == 2 and set(out["leads"]) <= set(out["bring"])
            continue
        assert isinstance(out, LegalAction) and out.action_id in {a["action_id"] for a in raw["legal_actions"]["actions"]}
        n_singles += c.pk.singles_why is not None
    assert n_singles >= 30


@needs_real
@needs_calc
def test_real_forced_switch_brings_a_garchomp_answer_not_pikachu():
    raw = next(r for r in real_states() if r["phase"] == "moving" and r["observation"].get("force_switch"))
    assert raw["observation"]["opponent_active_pokemon"]["species"] == "garchomp"
    out = Contestant().choose_action(GameState.from_mcp_state(raw), ctx(raw["game_type"]))
    sp = out.input["action"]["species"]
    assert sp in ("clefable", "rotomwash", "charizard"), sp


@needs_real
@needs_calc
def test_real_gen9same_decisions_are_fast():
    import time
    pol = PokemonPolicy()
    worst = 0.0
    for raw in real_states():
        t = time.perf_counter()
        pol.act(raw)
        worst = max(worst, time.perf_counter() - t)
    assert worst < 5.0


@needs_real
@needs_calc
def test_real_gen9same_damage_fits_the_assumed_sets():
    """The recorded match's damage (non-crit, unboosted hits) inside the calc range for level 100, 252 EVs in every
    stat, neutral nature (the EV-less gen9same sets as Showdown fills them) -- and the replay check stays legal."""
    from local.singles_replay import damage_check, protocol_hits, replay_decisions, singles_logs
    assert REAL_SAME.name in {Path(f).name for f in singles_logs(str(ROOT / "logs" / "*" / "*.jsonl"))}
    c = damage_check(str(REAL_SAME))
    assert c["mode"] == "same" and c["hits"] >= 20 and c["inside"] == c["hits"] and c["ko_consistent"] == c["ko_hits"]
    d = replay_decisions(str(REAL_SAME))
    assert d["illegal"] == 0 and d["decisions"] == 36 and d["singles_policy"] >= 30
    hits = protocol_hits(["|switch|p1a: A|Garchomp|420/420", "|switch|p2a: B|Garchomp|420/420",
                          "|move|p1a: A|Dragon Claw|p2a: B", "|-crit|p2a: B", "|-damage|p2a: B|100/420",
                          "|move|p2a: B|Dragon Claw|p1a: A", "|-damage|p1a: A|190/420", "|turn|2"])
    assert [(h["move"], h["damage"], h["hp_before"]) for h in hits] == [("Dragon Claw", 230, (420, 420))]


@needs_real
def test_without_the_calculator_singles_still_answers_legally(monkeypatch):
    monkeypatch.setattr(DamageCalc.get(), "enabled", False)
    c = Contestant()
    for raw in real_states():
        out = c.choose_action(GameState.from_mcp_state(raw), ctx(raw["game_type"]))
        if raw["phase"] == "moving":
            assert out.action_id in {a["action_id"] for a in raw["legal_actions"]["actions"]}


# ---------------------------------------------------------------- local singles env
@needs_sim
@needs_calc
@pytest.mark.parametrize("fmt", ["same", "random", "teambuild", "draft"])
def test_singles_env_every_format_completes_with_zero_invalid(fmt):
    from agent.agent import create_agent
    from local.showdown import Showdown
    from local.singles_env import ContestantPlayer, OfficialBaseline, play_singles
    with Showdown() as sd:
        for seed in (1, 2):
            r = play_singles(sd, fmt, ContestantPlayer(create_agent), OfficialBaseline(), seed=seed)
            assert r.winner in (0, 1, None) and r.turns >= 1
            assert r.invalid[0] == 0 and r.decisions[0] > 0
            assert len(r.teams[0]) == 6 and len(r.teams[1]) == 6


@needs_sim
def test_singles_env_with_public_reasoning_on(monkeypatch):
    """With the default public line, every move is a WithReasoning the runner accepts; the game still has 0 invalid."""
    monkeypatch.setenv("ARENA_REASONING", "safe")
    from agent.agent import create_agent
    from local.showdown import Showdown
    from local.singles_env import ContestantPlayer, OfficialBaseline, play_singles
    with Showdown() as sd:
        r = play_singles(sd, "same", ContestantPlayer(create_agent), OfficialBaseline(), seed=3)
        assert r.invalid[0] == 0 and r.decisions[0] > 0


# ---------------------------------------------------------------- review fixes
def test_first_turn_on_the_field_after_turn_1_a_switch_a_faint_or_a_pivot():
    """`fresh` (Fake Out / First Impression work; switching straight back out is a loop) is True on a Pokémon's first
    turn out. It used to remember the incoming Pokémon of a switch / forced switch, so only turn-1 leads were fresh."""
    pol = PokemonPolicy()
    c = _C()
    c.force, c.A = False, {"species": "Ambipom"}
    assert S.is_fresh(pol, "ambipom")                                      # turn 1: the lead
    S.remember(pol, c, {"type": "move", "move_id": "fakeout"})
    assert not S.is_fresh(pol, "ambipom")                                  # stayed in: Fake Out fails now
    S.remember(pol, c, {"type": "switch", "species": "garchomp"})          # we switch Ambipom out
    assert S.is_fresh(pol, "garchomp") and not S.is_fresh(pol, "ambipom")  # Garchomp's first turn
    c.A = {"species": "Garchomp"}
    S.remember(pol, c, {"type": "move", "move_id": "earthquake"})
    assert not S.is_fresh(pol, "garchomp")
    c.force, c.A = True, {"species": "Garchomp", "fainted": True}          # Garchomp faints: forced switch
    S.remember(pol, c, {"type": "switch", "species": "ambipom"})
    assert S.is_fresh(pol, "ambipom")                                      # the replacement's first turn is next
    c.force, c.A = False, {"species": "Ambipom"}
    S.remember(pol, c, {"type": "move", "move_id": "protect"})
    assert pol.singles_prev["protect"] == "ambipom"
    c.force = True                                                         # U-turn: mid-turn forced switch
    S.remember(pol, c, {"type": "switch", "species": "clefable"})
    assert S.is_fresh(pol, "clefable") and pol.singles_prev["protect"] is None
    # a forme change mid-battle is the same Pokémon (seen in a local game: Disguise broke, Mimikyu counted as new)
    c.force, c.A = False, {"species": "Mimikyu"}
    S.remember(pol, c, {"type": "move", "move_id": "playrough"})
    assert not S.is_fresh(pol, "mimikyubusted")
    c.A = {"species": "Aegislash-Blade"}
    S.remember(pol, c, {"type": "move", "move_id": "kingsshield"})
    assert S.protected_last(pol, "aegislash") and not S.is_fresh(pol, "aegislash")


def _truly_fresh(obs: dict) -> bool:
    """From the protocol: our active switched in (or was dragged in) after the previous turn started."""
    mine = S.my_side_id(obs)
    log = [x for x in obs.get("protocol_log") or [] if isinstance(x, str)]
    turns = [i for i, x in enumerate(log) if x.startswith("|turn|")]
    if len(turns) < 2:
        return True
    ins = [i for i, x in enumerate(log) if x.startswith(("|switch|", "|drag|")) and x.split("|")[2][:2] == mine]
    return bool(ins) and ins[-1] > turns[-2]


def _record_fresh(monkeypatch) -> list:
    rows = []
    orig = S.evaluate

    def wrapped(pol, c):
        rows.append((_truly_fresh(c.obs), S.is_fresh(pol, S.to_id((c.A or {}).get("species", "")))))
        return orig(pol, c)
    monkeypatch.setattr(S, "evaluate", wrapped)
    return rows


@needs_real
@needs_calc
def test_real_gen9same_fresh_matches_the_protocol(monkeypatch):
    """Replaying the recorded states (whatever we would have chosen) our `fresh` is exactly the protocol's truth,
    including Pokémon on their first turn after a forced switch (the old bookkeeping marked only turn 1)."""
    rows = _record_fresh(monkeypatch)
    pol = PokemonPolicy()
    for raw in real_states():
        pol.act(raw)
    assert len(rows) >= 20 and all(t == f for t, f in rows), [r for r in rows if r[0] != r[1]]
    assert sum(t for t, _ in rows) >= 2                                  # turn 1 and at least one switch-in


@needs_sim
@needs_calc
def test_local_random_games_fresh_matches_the_protocol(monkeypatch):
    from agent.agent import create_agent
    from local.showdown import Showdown
    from local.singles_env import ContestantPlayer, RandomPlayer, play_singles
    rows = _record_fresh(monkeypatch)
    with Showdown() as sd:
        for seed in (777000, 777001):
            play_singles(sd, "random", ContestantPlayer(create_agent), RandomPlayer(), seed=seed)
    assert rows and all(t == f for t, f in rows), [r for r in rows if r[0] != r[1]]
    assert sum(t for t, _ in rows) > 2                                   # more than the two turn-1 leads


def _ou_card(species, item, ability, nature, evs, moves):
    return {"card_id": "ou-" + S.to_id(species), "species": species, "item": item, "ability": ability,
            "nature": nature, "level": 100, "evs": evs, "ivs": {}, "moves": moves}


GLISCOR = _ou_card("Gliscor", "Toxic Orb", "Poison Heal", "Impish", {"hp": 244, "def": 248, "spe": 16},
                   ["Earthquake", "Knock Off", "Toxic", "Protect"])
CLEFABLE = _ou_card("Clefable", "Leftovers", "Magic Guard", "Bold", {"hp": 252, "def": 252, "spd": 4},
                    ["Moonblast", "Soft-Boiled", "Calm Mind", "Flamethrower"])
GARCHOMP = _ou_card("Garchomp", "Rocky Helmet", "Rough Skin", "Jolly", {"atk": 252, "spd": 4, "spe": 252},
                    ["Earthquake", "Dragon Tail", "Stealth Rock", "Spikes"])


def test_singles_threat_table_formula_has_no_spread_penalty():
    """Calculator-free fallback: in singles Earthquake is not cut to 0.75x (the VGC table keeps the cut)."""
    from agent.pokemon.matchup import ThreatTable
    g50, c50 = dict(GLISCOR, level=50), dict(CLEFABLE, level=50)
    fs, fd = ThreatTable("Singles")._formula(g50, c50), ThreatTable()._formula(g50, c50)
    assert fs["earthquake"] == pytest.approx(fd["earthquake"] / 0.75) and fs["knockoff"] == pytest.approx(fd["knockoff"])
    # level-100 cards: the singles formula uses their level (as their stats do), the VGC one keeps 50
    assert ThreatTable("Singles")._formula(GLISCOR, CLEFABLE)["knockoff"] > \
        1.5 * ThreatTable()._formula(GLISCOR, CLEFABLE)["knockoff"]


@needs_calc
def test_singles_threat_table_runs_the_calculator_on_a_singles_field():
    """The OU draft's counters: Earthquake (hits all adjacent foes) at full power, single-target moves unchanged."""
    from agent.pokemon.matchup import ThreatTable
    ts, td = ThreatTable("Singles"), ThreatTable()
    ts.ensure([GLISCOR, CLEFABLE])
    td.ensure([GLISCOR, CLEFABLE])
    g, c = GLISCOR["card_id"], CLEFABLE["card_id"]
    assert ts.calc_used and ts.best[(g, c)] == "earthquake" == td.best[(g, c)]
    assert ts.damage(g, c) == pytest.approx(td.damage(g, c) / 0.75, rel=0.02)
    assert ts.damage(c, g) == pytest.approx(td.damage(c, g), rel=1e-9)   # Moonblast: single target either way
    assert not ts.spread[(g, c)] and td.spread[(g, c)]


def test_singles_draft_uses_the_singles_threat_table(monkeypatch):
    from agent.pokemon import drafter
    from agent.pokemon.drafter import DraftModel, Drafter
    used = []

    class FakeTT:
        def __init__(self, kind):
            self.kind = kind

        def ensure(self, cards):
            used.append(self.kind)

        def beats(self, a, b):
            return 0.5
    monkeypatch.setattr(drafter, "threat_table", lambda: FakeTT("doubles"))
    monkeypatch.setattr(drafter, "singles_threat_table", lambda: FakeTT("singles"))
    cards = [GLISCOR, CLEFABLE, GARCHOMP]
    Drafter(DraftModel(S.SINGLES_DRAFT_MODEL), prior_score=S.card_value, singles=True)._tables(cards)
    Drafter(DraftModel(S.SINGLES_DRAFT_MODEL), prior_score=S.card_value)._tables(cards)
    assert used == ["singles", "doubles"]
    # the policy's OU draft builds its Drafter with singles=True; the VGC draft does not
    seen = []
    monkeypatch.setattr(Drafter, "pick", lambda self, pool, mine, opp, legal, n_taken=None:
                        seen.append(self.singles) or sorted(legal)[0])
    for game in ("pokemon_gen9ou_draft", "pokemon_vgc_doubles_draft"):
        st = {"phase": "draft", "game_type": game, "current_actor": {"agent_id": "me"},
              "observation": {"phase": "draft", "current_seat": "me", "available_cards": [dict(x) for x in cards],
                              "rosters": {"me": [], "o": []}, "pick_number": 1},
              "legal_actions": {"actions": [{"action_id": f"draft_pick:{x['card_id']}",
                                             "input": {"action": {"card_id": x["card_id"]}}} for x in cards]}}
        assert PokemonPolicy().act(st)["action"]["type"] == "draft_pick"
    assert seen == [True, False]


def test_singles_preview_state_without_battle_format_skips_the_doubles_model():
    pol = PokemonPolicy()
    roster = [{"species": s} for s in ("Pikachu", "Charizard", "Garchomp", "Clefable", "Ferrothorn", "Rotom-Wash")]
    obs = {"your_roster": roster, "opponent_roster": [dict(p) for p in roster]}      # no battle_format
    pol.game_type = "pokemon_gen9same"
    assert pol._preview_matrix(obs, "sid") is None
    pol.game_type = "pokemon_vgc_doubles_draft"                          # VGC: unchanged (format-only test)
    assert pol._singles_format(obs) is False


@needs_real
@needs_calc
def test_real_gen9same_preview_is_the_singles_one_with_or_without_battle_format():
    import copy
    raw = next(r for r in real_states() if r["phase"] == "team_preview")
    assert raw["observation"].get("battle_format")
    with_fmt = PokemonPolicy().act(copy.deepcopy(raw))
    bare = copy.deepcopy(raw)
    bare["observation"].pop("battle_format")
    without = PokemonPolicy().act(bare)
    assert without["action"] == with_fmt["action"] and "best average matchup" in without["reasoning_summary"]


def test_env_reveals_items_abilities_and_moves_from_the_protocol():
    from local.singles_env import Reveals
    r = Reveals()
    for line in ["|switch|p2a: Rotom|Rotom-Wash|100/100", "|move|p2a: Rotom|Volt Switch|p1a: X",
                 "|-heal|p2a: Rotom|60/100|[from] item: Leftovers",
                 "|-damage|p1a: X|50/100|[from] ability: Rough Skin|[of] p2a: Chomp",
                 "|-enditem|p2a: Sash|Focus Sash"]:
        r.feed(line)
    assert r.side["p2"]["Rotom"] == {"moves": ["voltswitch"], "item": "leftovers"}
    assert r.side["p2"]["Chomp"]["ability"] == "roughskin" and r.side["p2"]["Sash"]["item"] is None
