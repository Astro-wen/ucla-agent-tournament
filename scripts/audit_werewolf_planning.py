from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.audit_werewolf_counterfactual import decode, read_rows, require, same
from scripts.audit_werewolf_strength import Pair


def audit_stage(stage):
    out = ROOT / stage['directory']
    require(out.resolve().parent == (ROOT / 'logs/werewolf_benchmarks').resolve(), 'unexpected planning artifact path')
    manifest = decode(json.loads((out / 'manifest.json').read_text()))
    seed, n = manifest['seed'], manifest['games_per_background']
    expected = {f'{seed}/{b}/k1/{i}' for b in manifest['backgrounds'] for i in range(n)}
    profiles = [name for name, _ in manifest['profiles']]
    grouped, errors, planning, prediction = defaultdict(dict), defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    inner, games = 0, 0
    for row in read_rows(out / 'games.jsonl.gz'):
        require(row['id'] in expected and row['profile'] in profiles, 'unexpected game identity/profile')
        require(row['profile'] not in grouped[row['id']], 'duplicate profile')
        require(row['id'] == f"{row['scenario']['seed']}/{row['scenario']['background']}/k1/{row['scenario']['game']}", 'scenario id mismatch')
        grouped[row['id']][row['profile']] = row
        name = row['profile']
        errors[name].update({k: v for k, v in row['failures'].items() if k != 'window_close'})
        errors[name].update({'background_' + k: v for k, v in row['background_failures'].items() if k != 'window_close'})
        planning[name].update(row['planning'])
        prediction[name].update(row['prediction'])
        require(len(row['planning_trace']) == row['planning'].get('calls', 0), 'planning trace/call count mismatch')
        require(sum(t['selected'] != t['reference'] for t in row['planning_trace']) == row['planning'].get('changed', 0), 'changed-action count mismatch')
        require(sum(len(t['forecast']) for t in row['planning_trace']) == row['prediction'].get('n', 0), 'forecast denominator mismatch')
        for trace in row['planning_trace']:
            require(trace['alive'] in (3, 4, 5), 'unplanned alive-count search')
            for voter, counts in trace['forecast'].items():
                require(voter != row['seat'] and sum(counts.values()) == 16, 'forecast sample count/seat mismatch')
                require(voter not in counts and all(type(t) is int and 0 <= t <= 7 for t in counts), 'illegal forecast target')
                if 'trace' in row:
                    actual = next(v['votes'] for v in row['trace']['votes'] if v['day'] == trace['day'])
                    target = 7 if actual[voter] is None else actual[voter]
                    require(target == 7 or target in actual and target != voter, 'invalid actual ballot')
        inner += row['planning'].get('inner_games', 0)
        games += 1
    require(set(grouped) == expected and games == len(expected) * len(profiles), 'missing planning games')
    counts = defaultdict(Pair)
    for rows in grouped.values():
        require(set(rows) == set(profiles), 'unpaired planning profiles')
        ref = rows['reference']
        for name in profiles:
            row = rows[name]
            require(all(row[k] == ref[k] for k in ('id', 'scenario', 'role', 'seat', 'deal_seed')), 'unpaired scenario details')
            if name == 'reference':
                continue
            if name == 'mirror' or ref['role'] == 'wolf':
                require(row['trace_hash'] == ref['trace_hash'], 'baseline/wolf mirror mismatch')
            for scope in ('all', 'opponent:' + ref['scenario']['background'], 'seed:' + str(seed)):
                for role in ('seat', ref['role']):
                    counts[name, scope, role].add(row['win'], ref['win'])
    for (name, scope, role), value in counts.items():
        same(value.report(), stage['metrics'][name][scope][role])
    same({k: dict(v) for k, v in planning.items()}, stage['planning'])
    same({k: dict(v) for k, v in prediction.items()}, stage['prediction'])
    require(all(not any(v.values()) for v in errors.values()), 'rollout or actual protocol errors')
    same({k: dict(v) for k, v in errors.items()}, stage['errors'])
    require(stage['source_unchanged'] and stage['complete'], 'incomplete stage/source drift')
    negatives = []
    for (name, scope, role), value in counts.items():
        if scope.startswith('opponent:') and value.candidate_wins < value.reference_wins:
            negatives.append({'profile': name, 'scope': scope, 'role': role, **value.report()})
    calibrated = {}
    for name, value in prediction.items():
        if value.get('n'):
            calibrated[name] = {'n': value['n'], 'top1_accuracy': value['correct'] / value['n'],
                                'mean_logloss': value['logloss_sum'] / value['n'], 'mean_brier': value['brier_sum'] / value['n'],
                                'scope': 'descriptive on each policy own visited states, not matched-state superiority'}
    return {'pairs': len(expected), 'outer_games': games, 'hypothetical_inner_games': inner,
            'statistics_recomputed': len(counts), 'overall': {name: counts[name, 'all', 'seat'].report() for name in profiles if name != 'reference'},
            'roles': {name: {role: counts[name, 'all', role].report() for role in ('villager', 'seer', 'wolf')}
                      for name in profiles if name != 'reference'},
            'planning': {k: dict(v) for k, v in planning.items()}, 'prediction': calibrated,
            'negative_slices': negatives, 'raw_sha256': hashlib.sha256((out / 'games.jsonl.gz').read_bytes()).hexdigest(),
            'model_latency_max_ms': stage['max_ms'], 'all_outer_and_model_errors_zero': True}


def main():
    paths = [ROOT / 'data/werewolf_planning_results.json', ROOT / 'data/werewolf_planning_free_results.json']
    reports = [decode(json.loads(path.read_text())) for path in paths]
    result = {'status': 'pass', 'stages': {}, 'total_outer_games': 0, 'total_hypothetical_inner_games': 0,
              'heldout_seeds_used': [], 'live_platform_tested': False}
    for prefix, report in zip(('gated', 'free'), reports):
        require(report['status'] == 'complete_development_only', 'study incomplete')
        for name, stage in report['stages'].items():
            audited = audit_stage(stage)
            result['stages'][prefix + '/' + name] = audited
            result['total_outer_games'] += audited['outer_games']
            result['total_hypothetical_inner_games'] += audited['hypothetical_inner_games']
    first = reports[0]['stages']['271-rt']['metrics']
    selected = max(('rollout_prior', 'rollout_observed'), key=lambda p: first[p]['all']['seat']['delta'])
    require(selected == reports[0]['selected'], 'original selection mismatch')
    result['interpretation'] = 'Independent raw-record pairing/count/statistics audit. Inner modeled futures are not independent actual evaluation games. No model-assisted hidden-identity access is established by raw stats alone; source/input isolation tests are separate.'
    result['inputs'] = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    with (ROOT / 'data/werewolf_planning_audit.json').open('x') as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps({'status': result['status'], 'outer_games': result['total_outer_games'],
                      'hypothetical_games': result['total_hypothetical_inner_games'],
                      'stages': {k: {'overall': v['overall'], 'prediction': v['prediction']} for k, v in result['stages'].items()}}, indent=2))


if __name__ == '__main__':
    main()
