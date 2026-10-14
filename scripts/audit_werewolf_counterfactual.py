from __future__ import annotations

from collections import Counter, defaultdict, deque
import gzip
import hashlib
import json
import math
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.audit_werewolf_strength import Pair


def require(value, message):
    if not value:
        raise ValueError(message)


def decode(value):
    if isinstance(value, list):
        return [decode(v) for v in value]
    if isinstance(value, dict):
        if set(value) == {'__s'}:
            return set(map(decode, value['__s']))
        if set(value) == {'__t'}:
            return tuple(map(decode, value['__t']))
        if set(value) == {'__d'}:
            return {decode(k): decode(v) for k, v in value['__d']}
        return {k: decode(v) for k, v in value.items()}
    return value


def read_rows(path):
    with gzip.open(path, 'rt') as stream:
        for line in stream:
            yield decode(json.loads(line))


def same(actual, expected):
    if isinstance(expected, dict):
        require(set(actual) == set(expected), 'mapping keys differ')
        for key in expected:
            same(actual[key], expected[key])
    elif isinstance(expected, float):
        require(math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-12), 'statistics differ')
    elif isinstance(expected, (list, tuple)):
        require(len(actual) == len(expected), 'sequence length differs')
        for a, b in zip(actual, expected):
            same(a, b)
    else:
        require(actual == expected, 'values differ')


def audit(report_path):
    report = decode(json.loads(report_path.read_text()))
    require(report['status'] == 'complete_diagnostic_only', 'diagnosis not complete')
    baseline = json.loads((ROOT / report['plan']['baseline']).read_text())
    backgrounds = baseline['protocol']['backgrounds']
    out = Path(report['out'])
    require(out.resolve().parent == (ROOT / 'logs/werewolf_benchmarks').resolve(), 'unexpected artifact directory')
    for name, info in report['artifacts'].items():
        require(name in ('calibration.jsonl.gz', 'branches.jsonl.gz'), 'unexpected artifact name')
        data = (out / name).read_bytes()
        require(len(data) == info['bytes'] and hashlib.sha256(data).hexdigest() == info['sha256'], 'artifact changed')
    grouped, count = defaultdict(dict), 0
    expected_ids = {f'{s}/{b}/k1/{i}' for s in (251, 252) for b in backgrounds for i in range(100)}
    for row in read_rows(out / 'calibration.jsonl.gz'):
        require(row['id'] in expected_ids, 'unexpected calibration scenario')
        require(row['profile'] in ('b0', 'first', 'random') and row['profile'] not in grouped[row['id']], 'duplicate/profile error')
        require(not any(row['errors'].values()), 'calibration rollout error')
        scenario = row['scenario']
        rng = random.Random(f"deal:{scenario['seed']}:{scenario['game']}:{scenario['background']}:1")
        me = rng.sample(range(7), 1)[0]
        if scenario['background'] == 'mixed':
            for seat in range(7):
                if seat != me:
                    rng.choice(('first', 'heuristic', 'random', 'llm'))
        seed = rng.getrandbits(62)
        roles = ['wolf', 'wolf', 'seer', 'villager', 'villager', 'villager', 'villager']
        random.Random(seed).shuffle(roles)
        require((row['seat'], row['deal_seed'], row['role']) == (me, seed, roles[me]), 'deal identity mismatch')
        grouped[row['id']][row['profile']] = row
        count += 1
    require(set(grouped) == expected_ids and count == 16800, 'calibration count mismatch')
    stats = defaultdict(Pair)
    for case, rows in grouped.items():
        require(set(rows) == {'b0', 'first', 'random'}, 'incomplete calibration group')
        base = rows['b0']
        for profile in ('first', 'random'):
            for scope in ('all', 'background:' + base['scenario']['background'], 'seed:' + str(base['scenario']['seed'])):
                for role in ('seat', base['role']):
                    stats[profile, scope, role].add(base['win'], rows[profile]['win'])
    for (profile, scope, role), counter in stats.items():
        same(counter.report(), report['calibration']['b0_minus_simple'][profile][scope][role])
    selection = []
    for role in ('villager', 'seer'):
        queues = {}
        for b in backgrounds:
            rows = [r['b0'] for r in grouped.values() if r['b0']['role'] == role and not r['b0']['win']
                    and r['b0']['scenario']['background'] == b]
            queues[b] = deque(sorted(rows, key=lambda r: hashlib.sha256(('ww-causal-loss-v1:' + r['id']).encode()).hexdigest()))
        selected = 0
        while selected < 100:
            progress = False
            for b in backgrounds:
                if queues[b] and selected < 100:
                    selection.append(queues[b].popleft()['id'])
                    selected += 1
                    progress = True
            require(progress, 'not enough fixed-pool losses')
    same(selection, [r['id'] for r in report['selection']['records']])
    points, branches = defaultdict(dict), 0
    for branch in read_rows(out / 'branches.jsonl.gz'):
        case, point, action = branch['case'], branch['point'], branch['action']
        require(case in selection and type(action) is int, 'unexpected branch')
        key = case, point
        require(action not in points[key], 'duplicate branch action')
        base, result = grouped[case]['b0'], branch['outcome']
        require(not any(result['errors'].values()) and result['intervention_applied'], 'invalid intervention')
        require(all(result[k] == base[k] for k in ('id', 'scenario', 'seat', 'role', 'deal_seed')), 'branch identity changed')
        require(branch['noop'] == (action == branch['baseline_action']), 'wrong no-op marker')
        points[key][action] = branch
        branches += 1
    sums, by_role, by_background, information = Counter(), defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    visible_groups = defaultdict(list)
    for case in selection:
        base = grouped[case]['b0']
        case_points = sorted(p for c, p in points if c == case)
        require(case_points == list(range(base['decisions'])), 'missing controlled decision point')
        values = Counter(n=1, points=len(case_points))
        any_win = guarded_win = vote_win = look_win = False
        for point in case_points:
            actions = points[case, point]
            noops = [r for r in actions.values() if r['noop']]
            require(len(noops) == 1, 'missing or duplicate no-op')
            noop = noops[0]
            observation = noop['point_observation']
            require(set(actions) == set(observation['legal']), 'incomplete legal action enumeration')
            raw = observation['raw']
            require(hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
                    == observation['input_hash'], 'actor input digest mismatch')
            require(not {'roles', 'hidden_roles', 'deal_seed', 'opponent_types', 'intervention'} & set(raw), 'privileged actor input')
            require(not any({'role', 'secretRole'} & set(p) for p in raw['players']), 'other role in actor players')
            require(all(noop['outcome'][k] == base[k] for k in ('trace_hash', 'event_hash', 'win', 'winner', 'days')), 'no-op differs')
            for row in actions.values():
                context = row['context']
                if row['phase'] == 'day_vote':
                    guarded = row['action'] in context['private_wolves'] if context['private_wolves'] else row['action'] not in context['private_clears']
                else:
                    guarded = row['action'] not in context['checked'] or set(actions) <= set(context['checked'])
                require(guarded == row['retains_private_fact_and_unread_guard'], 'guard classification mismatch')
                require(row['context'] == noop['context'], 'context changed between alternatives')
            wins = [r for r in actions.values() if r['outcome']['win']]
            guards = [r for r in wins if r['retains_private_fact_and_unread_guard']]
            category = noop['context']['category']
            information[category].update(points=1, rescuable_points=bool(wins), guarded_rescuable_points=bool(guards))
            visible_groups[noop['context']['visible_hash']].append({'case': case, 'wins': {r['action'] for r in wins}})
            any_win |= bool(wins)
            guarded_win |= bool(guards)
            vote_win |= bool(wins) and noop['phase'] == 'day_vote'
            look_win |= bool(wins) and noop['phase'] == 'night_seer'
            values['branches'] += len(actions)
        values.update(rescued_any=any_win, rescued_preserving_private_guards=guarded_win,
                      vote_rescue=vote_win, investigation_rescue=look_win)
        sums.update(values)
        by_role[base['role']].update(values)
        by_background[base['scenario']['background']].update(values)
    same(dict(sums), report['diagnosis']['all'])
    for role, values in by_role.items():
        same(dict(values), report['diagnosis']['by_role'][role])
    same({k: dict(v) for k, v in information.items()}, report['diagnosis']['by_information'])
    shared = [g for g in visible_groups.values() if len({r['case'] for r in g}) > 1]
    possible = [g for g in shared if all(r['wins'] for r in g)]
    conflict = sum(not set.intersection(*(r['wins'] for r in g)) for g in possible)
    require(conflict == report['diagnosis']['visible_information_groups']['no_single_action_rescues_every_state'], 'visible conflict count')
    require(report['source_unchanged'], 'research source changed')
    require(branches == report['verification']['branches'] and len(points) == report['verification']['noop_replays'], 'branch total mismatch')
    result = {'status': 'pass', 'calibration_records': count, 'calibration_deals': len(grouped),
              'selected_losses': len(selection), 'branches': branches, 'noops': len(points), 'totals': dict(sums),
              'paired_statistics_recomputed': len(stats), 'visible_conflicts': conflict,
              'source_integrity': 'runner source begin/end digest matched; B0 source verification passed before execution',
              'prefix_limit': 'Prefix enforcement is tested in the replay implementation; this audit does not rerun games or reconstruct every callback.',
              'no_strategy_strength_claim_from_hindsight': True, 'input_report_sha256': hashlib.sha256(report_path.read_bytes()).hexdigest()}
    output = ROOT / 'data/werewolf_counterfactual_audit.json'
    with output.open('x') as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    audit(ROOT / 'data/werewolf_counterfactual_diagnostics.json')
