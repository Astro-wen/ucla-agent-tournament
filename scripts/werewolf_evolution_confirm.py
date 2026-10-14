import json
from datetime import datetime
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from scripts.werewolf_counterfactual_eval import save, sha, resources
from local.werewolf_evolution_research import MODEL_PATH, frozen_genome


REPORT=ROOT/'data/werewolf_evolution_confirmation.json'
ORDER=('vote_only','night_only','combined')


def select(metrics):
    best=max(metrics[name]['all']['seat']['delta'] for name in ORDER)
    eligible=[name for name in ORDER if metrics[name]['all']['seat']['delta']>0
              and metrics[name]['all']['seat']['delta']>=best-.003]
    return min(eligible,key=lambda name:(2 if name=='combined' else 1,ORDER.index(name))) if eligible else None


def run(stage,profiles,seeds,n,timing,split,deadline,bgs):
    resources(deadline)
    out=ROOT/f'logs/werewolf_benchmarks/evolution-{stage}-{timing}'
    args=[sys.executable,'-m','local.werewolf_research','--profile','reference=agent']
    for label,factory in profiles:
        args+=['--profile',label+'='+factory]
    args+=['--seed-registry','evolution','--split',split,'--seeds',*map(str,seeds),'--games',str(n),
           '--backgrounds',bgs,'--visibility','strict','--timing',timing,'--jobs','1','--trace-games','2',
           '--out',str(out),'--stop-at','2026-10-14T07:30:00-07:00']
    code=subprocess.run(args,cwd=ROOT).returncode
    report=json.loads((out/'summary.json').read_text())
    result={'directory':str(out.relative_to(ROOT)),'returncode':code,'gates':report['gates'],'metrics':report['metrics'],
            'completed_scenarios':report['completed_scenarios'],'games':report['games']}
    if not all(report['gates'].values()):
        raise ValueError('stage integrity failed')
    return result


def main():
    if REPORT.exists():
        raise ValueError('new confirmation report required')
    plan=json.loads((ROOT/'data/werewolf_evolution_ablation_plan.json').read_text())
    training=json.loads((ROOT/'data/werewolf_evolution_results.json').read_text())
    if training['status']!='complete_development_only' or sha(MODEL_PATH)!=training['model_sha256']:
        raise ValueError('unfrozen or changed learned program')
    baseline=json.loads((ROOT/'data/werewolf_current_baseline.json').read_text())
    bgs=','.join(baseline['protocol']['backgrounds'])
    deadline=datetime.fromisoformat(plan['resources']['deadline']).timestamp()
    result={'status':'running','plan':plan,'model':list(frozen_genome()),'model_sha256':sha(MODEL_PATH),
            'stages':{},'heldouts_used':[],'promoted':False}
    save(REPORT,result)
    try:
        profiles=[(name,'local.werewolf_evolution_ablation:'+name) for name in ORDER]
        ablation=run('ablation-505-506',profiles,(505,506),100,'rt','development',deadline,bgs)
        result['stages']['ablation']=ablation
        chosen=select(ablation['metrics'])
        result['selected']=chosen
        result['frozen_selection_at']=datetime.now().astimezone().isoformat()
        save(REPORT,result)
        print('FROZEN SELECTION',chosen,{n:ablation['metrics'][n]['all']['seat'] for n in ORDER},flush=True)
        if chosen is not None:
            result['heldouts_used']=[46101,46102]
            save(REPORT,result)
            for timing in ('rt','rounds'):
                result['stages'][timing]=run('confirm-46101-46102',[('candidate','local.werewolf_evolution_ablation:'+chosen)],
                                             (46101,46102),500,timing,'heldout',deadline,bgs)
                if sha(MODEL_PATH)!=result['model_sha256']:
                    raise ValueError('model changed during confirmation')
                save(REPORT,result)
                print('CONFIRMATION',timing,result['stages'][timing]['metrics']['candidate']['all'],flush=True)
        result['status']='complete'
    except Exception as exc:
        result.update(status='blocked_partial',error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        result['ended']=datetime.now().astimezone().isoformat()
        save(REPORT,result)


if __name__=='__main__':
    main()
