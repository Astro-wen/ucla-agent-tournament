from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime
import gzip
import importlib.metadata
import json
import os
from pathlib import Path
import random
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for name in ('ARENA_RECORD', 'ARENA_STATE', 'ARENA_SUMMARY'):
    os.environ[name] = '0'

from local import eval_werewolf as ev
from local import werewolf_benchmark as wb
from local import werewolf_evolution_research as evo
from local.werewolf_counterfactual_research import SETTINGS, digest
from local.werewolf_env import deal_roles
from local.werewolf_research import ResearchGame, PairCounts
from scripts.werewolf_counterfactual_eval import resources, emit, save, sha


PLAN = ROOT / 'data/werewolf_until8_plan.json'
REPORT = ROOT / 'data/werewolf_evolution_results.json'
OUT = ROOT / 'logs/werewolf_benchmarks/evolution-501-504'


def case_pool(seed, backgrounds, quota):
    pools = {role: [] for role in ev.ROLES}
    scan = []
    for background in backgrounds:
        counts = Counter()
        for index in range(1500):
            game_seed, ours, _ = ev.deal(seed, index, background, 1)
            role = deal_roles(random.Random(game_seed))[ours[0]]
            accepted = counts[role] < quota
            scan.append({'background': background, 'game': index, 'role': role, 'accepted': accepted})
            if accepted:
                pools[role].append(wb.Scenario(seed, index, background, 1))
                counts[role] += 1
            if all(counts[r] == quota for r in ev.ROLES):
                break
        else:
            raise ValueError('role quotas not reached within fixed scan bound')
    return pools, scan


class Evaluator:
    def __init__(self, stream, deadline):
        self.stream, self.deadline = stream, deadline
        self.cache = {}
        self.calls = self.cache_hits = 0

    def play(self, genes, role, scenario):
        resources(self.deadline)
        key = role, tuple(genes), scenario.key
        if key in self.cache:
            self.cache_hits += 1
            return self.cache[key]
        genome = evo.install_role(evo.BASE, role, genes)
        factory = ev.factory('agent') if genome == evo.BASE else lambda: evo.EvolutionContestant(genome)
        seed, ours, kinds = ev.deal(scenario.seed, scenario.game, scenario.background, 1)
        me = ours[0]
        with wb.environment(SETTINGS):
            seat = wb.MeasuredSeat(factory, {})
            game = ResearchGame([seat if s == me else ev.make_seat(kinds[s]) for s in range(7)], random.Random(seed), strict_messaging=True)
            returns = game.play()
        if game.roles[me] != role:
            raise ValueError('role-conditioned evaluation mismatch')
        errors = {k: sum(game.stats[k]) for k in ev.FAIL_KEYS if k != 'window_close'}
        errors.update(policy_errors=seat.policy_errors, policy_fallbacks=seat.policy_fallbacks,
                      logged_errors=seat.logged_errors, fallbacks=seat.agent.fallbacks, overruns=seat.agent.overruns,
                      listen_errors=seat.agent.listen_stats.get('errors', 0))
        row = {'id': scenario.key, 'scenario': asdict(scenario), 'role': role, 'seat': me, 'deal_seed': seed,
               'genes': list(genes), 'genome': list(genome), 'win': returns[me] > 0, 'winner': game.winner,
               'days': game.day, 'errors': errors, 'max_ms': game.stats['max_ms'][me],
               'trace_hash': digest({'roles': game.roles, 'dead': game.dead, 'votes': game.votes,
                                     'messages': game.messages, 'actions': game.action_trace})}
        emit(self.stream, row)
        self.calls += 1
        if any(errors.values()):
            raise ValueError(f'invalid program game:{errors}')
        self.cache[key] = row
        return row

    def evaluate(self, genes, role, cases):
        baseline = (0,) * len(genes)
        total, groups = PairCounts(), defaultdict(PairCounts)
        max_ms = 0
        for scenario in cases:
            ref = self.play(baseline, role, scenario)
            row = self.play(genes, role, scenario)
            total.add(row['win'], ref['win'])
            groups[scenario.background].add(row['win'], ref['win'])
            max_ms = max(max_ms, row['max_ms'])
        loss = sum(max(0, -g.report()['delta']) for g in groups.values()) / len(groups)
        return {'genes': list(genes), 'fitness': total.report()['delta'] - .25 * loss,
                'all': total.report(), 'backgrounds': {k: v.report() for k, v in groups.items()},
                'complexity': sum(x != 0 for i, x in enumerate(genes) if i % 3), 'max_ms': max_ms}


def ranking(result):
    return (-result['fitness'], -result['all']['delta'], result['complexity'], tuple(result['genes']))


def initial_population(role, rng):
    width = 3 * len(evo.ROLE_PHASES[role])
    population = {(0,) * width}
    for selector in range(1, 12):
        population.add(tuple(x for _ in evo.ROLE_PHASES[role] for x in (0, selector, selector)))
    while len(population) < 24:
        population.add(tuple(rng.randrange(12) for _ in range(width)))
    return sorted(population)


def next_population(elites, rng):
    population = {tuple(r['genes']) for r in elites}
    width = len(elites[0]['genes'])
    while len(population) < 24:
        if len(population) >= 22:
            child = [rng.randrange(12) for _ in range(width)]
        elif rng.random() < .25:
            a, b = rng.sample(elites, 2)
            child = [a['genes'][i] if rng.random() < .5 else b['genes'][i] for i in range(width)]
        else:
            child = list(rng.choice(elites)['genes'])
            for i in rng.sample(range(width), rng.choice((1, 1, 2))):
                child[i] = rng.randrange(12)
        population.add(tuple(child))
    return sorted(population)


def main():
    if REPORT.exists() or OUT.exists() or evo.MODEL_PATH.exists():
        raise ValueError('fresh evolution artifacts/model required')
    plan = json.loads(PLAN.read_text())
    deadline = datetime.fromisoformat(plan['window']['stop_new_expensive_jobs_by']).timestamp()
    resources(deadline)
    baseline = json.loads((ROOT / 'data/werewolf_current_baseline.json').read_text())
    backgrounds = baseline['protocol']['backgrounds'][:20]
    if any(sha(ROOT / p) != value for p, value in baseline['checked_werewolf_and_shared_runtime_files'].items()):
        raise ValueError('B0 changed')
    OUT.mkdir()
    source = wb.source_manifest()
    train, train_scan = case_pool(501, backgrounds, 12)
    selection, selection_scan = case_pool(502, backgrounds, 12)
    result = {'status': 'training', 'plan': plan, 'source_begin': source, 'train_role_quotas': {r: len(v) for r, v in train.items()},
              'selection_role_quotas': {r: len(v) for r, v in selection.items()}, 'train_scan': train_scan,
              'selection_scan': selection_scan, 'generations': {}, 'selected_roles': {},
              'environment': {k: os.environ.get(k) for k in ('PYTHONPATH', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD', 'PYTHONDONTWRITEBYTECODE')},
              'packages': {p: importlib.metadata.version(p) for p in ('httpx', 'mcp', 'pytest', 'anyio')},
              'started': datetime.now().astimezone().isoformat(), 'heldout_seeds_used': [], 'promoted': False}
    save(OUT / 'manifest.json', result)
    save(REPORT, result)
    best_genome = evo.BASE
    try:
        with gzip.open(OUT / 'fitness-games.jsonl.gz', 'wt', compresslevel=3) as stream:
            evaluator = Evaluator(stream, deadline)
            for role in ev.ROLES:
                rng = random.Random('fixed-terminal-evolution-501:' + role)
                population = initial_population(role, rng)
                history = []
                for generation in range(14):
                    resources(deadline)
                    evaluated = sorted([evaluator.evaluate(g, role, train[role]) for g in population], key=ranking)
                    elites = evaluated[:6]
                    compact = [{k: row[k] for k in ('genes', 'fitness', 'all', 'complexity', 'max_ms')} for row in evaluated]
                    history.append({'generation': generation, 'population': compact, 'best': elites[0],
                                    'unique_fitness_games_so_far': evaluator.calls, 'cache_hits': evaluator.cache_hits})
                    result['generations'][role] = history
                    print('GENERATION', role, generation, elites[0]['all']['delta'], elites[0]['genes'],
                          'games', evaluator.calls, datetime.now().astimezone().isoformat(), flush=True)
                    save(REPORT, result)
                    if generation < 13:
                        population = next_population(elites, rng)
                programs = {tuple(e['genes']) for e in elites} | {(0,) * len(elites[0]['genes'])}
                selected = sorted([evaluator.evaluate(g, role, selection[role]) for g in sorted(programs)], key=ranking)
                winner = selected[0]
                chosen = tuple(winner['genes']) if winner['all']['delta'] > 0 else (0,) * len(winner['genes'])
                best_genome = evo.install_role(best_genome, role, chosen)
                result['selected_roles'][role] = {'all_candidates': selected, 'chosen': list(chosen),
                                                'selection_delta': winner['all']['delta'] if any(chosen) else 0}
                print('SELECTION', role, chosen, result['selected_roles'][role]['selection_delta'], flush=True)
                save(REPORT, result)
            result['fitness_games'] = evaluator.calls
            result['cache_hits'] = evaluator.cache_hits
        result['frozen_genome'] = list(best_genome)
        with evo.MODEL_PATH.open('x') as stream:
            json.dump({'schema': 1, 'genome': list(best_genome)}, stream)
        result['model_sha256'] = sha(evo.MODEL_PATH)
        save(REPORT, result)
        if best_genome != evo.BASE:
            output = ROOT / 'logs/werewolf_benchmarks/evolution-replication-503-504'
            resources(deadline)
            command = [sys.executable, '-m', 'local.werewolf_research', '--profile', 'reference=agent',
                       '--profile', 'candidate=local.werewolf_evolution_research:trained', '--seed-registry', 'evolution',
                       '--split', 'development', '--seeds', '503', '504', '--games', '200',
                       '--backgrounds', ','.join(baseline['protocol']['backgrounds']), '--timing', 'rt',
                       '--visibility', 'strict', '--jobs', '1', '--trace-games', '2', '--out', str(output),
                       '--stop-at', plan['window']['stop_new_expensive_jobs_by']]
            process = subprocess.run(command, cwd=ROOT)
            replication = json.loads((output / 'summary.json').read_text())
            result['replication'] = {'directory': str(output.relative_to(ROOT)), 'returncode': process.returncode,
                                     'metrics': replication['metrics'], 'gates': replication['gates']}
            if not all(replication['gates'].values()):
                raise ValueError('replication integrity failed')
        else:
            result['replication'] = {'status': 'not_run', 'reason': 'no positive role candidate selected'}
        end = wb.source_manifest()
        if end['digest'] != source['digest'] or sha(evo.MODEL_PATH) != result['model_sha256']:
            raise ValueError('core source or fixed model changed')
        result.update(status='complete_development_only', source_end=end)
    except Exception as exc:
        result.update(status='blocked_partial', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        result['ended'] = datetime.now().astimezone().isoformat()
        save(REPORT, result)
        save(OUT / 'summary.json', result)
    print('EVOLUTION DONE', result['frozen_genome'], result['replication'].get('metrics', {}).get('candidate', {}).get('all'), flush=True)


if __name__ == '__main__':
    main()
