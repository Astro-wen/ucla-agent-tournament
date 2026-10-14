from collections import Counter, defaultdict
from datetime import datetime
from dataclasses import asdict
import gzip
import hashlib
import json
from pathlib import Path
import random
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from local import eval_werewolf as ev
from local import werewolf_benchmark as wb
from local.werewolf_evolution_research import BASE, EvolutionContestant
from local.werewolf_research import ResearchGame, PairCounts
from local.werewolf_counterfactual_research import SETTINGS, digest
from scripts.werewolf_counterfactual_eval import resources, emit, save
from scripts.werewolf_evolution_eval import case_pool
from scripts.audit_werewolf_strength import assessment


REPORT=ROOT/'data/werewolf_robust_vote_results.json'
OUT=ROOT/'logs/werewolf_benchmarks/robust-vote-517'


def canonical_programs():
    return [(0,s,s) for s in range(12)]+[(p,a,b) for p in range(1,12) for a in range(12) for b in range(12) if a!=b]


def genome(genes):
    result=list(BASE)
    result[9:12]=genes
    return tuple(result)


class Evaluator:
    def __init__(self,stream,deadline):
        self.stream,self.deadline=stream,deadline
        self.cache={}
        self.games=0

    def play(self,genes,scenario,timing):
        key=tuple(genes),scenario.key,timing
        if key in self.cache:
            return self.cache[key]
        resources(self.deadline)
        program=genome(genes)
        seed,ours,kinds=ev.deal(scenario.seed,scenario.game,scenario.background,1)
        me=ours[0]
        factory=ev.factory('agent') if not any(program) else lambda:EvolutionContestant(program)
        with wb.environment(SETTINGS|{'ARENA_WW_TIMING':timing}):
            control=wb.MeasuredSeat(factory,{})
            game=ResearchGame([control if s==me else ev.make_seat(kinds[s]) for s in range(7)],random.Random(seed),strict_messaging=True)
            returns=game.play()
        errors={k:sum(game.stats[k]) for k in ev.FAIL_KEYS if k!='window_close'}
        errors.update(policy_errors=control.policy_errors,policy_fallbacks=control.policy_fallbacks,logged_errors=control.logged_errors,
                      fallbacks=control.agent.fallbacks,overruns=control.agent.overruns,listen_errors=control.agent.listen_stats.get('errors',0))
        row={'id':scenario.key,'scenario':asdict(scenario),'timing':timing,'genes':list(genes),'seat':me,'role':game.roles[me],
             'deal_seed':seed,'win':returns[me]>0,'errors':errors,'days':game.day,'max_ms':game.stats['max_ms'][me],
             'trace_hash':digest({'roles':game.roles,'votes':game.votes,'dead':game.dead,'messages':game.messages,'actions':game.action_trace})}
        emit(self.stream,row)
        self.games+=1
        if any(errors.values()):
            raise ValueError('invalid robust program simulation')
        self.cache[key]=row
        return row

    def score(self,genes,cases):
        totals,groups={},{}
        penalty=0
        for timing in ('rt','rounds'):
            total,by_bg=PairCounts(),defaultdict(PairCounts)
            for scenario in cases:
                reference=self.play((0,0,0),scenario,timing)
                candidate=self.play(genes,scenario,timing)
                if candidate['role']!='wolf':
                    raise ValueError('training pool is not wolf-only')
                total.add(candidate['win'],reference['win'])
                by_bg[scenario.background].add(candidate['win'],reference['win'])
            totals[timing]=total.report()
            groups[timing]={b:v.report() for b,v in by_bg.items()}
            penalty+=sum(max(0,-v['delta']) for v in groups[timing].values())/len(by_bg)/2
        worst=min(v['delta'] for v in totals.values())
        return {'genes':list(genes),'fitness':worst-.5*penalty,'worst_timing_delta':worst,'timings':totals,
                'complexity':sum(g!=0 for g in genes[1:]),'backgrounds':groups}


def ranking(row):
    return (-row['fitness'],-row['worst_timing_delta'],row['complexity'],tuple(row['genes']))


def compact(row):
    return {k:v for k,v in row.items() if k!='backgrounds'}


def validate(evaluator,genes,seeds,n,backgrounds,label):
    counts=defaultdict(PairCounts)
    for seed in seeds:
        for background in backgrounds:
            for index in range(n):
                scenario=wb.Scenario(seed,index,background,1)
                for timing in ('rt','rounds'):
                    reference=evaluator.play((0,0,0),scenario,timing)
                    candidate=evaluator.play(genes,scenario,timing)
                    if reference['role']!='wolf' and reference['trace_hash']!=candidate['trace_hash']:
                        raise ValueError('robust program changed good role')
                    for scope in ('all','opponent:'+background,'seed:'+str(seed)):
                        for role in ('seat',reference['role']):
                            counts[timing,scope,role].add(candidate['win'],reference['win'])
            print(label,seed,background,'games',evaluator.games,flush=True)
    metrics={}
    for (timing,scope,role),counter in counts.items():
        metrics.setdefault(timing,{}).setdefault(scope,{})[role]=counter.report()
    return {'metrics':metrics,'assessment':{t:assessment(m) for t,m in metrics.items()}}


def main():
    if REPORT.exists() or OUT.exists():
        raise ValueError('fresh robust-search artifacts required')
    plan=json.loads((ROOT/'data/werewolf_robust_vote_plan.json').read_text())
    deadline=datetime.fromisoformat(plan['resources']['hard_stop']).timestamp()
    latest_start=datetime.fromisoformat(plan['resources']['latest_new_confirmation_start']).timestamp()
    resources(deadline)
    baseline=json.loads((ROOT/'data/werewolf_current_baseline.json').read_text())
    if any(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=pin for name,pin in baseline['checked_werewolf_and_shared_runtime_files'].items()):
        raise ValueError('B0 changed before robust training')
    backgrounds=baseline['protocol']['backgrounds']
    train,scan=case_pool(517,backgrounds[:20],12)
    selection,selection_scan=case_pool(518,backgrounds[:20],12)
    quota=Counter()
    first=[]
    for scenario in train['wolf']:
        if quota[scenario.background]<3:
            first.append(scenario)
            quota[scenario.background]+=1
    source=wb.source_manifest()
    script_sha=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    OUT.mkdir()
    result={'status':'running','plan':plan,'source':source,'script_sha256':script_sha,'scan':scan,'selection_scan':selection_scan,
            'stages':{},'heldouts_used':[],'promoted':False,'started':datetime.now().astimezone().isoformat()}
    save(REPORT,result)
    try:
        with gzip.open(OUT/'games.jsonl.gz','wt',compresslevel=3) as stream:
            evaluator=Evaluator(stream,deadline)
            scored=[]
            for i,genes in enumerate(canonical_programs()):
                scored.append(evaluator.score(genes,first))
                if (i+1)%100==0:
                    top=min(scored,key=ranking)
                    print('ROBUST SCREEN',i+1,'of1464',compact(top),'games',evaluator.games,flush=True)
            scored.sort(key=ranking)
            result['stages']['screen']=[compact(v) for v in scored]
            save(REPORT,result)
            stage2_genes={tuple(v['genes']) for v in scored[:96]}|{(0,0,0)}
            stage2=sorted([evaluator.score(g,train['wolf']) for g in sorted(stage2_genes)],key=ranking)
            result['stages']['full_training']=stage2
            save(REPORT,result)
            selected_genes={tuple(v['genes']) for v in stage2[:8]}|{(0,0,0)}
            selected=sorted([evaluator.score(g,selection['wolf']) for g in sorted(selected_genes)],key=ranking)
            result['stages']['selection']=selected
            best=selected[0]
            chosen=tuple(best['genes']) if best['worst_timing_delta']>0 else (0,0,0)
            result.update(chosen=list(chosen),frozen_at=datetime.now().astimezone().isoformat())
            save(REPORT,result)
            print('ROBUST FROZEN',chosen,compact(best),flush=True)
            evaluator.cache.clear()
            if any(chosen):
                result['development']=validate(evaluator,chosen,(519,),100,backgrounds,'ROBUST DEVELOPMENT')
                save(REPORT,result)
                sufficient=all(a['local_strength_gates_pass'] for a in result['development']['assessment'].values())
                if sufficient and time.time()<latest_start:
                    result['heldouts_used']=[46301,46302]
                    save(REPORT,result)
                    evaluator.cache.clear()
                    result['confirmation']=validate(evaluator,chosen,(46301,46302),500,backgrounds,'ROBUST CONFIRMATION')
            result['actual_simulations']=evaluator.games
        end=wb.source_manifest()
        if end['digest']!=source['digest'] or hashlib.sha256(Path(__file__).read_bytes()).hexdigest()!=script_sha:
            raise ValueError('robust search source changed')
        result.update(status='complete',source_end=end)
    except Exception as exc:
        result.update(status='blocked_partial',error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        result['ended']=datetime.now().astimezone().isoformat()
        save(REPORT,result)
        save(OUT/'summary.json',result)
    print('ROBUST DONE',result.get('chosen'),result.get('confirmation',{}).get('assessment'),flush=True)


if __name__=='__main__':
    main()
