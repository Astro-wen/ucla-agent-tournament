#!/usr/bin/env bash
# final5.sh <tag> [names...]: FINAL measurement #2 (after the real-recording check of 9a15e61) on fresh seeds
# 1471-1482 (never used before): base = the same snapshot <tag> with every v4.7 switch off (= v4.6), new = <tag>
# defaults. f2b_<name>.json (base), <tag>_<name>.json (new). h2h1481: new vs v4.6 AND base vs v4.6 on the same seeds
# (the head-to-head harness is not 50% for identical code on given seeds: compare paired).
R6=/home/linmengfdm/r6
O=/home/linmengfdm/wt/_ra6_out
t=$1; shift
want="$*"
[ -d /home/linmengfdm/wt/_ra6_snap/$t ] || { echo "no snapshot $t"; exit 2; }
for v in $(env | grep -o '^ARENA_RA_[A-Z0-9_]*'); do unset $v; done
OFF="ARENA_RA_ATTACK_GATE=0 ARENA_RA_TRADE_RETREAT=0 ARENA_RA_ATTACK_COMP=0 ARENA_RA_RECOVER=0"
MAIN=idle,rusher,turtle,llm
STRONG=tanks,swarm,pro,raider,hider
S=/home/linmengfdm/wt/_ra6_snap/$t
run() {   # name opps games seed [extra]
  name=$1; opps=$2; games=$3; seed=$4; shift 4
  if [ -n "$want" ] && [[ " $want " != *" $name "* ]]; then return; fi
  echo "##### $name ($opps x$games seed $seed $*) $(date +%T)"
  ( export $OFF; SNAP=$S bash $R6/lg.sh f2b_$name ours "$opps" $games $seed "$@" > /dev/null )
  SNAP=$S bash $R6/lg.sh ${t}_$name ours "$opps" $games $seed "$@" > /dev/null
  (cd $O && python3 $R6/compare.py f2b_$name.json ${t}_$name.json | grep -E '^  [A-Za-z]' | head -14)
}
run new1471 riffo,nashball 240 1471
run nash1472 nash 120 1472
run pro1473 pro 120 1473
run strong1474 $STRONG 120 1474
run main1475 $MAIN 96 1475
run rusher1476 rusher 60 1476
run fault1477 $MAIN 24 1477 --fault-rate 0.05
run lost1478 $MAIN 24 1478 --lost-rate 0.01
run win1479 $MAIN 12 1479 --pacing windows
run cash1480 $MAIN,$STRONG 54 1480 --cash 10000
run h2h1481 champion.agent.agent:create_agent 96 1481
if [ -z "$want" ] || [[ " $want " == *" rt1482 "* ]]; then
  echo "##### rt1482 (realtime clock speed 8, 4 games each, not reproducible) $(date +%T)"
  SNAP=$S bash $R6/lg.sh ${t}_rt1482 ours riffo,nashball,rusher,pro 4 1482 --clock realtime --speed 8 | tail -1
  ( export $OFF; SNAP=$S bash $R6/lg.sh f2b_rt1482 ours riffo,nashball,rusher,pro 4 1482 --clock realtime --speed 8 | tail -1 )
fi
echo ALLDONE $(date +%T)
