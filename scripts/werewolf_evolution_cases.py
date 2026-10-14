from collections import defaultdict
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from scripts.audit_werewolf_counterfactual import read_rows
from scripts.werewolf_counterfactual_eval import save
from local.werewolf_benchmark import Scenario
from local.werewolf_research import run_task
from local.werewolf_counterfactual_research import SETTINGS


def first_difference(a,b):
    for index in range(max(len(a),len(b))):
        left=a[index] if index<len(a) else None
        right=b[index] if index<len(b) else None
        if left!=right:
            return {'index':index,'reference':left,'candidate':right}
    return None


def main():
    path=ROOT/'data/werewolf_evolution_mechanism_cases.json'
    if path.exists():
        raise ValueError('new mechanism report required')
    grouped=defaultdict(dict)
    raw=ROOT/'logs/werewolf_benchmarks/evolution-ablation-505-506-rt/games.jsonl.gz'
    for row in read_rows(raw):
        if row['profile'] in ('reference','vote_only'):
            grouped[row['id']][row['profile']]=row
    examples=[]
    for improved in (True,False):
        matches=[key for key,rows in grouped.items() if rows['vote_only']['win']!=rows['reference']['win']
                 and rows['vote_only']['win']==improved]
        key=sorted(matches)[0]
        previous=grouped[key]
        data=previous['reference']['scenario']
        scenario=Scenario(data['seed'],data['game'],data['background'],data['k'])
        rows=run_task((scenario,[('reference','agent',{}),('vote_only','local.werewolf_evolution_ablation:vote_only',{})],SETTINGS,True,True))
        for row in rows:
            original=previous[row['profile']]
            if any(row[k]!=original[k] for k in ('win','role','seat','winner','days','chats','dms','diagnostics')):
                raise ValueError('selected mechanism replay did not reproduce original game')
        ref,cand=rows
        examples.append({'id':key,'improved':improved,'role':ref['role'],'seat':ref['seat'],
                         'first_message_difference':first_difference(ref['trace']['messages'],cand['trace']['messages']),
                         'first_action_difference':first_difference(ref['trace']['actions'],cand['trace']['actions']),
                         'reference':ref,'candidate':cand})
    result={'selection':'posthoc first lexicographic improved and worsened development505/506case; explanatory only, not new independent strength evidence',
            'additional_replay_games':4,'all_selected_replays_reproduced':True,'cases':examples}
    save(path,result)
    print(json.dumps({'replay_verified':True,'cases':[{'id':c['id'],'improved':c['improved'],'first_message_difference':c['first_message_difference'],
                                                   'first_action_difference':c['first_action_difference']} for c in examples]},indent=2))


if __name__=='__main__':
    main()
