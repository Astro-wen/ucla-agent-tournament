from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime
import gzip
import importlib.metadata
import json
import math
import os
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for name in ('ARENA_RECORD', 'ARENA_STATE', 'ARENA_SUMMARY'):
    os.environ[name] = '0'

from local import eval_werewolf as ev
from local import werewolf_benchmark as wb
from local.werewolf_counterfactual_research import SETTINGS, digest
from local.werewolf_planning_research import PARTICLES
from local.werewolf_research import ResearchGame, PairCounts
from scripts.werewolf_counterfactual_eval import emit, save, resources


PLAN = ROOT / 'data/werewolf_planning_plan.json'
REPORT = ROOT / 'data/werewolf_planning_results.json'


def prediction_metrics(traces, votes):
    out = Counter()
    for trace in traces:
        actual = next((row['votes'] for row in votes if row['day'] == trace['day']), {})
        for voter, counts in trace['forecast'].items():
            if voter not in actual:
                raise ValueError('forecast voter absent from actual voting stage')
            legal = sorted((set(actual) - {voter}) | {7})
            target = 7 if actual[voter] is None else actual[voter]
            if sum(counts.values()) != PARTICLES or target not in legal or not set(counts) <= set(legal):
                raise ValueError('invalid sampled ballot distribution')
            probabilities = {s: .98 * counts.get(s, 0) / PARTICLES + .02 / len(legal) for s in legal}
            out['n'] += 1
            out['correct'] += max(legal, key=lambda s: (probabilities[s], -s)) == target
            out['logloss_sum'] -= math.log(probabilities[target])
            out['brier_sum'] += sum((probabilities[s] - (s == target)) ** 2 for s in legal)
    return dict(out)


def play(scenario, label, factory, timing='rt', trace=False):
    seed, ours, types = ev.deal(scenario.seed, scenario.game, scenario.background, 1)
    me = ours[0]
    with wb.environment(SETTINGS | {'ARENA_WW_TIMING': timing}):
        controlled = wb.MeasuredSeat(ev.factory(factory), {})
        game = ResearchGame([controlled if s == me else ev.make_seat(types[s]) for s in range(7)],
                            random.Random(seed), strict_messaging=True)
        returns = game.play()
    failures = {key: game.stats[key][me] for key in ev.FAIL_KEYS}
    failures.update(policy_errors=controlled.policy_errors, policy_fallbacks=controlled.policy_fallbacks,
                    logged_errors=controlled.logged_errors, fallbacks=controlled.agent.fallbacks,
                    overruns=controlled.agent.overruns, listen_errors=controlled.agent.listen_stats.get('errors', 0))
    model_trace = getattr(controlled.agent.ww, 'planning_trace', [])
    record = {'id': scenario.key, 'scenario': asdict(scenario), 'timing': timing, 'profile': label,
              'role': game.roles[me], 'seat': me, 'deal_seed': seed, 'win': returns[me] > 0,
              'winner': game.winner, 'days': game.day, 'failures': failures,
              'background_failures': {k: sum(game.stats[k][s] for s in types) for k in ev.FAIL_KEYS},
              'planning': dict(getattr(controlled.agent.ww, 'planning_stats', {})),
              'planning_trace': model_trace, 'prediction': prediction_metrics(model_trace, game.votes),
              'max_ms': game.stats['max_ms'][me], 'chats': game.stats['chats'][me],
              'trace_hash': digest({'roles': game.roles, 'votes': game.votes, 'dead': game.dead,
                                    'messages': game.messages, 'actions': game.action_trace})}
    if trace:
        record['trace'] = {'roles': game.roles, 'votes': game.votes, 'dead': game.dead,
                           'messages': game.messages, 'actions': game.action_trace}
    return record


def stage(seed, games, backgrounds, modes, timing, deadline, source):
    out = ROOT / f'logs/werewolf_benchmarks/planning-{seed}-{timing}'
    if out.exists():
        raise ValueError('new planning artifact directory required')
    out.mkdir()
    profiles = [('reference', 'agent')] + [(mode, f'local.werewolf_planning_research:{mode}') for mode in modes]
    manifest = {'source': source, 'seed': seed, 'games_per_background': games, 'backgrounds': backgrounds,
                'profiles': profiles, 'timing': timing, 'strict': True, 'particles': PARTICLES,
                'started': datetime.now().astimezone().isoformat()}
    save(out / 'manifest.json', manifest)
    counts, forecasts, planning, errors = defaultdict(PairCounts), defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    maxima = defaultdict(float)
    completed = total = 0
    with gzip.open(out / 'games.jsonl.gz', 'wt', compresslevel=3) as stream:
        for background in backgrounds:
            for index in range(games):
                resources(deadline)
                scenario = wb.Scenario(seed, index, background, 1)
                rows = []
                for label, factory in profiles:
                    resources(deadline)
                    row = play(scenario, label, factory, timing, trace=index < 2)
                    rows.append(row)
                    emit(stream, row)
                    forecasts[label].update(row['prediction'])
                    planning[label].update(row['planning'])
                    errors[label].update({k: v for k, v in row['failures'].items() if k != 'window_close'})
                    errors[label].update({'background_' + k: v for k, v in row['background_failures'].items() if k != 'window_close'})
                    maxima[label] = max(maxima[label], row['max_ms'])
                    total += 1
                base = rows[0]
                for row in rows[1:]:
                    if any(row[k] != base[k] for k in ('id', 'scenario', 'seat', 'role', 'deal_seed')):
                        raise ValueError('planning evaluation pairing mismatch')
                    if row['profile'] == 'mirror' and row['trace_hash'] != base['trace_hash']:
                        raise ValueError('mirror wrapper changed B0 game')
                    if row['role'] == 'wolf' and row['trace_hash'] != base['trace_hash']:
                        raise ValueError('good-role planner changed a wolf-controlled game')
                    for scope in ('all', 'opponent:' + background, 'seed:' + str(seed)):
                        for role in ('seat', row['role']):
                            counts[row['profile'], scope, role].add(row['win'], base['win'])
                completed += 1
                if any(any(c.values()) for c in errors.values()):
                    raise ValueError(f'rollout or outer policy failure:{dict(errors)}')
            print('STAGE', seed, timing, background, completed, 'model_games', sum(c['inner_games'] for c in planning.values()), flush=True)
    end = wb.source_manifest()
    metrics = {}
    for (label, scope, role), counter in counts.items():
        metrics.setdefault(label, {}).setdefault(scope, {})[role] = counter.report()
    result = {'directory': str(out.relative_to(ROOT)), 'complete': completed == games * len(backgrounds),
              'source_unchanged': source['digest'] == end['digest'], 'seed': seed, 'timing': timing,
              'scenarios': completed, 'outer_games': total, 'metrics': metrics,
              'planning': {k: dict(v) for k, v in planning.items()},
              'prediction': {k: dict(v) for k, v in forecasts.items()}, 'max_ms': dict(maxima),
              'errors': {k: dict(v) for k, v in errors.items()}, 'ended': datetime.now().astimezone().isoformat()}
    save(out / 'summary.json', result)
    if not result['complete'] or not result['source_unchanged']:
        raise ValueError('planning stage incomplete or source changed')
    print('RESULT', seed, timing, {k: v['all']['seat'] for k, v in metrics.items()}, flush=True)
    return result


def main():
    if REPORT.exists():
        raise ValueError('new planning report required')
    plan = json.loads(PLAN.read_text())
    deadline = datetime.fromisoformat(plan['resources']['deadline']).timestamp()
    resources(deadline)
    baseline = json.loads((ROOT / 'data/werewolf_current_baseline.json').read_text())
    import hashlib
    if any(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected
           for name, expected in baseline['checked_werewolf_and_shared_runtime_files'].items()):
        raise ValueError('B0 changed')
    source = wb.source_manifest()
    result = {'status': 'running', 'plan': plan, 'source': source, 'stages': {},
              'packages': {p: importlib.metadata.version(p) for p in ('httpx', 'mcp', 'pytest', 'anyio')},
              'environment': {k: os.environ.get(k) for k in ('PYTHONPATH', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD', 'PYTHONDONTWRITEBYTECODE')},
              'scope': 'new planner prototype, local scripted development; no deployment or live strength claim'}
    save(REPORT, result)
    try:
        backgrounds = baseline['protocol']['backgrounds']
        first = stage(271, 20, backgrounds, ('mirror', 'greedy', 'rollout_prior', 'rollout_observed'), 'rt', deadline, source)
        result['stages']['271-rt'] = first
        selected = max(('rollout_prior', 'rollout_observed'), key=lambda mode: first['metrics'][mode]['all']['seat']['delta'])
        result['selected'] = selected
        result['selection_is_positive'] = first['metrics'][selected]['all']['seat']['delta'] > 0
        save(REPORT, result)
        second = stage(272, 50, backgrounds, (selected,), 'rt', deadline, source)
        result['stages']['272-rt'] = second
        save(REPORT, result)
        if result['selection_is_positive'] and second['metrics'][selected]['all']['seat']['delta'] > 0:
            result['stages']['273-rounds'] = stage(273, 50, backgrounds, (selected,), 'rounds', deadline, source)
            save(REPORT, result)
            stress = ['first_switch', 'pop_anti_majority', 'mixed:first+random+llmq', 'mixed:heuristic+llmq+pop_coordinated']
            result['stages']['273-rt'] = stage(273, 100, stress, (selected,), 'rt', deadline, source)
        result.update(status='complete_development_only', heldouts_used=[], promoted=False)
    except Exception as exc:
        result.update(status='blocked_partial', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        result['ended'] = datetime.now().astimezone().isoformat()
        save(REPORT, result)
    print('DONE', result['status'], result['selected'], flush=True)


if __name__ == '__main__':
    main()
