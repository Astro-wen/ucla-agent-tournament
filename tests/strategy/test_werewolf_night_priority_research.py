from dataclasses import replace
from itertools import combinations
import random

import pytest

from agent.werewolf.policy import WerewolfPolicy
from agent.werewolf.state import View
from local.werewolf_env import OfficialBaseline
from local.werewolf_research import ResearchGame
from local import werewolf_night_priority_research as night


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for key in ('ARENA_RECORD', 'ARENA_STATE', 'ARENA_SUMMARY', 'ARENA_TC_WATCHDOG'):
        monkeypatch.setenv(key, '0')
    monkeypatch.setenv('ARENA_WW_DM', 'off')
    monkeypatch.setenv('ARENA_WW_TACTICS', 'off')


def view(me=3, allies=()):
    return View(me=me, role='wolf', day=3, sub_phase='night_wolf',
                alive=[1, 3, 4, 6], dead={0: 'wolf', 2: 'villager', 5: 'villager'},
                dead_cause={0: 'vote', 2: 'night_kill', 5: 'vote'}, allies=list(allies))


@pytest.mark.parametrize('mode', night.MODES)
def test_factories_reinstall_correct_profile_on_fresh(mode):
    agent = getattr(night, mode)()
    original = agent.ww
    agent._fresh()
    assert agent.ww is not original and agent.ww.mode == mode


def test_high_seat_and_preserve_vote_are_distinct_fixed_rules(monkeypatch):
    v = view()
    scores = {1: 0.0, 4: 0.0, 6: 0.0}
    monkeypatch.setattr(WerewolfPolicy, 'suspicion', lambda self, v, **kw: scores)
    assert night.NightPriorityPolicy('baseline')._wolf_kill(v, [1, 4, 6])[0] == 1
    assert night.NightPriorityPolicy('high')._wolf_kill(v, [1, 4, 6])[0] == 6
    assert night.NightPriorityPolicy('tie_high')._wolf_kill(v, [1, 4, 6])[0] == 6
    assert night.NightPriorityPolicy('keep_vote')._wolf_kill(v, [1, 4, 6])[0] == 4
    scores.update({1: -1.0, 4: 2.0, 6: 3.0})
    assert night.NightPriorityPolicy('tie_high')._wolf_kill(v, [1, 4, 6])[0] == 1
    assert night.NightPriorityPolicy('keep_vote')._wolf_kill(v, [1, 4, 6])[0] == 1


@pytest.mark.parametrize('mode', night.MODES)
def test_claimed_seer_priority_is_identical_to_baseline(mode):
    v = replace(view(), messages=[{'sender': 4, 'content': 'I am the Seer. Player3 is a WOLF.',
                                  'day': 2, 'seq': 0, 'recipients': [], 'visibility': 'broadcast'}])
    expected = WerewolfPolicy()._wolf_kill(v, [1, 4, 6])
    assert expected[0] == 4
    assert night.NightPriorityPolicy(mode)._wolf_kill(v, [1, 4, 6]) == expected


@pytest.mark.parametrize('mode', night.MODES)
def test_lower_ally_plan_still_coordinates_even_when_tie_rule_differs(mode):
    v = replace(view(3, (0,)), alive=[0, 1, 3, 4, 6], dead={2: 'villager', 5: 'seer'},
                messages=[{'sender': 0, 'content': 'Tonight: Player4.', 'day': 2, 'seq': 0,
                           'recipients': [3], 'visibility': 'direct'}])
    assert night.NightPriorityPolicy(mode)._wolf_kill(v, [1, 4, 6]) == (4, 'coordinated kill')


@pytest.mark.parametrize('mode', night.MODES)
def test_every_legal_night_target_set_keeps_a_legal_nonwolf(mode):
    policy = night.NightPriorityPolicy(mode)
    for wolves in combinations(range(7), 2):
        humans = [s for s in range(7) if s not in wolves]
        for size in range(1, len(humans) + 1):
            for living_humans in combinations(humans, size):
                dead = {s: 'villager' for s in humans if s not in living_humans}
                v = View(me=wolves[0], role='wolf', day=2, sub_phase='night_wolf',
                         alive=sorted([*wolves, *living_humans]), allies=[wolves[1]],
                         dead=dead, dead_cause={s: 'vote' for s in dead})
                assert policy._wolf_kill(v, list(living_humans))[0] in living_humans


@pytest.mark.parametrize('mode', night.MODES)
def test_good_roles_and_wolf_ballots_and_chats_are_unchanged(mode):
    g = ResearchGame([OfficialBaseline() for _ in range(7)], random.Random(231))
    g.roles = ['seer', 'wolf', 'villager', 'villager', 'wolf', 'villager', 'villager']
    g.invest = {1: 'wolf', 2: 'villager'}
    for seat in range(7):
        raw = g.state(seat, 'moving', 'day_vote', [s for s in range(8) if s != seat])
        assert night.NightPriorityPolicy(mode).act(raw) == WerewolfPolicy().act(raw)
        talk = g.state(seat, 'messaging', 'day_vote')
        assert night.NightPriorityPolicy(mode).chat(talk) == WerewolfPolicy().chat(talk)
    raw = g.state(0, 'moving', 'night_seer', list(range(1, 7)))
    assert night.NightPriorityPolicy(mode).act(raw) == WerewolfPolicy().act(raw)


@pytest.mark.parametrize('mode', night.MODES)
def test_hidden_metadata_cannot_affect_the_rule(mode):
    g = ResearchGame([OfficialBaseline() for _ in range(7)], random.Random(231))
    g.roles = ['wolf', 'villager', 'seer', 'villager', 'wolf', 'villager', 'villager']
    raw = g.state(0, 'moving', 'night_wolf', [1, 2, 3, 5, 6])
    hidden = raw | {'roles': ['wolf'] * 7, 'secretRole': 'seer', 'opponent_types': ['first'] * 7, 'seed': 999}
    assert night.NightPriorityPolicy(mode).act(raw) == night.NightPriorityPolicy(mode).act(hidden)
