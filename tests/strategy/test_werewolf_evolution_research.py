from copy import deepcopy
from dataclasses import replace
import json
import random

import pytest

from agent.werewolf.policy import WerewolfPolicy, chat_guard
from agent.werewolf.state import View
from local import werewolf_evolution_research as evo
from local.werewolf_counterfactual_research import SETTINGS
from local.werewolf_benchmark import Scenario
from local.werewolf_env import ContestantSeat, OfficialBaseline
from local.werewolf_research import ResearchGame, run_task


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for name, value in SETTINGS.items():
        monkeypatch.setenv(name, value)


def genomes():
    rng = random.Random(501)
    return [evo.BASE] + [tuple(rng.randrange(12) for _ in range(15)) for _ in range(40)]


@pytest.mark.parametrize('genome', genomes())
def test_every_program_keeps_private_wolf_clear_unread_and_legal_guards(genome):
    p = evo.EvolutionPolicy(genome)
    g = ResearchGame([OfficialBaseline() for _ in range(7)], random.Random(501))
    g.roles = ['seer', 'wolf', 'villager', 'villager', 'wolf', 'villager', 'villager']
    g.invest = {1: 'wolf', 2: 'villager'}
    raw = g.state(0, 'moving', 'day_vote', [1, 2, 3, 4, 5, 6, 7])
    assert p.act(raw)['action_id'] == '1'
    g.invest = {2: 'villager'}
    raw = g.state(0, 'moving', 'day_vote', [1, 2, 3, 4, 5, 6, 7])
    assert p.act(raw)['action_id'] in {'1', '3', '4', '5', '6'}
    night = g.state(0, 'moving', 'night_seer', [1, 2, 3, 4, 5, 6])
    assert p.act(night)['action_id'] != '2'
    wolf = g.state(1, 'moving', 'night_wolf', [0, 2, 3, 5, 6])
    assert p.act(wolf)['action_id'] in {'0', '2', '3', '5', '6'}
    poisoned = deepcopy(raw)
    poisoned.update(hidden_roles=['wolf'] * 7, seed=9, opponent_types=['first'] * 7)
    assert evo.EvolutionPolicy(genome).act(raw) == evo.EvolutionPolicy(genome).act(poisoned)


@pytest.mark.parametrize('mode', ['off', 'no_skip', 'public_vote'])
def test_other_role_genes_do_not_leak_into_wolf_public_view(mode, monkeypatch):
    monkeypatch.setenv('ARENA_WW_TACTICS', mode)
    v = View(me=3, role='wolf', day=1, sub_phase='day_vote', alive=list(range(7)), dead={}, dead_cause={}, allies=[0])
    base = evo.EvolutionPolicy()
    for genome in genomes():
        only_good = evo.install_role(evo.install_role(evo.BASE, 'villager', evo.role_key(genome, 'villager')),
                                     'seer', evo.role_key(genome, 'seer'))
        p = evo.EvolutionPolicy(only_good)
        assert p._day_vote(v, v.others_alive) == base._day_vote(v, v.others_alive)
        assert p._opening(v) == base._opening(v)
        assert not p._wolf_context


@pytest.mark.parametrize('background', ['first', 'random', 'llmq', 'rt_talker', 'guard_fake', 'pop_coordinated'])
def test_allbase_program_is_exact_b0_mirror(background):
    rows = run_task((Scenario(501, 0, background, 1), [('reference', 'agent', {}),
                     ('mirror', 'local.werewolf_evolution_research:mirror', {})], SETTINGS, True, True))
    assert not any('harness_error' in r for r in rows)
    for key in ('win', 'role', 'seat', 'trace', 'failures'):
        assert rows[0][key] == rows[1][key]


@pytest.mark.parametrize('genome', genomes()[:12])
def test_program_output_chat_keeps_existing_guard(genome):
    g = ResearchGame([OfficialBaseline() for _ in range(7)], random.Random(501))
    g.roles = ['seer', 'wolf', 'villager', 'villager', 'wolf', 'villager', 'villager']
    g.invest = {1: 'wolf'}
    for seat in range(7):
        raw = g.state(seat, 'messaging', 'day_vote')
        p = evo.EvolutionPolicy(genome)
        v = p._view(raw)
        for topic, msg in p._plan(v):
            accepted = chat_guard(msg, v, topic)
            if accepted:
                assert len(accepted['content'].split()) <= 50 and len(accepted['content'].encode()) <= 1024
                assert not accepted['recipients']


def test_role_blocks_and_model_validation(tmp_path):
    model = tuple(range(12)) + (0, 1, 2)
    assert evo.install_role(evo.BASE, 'seer', evo.role_key(model, 'seer'))[3:9] == model[3:9]
    path = tmp_path / 'model.json'
    path.write_text(json.dumps({'schema': 1, 'genome': model}))
    assert evo.load_genome(path) == model
    for bad in ([0] * 14, [True] * 15, [12] * 15):
        with pytest.raises(ValueError):
            evo.EvolutionPolicy(bad)
    path.write_text(json.dumps({'schema': 1, 'genome': model, 'seed': 7}))
    with pytest.raises(ValueError):
        evo.load_genome(path)


def test_fresh_preserves_program_without_match_memory():
    genome = genomes()[1]
    agent = evo.EvolutionContestant(genome)
    before = agent.ww
    before._topics.add('claim')
    agent._fresh()
    assert agent.ww is not before and agent.ww.genome == genome and not agent.ww._topics
    seat = ContestantSeat(lambda: evo.EvolutionContestant(genome))
    seat.reset()
    old = seat.agent
    seat.reset()
    assert seat.agent is not old


@pytest.mark.parametrize('seat,role', [(0, 'seer'), (1, 'wolf'), (2, 'villager')])
def test_role_cache_factorization_holds_for_complete_game_traces(seat, role):
    def simulate(genome):
        g = ResearchGame([ContestantSeat(lambda: evo.EvolutionContestant(genome)) if s == seat else OfficialBaseline()
                          for s in range(7)], random.Random(501))
        g.roles = ['seer', 'wolf', 'villager', 'villager', 'wolf', 'villager', 'villager']
        returns = g.play()
        return returns, g.votes, g.dead, g.messages, g.action_trace
    original = genomes()[2]
    changed = original
    for other in evo.ROLE_PHASES:
        if other != role:
            changed = evo.install_role(changed, other, evo.role_key(genomes()[3], other))
    assert simulate(original) == simulate(changed)


def test_population_generation_and_selection_are_deterministic():
    from scripts.werewolf_evolution_eval import initial_population, next_population, ranking, case_pool
    for role in evo.ROLE_PHASES:
        population = initial_population(role, random.Random(501))
        assert len(population) == len(set(population)) == 24
        assert population == initial_population(role, random.Random(501))
        elites = [{'genes': list(g)} for g in population[:6]]
        nxt = next_population(elites, random.Random(501))
        assert len(nxt) == len(set(nxt)) == 24 and set(population[:6]) <= set(nxt)
        for genes in nxt:
            evo.validate(evo.install_role(evo.BASE, role, genes))
    a = {'fitness': 0, 'all': {'delta': 0}, 'complexity': 0, 'genes': [0, 0, 0]}
    b = {'fitness': 0, 'all': {'delta': 0}, 'complexity': 1, 'genes': [0, 1, 0]}
    assert ranking(a) < ranking(b)
    pools, scan = case_pool(501, ['first'], 2)
    assert all(len(v) == 2 for v in pools.values()) and sum(r['accepted'] for r in scan) == 6


def test_private_authenticated_report_and_doomed_ally_not_removed():
    v = View(me=5, role='villager', day=2, sub_phase='day_vote', alive=[0, 2, 3, 4, 5],
             dead={1: 'seer', 6: 'villager'}, dead_cause={1: 'vote', 6: 'night_kill'},
             messages=[{'seq': 0, 'day': 1, 'sender': 1, 'content': 'I am the Seer. Player0 is a WOLF.', 'recipients': []}])
    for genome in genomes():
        p = evo.EvolutionPolicy(genome)
        assert p._day_vote(v, v.others_alive) == WerewolfPolicy()._day_vote(v, v.others_alive)
        wolf = replace(v, me=3, role='wolf', allies=[0])
        assert p._wolf_vote(wolf, wolf.others_alive) == WerewolfPolicy()._wolf_vote(wolf, wolf.others_alive)
