import pytest

from agent.werewolf.state import View
from local import werewolf_planning_research as model
from local.werewolf_planning_free_research import FreePlanningPolicy, rollout_free


def view():
    return View(me=5, role='villager', day=2, sub_phase='day_vote', alive=[0, 2, 3, 4, 5],
                dead={1: 'seer', 6: 'villager'}, dead_cause={1: 'vote', 6: 'night_kill'},
                votes=[(1, {0: 1, 1: 0, 2: 4, 3: 1, 4: 1, 5: 0, 6: 4}, 1)])


def test_free_and_gated_planners_share_particles_but_not_the_switch_gate(monkeypatch):
    calls, first_seed = [], []
    def trial(v, world, styles, seed, action, memory, forecast=None):
        calls.append((world, dict(styles), seed, action))
        if not first_seed:
            first_seed.append(seed)
        return int(action == 4 and seed == first_seed[0])
    monkeypatch.setattr(model, 'terminal_trial', trial)
    v = view()
    worlds = model.distribution(v)
    gated, free = model.PlanningPolicy(), FreePlanningPolicy()
    assert gated.plan(v, worlds, [0, 2, 3, 4, 7], 0, 'matched') == 0
    assert free.plan(v, worlds, [0, 2, 3, 4, 7], 0, 'matched') == 4
    assert calls[:80] == calls[80:]
    assert free.planning_trace[0]['model_advantage'] == pytest.approx(1 / 16)


def test_equal_modeled_outcomes_retain_reference_not_arbitrary_seat(monkeypatch):
    monkeypatch.setattr(model, 'terminal_trial', lambda *args, **kwargs: 0)
    v = view()
    assert FreePlanningPolicy().plan(v, model.distribution(v), [0, 2, 3, 4, 7], 3, 'tie') == 3


def test_only_model_action_selection_changes_and_fresh_resets():
    for name in ('act', '_day_vote', '_wolf_vote', '_wolf_kill', '_investigate', 'chat'):
        assert getattr(FreePlanningPolicy, name) is getattr(model.PlanningPolicy, name)
    agent = rollout_free()
    old = agent.ww
    agent._fresh()
    assert isinstance(agent.ww, FreePlanningPolicy) and agent.ww is not old
