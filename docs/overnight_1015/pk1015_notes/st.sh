source ~/wt/pk1015/_notes/env.sh
date +%T
tail -n 1 _notes/memguard.log
wc -l _notes/runs/*.jsonl
for f in _notes/dev_base.log _notes/dev_cand.log _notes/fresh_base.log _notes/fresh_cand.log; do [ -f $f ] && { echo "== $f"; grep -E "start|end|^(meta|champion) " $f | tail -n 4; }; done
