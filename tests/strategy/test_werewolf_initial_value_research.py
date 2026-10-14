from copy import deepcopy
from dataclasses import replace
import random

import pytest

from agent.werewolf.policy import WerewolfPolicy
from agent.werewolf.state import View
from local import werewolf_initial_value_research as study
from local import werewolf_counterfactual_research as cf
from local.werewolf_benchmark import Scenario
from local.werewolf_research import run_task


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for name, value in cf.SETTINGS.items():
        monkeypatch.setenv(name, value)


def view(me):
    return View(me=me, role='seer', day=1, sub_phase='night_seer', alive=list(range(7)), dead={}, dead_cause={})


@pytest.mark.parametrize('rank', range(6))
@pytest.mark.parametrize('me', range(7))
def test_rank_uses_only_sorted_legal_targets_not_presentation_order_or_hidden_identity(rank, me):
    v = view(me)
    targets = v.others_alive
    expected = sorted(targets)[rank]
    before = deepcopy(v)
    random.Random(91).shuffle(targets)
    assert study.InitialValuePolicy(rank)._investigate(v, targets)[0] == expected
    assert v == before


@pytest.mark.parametrize('rank', range(6))
@pytest.mark.parametrize('change', ['day', 'checked', 'message', 'vote', 'dead', 'fewer_legal', 'role'])
def test_later_or_informed_context_is_exactly_baseline(rank, change):
    v, targets = view(3), [0, 1, 2, 4, 5, 6]
    if change == 'day':
        v.day = 2
    elif change == 'checked':
        v.investigations = {6: 'wolf'}
    elif change == 'message':
        v.messages = [{'sender': 1, 'content': 'I am the Seer.', 'day': 1, 'seq': 0}]
    elif change == 'vote':
        v.votes = [(1, {0: 1}, 1)]
    elif change == 'dead':
        v = replace(v, alive=[0, 1, 2, 3, 4, 5], dead={6: 'villager'})
        targets.remove(6)
    elif change == 'fewer_legal':
        targets.remove(6)
    else:
        v.role = 'villager'
    assert study.InitialValuePolicy(rank)._investigate(v, targets) == WerewolfPolicy()._investigate(v, targets)


@pytest.mark.parametrize('rank', range(6))
def test_fresh_and_other_decision_methods_stay_baseline(rank):
    agent = getattr(study, f'rank{rank}')()
    old = agent.ww
    agent._fresh()
    assert agent.ww is not old and agent.ww.rank == rank
    for name in ('_day_vote', '_wolf_vote', '_wolf_kill', '_plan', '_seer_chat', '_villager_chat'):
        assert getattr(study.InitialValuePolicy, name) is getattr(WerewolfPolicy, name)


@pytest.mark.parametrize('bad', [-1, 6, True, None])
def test_invalid_rank_is_rejected(bad):
    with pytest.raises(ValueError):
        study.InitialValuePolicy(bad)


@pytest.mark.parametrize('rank', range(6))
def test_actual_rank_policy_matches_training_single_action_intervention(rank):
    scenario = Scenario(251, 99, 'llmq', 1)
    baseline, points = cf.play(scenario, collect=True)
    assert baseline['role'] == 'seer'
    p = points[0]
    forced = cf.Intervention(p['index'], sorted(p['legal'])[rank], p['evaluator_prefix'], p['input_hash'], p['proposed'])
    replay, _ = cf.play(scenario, intervention=forced)
    direct = run_task((scenario, [('rank', f'local.werewolf_initial_value_research:rank{rank}', {})], cf.SETTINGS, True, True))[0]
    assert 'harness_error' not in direct
    assert direct['win'] == replay['win'] and cf.digest(direct['trace']) == replay['trace_hash']


def test_training_selection_keeps_b0_on_ties_or_one_seed_regression():
    from scripts.werewolf_initial_value_eval import select_rank
    metrics = {str(r): {k: {'delta': 0} for k in ('all', 'seed:251', 'seed:252')} for r in range(6)}
    assert select_rank(metrics) == (5, 5)
    metrics['2'] = {'all': {'delta': .1}, 'seed:251': {'delta': .3}, 'seed:252': {'delta': -.1}}
    assert select_rank(metrics) == (2, 5)
    metrics['2']['seed:252']['delta'] = .1
    assert select_rank(metrics) == (2, 2)
    metrics['4'] = deepcopy(metrics['2'])
    assert select_rank(metrics) == (4, 4)


@pytest.mark.parametrize('background', ['first', 'random', 'llmq', 'rt_detective', 'pop_coordinated'])
def test_high_rank_is_a_full_game_b0_mirror(background):
    rows = run_task((Scenario(251, 5, background, 1),
                     [('b0', 'agent', {}), ('rank5', 'local.werewolf_initial_value_research:rank5', {})],
                     cf.SETTINGS, True, True))
    assert not any('harness_error' in r for r in rows)
    for key in ('win', 'winner', 'role', 'seat', 'trace', 'failures'):
        assert rows[0][key] == rows[1][key]
