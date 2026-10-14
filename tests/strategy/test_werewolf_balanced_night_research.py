from dataclasses import replace
from itertools import combinations

import pytest

from agent.werewolf.policy import WerewolfPolicy
from agent.werewolf.state import View
from local.werewolf_balanced_night_research import BalancedNightPolicy, balanced, extremes


def view(living, me):
    dead = {s: 'villager' for s in range(7) if s not in living}
    dead[min(dead)] = 'wolf'
    return View(me=me, role='wolf', day=3, sub_phase='night_wolf', alive=list(living),
                dead=dead, dead_cause={s: 'vote' for s in dead})


@pytest.mark.parametrize('living', list(combinations(range(7), 4)))
def test_equal_score_choice_avoids_both_extreme_voting_orders_when_possible(living, monkeypatch):
    scores = {s: 0 for s in living}
    monkeypatch.setattr(WerewolfPolicy, 'suspicion', lambda self, v, **kw: scores)
    for me in living:
        v = view(living, me)
        legal = [s for s in living if s != me]
        old = WerewolfPolicy()._kill_plan(v, legal)
        actual = BalancedNightPolicy()._kill_plan(v, legal)
        assert actual[0] in legal
        remaining = sorted(s for s in living if s != actual[0])
        if me == living[1]:
            assert remaining[1] == me and actual[0] == living[-1]
            assert sorted(s for s in living if s != old[0])[0] == me
        else:
            assert actual == old


def test_does_not_sacrifice_a_better_trust_score(monkeypatch):
    v = view([1, 3, 4, 6], 3)
    monkeypatch.setattr(WerewolfPolicy, 'suspicion', lambda self, v, **kw: {1: -1, 4: 0, 6: 0})
    assert BalancedNightPolicy()._kill_plan(v, [1, 4, 6]) == WerewolfPolicy()._kill_plan(v, [1, 4, 6])


def test_keeps_seer_priority_and_living_wolf_coordination():
    v = view([1, 3, 4, 6], 3)
    v.messages = [{'sender': 1, 'content': 'I am the Seer. Player3 is a WOLF.', 'day': 2,
                   'seq': 0, 'visibility': 'broadcast', 'recipients': []}]
    assert BalancedNightPolicy()._kill_plan(v, [1, 4, 6]) == WerewolfPolicy()._kill_plan(v, [1, 4, 6])
    v = replace(v, alive=[0, 1, 3, 4, 6], allies=[0], dead={2: 'villager', 5: 'seer'},
                messages=[{'sender': 0, 'content': 'Tonight: Player4.', 'day': 2,
                           'seq': 0, 'visibility': 'direct', 'recipients': [3]}])
    assert BalancedNightPolicy()._wolf_kill(v, [1, 4, 6]) == (4, 'coordinated kill')


@pytest.mark.parametrize('factory,general', [(balanced, False), (extremes, True)])
def test_fresh_reinstalls_only_the_balanced_night_policy(factory, general):
    agent = factory()
    old = agent.ww
    agent._fresh()
    assert isinstance(agent.ww, BalancedNightPolicy) and agent.ww is not old and agent.ww.general == general


def test_general_rule_keeps_both_wolves_away_from_extreme_voter_targets(monkeypatch):
    v = View(me=1, role='wolf', day=2, sub_phase='night_wolf', alive=list(range(6)),
             allies=[4], dead={6: 'villager'}, dead_cause={6: 'vote'})
    monkeypatch.setattr(WerewolfPolicy, 'suspicion', lambda self, v, **kw: {s: 0 for s in v.alive})
    assert BalancedNightPolicy()._kill_plan(v, [0, 2, 3, 5])[0] == 0
    assert BalancedNightPolicy(True)._kill_plan(v, [0, 2, 3, 5])[0] == 3


@pytest.mark.parametrize('wolves', list(combinations(range(7), 2)))
def test_general_rule_never_increases_extreme_exposure_at_equal_score(wolves, monkeypatch):
    policy = BalancedNightPolicy(True)
    for size in (5, 6, 7):
        for alive in combinations(range(7), size):
            if not set(wolves) <= set(alive):
                continue
            dead = {s: 'villager' for s in range(7) if s not in alive}
            v = View(me=wolves[0], role='wolf', day=2, sub_phase='night_wolf', alive=list(alive),
                     allies=[wolves[1]], dead=dead, dead_cause={s: 'vote' for s in dead})
            monkeypatch.setattr(WerewolfPolicy, 'suspicion', lambda self, v, **kw: {s: 0 for s in v.alive})
            targets = [s for s in alive if s not in wolves]
            old = WerewolfPolicy()._kill_plan(v, targets)
            new = policy._kill_plan(v, targets)
            def exposure(target):
                live = [s for s in alive if s != target]
                return len({min(live), max(live)} & set(wolves))
            assert new[0] in targets and exposure(new[0]) <= exposure(old[0])
            if exposure(new[0]) == exposure(old[0]):
                assert new == old
