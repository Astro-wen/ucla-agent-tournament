from collections import Counter
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import random
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from altruagent import GameState
from local import eval_werewolf as ev
from local import werewolf_benchmark as wb
from local.werewolf_env import deal_roles
from local.werewolf_minimal_research import MinimalPolicy
from local.werewolf_counterfactual_research import SETTINGS, digest
from scripts import werewolf_local_model_eval as lm
from scripts import werewolf_model_scale_eval as scale
from scripts.werewolf_minimal_model_eval import PilotProvider, PilotStop, SourceLock, wall_limit, verify_cache_keys


OUT=ROOT/'logs/werewolf_benchmarks/evolution-local14b-515'
REPORT=ROOT/'data/werewolf_evolution_llm_results.json'
PIN='7cdf5a0187d5c58cc5d369b255592f7841d1c4696d45a8c8a9489440385b22f6'
MODEL='qwen2.5:14b-instruct-q4_K_M'


class GuardedLocalSeat(lm.LocalLLMSeat):
    def reset(self):
        super().reset()
        self.guard=MinimalPolicy()
        self.guard_stats=Counter()

    def allowed(self,raw):
        v=self.guard._view(raw)
        legal={int(a['action_id']) for a in raw['legal_actions']['actions']}
        allowed=set(legal)
        if v.role=='seer' and v.sub_phase=='night_seer':
            unread=allowed-set(v.investigations)
            allowed=unread or allowed
        elif v.sub_phase=='day_vote' and v.role in ('seer','villager'):
            known,clears=self.guard.hard_knowledge(v)
            wolves=allowed & known
            allowed=wolves if wolves else allowed-clears
        if not allowed:
            raise ValueError('hard facts contradict the entire legal action set')
        return allowed

    def choose(self,raw):
        allowed=self.allowed(raw)
        masked=deepcopy(raw)
        masked['legal_actions']['actions']=[a for a in masked['legal_actions']['actions'] if int(a['action_id']) in allowed]
        self.guard_stats['decisions']+=1
        self.guard_stats['removed_options']+=len(raw['legal_actions']['actions'])-len(allowed)
        if len(allowed)==1:
            self.guard_stats['forced_fact_action']+=1
            self.agent._remember(GameState.from_mcp_state(masked))
            return str(next(iter(allowed)))
        return super().choose(masked)

    def message(self,raw):
        self.guard.observe(raw)
        return super().message(raw)


def provider_for_plan(plan,check,out=None):
    provider=PilotProvider(check_run=check,model=MODEL,expected_digest=PIN,out=out,
                           timeout=plan['resources']['request_timeout_seconds'])
    provider.options['seed']=plan['model']['decoder_seed']
    if provider.options['num_ctx']!=plan['model']['context'] or provider.options['num_predict']!=plan['model']['output_tokens']:
        raise ValueError('model context/output budget differs from the registered plan')
    return provider


def cases(plan):
    result,scan=[],[]
    for background in plan['sample']['backgrounds']:
        selected=0
        for index in range(1000):
            seed,ours,kinds=ev.deal(515,index,background,1)
            role=deal_roles(random.Random(seed))[ours[0]]
            accepted=role=='wolf'
            scan.append({'background':background,'game':index,'role':role,'accepted':accepted})
            if accepted:
                placements=random.Random(f'evolution-llm-placements:515:{background}:{index}').sample([s for s in range(7) if s!=ours[0]],2)
                result.append({'id':f'515/{background}/k1/{index}','game':index,'background':background,'seed':seed,
                               'ours':ours[0],'types':kinds,'llm_seats':sorted(placements)})
                selected+=1
                if selected==4:
                    break
        else:
            raise ValueError('wolf quota unavailable')
    return result,scan


def play(case,profile,factory,provider):
    me=case['ours']
    with wb.environment(SETTINGS):
        controlled=wb.MeasuredSeat(ev.factory(factory),{})
        seats=[controlled if s==me else GuardedLocalSeat(provider) if s in case['llm_seats'] else ev.make_seat(case['types'][s]) for s in range(7)]
        game=lm.ObservingResearchGame(seats,random.Random(case['seed']),strict_messaging=True)
        returns=game.play()
    llms={s:seat for s,seat in enumerate(seats) if isinstance(seat,GuardedLocalSeat)}
    errors={k:sum(game.stats[k]) for k in ev.FAIL_KEYS if k!='window_close'}
    errors.update(policy_errors=controlled.policy_errors,policy_fallbacks=controlled.policy_fallbacks,
                  logged_errors=controlled.logged_errors,fallbacks=controlled.agent.fallbacks,overruns=controlled.agent.overruns)
    return {'id':case['id'],'profile':profile,'win':returns[me]>0,'seat':me,'role':game.roles[me],
            'roles_evaluator_only':game.roles,'votes':game.votes,'dead':game.dead,'messages':game.messages,
            'actions':game.action_trace,'errors':errors,'max_ms':game.stats['max_ms'],
            'llm_stats':{s:dict(seat.stats) for s,seat in llms.items()},
            'guard_stats':{s:dict(seat.guard_stats) for s,seat in llms.items()},
            'model_logs':{s:seat.logs for s,seat in llms.items()}}


def main():
    if OUT.exists() or REPORT.exists():
        raise ValueError('new local model pilot artifacts required')
    plan=json.loads((ROOT/'data/werewolf_evolution_llm_plan.json').read_text())
    selection=json.loads((ROOT/'data/werewolf_evolution_confirmation.json').read_text())
    chosen=selection.get('selected')
    if chosen not in ('vote_only','night_only','combined') or not selection.get('frozen_selection_at'):
        raise ValueError('505/506must freeze a candidate before model pilot')
    stop=datetime.fromisoformat(plan['resources']['deadline']).timestamp()
    base_resource=scale.resource_state()
    if base_resource['pressure']>=4 or base_resource['disk_free']<2*1024**3:
        raise ValueError('unsafe resources before model load')
    OUT.mkdir()
    (OUT/'requests').mkdir()
    pool,scan=cases(plan)
    source=lm.source_manifest()
    for name in ('scripts/werewolf_evolution_llm_eval.py','tests/strategy/test_werewolf_evolution_llm.py'):
        source['files'][name]=hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
    source['digest']=digest(source['files'])
    lock=SourceLock(source)
    report={'status':'running','plan':plan,'selected':chosen,'scenarios':pool,'selection_scan':scan,'source':source,
            'resources':[base_resource],'games':[],'external_cost_usd':0}
    lm.write_json(OUT/'manifest.json',report)
    lm.write_json(REPORT,report)
    provider=None
    def check():
        if time.time()>=stop:
            raise PilotStop('deadline_partial')
        lock.check()
        state=scale.resource_state()
        if not scale.resource_safe(state,base_resource) or state['disk_free']<2*1024**3:
            raise PilotStop('unsafe_resources')
        if provider is not None and len(provider.events)>=600:
            raise PilotStop('model_request_budget')
    loaded=False
    try:
        provider=provider_for_plan(plan,check,out=OUT)
        before=provider.inspect_server()
        if before['/api/ps'].get('models'):
            raise PilotStop('isolated_server_not_empty_before_run')
        check()
        loaded=True
        provider.request('POST','/api/chat',{'model':MODEL,'messages':[{'role':'user','content':'Reply OK.'}],
                                          'stream':False,'keep_alive':-1,'options':dict(provider.options,num_predict=8)})
        health=provider.inspect_server()
        if not provider.context_verified(health):
            raise PilotStop('context_verification_failed')
        report['health']=health
        with wall_limit(stop-time.time()):
            for case in pool:
                for label,factory in [('reference','agent'),('candidate','local.werewolf_evolution_ablation:'+chosen)]:
                    check()
                    start=len(provider.events)
                    row=play(case,label,factory,provider)
                    row['requests']=[start,len(provider.events)]
                    report['games'].append(row)
                    lm.write_json(OUT/'games.json',report['games'])
                    print('LOCAL14B',case['id'],label,row['win'],len(provider.events),datetime.now().astimezone().isoformat(),flush=True)
        report['cache_audit']=verify_cache_keys(provider.events,MODEL,PIN)
        report['status']='complete'
    except (Exception,PilotStop) as exc:
        report.update(status='blocked_partial',error=f'{type(exc).__name__}: {exc}')
    finally:
        if provider is not None:
            report['provider']=lm.provider_summary(provider)
            if loaded:
                try:
                    provider.request('POST','/api/chat',{'model':MODEL,'messages':[],'stream':False,'keep_alive':0})
                    report['unloaded']=True
                except Exception as exc:
                    report['unload_error']=type(exc).__name__
        try:
            report['source_unchanged']=all(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==pin for name,pin in source['files'].items())
        except OSError as exc:
            report['source_unchanged']=False
            report['source_validation_error']=f'{type(exc).__name__}: {exc}'
        report['ended']=datetime.now().astimezone().isoformat()
        lm.write_json(REPORT,report)
        lm.write_json(OUT/'summary.json',report)
    print('LOCAL14B DONE',report['status'],len(report['games']),flush=True)


if __name__=='__main__':
    main()
