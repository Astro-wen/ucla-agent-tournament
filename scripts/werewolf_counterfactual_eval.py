from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import gzip
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for name in ('ARENA_RECORD', 'ARENA_STATE', 'ARENA_SUMMARY'):
    os.environ[name] = '0'

from agent.matchstate import encode
from local import werewolf_benchmark as wb
from local import werewolf_counterfactual_research as cf


PLAN = ROOT / 'data/werewolf_counterfactual_plan.json'
REPORT = ROOT / 'data/werewolf_counterfactual_diagnostics.json'


def save(path, value):
    temporary = path.with_name(path.name + '.partial')
    temporary.write_text(json.dumps(encode(value), indent=2, allow_nan=False) + '\n')
    temporary.replace(path)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def emit(stream, row):
    stream.write(json.dumps(encode(row), separators=(',', ':'), allow_nan=False) + '\n')


def resources(deadline):
    if time.time() >= deadline:
        raise TimeoutError('predeclared local research stop reached')
    if shutil.disk_usage(ROOT).free < 2 * 1024 ** 3:
        raise RuntimeError('less than2GiB disk reserve')


def diagnostic_summary(cases, groups):
    result = {'cases': len(cases), 'by_role': {}, 'by_background': {}, 'by_information': {}}
    for key, values in [('all', cases)] + [(role, [c for c in cases if c['role'] == role]) for role in ('villager', 'seer')]:
        counts = {'n': len(values), 'rescued_any': sum(c['rescued_any'] for c in values),
                  'rescued_preserving_private_guards': sum(c['rescued_guarded'] for c in values),
                  'vote_rescue': sum(c['vote_rescue'] for c in values),
                  'investigation_rescue': sum(c['investigation_rescue'] for c in values),
                  'points': sum(c['points'] for c in values), 'branches': sum(c['branches'] for c in values)}
        if key == 'all':
            result['all'] = counts
        else:
            result['by_role'][key] = counts
    for background in sorted({c['background'] for c in cases}):
        rows = [c for c in cases if c['background'] == background]
        result['by_background'][background] = {'n': len(rows), 'rescued_any': sum(c['rescued_any'] for c in rows),
                                             'rescued_guarded': sum(c['rescued_guarded'] for c in rows)}
    for case in cases:
        for category, counts in case['information'].items():
            target = result['by_information'].setdefault(category, {'points': 0, 'rescuable_points': 0, 'guarded_rescuable_points': 0})
            for k, count in counts.items():
                target[k] += count
    shared = [rows for rows in groups.values() if len({r['case'] for r in rows}) > 1]
    all_rescuable = [rows for rows in shared if all(r['rescuing_actions'] for r in rows)]
    conflict = [rows for rows in all_rescuable if not set.intersection(*(set(r['rescuing_actions']) for r in rows))]
    result['visible_information_groups'] = {'groups': len(groups), 'shared_across_deals': len(shared),
                                          'all_states_individually_rescuable': len(all_rescuable),
                                          'no_single_action_rescues_every_state': len(conflict),
                                          'examples': conflict[:8]}
    result['limitations'] = ['Loss-conditioned balanced-role selection is not an overall win-rate estimate.',
                            'Best action chosen in hindsight over a fixed hidden deal is not an executable strategy.',
                            'No winning single-action branch does not prove no useful speech or multi-action strategy exists.',
                            'One common-seed continuation per action, not an estimated expected action value.',
                            'All background policies continue normally; later ballots are not held fixed.']
    return result


def run(out, deadline):
    if out.parent.resolve() != (ROOT / 'logs/werewolf_benchmarks').resolve() or out.exists() or REPORT.exists():
        raise ValueError('new dedicated output and report required')
    plan = json.loads(PLAN.read_text())
    baseline = json.loads((ROOT / plan['baseline']).read_text())
    if any(sha(ROOT / p) != h for p, h in baseline['checked_werewolf_and_shared_runtime_files'].items()):
        raise ValueError('B0 production sources changed')
    if plan['calibration']['seeds'] != [251, 252] or wb.SEED_REGISTRIES['causal'][1] != (39701, 39702):
        raise ValueError('predeclared seed registry mismatch')
    if deadline > datetime.fromisoformat(plan['resources']['deadline']).timestamp() or cf.SETTINGS != plan['settings']:
        raise ValueError('deadline or settings differ from predeclared bounds')
    resources(deadline)
    out.mkdir()
    source = wb.source_manifest()
    report = {'status': 'running', 'started': datetime.now().astimezone().isoformat(),
              'plan': plan, 'plan_sha256': sha(PLAN), 'baseline_source_matches': True,
              'python': sys.version, 'executable': sys.executable,
              'packages': {p: importlib.metadata.version(p) for p in ('httpx', 'mcp', 'pytest')},
              'source_begin': source, 'script_sha256': sha(Path(__file__)), 'out': str(out),
              'new_opponent_families_tested': False, 'candidate_learned_or_promoted': False}
    save(out / 'manifest.json', report)
    save(REPORT, report)
    records, cases, groups = [], [], defaultdict(list)
    noop_count = baseline_replays = branch_count = 0
    try:
        with gzip.open(out / 'calibration.jsonl.gz', 'wt', compresslevel=3) as stream:
            for seed in (251, 252):
                for background in baseline['protocol']['backgrounds']:
                    resources(deadline)
                    for index in range(100):
                        scenario = wb.Scenario(seed, index, background, 1)
                        for profile in cf.PROFILES:
                            row, _ = cf.play(scenario, profile)
                            records.append(row)
                            emit(stream, row)
                    print('CALIBRATION', seed, background, len(records), flush=True)
        report['calibration'] = {'records': len(records), 'scenarios': len(records) // 3,
                                 'independent_deals': len({r['deal_seed'] for r in records}),
                                 'b0_minus_simple': cf.calibration_metrics(records)}
        selected, availability = cf.select_losses(records, baseline['protocol']['backgrounds'])
        report['selection'] = {'records': selected, 'loss_pool_by_role_background': availability,
                               'selection_rule': plan['loss_selection'], 'complete': len(selected) == 200}
        save(REPORT, report)
        print('SELECTED', Counter(r['role'] for r in selected), flush=True)
        with gzip.open(out / 'branches.jsonl.gz', 'wt', compresslevel=3) as stream:
            for record in selected:
                resources(deadline)
                rows = defaultdict(list)
                for branch in cf.branches(record, lambda: resources(deadline)):
                    emit(stream, branch)
                    rows[branch['point']].append(branch)
                    branch_count += 1
                    noop_count += branch['noop']
                baseline_replays += 1
                any_win = guarded_win = vote_win = look_win = False
                information = defaultdict(lambda: {'points': 0, 'rescuable_points': 0, 'guarded_rescuable_points': 0})
                example = []
                for point, outcomes in rows.items():
                    winner = [r for r in outcomes if r['outcome']['win']]
                    guarded = [r for r in winner if r['retains_private_fact_and_unread_guard']]
                    first = outcomes[0]
                    category = first['context']['category']
                    information[category]['points'] += 1
                    information[category]['rescuable_points'] += bool(winner)
                    information[category]['guarded_rescuable_points'] += bool(guarded)
                    any_win |= bool(winner)
                    guarded_win |= bool(guarded)
                    vote_win |= bool(winner) and first['phase'] == 'day_vote'
                    look_win |= bool(winner) and first['phase'] == 'night_seer'
                    context = {'case': record['id'], 'point': point, 'phase': first['phase'],
                               'rescuing_actions': [r['action'] for r in winner],
                               'guarded_rescuing_actions': [r['action'] for r in guarded],
                               'baseline_action': first['baseline_action'], 'category': category}
                    groups[first['context']['visible_hash']].append(context)
                    if winner and len(example) < 2:
                        example.append(context)
                cases.append({'id': record['id'], 'role': record['role'], 'background': record['scenario']['background'],
                              'points': len(rows), 'branches': sum(map(len, rows.values())), 'rescued_any': any_win,
                              'rescued_guarded': guarded_win, 'vote_rescue': vote_win, 'investigation_rescue': look_win,
                              'information': dict(information), 'examples': example})
                print('CASE', len(cases), record['role'], record['id'], 'rescue', any_win, 'branches', branch_count, flush=True)
                if len(cases) % 20 == 0:
                    report['diagnosis'] = diagnostic_summary(cases, groups)
                    save(REPORT, report)
        report['diagnosis'] = diagnostic_summary(cases, groups)
        report['cases'] = cases
        report['verification'] = {'complete': len(cases) == 200, 'baseline_replays': baseline_replays,
                                  'branches': branch_count, 'noop_replays': noop_count,
                                  'all_prefixes_matched': True, 'all_noops_terminal_trace_matched': True,
                                  'no_actor_or_background_errors': True}
        report['source_end'] = wb.source_manifest()
        report['source_unchanged'] = report['source_end']['digest'] == source['digest']
        if not report['source_unchanged'] or sha(Path(__file__)) != report['script_sha256']:
            raise ValueError('source changed during diagnosis')
        report['status'] = 'complete_diagnostic_only'
    except Exception as exc:
        report.update(status='blocked_partial', error=f'{type(exc).__name__}: {exc}', completed_cases=len(cases))
        raise
    finally:
        report['ended'] = datetime.now().astimezone().isoformat()
        report['total_simulations'] = len(records) + baseline_replays + branch_count
        report['artifacts'] = {name: {'bytes': (out / name).stat().st_size, 'sha256': sha(out / name)}
                               for name in ('calibration.jsonl.gz', 'branches.jsonl.gz') if (out / name).exists()}
        save(REPORT, report)
        save(out / 'summary.json', report)
    print(json.dumps({'calibration': report['calibration']['b0_minus_simple'], 'diagnosis': report['diagnosis'],
                      'verification': report['verification']}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--stop-at', required=True)
    args = parser.parse_args()
    stop = datetime.fromisoformat(args.stop_at)
    if stop.tzinfo is None or stop.timestamp() <= time.time():
        parser.error('future timezone-aware deadline required')
    run(args.out.resolve(), stop.timestamp())


if __name__ == '__main__':
    main()
