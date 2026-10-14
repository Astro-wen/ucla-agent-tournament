from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.audit_werewolf_counterfactual import decode, read_rows, require, same
from scripts.audit_werewolf_strength import Pair


def main():
    report_path = ROOT / 'data/werewolf_initial_value_results.json'
    report = decode(json.loads(report_path.read_text()))
    require(report['status'] == 'complete_exploratory_only', 'initial value experiment not complete')
    calibration = decode(json.loads((ROOT / 'data/werewolf_counterfactual_diagnostics.json').read_text()))
    originals = {r['id']: r for r in read_rows(Path(calibration['out']) / 'calibration.jsonl.gz')
                 if r['profile'] == 'b0' and r['role'] == 'seer'}
    raw = ROOT / 'logs/werewolf_benchmarks/initial-value-251-252/training.jsonl.gz'
    require(hashlib.sha256(raw.read_bytes()).hexdigest() == report['raw_sha256'], 'training artifact hash mismatch')
    groups, stats = defaultdict(dict), defaultdict(Pair)
    for row in read_rows(raw):
        case, rank = row['case'], row['rank']
        require(case in originals and type(rank) is int and 0 <= rank < 6 and rank not in groups[case], 'training case/rank error')
        base, outcome = originals[case], row['outcome']
        require(row['baseline_win'] == base['win'], 'winning baseline changed')
        require(row['action'] == [s for s in range(7) if s != base['seat']][rank], 'rank target mismatch')
        require(all(outcome[k] == base[k] for k in ('id', 'seat', 'role', 'deal_seed')), 'training identity mismatch')
        require(outcome['intervention_applied'] and not any(outcome['errors'].values()), 'invalid training intervention')
        if rank == 5:
            require(all(outcome[k] == base[k] for k in ('win', 'trace_hash', 'event_hash')), 'high rank not a full B0 mirror')
        groups[case][rank] = row
        for scope in ('all', 'seed:' + str(base['scenario']['seed']), 'background:' + base['scenario']['background'],
                      'seat:' + str(base['seat'])):
            stats[rank, scope].add(outcome['win'], base['win'])
    require(set(groups) == set(originals) and len(groups) == report['training_cases'], 'training dropped wins/losses')
    require(all(set(ranks) == set(range(6)) for ranks in groups.values()), 'missing training rank')
    for (rank, scope), value in stats.items():
        same(value.report(), report['training'][str(rank)][scope])
    leader = max(range(6), key=lambda rank: (stats[rank, 'all'].candidate_wins, rank))
    selected = leader if all(stats[leader, 'seed:' + str(s)].candidate_wins > stats[leader, 'seed:' + str(s)].reference_wins
                             for s in (251, 252)) else 5
    require((leader, selected) == (report['training_leader'], report['selected_rank']), 'selection changed')
    result = {'status': 'pass', 'training_cases': len(groups), 'training_branches': 6 * len(groups),
              'winning_controls': sum(r['win'] for r in originals.values()),
              'losing_controls': sum(not r['win'] for r in originals.values()),
              'training_statistics_recomputed': len(stats), 'selected_rank': selected,
              'heldout_seeds_used': [], 'unseen_opponent_families_tested': False}
    if selected != 5:
        out = ROOT / report['replication']['directory']
        require(out.parent.resolve() == (ROOT / 'logs/werewolf_benchmarks').resolve(), 'unexpected replication path')
        paired = defaultdict(dict)
        for row in read_rows(out / 'games.jsonl.gz'):
            require(row['profile'] in ('reference', 'candidate') and row['profile'] not in paired[row['id']], 'duplicate replication profile')
            require(not any(v for k, v in row['failures'].items() if k != 'window_close'), 'replication policy failure')
            require(not any(v for k, v in row['background_failures'].items() if k != 'window_close'), 'replication background failure')
            paired[row['id']][row['profile']] = row
        bgs = json.loads((ROOT / 'data/werewolf_current_baseline.json').read_text())['protocol']['backgrounds']
        wanted = {f'{s}/{b}/k1/{i}' for s in (253, 254) for b in bgs for i in range(200)}
        require(set(paired) == wanted, 'missing replication deals')
        counters = defaultdict(Pair)
        for rows in paired.values():
            require(set(rows) == {'reference', 'candidate'}, 'unpaired replication')
            reference, candidate = rows['reference'], rows['candidate']
            require(all(reference[k] == candidate[k] for k in ('id', 'scenario', 'seat', 'role', 'deal_seed')), 'replication pair identity mismatch')
            if reference['role'] != 'seer':
                require(all(reference[k] == candidate[k] for k in ('win', 'winner', 'days', 'chats', 'dms', 'diagnostics')),
                        'non-seer behavior changed')
            for scope in ('all', 'opponent:' + reference['scenario']['background'], 'seed:' + str(reference['scenario']['seed']),
                          'seat:' + str(reference['seat'])):
                for role in ('seat', reference['role']):
                    counters[scope, role].add(candidate['win'], reference['win'])
        for (scope, role), value in counters.items():
            same(value.report(), report['replication']['metrics']['candidate'][scope][role])
        result['replication'] = {'pairs': len(paired), 'raw_records': 2 * len(paired),
                                  'statistics_recomputed': len(counters),
                                  'overall': counters['all', 'seat'].report(), 'seer': counters['all', 'seer'].report(),
                                  'nonseer_pairs_unchanged': True}
    require(report['source_begin']['digest'] == report['source_end']['digest'], 'source changed')
    result['integrity_limit'] = 'Source begin/end verified by runner; audit independently recomputes artifact pairing/counts/statistics without replaying games.'
    result['report_sha256'] = hashlib.sha256(report_path.read_bytes()).hexdigest()
    with (ROOT / 'data/werewolf_initial_value_audit.json').open('x') as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
