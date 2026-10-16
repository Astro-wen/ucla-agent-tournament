# usage: bash arm.sh NAME SEED "ENV" opp1:maxdeals opp2:maxdeals ...   (ENV e.g. ARENA_SEARCH_SUCKER=1 or "")
# Plays the deals whose screened six has a Sucker Punch user; --resume so a killed arm can be restarted.
source ~/wt/pk1015/_notes/env.sh
name=$1; seed=$2; envkv=$3; shift 3
for spec in "$@"; do
  opp=${spec%%:*}; mx=${spec##*:}
  only=$(python _notes/sel.py $opp $seed $mx 2>/dev/null)
  extra=""
  [ -n "$envkv" ] && extra="--env $envkv"
  while [ $(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo) -lt 2200 ]; do echo "$(date +%T) waiting for memory"; sleep 60; done
  echo "$(date +%T) start $name $opp seed $seed: $(echo $only | tr ',' ' ' | wc -w) deals"
  python -m local.panel --opps $opp --games 1000 --seed $seed --only "$only" --jobs 2 --name $name $extra \
     --out _notes/runs/$name-$opp-$seed.jsonl --resume --save-logs --progress 120 2>&1 | grep -v "^\s*$" | tail -30
  echo "$(date +%T) end $name $opp"
done
