import pytest

from local import werewolf_evolution_ablation as ablation
from local.werewolf_evolution_research import BASE
from scripts.werewolf_evolution_confirm import select


def test_ablation_factories_change_only_the_declared_components(monkeypatch):
    model = (0,) * 9 + (5, 2, 4, 10, 2, 10)
    monkeypatch.setattr(ablation, 'frozen_genome', lambda: model)
    assert ablation.vote_only().ww.genome == BASE[:9] + model[9:12] + BASE[12:]
    assert ablation.night_only().ww.genome == BASE[:12] + model[12:]
    assert ablation.combined().ww.genome == model
    for factory in (ablation.vote_only, ablation.night_only, ablation.combined):
        agent = factory()
        expected = agent.ww.genome
        agent._fresh()
        assert agent.ww.genome == expected


@pytest.mark.parametrize('deltas,expected', [((.024, .005, .026), 'vote_only'), ((.020, .005, .026), 'combined'),
                                              ((0, -.01, -.02), None), ((.01, .01, .01), 'vote_only')])
def test_parsimony_selection_is_fixed_before_confirmation(deltas, expected):
    metrics = {name: {'all': {'seat': {'delta': delta}}} for name, delta in zip(('vote_only', 'night_only', 'combined'), deltas)}
    assert select(metrics) == expected
