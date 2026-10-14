from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from scripts.audit_werewolf_strength import Pair


def main():
    path=ROOT/'data/werewolf_evolution_llm_retry1_results.json'
    report=json.loads(path.read_text())
    if report['status']!='complete' or not report['source_unchanged'] or not report.get('unloaded'):
        raise ValueError('model pilot incomplete, changed source, or not unloaded')
    groups=defaultdict(dict)
    for row in report['games']:
        if row['profile'] in groups[row['id']] or row['role']!='wolf':
            raise ValueError('duplicate profile or wrong role')
        groups[row['id']][row['profile']]=row
    if len(groups)!=16 or len(report['games'])!=32:
        raise ValueError('incomplete paired pilot')
    pairs,clean,retry_free=Pair(),Pair(),Pair()
    guard,stats=Counter(),Counter()
    adverse=[]
    for ident,group in groups.items():
        if set(group)!={'reference','candidate'}:
            raise ValueError('unpaired pilot')
        ref,candidate=group['reference'],group['candidate']
        if ref['seat']!=candidate['seat'] or ref['roles_evaluator_only']!=candidate['roles_evaluator_only']:
            raise ValueError('unpaired actual roles/seat')
        pairs.add(candidate['win'],ref['win'])
        bad=False
        retried=False
        for row in (ref,candidate):
            bad=bad or any(row['errors'].values())
            for values in row['llm_stats'].values():
                stats.update(values)
                bad=bad or bool(values.get('fallbacks',0))
                retried=retried or bool(values.get('retries',0))
            for values in row['guard_stats'].values():
                guard.update(values)
        if not bad:
            clean.add(candidate['win'],ref['win'])
            if not retried:
                retry_free.add(candidate['win'],ref['win'])
        if bad or retried:
            adverse.append({'id':ident,'has_fallback_or_engine_error':bool(bad),'had_retry':bool(retried)})
    provider=report['provider']
    if provider['model_digest']!=report['plan']['model']['sha256'] or provider['options']['seed']!=1013:
        raise ValueError('model pin or decoder differs from registered retry configuration')
    result={'status':'pass','primary_all_pairs':pairs.report(),'no_fallback_pairs':clean.report(),
            'no_fallback_or_retry_pairs':retry_free.report(),'adverse_pairs':adverse,'model_stats':dict(stats),
            'guard_stats':dict(guard),'request_scope':provider['request_scope'],'cache_audit':report['cache_audit'],
            'unique_deals':16,'controlled_role':'wolf','unloaded':True,
            'scope':'descriptive16pair actual localmodel pilot; not overall win-rate or frontier-opponent proof',
            'initial_zero_game_failure_preserved':'data/werewolf_evolution_llm_results.json',
            'source_report_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    with (ROOT/'data/werewolf_evolution_llm_audit.json').open('x') as f:
        json.dump(result,f,indent=2)
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
