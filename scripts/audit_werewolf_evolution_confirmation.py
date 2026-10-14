from collections import defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from scripts.audit_werewolf_counterfactual import decode, read_rows, require
from scripts.audit_werewolf_evolution import audit_research
from scripts.audit_werewolf_strength import Pair, PANELS, assessment, BACKGROUNDS


ORDER=('vote_only','night_only','combined')


def main():
    path=ROOT/'data/werewolf_evolution_confirmation.json'
    report=decode(json.loads(path.read_text()))
    require(report['status']=='complete','confirmation not complete')
    result={'status':'pass','ablation':audit_research(report['stages']['ablation']['directory']),
            'runs':{},'new_simulations_by_audit':0,'heldout_unique_deals':28000,
            'timings_reuse_deals':True,'promoted':False}
    metrics=report['stages']['ablation']['metrics']
    highest=max(metrics[p]['all']['seat']['delta'] for p in ORDER)
    eligible=[p for p in ORDER if metrics[p]['all']['seat']['delta']>0 and metrics[p]['all']['seat']['delta']>=highest-.003]
    selected=min(eligible,key=lambda p:(2 if p=='combined' else 1,ORDER.index(p))) if eligible else None
    require(selected==report['selected'],'selected program mismatch')
    result['selected']=selected
    for timing in ('rt','rounds'):
        stage=report['stages'][timing]
        audited=audit_research(stage['directory'])
        out=ROOT/stage['directory']
        manifest=json.loads((out/'manifest.json').read_text())
        config=manifest['config']
        require(config['seeds']==[46101,46102] and config['games']==500,'confirmation sample changed')
        require(config['timing']==timing and config['visibility']=='strict','wrong timing/visibility')
        require(config['backgrounds'].split(',')==list(BACKGROUNDS),'background panel changed')
        require(manifest['profiles'][1][1]=='local.werewolf_evolution_ablation:'+selected,'factory drift')
        require(datetime.fromisoformat(report['frozen_selection_at'])<datetime.fromisoformat(manifest['started']),'selection not frozen before confirmation')
        expected={f'{seed}/{bg}/k1/{i}' for seed in (46101,46102) for bg in BACKGROUNDS for i in range(500)}
        rows=defaultdict(dict)
        for row in read_rows(out/'games.jsonl.gz'):
            require(row['id'] in expected,'unexpected heldout scenario')
            rows[row['id']][row['profile']]=row
        require(set(rows)==expected and audited['pairs']==28000,'missing heldout scenario')
        panels=defaultdict(Pair)
        for pair in rows.values():
            ref,candidate=pair['reference'],pair['candidate']
            for name,bgs in PANELS.items():
                if ref['scenario']['background'] in bgs:
                    for role in ('seat',ref['role']):
                        panels[name,role].add(candidate['win'],ref['win'])
        verified_metrics=stage['metrics']['candidate']
        audited['assessment']=assessment(verified_metrics)
        audited['role_metrics']=verified_metrics['all']
        audited['panels']={name:{role:panels[name,role].report() for role in ('seat','villager','seer','wolf')} for name in PANELS}
        audited['source_digest']=manifest['source']['digest']
        result['runs'][timing]=audited
    require(result['runs']['rt']['source_digest']==result['runs']['rounds']['source_digest'],'confirmation source drift between modes')
    require(hashlib.sha256((ROOT/'data/werewolf_evolution_model.json').read_bytes()).hexdigest()==report['model_sha256'],'fixed model changed')
    result['all_local_strength_gates_pass']=all(r['assessment']['local_strength_gates_pass'] for r in result['runs'].values())
    result['total_confirmation_outer_games']=sum(r['games'] for r in result['runs'].values())
    result['limits']=['Predefined localpanel, not global optimality','No actual platform promotion','Modelpilot and timing stress are separate evidence',
                      'Ordinary and conservative intervals retained; multiple-comparison reporting does not erase adverse slices']
    result['confirmation_report_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    with (ROOT/'data/werewolf_evolution_confirmation_audit.json').open('x') as stream:
        json.dump(result,stream,indent=2)
    print(json.dumps({'status':result['status'],'selected':selected,'all_local_strength_gates_pass':result['all_local_strength_gates_pass'],
                      'runs':{t:{'overall':r['overall'],'gates':r['assessment']['gates'],
                                 'regression_flags':r['assessment']['unacceptable_background_role_slices']}
                              for t,r in result['runs'].items()}},indent=2))


if __name__=='__main__':
    main()
