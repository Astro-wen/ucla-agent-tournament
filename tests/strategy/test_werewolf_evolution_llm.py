from copy import deepcopy

import pytest

from scripts.werewolf_evolution_llm_eval import GuardedLocalSeat, cases
from scripts.werewolf_model_scale_eval import validation_cases


class NoModel:
    def complete_structured(self,*args,**kwargs):
        pytest.fail('a unique hard-fact action must not call a model')


@pytest.mark.parametrize('case',validation_cases(),ids=lambda c:c['id'])
def test_guard_filters_only_available_actions_using_own_known_facts(case):
    seat=GuardedLocalSeat(NoModel())
    raw=case['raw']
    if case['discussion'] is not None:
        seat.guard.observe(case['discussion'])
    allowed=seat.allowed(raw)
    assert {str(a) for a in allowed}<=set(case['expected'])
    assert allowed<={int(a['action_id']) for a in raw['legal_actions']['actions']}
    if len(allowed)==1:
        assert seat.choose(raw) in case['expected']
        assert seat.guard_stats['forced_fact_action']==1
    poisoned=deepcopy(raw)
    poisoned['game_state']['hidden_roles']=['wolf']*7
    for player in poisoned['players']:
        player.update(role='wolf',secret_role='seer')
    other=GuardedLocalSeat(NoModel())
    if case['discussion'] is not None:
        other.guard.observe(case['discussion'])
    assert other.allowed(poisoned)==allowed


def test_registered_decoder_and_request_budget_are_not_inherited_defaults():
    from scripts.werewolf_evolution_llm_eval import provider_for_plan
    plan={'model':{'decoder_seed':1013,'context':16384,'output_tokens':192},'resources':{'request_timeout_seconds':20}}
    provider=provider_for_plan(plan,lambda:None)
    assert provider.options['seed']==1013 and provider.options['num_ctx']==16384
    assert provider.timeout==20 and provider.explicit_rules
    plan['model']['context']=8192
    with pytest.raises(ValueError):
        provider_for_plan(plan,lambda:None)


def test_wolf_only_pilot_selection_and_placements_are_fixed_before_outcomes():
    plan={'sample':{'backgrounds':['llmq','rt_detective','pop_coordinated','pop_observed_switch']}}
    first,scan=cases(plan)
    second,other=cases(plan)
    assert first==second and scan==other and len(first)==16
    assert len({c['seed'] for c in first})==16
    for case in first:
        assert case['ours'] not in case['llm_seats'] and len(set(case['llm_seats']))==2
    assert all(c['role']=='wolf' for c in scan if c['accepted'])
