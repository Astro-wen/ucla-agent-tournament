from io import StringIO

from local.werewolf_benchmark import Scenario
from local.werewolf_evolution_research import BASE, validate
from scripts.werewolf_robust_vote_eval import Evaluator, canonical_programs, genome, ranking


def test_canonical_space_has_every_distinct_one_predicate_tree():
    programs=canonical_programs()
    assert len(programs)==len(set(programs))==1464
    assert all(p==0 or a!=b for p,a,b in programs)
    for genes in programs:
        full=genome(genes)
        assert full[:9]==BASE[:9] and full[12:]==BASE[12:]
        assert validate(full)==full


def test_robust_objective_rejects_one_timing_only_gain(monkeypatch):
    evaluator=Evaluator(StringIO(),float('inf'))
    cases=[Scenario(517,i,b,1) for b in ('first','random') for i in range(4)]
    def fake(genes,case,timing):
        baseline=case.game>=2
        if genes==(0,0,0):
            win=baseline
        elif genes==(0,1,1):
            win=timing=='rt'
        else:
            win=case.game>=1
        return {'role':'wolf','win':win}
    monkeypatch.setattr(evaluator,'play',fake)
    base=evaluator.score((0,0,0),cases)
    fragile=evaluator.score((0,1,1),cases)
    robust=evaluator.score((0,2,2),cases)
    assert base['fitness']==0
    assert fragile['worst_timing_delta']==-.5 and fragile['fitness']<0
    assert robust['worst_timing_delta']==.25 and robust['fitness']==.25
    assert sorted([base,fragile,robust],key=ranking)==[robust,base,fragile]
