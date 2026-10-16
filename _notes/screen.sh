source /home/user/ucla-agent-tournament/_notes/env.sh
mkdir -p _notes/screen
s=197
for o in champion meta; do
  [ -s _notes/screen/$o-$s.jsonl ] && continue
  python scripts/draft_sim.py --opp $o --games 800 --seed $s --cand off='{}' --out _notes/screen/$o-$s.jsonl > _notes/screen/$o-$s.log 2>&1 &
done
wait
echo done > _notes/screen/DONE
