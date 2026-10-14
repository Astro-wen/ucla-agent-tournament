from collections import Counter, defaultdict
from datetime import datetime
import gzip
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from local import werewolf_benchmark as wb
from local.werewolf_research import PairCounts
from scripts.werewolf_planning_eval import play
from scripts.werewolf_counterfactual_eval import emit, resources, save


REPORT = ROOT / 'data/werewolf_planning_free_results.json'


def main():
    if REPORT.exists():
        raise ValueError('new follow-up report required')
    original = json.loads((ROOT / 'data/werewolf_planning_results.json').read_text())
    if original['status'] != 'complete_development_only':
        raise ValueError('original experiment must be complete first')
    plan = json.loads((ROOT / 'data/werewolf_planning_free_plan.json').read_text())
    baseline = json.loads((ROOT / 'data/werewolf_current_baseline.json').read_text())
    deadline = datetime.fromisoformat(plan['resources']['deadline']).timestamp()
    resources(deadline)
    source = wb.source_manifest()
    if any(source['files'].get(name) != expected for name, expected in baseline['checked_werewolf_and_shared_runtime_files'].items()):
        raise ValueError('B0 changed')
    report = {'status': 'running', 'source': source, 'plan': plan, 'stages': {},
              'environment': original['environment'], 'packages': original['packages']}
    save(REPORT, report)
    try:
        for seed, games in ((274, 50), (275, 100)):
            out = ROOT / f'logs/werewolf_benchmarks/planning-free-{seed}-rt'
            if out.exists():
                raise ValueError('new stage directory required')
            out.mkdir()
            profiles = [('reference', 'agent'), ('rollout_free', 'local.werewolf_planning_free_research:rollout_free')]
            if seed == 274:
                profiles.insert(1, ('rollout_prior', 'local.werewolf_planning_research:rollout_prior'))
            save(out / 'manifest.json', {'source': source, 'profiles': profiles, 'seed': seed, 'timing': 'rt',
                                         'backgrounds': baseline['protocol']['backgrounds'], 'games_per_background': games})
            counts, planning, predictions, errors = defaultdict(PairCounts), defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
            maxima, completed = defaultdict(float), 0
            with gzip.open(out / 'games.jsonl.gz', 'wt', compresslevel=3) as stream:
                for background in baseline['protocol']['backgrounds']:
                    for game in range(games):
                        resources(deadline)
                        scenario = wb.Scenario(seed, game, background, 1)
                        rows = []
                        for name, factory in profiles:
                            resources(deadline)
                            row = play(scenario, name, factory, 'rt', trace=game < 2)
                            emit(stream, row)
                            rows.append(row)
                            planning[name].update(row['planning'])
                            predictions[name].update(row['prediction'])
                            errors[name].update({k: v for k, v in row['failures'].items() if k != 'window_close'})
                            errors[name].update({'background_' + k: v for k, v in row['background_failures'].items() if k != 'window_close'})
                            maxima[name] = max(maxima[name], row['max_ms'])
                        ref = rows[0]
                        for row in rows[1:]:
                            if any(row[k] != ref[k] for k in ('id', 'scenario', 'role', 'seat', 'deal_seed')):
                                raise ValueError('follow-up pair mismatch')
                            if ref['role'] == 'wolf' and row['trace_hash'] != ref['trace_hash']:
                                raise ValueError('wolf behavior changed')
                            for scope in ('all', 'opponent:' + background, 'seed:' + str(seed)):
                                for role in ('seat', ref['role']):
                                    counts[row['profile'], scope, role].add(row['win'], ref['win'])
                        completed += 1
                        if any(any(v.values()) for v in errors.values()):
                            raise ValueError('hypothetical or outer game failure')
                    print('FREE STAGE', seed, background, completed, flush=True)
            metrics = {}
            for (name, scope, role), value in counts.items():
                metrics.setdefault(name, {}).setdefault(scope, {})[role] = value.report()
            end = wb.source_manifest()
            stage = {'directory': str(out.relative_to(ROOT)), 'seed': seed, 'scenarios': completed,
                     'outer_games': completed * len(profiles), 'complete': completed == games * 28,
                     'source_unchanged': source['digest'] == end['digest'], 'metrics': metrics,
                     'planning': {k: dict(v) for k, v in planning.items()},
                     'prediction': {k: dict(v) for k, v in predictions.items()}, 'errors': {k: dict(v) for k, v in errors.items()},
                     'max_ms': dict(maxima)}
            save(out / 'summary.json', stage)
            report['stages'][str(seed)] = stage
            save(REPORT, report)
            if not stage['complete'] or not stage['source_unchanged']:
                raise ValueError('follow-up incomplete or source changed')
            print('FREE RESULT', seed, {k: v['all']['seat'] for k, v in metrics.items()}, flush=True)
        report.update(status='complete_development_only', heldout_seeds_used=[], promoted=False)
    except Exception as exc:
        report.update(status='blocked_partial', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        report['ended'] = datetime.now().astimezone().isoformat()
        save(REPORT, report)
    print('FREE DONE', report['status'], flush=True)


if __name__ == '__main__':
    main()
