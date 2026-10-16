source ~/wt/pk1015/_notes/env.sh
pkill -f "_notes/fresh_base.sh"; pkill -f "_notes/fresh_cand.sh"; pkill -f "arm.sh base 127"; pkill -f "arm.sh suck1 127"
pkill -f "local.panel.*_notes/runs/.*-champion-127"
sleep 8
ps aux | grep "local.panel" | grep -v grep | wc -l
python _notes/paired.py _notes/runs/suck1-champion-127.jsonl _notes/runs/base-champion-127.jsonl
mv _notes/fresh_base.log _notes/fresh_base_c127.log; mv _notes/fresh_cand.log _notes/fresh_cand_c127.log
bash _notes/bg.sh _notes/fresh_base.sh _notes/fresh_base.log
bash _notes/bg.sh _notes/fresh_cand.sh _notes/fresh_cand.log
