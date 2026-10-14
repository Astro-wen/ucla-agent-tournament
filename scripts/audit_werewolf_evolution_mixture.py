import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from scripts.audit_werewolf_evolution import audit_research


def main():
    report=json.loads((ROOT/'data/werewolf_evolution_mixture_results.json').read_text())
    if report['status']!='complete':
        raise ValueError('mixture study incomplete')
    result={'status':'pass','selected':report['selected'],'unique_deals':4000,'timings_reuse_deals':True,
            'runs':{t:audit_research(r['directory']) for t,r in report['runs'].items()},
            'scope':'newmixtures of existing scripts; not newindependent implementations or global dominance'}
    with (ROOT/'data/werewolf_evolution_mixture_audit.json').open('x') as f:
        json.dump(result,f,indent=2)
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
