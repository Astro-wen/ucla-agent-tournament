# Keep-first A/B: (1) cand on the seed-197 deals already played by base; (2) fresh seed 263, both arms, all deals.
source /home/user/ucla-agent-tournament/_notes/env.sh
E=ARENA_PREVIEW_KEEP_FIRST=1
run() {  # name opp seed only env
  extra=""; [ -n "$5" ] && extra="--env $5"
  python -m local.panel --opps $2 --games 1000 --seed $3 --only "$4" --jobs 2 --name $1 $extra \
     --out _notes/runs/$1-$2-$3.jsonl --resume --save-logs --progress 300 2>&1 | grep -v "^\s*$" | tail -12
  echo "$(date +%T) end $1 $2 $3"
}
m=$(python _notes/sel.py meta 197 150 2>/dev/null); c=$(python _notes/sel.py champion 197 100 2>/dev/null)
echo "$(date +%T) stage 1"
run kf1 meta 197 "$m" $E & run kf1 champion 197 "$c" $E & wait
echo "$(date +%T) stage 2 seed 263"
run base meta 263 "0-399" "" & run kf1 meta 263 "0-399" $E & wait
