from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from scripts.audit_werewolf_counterfactual import decode, read_rows, require, same
from scripts.audit_werewolf_strength import Pair


def rank(value):
    return (-value['fitness'],-value['worst_timing_delta'],value['complexity'],tuple(value['genes']))


def main():
    path=ROOT/'data/werewolf_robust_vote_results.json'
    report=decode(json.loads(path.read_text()))
    require(report['status']=='complete','robust search not complete')
    raw=ROOT/'logs/werewolf_benchmarks/robust-vote-517/games.jsonl.gz'
    rows={}
    for row in read_rows(raw):
        key=tuple(row['genes']),row['id'],row['timing']
        require(key not in rows and not any(row['errors'].values()),'duplicate or failed robust game')
        require(len(row['genes'])==3 and all(type(g) is int and 0<=g<12 for g in row['genes']),'invalid program')
        rows[key]=row
    require(len(rows)==report['actual_simulations'],'simulation accounting mismatch')
    pools={}
    for name,seed in (('scan',517),('selection_scan',518)):
        by_bg=defaultdict(list)
        for row in report[name]:
            if row['accepted'] and row['role']=='wolf':
                by_bg[row['background']].append(f"{seed}/{row['background']}/k1/{row['game']}")
        require(len(by_bg)==20 and all(len(v)==12 for v in by_bg.values()),'training role quota mismatch')
        pools[seed,12]=[case for values in by_bg.values() for case in values]
        pools[seed,3]=[case for values in by_bg.values() for case in values[:3]]
    checked=0
    def verify(value,seed,quota,full=False):
        nonlocal checked
        genes=tuple(value['genes'])
        totals,groups={},{}
        penalty=0
        for timing in ('rt','rounds'):
            total,by_bg=Pair(),defaultdict(Pair)
            for case in pools[seed,quota]:
                ref=rows[(0,0,0),case,timing]
                candidate=rows[genes,case,timing]
                require(candidate['role']=='wolf' and all(candidate[k]==ref[k] for k in ('scenario','seat','role','deal_seed')),'unpaired training')
                total.add(candidate['win'],ref['win'])
                by_bg[candidate['scenario']['background']].add(candidate['win'],ref['win'])
            totals[timing]=total.report()
            groups[timing]={b:g.report() for b,g in by_bg.items()}
            penalty+=sum(max(0,-g['delta']) for g in groups[timing].values())/len(by_bg)/2
        worst=min(v['delta'] for v in totals.values())
        same(totals,value['timings'])
        require(abs(worst-value['worst_timing_delta'])<1e-12 and abs(worst-.5*penalty-value['fitness'])<1e-12,'robust objective mismatch')
        require(value['complexity']==sum(g!=0 for g in genes[1:]),'complexity mismatch')
        if full:
            same(groups,value['backgrounds'])
        checked+=1
    screen=report['stages']['screen']
    expected={(0,s,s) for s in range(12)}|{(p,a,b) for p in range(1,12) for a in range(12) for b in range(12) if a!=b}
    require(len(screen)==1464 and {tuple(v['genes']) for v in screen}==expected,'nonexhaustive initial screen')
    for row in screen:
        verify(row,517,3)
    require(screen==sorted(screen,key=rank),'screen ranking mismatch')
    full=report['stages']['full_training']
    require({tuple(v['genes']) for v in full}=={tuple(v['genes']) for v in screen[:96]}|{(0,0,0)},'invalid stage2selection')
    for row in full:
        verify(row,517,12,True)
    require(full==sorted(full,key=rank),'full ranking mismatch')
    selection=report['stages']['selection']
    require({tuple(v['genes']) for v in selection}=={tuple(v['genes']) for v in full[:8]}|{(0,0,0)},'invalid final selection candidates')
    for row in selection:
        verify(row,518,12,True)
    require(selection==sorted(selection,key=rank),'selection ranking mismatch')
    chosen=selection[0]['genes'] if selection[0]['worst_timing_delta']>0 else [0,0,0]
    require(chosen==report['chosen'],'chosen program drift')
    stages={}
    for label,seeds in (('development',{519}),('confirmation',{46301,46302})):
        if label not in report:
            continue
        counts=defaultdict(Pair)
        for (genes,case,timing),row in rows.items():
            if genes!=tuple(chosen) or row['scenario']['seed'] not in seeds:
                continue
            ref=rows[(0,0,0),case,timing]
            require(all(ref[k]==row[k] for k in ('scenario','role','seat','deal_seed')),'unpaired evaluation')
            if row['role']!='wolf':
                require(row['trace_hash']==ref['trace_hash'],'good-role full trace changed')
            for scope in ('all','opponent:'+row['scenario']['background'],'seed:'+str(row['scenario']['seed'])):
                for role in ('seat',row['role']):
                    counts[timing,scope,role].add(row['win'],ref['win'])
        for (timing,scope,role),value in counts.items():
            same(value.report(),report[label]['metrics'][timing][scope][role])
        stages[label]={'overall':{t:counts[t,'all','seat'].report() for t in ('rt','rounds')},'statistics_recomputed':len(counts)}
    require(report['source']['digest']==report['source_end']['digest'],'source drift')
    result={'status':'pass','actual_simulations':len(rows),'training_evaluations_recomputed':checked,'screen_programs':1464,
            'chosen':chosen,'stages':stages,'heldouts_used':report['heldouts_used'],
            'raw_sha256':hashlib.sha256(raw.read_bytes()).hexdigest(),'report_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'scope':'independent standard-library raw-count/objective/selection/statistical audit; no new simulations'}
    with (ROOT/'data/werewolf_robust_vote_audit.json').open('x') as f:
        json.dump(result,f,indent=2)
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
