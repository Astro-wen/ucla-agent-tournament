source ~/wt/pk1015/_notes/env.sh
mkdir -p _notes/screen
for s in 127 197; do
  for o in champion meta; do
    [ -s _notes/screen/$o-$s.jsonl ] && continue
    python scripts/draft_sim.py --opp $o --games 800 --seed $s --cand off='{}' --out _notes/screen/$o-$s.jsonl > _notes/screen/$o-$s.log 2>&1 &
  done
  wait
done
echo done > _notes/screen/DONE
