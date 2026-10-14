from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import gzip
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for name in ('ARENA_RECORD', 'ARENA_STATE', 'ARENA_SUMMARY'):
    os.environ[name] = '0'

from local import werewolf_counterfactual_research as cf
from local import werewolf_benchmark as wb
from local.werewolf_research import PairCounts
from scripts.audit_werewolf_counterfactual import decode, read_rows
from scripts.werewolf_counterfactual_eval import save, resources, emit, sha


REPORT = ROOT / 'data/werewolf_initial_value_results.json'
OUT = ROOT / 'logs/werewolf_benchmarks/initial-value-251-252'
PLAN = ROOT / 'data/werewolf_initial_value_plan.json'


def select_rank(metrics):
    leader = max(range(6), key=lambda rank: (metrics[str(rank)]['all']['delta'], rank))
    selected = leader if all(metrics[str(leader)]['seed:' + str(s)]['delta'] > 0 for s in (251, 252)) else 5
    return leader, selected


def main():
    if REPORT.exists() or OUT.exists():
        raise ValueError('new immutable experiment output required')
    plan = json.loads(PLAN.read_text())
    deadline = datetime.fromisoformat(plan['resources']['stop_at']).timestamp()
    resources(deadline)
    diagnostic = decode(json.loads((ROOT / 'data/werewolf_counterfactual_diagnostics.json').read_text()))
    if diagnostic['status'] != 'complete_diagnostic_only':
        raise ValueError('requires completed calibration')
    baseline = json.loads((ROOT / 'data/werewolf_current_baseline.json').read_text())
    if any(sha(ROOT / name) != expected for name, expected in baseline['checked_werewolf_and_shared_runtime_files'].items()):
        raise ValueError('B0 source changed')
    rows = [r for r in read_rows(Path(diagnostic['out']) / 'calibration.jsonl.gz') if r['profile'] == 'b0' and r['role'] == 'seer']
    OUT.mkdir()
    source = wb.source_manifest()
    result = {'status': 'training', 'plan': plan, 'plan_sha256': sha(PLAN), 'source_begin': source,
              'script_sha256': sha(Path(__file__)), 'started': datetime.now().astimezone().isoformat(),
              'environment': {k: os.environ.get(k) for k in ('PYTHONPATH', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD', 'PYTHONDONTWRITEBYTECODE')},
              'packages': {p: importlib.metadata.version(p) for p in ('httpx', 'mcp', 'pytest', 'anyio')},
              'training_cases': len(rows), 'training_b0_wins': sum(r['win'] for r in rows),
              'training_b0_losses': sum(not r['win'] for r in rows), 'local_only': True, 'replication': {}}
    save(OUT / 'manifest.json', result)
    save(REPORT, result)
    stats = defaultdict(PairCounts)
    simulations = 0
    try:
        with gzip.open(OUT / 'training.jsonl.gz', 'wt', compresslevel=3) as stream:
            for index, record in enumerate(rows):
                resources(deadline)
                scenario = wb.Scenario(**record['scenario'])
                base, points = cf.play(scenario, collect=True)
                simulations += 1
                if any(base[k] != record[k] for k in ('trace_hash', 'event_hash', 'win', 'role', 'seat', 'deal_seed')):
                    raise ValueError('training baseline replay mismatch')
                point = points[0]
                if point['day'] != 1 or point['phase'] != 'night_seer' or len(point['legal']) != 6:
                    raise ValueError('not an initial six-action seer point')
                legal = sorted(point['legal'])
                if point['proposed'] != legal[5]:
                    raise ValueError('high-rank action not a B0 mirror')
                for rank, action in enumerate(legal):
                    forced = cf.Intervention(point['index'], action, point['evaluator_prefix'], point['input_hash'], point['proposed'])
                    outcome, _ = cf.play(scenario, intervention=forced)
                    simulations += 1
                    if rank == 5 and any(outcome[k] != base[k] for k in ('win', 'trace_hash', 'event_hash')):
                        raise ValueError('rank5 full replay is not B0')
                    for scope in ('all', 'seed:' + str(scenario.seed), 'background:' + scenario.background,
                                  'seat:' + str(record['seat'])):
                        stats[rank, scope].add(outcome['win'], base['win'])
                    emit(stream, {'case': record['id'], 'rank': rank, 'action': action, 'baseline_win': base['win'],
                                  'outcome': outcome, 'visible_first_input': point['input_hash']})
                if (index + 1) % 50 == 0:
                    print('TRAIN', index + 1, '/', len(rows), flush=True)
        metrics = {str(rank): {scope: value.report() for (r, scope), value in stats.items() if r == rank} for rank in range(6)}
        leader, selected = select_rank(metrics)
        result.update(training=metrics, training_simulations=simulations, selected_rank=selected,
                      training_leader=leader, selection_rule=plan['training']['selection'])
        save(REPORT, result)
        print('TRAINING RESULT', {rank: cells['all'] for rank, cells in metrics.items()}, 'selected', selected, flush=True)
        if selected != 5:
            bgs = ','.join(baseline['protocol']['backgrounds'])
            out = ROOT / 'logs/werewolf_benchmarks/initial-value-replication-253-254'
            resources(deadline)
            command = [sys.executable, '-m', 'local.werewolf_research', '--profile', 'reference=agent',
                       '--profile', f'candidate=local.werewolf_initial_value_research:rank{selected}',
                       '--seed-registry', 'causal', '--split', 'development', '--seeds', '253', '254',
                       '--games', '200', '--backgrounds', bgs, '--timing', 'rt', '--visibility', 'strict',
                       '--jobs', '1', '--trace-games', '2', '--out', str(out), '--stop-at', plan['resources']['stop_at']]
            process = subprocess.run(command, cwd=ROOT)
            report = json.loads((out / 'summary.json').read_text())
            result['replication'] = {'directory': str(out.relative_to(ROOT)), 'returncode': process.returncode,
                                     'gates': report['gates'], 'metrics': report['metrics']}
            if not all(report['gates'].values()):
                raise ValueError('replication integrity failed')
        else:
            result['replication'] = {'status': 'not_run', 'reason': 'B0 high-rank retained by predeclared selection'}
        result['source_end'] = wb.source_manifest()
        if result['source_end']['digest'] != source['digest'] or sha(Path(__file__)) != result['script_sha256']:
            raise ValueError('source changed during training/evaluation')
        result.update(status='complete_exploratory_only', confirmed_stronger=False, promoted=False,
                      heldout_seeds_used=[], unseen_opponent_family_tested=False)
    except Exception as exc:
        result.update(status='blocked_partial', error=f'{type(exc).__name__}: {exc}', training_simulations=simulations)
        raise
    finally:
        result['ended'] = datetime.now().astimezone().isoformat()
        if (OUT / 'training.jsonl.gz').exists():
            result['raw_sha256'] = hashlib.sha256((OUT / 'training.jsonl.gz').read_bytes()).hexdigest()
        save(REPORT, result)
        save(OUT / 'summary.json', result)
    print('DONE', result['status'], result['selected_rank'], flush=True)


if __name__ == '__main__':
    main()
