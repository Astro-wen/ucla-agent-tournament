from copy import deepcopy
from dataclasses import replace
import random

import pytest

from agent.werewolf.policy import WerewolfPolicy
from agent.werewolf.state import View
from local import werewolf_planning_research as p
from local.werewolf_belief_research import Deal, feasible
from local.werewolf_benchmark import Scenario
from local.werewolf_counterfactual_research import SETTINGS
from local.werewolf_env import OfficialBaseline
from local.werewolf_research import ResearchGame, run_task


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for key, value in SETTINGS.items():
        monkeypatch.setenv(key, value)


def view():
    messages = [{'seq': 0, 'sender': 1, 'day': 1, 'type': 'chat', 'visibility': 'broadcast',
                 'recipients': [], 'content': 'I am the Seer. Player0 is a WOLF.'}]
    for sender, target in ((0, 3), (2, 4), (3, 4), (4, 3)):
        messages.append({'seq': len(messages), 'sender': sender, 'day': 2, 'type': 'chat',
                         'visibility': 'broadcast', 'recipients': [], 'content': f'I vote Player{target}.'})
    return View(me=5, role='villager', day=2, sub_phase='day_vote', alive=[0, 2, 3, 4, 5],
                dead={1: 'seer', 6: 'villager'}, dead_cause={1: 'vote', 6: 'night_kill'},
                votes=[(1, {0: 1, 1: 0, 2: 4, 3: 1, 4: 1, 5: 0, 6: 4}, 1)], messages=messages)


def test_hypotheses_use_only_own_and_public_information_not_the_actual_world():
    v = view()
    worlds = p.distribution(v)
    assert len(worlds) == 3 and sum(w for _, w in worlds) == pytest.approx(1)
    assert {d.wolves for d, _ in worlds} == {(0, 2), (0, 3), (0, 4)}
    assert all(feasible(d, v) and d.seer == 1 for d, _ in worlds)
    v.hidden_roles = ['wolf'] * 7
    v.opponent_types = ['first'] * 7
    v.deal_seed = 123
    assert p.distribution(v) == worlds
    assert p.signature(v, 0) == p.signature(view(), 0)


def test_own_private_checks_are_preserved_in_every_world():
    v = replace(view(), me=3, role='seer', dead={1: 'villager', 6: 'villager'}, investigations={0: 'wolf', 2: 'villager'}, messages=[])
    worlds = p.distribution(v)
    assert worlds and all(0 in d.wolves and 2 not in d.wolves and d.seer == 3 for d, _ in worlds)


def test_public_history_is_reconstructed_without_other_private_messages():
    v = view()
    v.messages.append({'seq': 200, 'sender': 0, 'day': 2, 'type': 'chat', 'visibility': 'direct',
                       'recipients': [5], 'content': 'a private statement'})
    world = Deal((0, 4), 1)
    g = p.hypothetical_game(v, world, {s: 'pledge' for s in range(7)}, 271, 4, WerewolfPolicy().export_memory())
    assert g.roles == ['wolf', 'seer', 'villager', 'villager', 'wolf', 'villager', 'villager']
    assert g.living() == v.alive and g.day == 2 and g.stall == 0
    assert g.dead == [{'day': 1, 'seat': 1, 'role': 'seer', 'cause': 'vote'},
                      {'day': 2, 'seat': 6, 'role': 'villager', 'cause': 'night_kill'}]
    assert [m['seq'] for m in g.messages] == list(range(5))
    assert all(not m['recipients'] for m in g.messages)
    assert all(isinstance(s.policy, p.ModelPolicy) and not isinstance(s.policy, p.PlanningPolicy) for s in g.seats)
    raw = g.state(5, 'moving', 'day_vote', [0, 2, 3, 4, 7])
    assert 'roles' not in raw and 'hypotheses' not in raw
    assert raw['new_messages'] == []
    assert 'Your role: VILLAGER' in raw['observation']


def test_full_terminal_rollout_can_choose_a_useful_vote_and_does_not_know_actual_identity():
    v = view()
    styles = {s: 'pledge' for s in range(7)}
    memory = WerewolfPolicy().export_memory()
    assert p.terminal_trial(v, Deal((0, 4), 1), styles, 271, 0, memory) == 0
    forecast = {}
    assert p.terminal_trial(v, Deal((0, 4), 1), styles, 271, 4, memory, forecast=forecast) == 1
    assert forecast == {0: 3, 2: 4, 3: 4, 4: 3}
    assert p.terminal_trial(v, Deal((0, 2), 1), styles, 271, 4, memory) == 0


@pytest.mark.parametrize('mode', p.MODES)
def test_factories_reset_cache_and_modes(mode):
    agent = getattr(p, mode)()
    old = agent.ww
    agent.ww.cache['x'] = 0
    agent._fresh()
    assert agent.ww is not old and agent.ww.mode == mode and not agent.ww.cache


def test_sampling_uses_same_worlds_styles_seeds_for_all_root_actions(monkeypatch):
    calls = []
    def trial(v, world, styles, seed, action, memory, forecast=None):
        calls.append((world, dict(styles), seed, action))
        return int(action == 4)
    monkeypatch.setattr(p, 'terminal_trial', trial)
    policy = p.PlanningPolicy()
    assert policy.plan(view(), p.distribution(view()), [0, 2, 3, 4, 7], 0, 'fixed') == 4
    assert len(calls) == p.PARTICLES * 5
    for i in range(0, len(calls), 5):
        assert all(c[:3] == calls[i][:3] for c in calls[i:i + 5])
        assert [c[3] for c in calls[i:i + 5]] == [0, 2, 3, 4, 7]
    again = p.PlanningPolicy()
    assert again.plan(view(), p.distribution(view()), [0, 2, 3, 4, 7], 0, 'fixed') == 4
    assert calls[:80] == calls[80:]
    observed = p.PlanningPolicy('rollout_observed')
    observed.plan(view(), p.distribution(view()), [0, 2, 3, 4, 7], 0, 'fixed')
    assert [(c[0], c[2]) for c in calls[:80]] == [(c[0], c[2]) for c in calls[160:]]


def test_abstention_is_only_added_if_actually_offered(monkeypatch):
    v = view()
    policy = p.PlanningPolicy()
    policy.moving = True
    seen = []
    def plan(v, worlds, choices, reference, key):
        seen.append(choices)
        return 7 if 7 in choices else reference
    monkeypatch.setattr(policy, 'plan', plan)
    policy.offered_abstain = True
    assert policy._day_vote(v, [0, 2, 3, 4])[0] == 7
    assert 7 in seen[-1]
    policy.offered_abstain = False
    assert policy._day_vote(v, [0, 2, 3, 4])[0] != 7
    assert 7 not in seen[-1]


def test_bounded_observation_update_does_not_turn_promises_into_certainty():
    v = view()
    before = p.style_weights(v, 2, False)
    after = p.style_weights(v, 2, True)
    assert before == p.PRIOR and after != before
    assert sum(after) == pytest.approx(1) and all(w > 0 for w in after)
    assert max(after) < 1


@pytest.mark.parametrize('mode', p.MODES)
def test_wolf_and_night_actions_and_actual_chat_do_not_invoke_planning(mode, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('planning entered outside good-role moving vote')
    monkeypatch.setattr(p.PlanningPolicy, 'plan', forbidden)
    g = ResearchGame([OfficialBaseline() for _ in range(7)], random.Random(271))
    g.roles = ['seer', 'wolf', 'villager', 'villager', 'wolf', 'villager', 'villager']
    for seat in range(7):
        raw = g.state(seat, 'messaging', 'day_vote')
        assert p.PlanningPolicy(mode).chat(raw) == WerewolfPolicy().chat(raw)
    for seat, phase, legal in [(0, 'night_seer', [1, 2, 3, 4, 5, 6]), (1, 'night_wolf', [0, 2, 3, 5, 6]),
                               (1, 'day_vote', [0, 2, 3, 4, 5, 6, 7])]:
        raw = g.state(seat, 'moving', phase, legal)
        assert p.PlanningPolicy(mode).act(raw) == WerewolfPolicy().act(raw)


@pytest.mark.parametrize('background', ['first', 'llmq', 'guard_fake', 'pop_coordinated'])
def test_mirror_wrapper_preserves_full_b0_trace(background):
    rows = run_task((Scenario(271, 0, background, 1), [('reference', 'agent', {}),
                     ('mirror', 'local.werewolf_planning_research:mirror', {})], SETTINGS, True, True))
    assert not any('harness_error' in r for r in rows)
    for key in ('win', 'role', 'seat', 'trace', 'failures'):
        assert rows[0][key] == rows[1][key]


def test_incomplete_public_history_declines_search_without_fabricating_a_death(monkeypatch):
    v = replace(view(), votes=[])
    policy = p.PlanningPolicy()
    policy.moving = True
    def forbidden(*args, **kwargs):
        pytest.fail('incomplete public state entered a model')
    monkeypatch.setattr(policy, 'plan', forbidden)
    assert policy._day_vote(v, v.others_alive) == WerewolfPolicy()._day_vote(v, v.others_alive)
    assert policy.planning_stats['unsupported_public_history'] == 1


def test_ballot_prediction_metric_uses_observed_ballots_only_after_game():
    from scripts.werewolf_planning_eval import prediction_metrics
    traces = [{'day': 2, 'forecast': {0: {3: 16}, 2: {4: 16}}}]
    actual = [{'day': 2, 'votes': {0: 3, 2: 4, 3: 4, 4: 3, 5: 0}}]
    result = prediction_metrics(traces, actual)
    assert result['n'] == result['correct'] == 2
    assert 0 < result['logloss_sum'] < 0.1 and result['brier_sum'] > 0
    with pytest.raises(ValueError):
        prediction_metrics([{'day': 2, 'forecast': {0: {3: 1}}}], actual)


def test_hidden_input_poison_keeps_a_midgame_planning_action_identical():
    g = ResearchGame([OfficialBaseline() for _ in range(7)], random.Random(271))
    g.roles = ['wolf', 'seer', 'villager', 'villager', 'wolf', 'villager', 'villager']
    g.day, g.alive = 2, [True, False, True, True, True, True, False]
    g.dead = [{'day': 1, 'seat': 1, 'role': 'seer', 'cause': 'vote'}, {'day': 2, 'seat': 6, 'role': 'villager', 'cause': 'night_kill'}]
    g.votes = [{'day': 1, 'votes': {0: 1, 1: 0, 2: 4, 3: 1, 4: 1, 5: 0, 6: 4}, 'eliminated': 1}]
    raw = g.state(5, 'moving', 'day_vote', [0, 2, 3, 4, 7])
    poison = deepcopy(raw)
    poison.update(roles=['wolf'] * 7, seed=-91, opponent_types=['first'] * 7)
    poison['game_state']['roles'] = ['wolf'] * 7
    for player in poison['players']:
        player.update(role='wolf', secretRole='seer', agent_id='hidden-opponent-type')
    normal, changed = p.PlanningPolicy(), p.PlanningPolicy()
    assert normal.act(raw) == changed.act(poison)
    assert normal.planning_stats['calls'] == changed.planning_stats['calls'] == 1
    assert normal.planning_stats['inner_games'] > 0
    assert normal.planning_trace[0]['forecast'] == changed.planning_trace[0]['forecast']
