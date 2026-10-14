from copy import deepcopy
from dataclasses import replace
import random

import pytest

from agent.werewolf.policy import WerewolfPolicy
from local import werewolf_counterfactual_research as cf
from local import werewolf_benchmark as wb
from local.werewolf_env import ContestantSeat, OfficialBaseline, RandomSeat
from local.werewolf_research import play_profile


@pytest.fixture(autouse=True)
def local_only(monkeypatch):
    for key, value in cf.SETTINGS.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize('background', ['first', 'random', 'llmq', 'guard_fake', 'pop_coordinated'])
@pytest.mark.parametrize('timing', ['rt', 'rounds'])
def test_instrumented_b0_is_the_same_full_game(background, timing):
    scenario = wb.Scenario(251, 0, background, 1)
    with wb.environment(cf.SETTINGS | {'ARENA_WW_TIMING': timing}):
        expected = play_profile(scenario, 'b0', 'agent', {}, True, True)
    actual, points = cf.play(scenario, collect=True, timing=timing)
    for key in ('id', 'deal_seed', 'seat', 'role', 'win', 'winner', 'days'):
        assert actual[key] == expected[key]
    assert actual['trace_hash'] == cf.digest(expected['trace'])
    assert points and not any(actual['errors'].values())


@pytest.mark.parametrize('mode,original', [('first', OfficialBaseline), ('random', RandomSeat)])
def test_simple_control_is_silent_legal_and_keeps_contestant_scheduling(mode, original):
    simple, old = cf.SimpleControlledSeat(mode), original()
    assert isinstance(simple, ContestantSeat)
    simple.rng, old.rng = random.Random(91), random.Random(91)
    simple.reset()
    old.reset()
    for actions in ([0, 1, 7], [2, 4], [1]):
        raw = {'legal_actions': {'actions': [{'action_id': str(s)} for s in actions]}}
        assert simple.choose(raw) == old.choose(raw)
        assert simple.message(raw) is None
    with pytest.raises(ValueError):
        cf.SimpleControlledSeat('unknown')


def good_case(background='llmq'):
    for index in range(30):
        scenario = wb.Scenario(251, index, background, 1)
        row, points = cf.play(scenario, collect=True)
        if row['role'] in ('villager', 'seer'):
            return scenario, row, points
    pytest.fail('fixed test scan contains no good role')


@pytest.mark.parametrize('background', ['first', 'random', 'llmq', 'guard_fake'])
def test_original_action_intervention_replays_complete_outcome_and_prefix(background):
    scenario, row, points = good_case(background)
    for point in points:
        intervention = cf.Intervention(point['index'], point['proposed'], point['evaluator_prefix'],
                                       point['input_hash'], point['proposed'])
        changed, _ = cf.play(scenario, intervention=intervention)
        assert changed['intervention_applied']
        for key in ('event_hash', 'trace_hash', 'win', 'winner', 'days'):
            assert changed[key] == row[key]
        assert cf.describe(point)['visible_hash']


def test_one_legal_substitution_executes_the_rest_of_the_game():
    scenario, row, points = good_case()
    point = next(p for p in points if len(p['legal']) >= 2)
    alternate = next(a for a in point['legal'] if a != point['proposed'])
    override = cf.Intervention(point['index'], alternate, point['evaluator_prefix'], point['input_hash'], point['proposed'])
    changed, changed_points = cf.play(scenario, intervention=override, collect=True)
    assert changed['intervention_applied'] and changed['winner'] in ('wolf', 'villager')
    assert not any(changed['errors'].values()) and changed['trace_hash'] != row['trace_hash']
    assert changed_points[point['index']]['action'] == alternate
    assert changed_points[point['index']]['evaluator_prefix'] == point['evaluator_prefix']
    assert sum(p['action'] != p['proposed'] for p in changed_points) == 1


@pytest.mark.parametrize('bad', ['prefix', 'input', 'proposal', 'illegal', 'boolean', 'unreached'])
def test_bad_replay_never_silently_becomes_a_valid_rollout(bad):
    scenario, _, points = good_case('first')
    point = points[0]
    forced = cf.Intervention(point['index'], point['proposed'], point['evaluator_prefix'], point['input_hash'], point['proposed'])
    patch = {'prefix': {'expected_prefix': '0' * 64}, 'input': {'expected_input': '0' * 64},
             'proposal': {'expected_proposal': -2}, 'illegal': {'action': 99}, 'boolean': {'action': True},
             'unreached': {'index': 1000}}[bad]
    with pytest.raises(ValueError):
        cf.play(scenario, intervention=replace(forced, **patch))


def test_evaluator_overrides_and_truth_never_enter_policy_payload(monkeypatch):
    observed = []
    original = WerewolfPolicy.act
    def inspect(self, raw):
        forbidden = {'intervention', 'evaluator_prefix', 'roles', 'hidden_roles', 'deal_seed', 'opponent_types'}
        assert not forbidden & raw.keys()
        assert not forbidden & raw['game_state'].keys()
        assert all(not {'role', 'secretRole'} & p.keys() for p in raw['players'])
        observed.append(raw['your_position'])
        return original(self, raw)
    monkeypatch.setattr(WerewolfPolicy, 'act', inspect)
    scenario, _, points = good_case('first')
    p = points[0]
    forced = cf.Intervention(p['index'], p['legal'][-1], p['evaluator_prefix'], p['input_hash'], p['proposed'])
    cf.play(scenario, intervention=forced)
    assert observed


def test_selection_is_deterministic_loss_conditioned_role_balanced_and_background_spread():
    rows = []
    for role in ('villager', 'seer', 'wolf'):
        for bg in ('first', 'random'):
            for index in range(4):
                rows.append({'id': f'{role}/{bg}/{index}', 'role': role, 'profile': 'b0',
                             'scenario': {'background': bg}, 'win': index == 3})
    chosen, available = cf.select_losses(rows, ['first', 'random'], 4)
    assert len(chosen) == 8 and not any(r['win'] for r in chosen)
    assert [r['scenario']['background'] for r in chosen] == ['first', 'random'] * 4
    assert available == {'villager': {'first': 3, 'random': 3}, 'seer': {'first': 3, 'random': 3}}
    assert cf.select_losses(list(reversed(rows)), ['first', 'random'], 4)[0] == chosen
    with pytest.raises(ValueError, match='insufficient'):
        cf.select_losses(rows, ['first', 'random'], 7)


def test_paired_calibration_orientation_and_corruption_guards():
    scenario = wb.Scenario(251, 0, 'first', 1)
    rows = [cf.play(scenario, profile)[0] for profile in cf.PROFILES]
    rows[0]['win'], rows[1]['win'], rows[2]['win'] = True, False, True
    result = cf.calibration_metrics(rows)
    assert result['first']['all']['seat']['delta'] == 1
    assert result['random']['all']['seat']['delta'] == 0
    with pytest.raises(ValueError, match='duplicate'):
        cf.calibration_metrics(rows + rows[:1])
    with pytest.raises(ValueError, match='incomplete'):
        cf.calibration_metrics(rows[:-1])
    corrupted = deepcopy(rows)
    corrupted[-1]['role'] = 'invalid'
    with pytest.raises(ValueError, match='identity'):
        cf.calibration_metrics(corrupted)


def test_full_branch_enumeration_includes_every_legal_action_and_one_noop_per_point():
    for index in range(50):
        record, points = cf.play(wb.Scenario(251, index, 'first', 1), collect=True)
        if record['role'] in ('villager', 'seer') and not record['win']:
            break
    else:
        pytest.fail('no good loss in the fixed diagnostic fixture pool')
    rows = list(cf.branches(record))
    assert len(rows) == sum(len(p['legal']) for p in points)
    assert sum(r['noop'] for r in rows) == len(points)
    assert all(r['outcome']['intervention_applied'] for r in rows)
    for point in points:
        actual = [r for r in rows if r['point'] == point['index']]
        assert {r['action'] for r in actual} == set(point['legal'])
        noop = next(r for r in actual if r['noop'])
        assert noop['point_observation']['raw'] == point['raw']
        assert noop['outcome']['trace_hash'] == record['trace_hash']
    for patch in ({'profile': 'random'}, {'role': 'wolf'}, {'win': True}):
        with pytest.raises(ValueError, match='selected'):
            list(cf.branches(record | patch))


def test_diagnostic_summary_does_not_treat_hindsight_as_an_executable_policy():
    from scripts.werewolf_counterfactual_eval import diagnostic_summary
    cases = [{'id': str(i), 'role': role, 'background': 'first', 'points': 1, 'branches': 3,
              'rescued_any': True, 'rescued_guarded': i == 0, 'vote_rescue': True,
              'investigation_rescue': False, 'information': {'soft': {'points': 1, 'rescuable_points': 1,
                                                                    'guarded_rescuable_points': int(i == 0)}}}
             for i, role in enumerate(('villager', 'seer'))]
    groups = {'same': [{'case': '0', 'rescuing_actions': [1]}, {'case': '1', 'rescuing_actions': [2]}]}
    result = diagnostic_summary(cases, groups)
    assert result['all']['n'] == 2 and result['all']['rescued_any'] == 2
    assert result['all']['rescued_preserving_private_guards'] == 1
    assert result['visible_information_groups']['no_single_action_rescues_every_state'] == 1
    assert result['limitations']


def test_resource_boundaries_fail_before_more_games(monkeypatch):
    from scripts import werewolf_counterfactual_eval as driver
    from types import SimpleNamespace
    monkeypatch.setattr(driver.time, 'time', lambda: 10)
    with pytest.raises(TimeoutError):
        driver.resources(9)
    monkeypatch.setattr(driver.shutil, 'disk_usage', lambda path: SimpleNamespace(free=1024))
    with pytest.raises(RuntimeError, match='reserve'):
        driver.resources(11)


def test_unsupported_multiseat_or_network_profiles_are_rejected():
    with pytest.raises(ValueError):
        cf.play(wb.Scenario(251, 0, 'first', 3))
    with pytest.raises(ValueError):
        cf.play(wb.Scenario(251, 0, 'llm_real', 1))
