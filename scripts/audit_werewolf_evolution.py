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


ROLES={'villager':(0,),'seer':(1,2),'wolf':(3,4)}


def genes_for(genome,role):
    return tuple(genome[i] for phase in ROLES[role] for i in range(phase*3,phase*3+3))


def ranking(row):
    return (-row['fitness'],-row['all']['delta'],row['complexity'],tuple(row['genes']))


def audit_training():
    report_path=ROOT/'data/werewolf_evolution_results.json'
    r=decode(json.loads(report_path.read_text()))
    require(r['status']=='complete_development_only','training incomplete')
    raw=ROOT/'logs/werewolf_benchmarks/evolution-501-504/fitness-games.jsonl.gz'
    rows={}
    cases=defaultdict(set)
    for row in read_rows(raw):
        role,genes,case=row['role'],tuple(row['genes']),row['id']
        key=(role,genes,case)
        require(key not in rows and role in ROLES,'duplicate/unknown role cache entry')
        require(not any(row['errors'].values()),'training policy/protocol errors')
        require(len(row['genome'])==15 and all(type(g) is int and 0<=g<12 for g in row['genome']),'invalid program')
        require(genes_for(row['genome'],role)==genes,'role allele mismatch')
        require(all(not any(genes_for(row['genome'],other)) for other in ROLES if other!=role),'cross-role training code')
        require(row['scenario']['seed'] in (501,502),'unexpected training/selection seed')
        rows[key]=row
        cases[row['scenario']['seed'],role].add(case)
    require(len(rows)==r['fitness_games'],'cached game accounting mismatch')
    for (seed,role),ids in cases.items():
        require(len(ids)==240,'wrong role quota')
        by_bg=defaultdict(int)
        for case in ids:
            by_bg[case.split('/')[1]]+=1
        require(len(by_bg)==20 and set(by_bg.values())=={12},'unbalanced role/background pool')
    counters={}
    recomputed=0
    def verify(value,role,seed,full=False):
        nonlocal recomputed
        genes=tuple(value['genes'])
        key=role,seed,genes
        if key not in counters:
            total,groups=Pair(),defaultdict(Pair)
            for case in cases[seed,role]:
                ref=rows[role,(0,)*len(genes),case]
                row=rows[role,genes,case]
                require(all(ref[k]==row[k] for k in ('scenario','role','seat','deal_seed')),'unpaired genotype evaluation')
                total.add(row['win'],ref['win'])
                groups[row['scenario']['background']].add(row['win'],ref['win'])
            report=total.report()
            penalty=sum(max(0,-group.report()['delta']) for group in groups.values())/len(groups)
            counters[key]=(report,report['delta']-.25*penalty,{b:g.report() for b,g in groups.items()})
        total,fitness,groups=counters[key]
        same(total,value['all'])
        require(abs(fitness-value['fitness'])<1e-12,'fitness mismatch')
        require(value['complexity']==sum(g!=0 for i,g in enumerate(genes) if i%3),'complexity mismatch')
        if full:
            same(groups,value['backgrounds'])
        recomputed+=1
    combined=[0]*15
    for role in ROLES:
        history=r['generations'][role]
        require(len(history)==14,'missing generations')
        for generation,record in enumerate(history):
            require(record['generation']==generation and len(record['population'])==24,'generation shape')
            population=record['population']
            require(len({tuple(x['genes']) for x in population})==24,'duplicate population programs')
            for value in population:
                verify(value,role,501)
            require(population==sorted(population,key=ranking),'unsorted fitness ranking')
            verify(record['best'],role,501,True)
            require(record['best']['genes']==population[0]['genes'],'winner mismatch')
        selected=r['selected_roles'][role]
        candidates=selected['all_candidates']
        expected={tuple(v['genes']) for v in history[-1]['population'][:6]}|{(0,)*(3*len(ROLES[role]))}
        require({tuple(v['genes']) for v in candidates}==expected,'selection cherry-picked nonelite')
        for value in candidates:
            verify(value,role,502,True)
        require(candidates==sorted(candidates,key=ranking),'selection ranking mismatch')
        winner=candidates[0]
        chosen=winner['genes'] if winner['all']['delta']>0 else [0]*len(winner['genes'])
        require(chosen==selected['chosen'],'selected role drift')
        indices=[i for phase in ROLES[role] for i in range(phase*3,phase*3+3)]
        for i,g in zip(indices,chosen):
            combined[i]=g
    model=json.loads((ROOT/'data/werewolf_evolution_model.json').read_text())
    require(combined==r['frozen_genome']==model['genome'],'frozen model does not match selection')
    require(hashlib.sha256((ROOT/'data/werewolf_evolution_model.json').read_bytes()).hexdigest()==r['model_sha256'],'model hash mismatch')
    require(r['source_begin']['digest']==r['source_end']['digest'],'training source drift')
    return {'status':'pass','training_games':len(rows),'unique_training_selection_deals':sum(map(len,cases.values())),
            'evaluations_recomputed':recomputed,'distinct_fitness_tables_recomputed':len(counters),
            'frozen_genome':combined,'raw_sha256':hashlib.sha256(raw.read_bytes()).hexdigest(),
            'report_sha256':hashlib.sha256(report_path.read_bytes()).hexdigest()}


def audit_research(directory):
    out=ROOT/directory
    summary=decode(json.loads((out/'summary.json').read_text()))
    manifest=decode(json.loads((out/'manifest.json').read_text()))
    require(all(summary['gates'].values()),'research integrity failed')
    grouped=defaultdict(dict)
    for row in read_rows(out/'games.jsonl.gz'):
        require('harness_error' not in row and row['profile'] not in grouped[row['id']],'invalid/duplicate research row')
        require(not any(v for k,v in row['failures'].items() if k!='window_close'),'research policy failure')
        require(not any(v for k,v in row['background_failures'].items() if k!='window_close'),'background failure')
        grouped[row['id']][row['profile']]=row
    profiles=[p[0] for p in manifest['profiles']]
    require(len(grouped)==summary['completed_scenarios'],'missing research scenarios')
    counts=defaultdict(Pair)
    for rows in grouped.values():
        require(set(rows)==set(profiles),'missing profile')
        ref=rows['reference']
        for name in profiles[1:]:
            row=rows[name]
            require(all(row[k]==ref[k] for k in ('id','scenario','role','seat','deal_seed')),'research unpaired')
            if ref['role']!='wolf':
                require(all(row[k]==ref[k] for k in ('win','winner','days','chats','dms','diagnostics')),'good-role regression')
                if 'trace' in row:
                    require(row['trace']==ref['trace'],'good-role full trace drift')
            for scope in ('all','opponent:'+row['scenario']['background'],'seed:'+str(row['scenario']['seed']),'seat:'+str(row['seat'])):
                for role in ('seat',row['role']):
                    counts[name,scope,role].add(row['win'],ref['win'])
    for (name,scope,role),counter in counts.items():
        same(counter.report(),summary['metrics'][name][scope][role])
    return {'status':'pass','pairs':len(grouped),'games':summary['games'],'statistics_recomputed':len(counts),
            'overall':{p:counts[p,'all','seat'].report() for p in profiles[1:]},
            'raw_sha256':hashlib.sha256((out/'games.jsonl.gz').read_bytes()).hexdigest()}


def main():
    result=audit_training()
    result['development']=audit_research('logs/werewolf_benchmarks/evolution-replication-503-504')
    with (ROOT/'data/werewolf_evolution_development_audit.json').open('x') as stream:
        json.dump(result,stream,indent=2)
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
