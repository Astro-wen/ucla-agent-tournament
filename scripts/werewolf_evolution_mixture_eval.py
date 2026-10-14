from datetime import datetime
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from scripts.werewolf_evolution_confirm import run
from scripts.werewolf_counterfactual_eval import save


REPORT=ROOT/'data/werewolf_evolution_mixture_results.json'


def main():
    if REPORT.exists():
        raise ValueError('new stress report required')
    plan=json.loads((ROOT/'data/werewolf_evolution_mixture_plan.json').read_text())
    confirmation=json.loads((ROOT/'data/werewolf_evolution_confirmation.json').read_text())
    chosen=confirmation['selected']
    if chosen not in ('vote_only','night_only','combined') or not confirmation.get('frozen_selection_at'):
        raise ValueError('candidate not frozen')
    result={'status':'running','plan':plan,'selected':chosen,'runs':{},'promoted':False}
    save(REPORT,result)
    deadline=datetime.fromisoformat(plan['deadline']).timestamp()
    try:
        for timing in plan['timings']:
            result['runs'][timing]=run('mixture-stress-516',[('candidate','local.werewolf_evolution_ablation:'+chosen)],
                                      (516,),plan['games_per_background'],timing,'development',deadline,','.join(plan['backgrounds']))
            save(REPORT,result)
        result['status']='complete'
    except Exception as exc:
        result.update(status='blocked_partial',error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        result['ended']=datetime.now().astimezone().isoformat()
        save(REPORT,result)
    print('MIXTURE COMPLETE',{t:r['metrics']['candidate']['all'] for t,r in result['runs'].items()},flush=True)


if __name__=='__main__':
    main()
