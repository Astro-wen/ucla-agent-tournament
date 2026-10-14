from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import random
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from local import eval_werewolf as ev
from local import werewolf_benchmark as wb
from local.werewolf_counterfactual_research import SETTINGS
from local.werewolf_evolution_research import EvolutionPolicy
from local.werewolf_research import ResearchGame
from scripts.werewolf_counterfactual_eval import save


class ProbeSeat(wb.MeasuredSeat):
    def __init__(self,factory):
        super().__init__(factory,{})
        self.probes=Counter()

    def choose(self,raw):
        before=deepcopy(self.agent.ww.export_memory())
        answer=super().choose(raw)
        genome=self.agent.ww.genome
        clone=EvolutionPolicy(genome)
        clone.import_memory(deepcopy(before))
        replay=clone.act(deepcopy(raw))['action_id']
        if answer!=replay:
            raise ValueError('restart clone changed the visible-state action')
        poisoned=deepcopy(raw)
        poisoned.update(evaluator_seed=-1,hidden_roles=['wolf']*7,opponent_types=['claimed_privileged_type']*7)
        poisoned['game_state']['unavailable_roles']=['seer']*7
        for player in poisoned['players']:
            if player['position']!=poisoned['your_position']:
                player.update(role='wolf',secret_role='seer',agent_id='opaque-other-seat-id')
        shadow=EvolutionPolicy(genome)
        shadow.import_memory(deepcopy(before))
        actual=shadow.act(poisoned)['action_id']
        if actual!=answer:
            raise ValueError('unavailable information changed policy action')
        legal={a['action_id'] for a in raw['legal_actions']['actions']}
        if actual not in legal:
            raise ValueError('poison probe produced an illegal action')
        self.probes['actions']+=1
        self.probes[raw['game_state']['phase']]+=1
        self.probes['hidden_metadata_invariant']+=1
        self.probes['restart_equal']+=1
        return answer


def main():
    output=ROOT/'data/werewolf_evolution_isolation_probe.json'
    if output.exists():
        raise ValueError('new probe report required')
    confirmation=json.loads((ROOT/'data/werewolf_evolution_confirmation.json').read_text())
    chosen=confirmation['selected']
    backgrounds=json.loads((ROOT/'data/werewolf_current_baseline.json').read_text())['protocol']['backgrounds']
    totals,roles=Counter(),Counter()
    source=wb.source_manifest()
    for timing in ('rt','rounds'):
        for background in backgrounds:
            for i in range(10):
                seed,ours,kinds=ev.deal(9001,i,background,1)
                me=ours[0]
                with wb.environment(SETTINGS|{'ARENA_WW_TIMING':timing}):
                    seat=ProbeSeat(ev.factory('local.werewolf_evolution_ablation:'+chosen))
                    game=ResearchGame([seat if s==me else ev.make_seat(kinds[s]) for s in range(7)],random.Random(seed),strict_messaging=True)
                    game.play()
                errors={k:sum(game.stats[k]) for k in ev.FAIL_KEYS if k!='window_close'}
                errors.update(policy_errors=seat.policy_errors,policy_fallbacks=seat.policy_fallbacks,logged_errors=seat.logged_errors,
                              fallbacks=seat.agent.fallbacks,overruns=seat.agent.overruns)
                if any(errors.values()):
                    raise ValueError(f'probe failed:{errors}')
                totals.update(seat.probes)
                totals['games']+=1
                roles[game.roles[me]]+=1
            print('ISOLATION',timing,background,totals['actions'],flush=True)
    end=wb.source_manifest()
    if source['digest']!=end['digest']:
        raise ValueError('source drift during probe')
    result={'status':'pass','selected':chosen,'counts':dict(totals),'role_games':dict(roles),'source':source,
            'scope':'permitted-input/hidden-metadata and restart invariance on560actual trajectories,280deals reused across timings; not extra strength data'}
    save(output,result)
    print('ISOLATION PASSED',dict(totals),flush=True)


if __name__=='__main__':
    main()
