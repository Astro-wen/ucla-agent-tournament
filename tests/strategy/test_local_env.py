"""Tests for the local environments (local/). Showdown-backed tests skip if node_modules is missing."""
import random

import pytest

from agent.werewolf.state import parse
from local import pokemon_env as pe
from local import werewolf_env as we
from local.showdown import JS_DIR

# ---------------------------------------------------------------- pokemon (pure python parts)

OPP = [{"species": "kyogre", "fainted": False}, {"species": "amoonguss", "fainted": False}]


def test_target_mapping_follows_platform_legend():
    assert pe._targets("normal", 0, OPP) == [1, 2, -2]          # foes, then the ally (slot 1 = -2)
    assert pe._targets("normal", 1, OPP) == [1, 2, -1]
    assert pe._targets("adjacentFoe", 0, OPP) == [1, 2]
    assert pe._targets("adjacentAlly", 0, OPP) == [-2]
    assert pe._targets("allAdjacentFoes", 0, OPP) == [0]
    assert pe._targets("self", 0, OPP) == [0]
    dead_b = [OPP[0], {"species": "amoonguss", "fainted": True}]
    assert pe._targets("normal", 0, dead_b) == [1, -2]          # never offer a fainted foe slot


def tmpl():
    return {"slots": [
        {"slot": 0, "options": [{"type": "move", "move_id": "fakeout", "targets": [1, 2, -2], "_k": 0},
                                {"type": "move", "move_id": "electroshot", "targets": [0], "_k": 1},   # locked move
                                {"type": "switch", "species": "gholdengo", "_j": 2}]},
        {"slot": 1, "options": [{"type": "move", "move_id": "protect", "targets": [0], "_k": 3},
                                {"type": "switch", "species": "gholdengo", "_j": 2},
                                {"type": "pass"}]}]}


def test_to_choice_builds_showdown_strings():
    t = tmpl()
    out = pe._to_choice({"type": "doubles_turn", "slot_0": {"type": "move", "move_id": "fakeout", "target": 2},
                         "slot_1": {"type": "move", "move_id": "protect", "target": 0}}, {}, t)
    assert out == "move 1 2, move 4"
    out = pe._to_choice({"type": "doubles_turn", "slot_0": {"type": "move", "move_id": "electroshot", "target": 0},
                         "slot_1": {"type": "switch", "species": "Gholdengo"}}, {}, t)
    assert out == "move 2, switch 3"


def test_to_choice_rejects_illegal():
    t = tmpl()
    bad = [
        {"type": "doubles_turn", "slot_0": {"type": "move", "move_id": "electroshot", "target": 1}, "slot_1": {"type": "pass"}},
        {"type": "doubles_turn", "slot_0": {"type": "switch", "species": "gholdengo"}, "slot_1": {"type": "switch", "species": "gholdengo"}},
        {"type": "doubles_turn", "slot_0": {"type": "pass"}, "slot_1": {"type": "pass"}},
        {"type": "select_lineup"},
    ]
    for b in bad:
        assert pe._to_choice(b, {}, t) is None


def test_random_action_never_double_switches_and_survives_empty_options():
    raw = {"phase": "moving", "observation": {}, "legal_actions": {"actions": [{"input": {"action": {
        "slots": [{"slot": 0, "options": [{"type": "switch", "species": "x"}]},
                  {"slot": 1, "options": [{"type": "switch", "species": "x"}]}]}}}]}}
    for _ in range(20):
        a = pe.random_action(raw)
        assert a["slot_0"] == {"type": "switch", "species": "x"} and a["slot_1"] == {"type": "pass"}


def test_six_team_lists_the_whole_roster_like_the_platform():
    """ARENA_SIX_TEAM=1: `team` as the real platform sends it (roster order, the two left out at full HP)."""
    from agent.pokemon.dex import card_catalog, to_id
    roster, seen = [], set()
    for c in card_catalog().values():
        if to_id(c["species"]) not in seen and len(roster) < 6:
            seen.add(to_id(c["species"]))
            roster.append(c)
    live = [{"id": to_id(c["species"]), "species": c["species"], "types": [], "current_hp": 10, "max_hp": 150,
             "current_hp_fraction": 10 / 150, "status": None, "fainted": False, "active": i < 2, "boosts": {},
             "base_stats": {}} for i, c in enumerate(reversed(roster[2:]))]           # brought: the last four
    team = pe._six_team("p1", live, roster)
    assert [v["species"] for v in team.values()] == [to_id(c["species"]) for c in roster]
    assert all(k.startswith("p1: ") for k in team)
    for n, v in enumerate(team.values()):
        if n < 2:                                   # left at team preview
            assert v["current_hp"] == v["max_hp"] > 0 and v["current_hp_fraction"] == 1.0
            assert not v["fainted"] and not v["revealed"] and v["moves"]
            assert v["active"] is True              # the platform's stale team-preview flag (roster[0] and [1])
        else:
            assert v["current_hp"] == 10
    # the same two left out, third and fourth in the roster: not flagged
    team = pe._six_team("p1", live, roster[2:4] + roster[:2] + roster[4:])
    assert [v["species"] for v in team.values()][2:4] == [to_id(c["species"]) for c in roster[:2]]
    assert [v["active"] for v in team.values()][2:4] == [False, False]


@pytest.mark.skipif(not (JS_DIR / "node_modules").exists(), reason="npm install not run in local/js")
def test_showdown_match_ours_vs_baseline_completes():
    from agent.agent import create_agent
    from local.showdown import Showdown
    with Showdown() as sd:
        r = pe.play_match(sd, pe.ContestantPlayer(create_agent), pe.OfficialBaseline(), seed=7)
    assert r.winner in (0, 1, None) and r.turns >= 1
    assert r.invalid[0] == 0                      # our deployed agent never sends an illegal action
    assert len(r.rosters[0]) == 6 and r.brought[0].startswith("team ")


# ---------------------------------------------------------------- werewolf engine

class Script(we.Seat):
    """Scripted seat: kill/investigate/vote targets by sub-phase; None -> legal_actions[0]."""

    def __init__(self, kill=None, vote=None):
        self.kill, self.vote = kill, vote

    def choose(self, raw):
        sub = raw["game_state"]["phase"]
        legal = [a["action_id"] for a in raw["legal_actions"]["actions"]]
        want = {"night_wolf": self.kill, "day_vote": self.vote}.get(sub)
        return str(want) if want is not None and str(want) in legal else legal[0]


def fixed_game(seats, roles, seed=0):
    g = we.Game(seats, random.Random(seed))
    g.roles = list(roles)
    return g


def test_roles_and_peaceful_first_night():
    g = we.Game([we.OfficialBaseline() for _ in range(7)], random.Random(1))
    assert sorted(g.roles) == ["seer", "villager", "villager", "villager", "villager", "wolf", "wolf"]
    g.night()
    assert g.dead == [] and len(g.invest) == 1


def test_wolf_disagreement_kills_lower_seat():
    roles = ["villager", "wolf", "villager", "wolf", "villager", "seer", "villager"]
    seats = [Script() for _ in range(7)]
    seats[1], seats[3] = Script(kill=6), Script(kill=4)
    g = fixed_game(seats, roles)
    g.day = 2
    g.night()
    assert [d["seat"] for d in g.dead] == [4] and g.dead[0]["cause"] == "night_kill" and g.dead[0]["role"] == "villager"


def test_plurality_tie_lynches_nobody_and_three_stalls_hand_wolves_the_win():
    roles = ["villager", "wolf", "villager", "wolf", "villager", "seer", "villager"]
    seats = [Script(vote=7) for _ in range(7)]          # everyone abstains
    g = fixed_game(seats, roles)
    for _ in range(3):
        g.vote()
        g.day += 1
    assert all(v["eliminated"] is None for v in g.votes) and g.stall == 3
    assert g.check() and g.winner == "wolf"
    # tie: 0,2 vote 4; 4,6 vote 0; rest abstain -> nobody
    seats = [Script(vote=4), Script(vote=7), Script(vote=4), Script(vote=7), Script(vote=0), Script(vote=7), Script(vote=0)]
    g = fixed_game(seats, roles)
    g.vote()
    assert g.votes[0]["eliminated"] is None and g.dead == []


def test_state_payload_parses_with_our_parser():
    roles = ["villager", "wolf", "villager", "wolf", "villager", "seer", "villager"]
    g = fixed_game([Script() for _ in range(7)], roles)
    g.invest = {1: "wolf"}
    g._kill(0, "lynch")
    st = g.state(5, "moving", "day_vote", [1, 2, 3, 4, 6, 7])
    v = parse(st)
    assert v.role == "seer" and v.investigations == {1: "wolf"} and v.dead == {0: "villager"}
    assert v.alive == [1, 2, 3, 4, 5, 6] and [a["action_id"] for a in st["legal_actions"]["actions"]][-1] == "7"
    w = parse(g.state(1, "moving", "night_wolf", [2, 4, 5, 6]))
    assert w.role == "wolf" and w.allies == [3]


def test_full_game_scores_plus_minus_one_per_side():
    g = we.Game([we.OfficialBaseline() for _ in range(7)], random.Random(5))
    r = g.play()
    assert g.winner in ("wolf", "villager") and set(r.values()) <= {1.0, -1.0}
    for i in range(7):
        side = "wolf" if g.roles[i] == "wolf" else "villager"
        assert r[i] == (1.0 if side == g.winner else -1.0)
