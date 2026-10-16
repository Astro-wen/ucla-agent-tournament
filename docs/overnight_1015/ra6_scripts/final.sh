#!/usr/bin/env bash
# final.sh <tag> [names...]: FINAL measurement of the v4.8 branch on fresh seeds 1551-1567 (never used before: not in any
# v4.5-v4.7 list, not the dev seeds 1501-1510). Snapshot <tag> = the final commit. Three arms on the same seeds:
#   fb_<name>   base: every v4.8 switch off (= v4.7; identity checked against the v47 snapshot = main 4dc4457)
#   fin_<name>  the candidate defaults (only anchor2 on: lockstep games must be the same as base)
#   fs_<name>   information only: split_fire on (its default parameters = the dev variant "S"), not shipped
# Stress and h2h: base and candidate only. Never edit while running.
R6=/home/linmengfdm/r6
O=/home/linmengfdm/wt/_ra6_out
t=$1; shift
want="$*"
[ -d /home/linmengfdm/wt/_ra6_snap/$t ] || { echo "no snapshot $t"; exit 2; }
for v in $(env | grep -o '^ARENA_RA_[A-Z0-9_]*'); do unset $v; done
OFF="ARENA_RA_AT_TOWERS=0 ARENA_RA_AT_COMP=0 ARENA_RA_HF_KEEP=0 ARENA_RA_ANCHOR2=0 ARENA_RA_SPLIT_FIRE=0"
MAIN=idle,rusher,turtle,llm
STRONG=tanks,swarm,pro,raider,hider
S=/home/linmengfdm/wt/_ra6_snap/$t
sel() { [ -z "$want" ] || [[ " $want " == *" $1 "* ]]; }
run3() {   # name opps games seed [extra]: base, candidate, split
  name=$1; opps=$2; games=$3; seed=$4; shift 4
  sel $name || return 0
  echo "##### $name ($opps x$games seed $seed $*) $(date +%T)"
  ( export $OFF; SNAP=$S bash $R6/lg.sh fb_$name ours "$opps" $games $seed "$@" > /dev/null )
  SNAP=$S bash $R6/lg.sh fin_$name ours "$opps" $games $seed "$@" > /dev/null
  ( export ARENA_RA_SPLIT_FIRE=1; SNAP=$S bash $R6/lg.sh fs_$name ours "$opps" $games $seed "$@" > /dev/null )
  (cd $O && python3 $R6/compare.py fb_$name.json fin_$name.json | grep -E '^  ALL' | sed 's/^/  cand  /' ;
   python3 $R6/compare.py fb_$name.json fs_$name.json | grep -E '^  ALL' | sed 's/^/  split /')
}
run2() {   # name opps games seed [extra]: base and candidate
  name=$1; opps=$2; games=$3; seed=$4; shift 4
  sel $name || return 0
  echo "##### $name ($opps x$games seed $seed $*) $(date +%T)"
  ( export $OFF; SNAP=$S bash $R6/lg.sh fb_$name ours "$opps" $games $seed "$@" > /dev/null )
  SNAP=$S bash $R6/lg.sh fin_$name ours "$opps" $games $seed "$@" > /dev/null
  (cd $O && python3 $R6/compare.py fb_$name.json fin_$name.json | grep -E '^  ALL|health')
}
run3 tom1551 tom 240 1551
run3 tomsf1552 tom 120 1552 --maps shadowfiend-ii
run3 nb1553 riffo,nashball 240 1553
run3 nash1554 nash 120 1554
run3 pro1555 pro 120 1555
run3 strong1556 $STRONG 120 1556
run3 main1557 $MAIN 96 1557
run3 rusher1558 rusher 60 1558
run3 sfmix1559 pro,nash,nashball,riffo,tanks,swarm,raider,rusher 96 1559 --maps shadowfiend-ii
run2 fault1560 $MAIN,tom 30 1560 --fault-rate 0.05
run2 lost1561 $MAIN,tom 30 1561 --lost-rate 0.01
run2 win1562 $MAIN,tom 15 1562 --pacing windows
run2 cash1563 $MAIN,$STRONG,tom 60 1563 --cash 10000
run2 lat1564 $MAIN,$STRONG,tom 60 1564 --latency-ticks 25
run2 lat1567 tom,nash,nashball 60 1567 --latency-ticks 50
run2 h2h1565 champion.agent.agent:create_agent 96 1565
if sel rt1566; then
  echo "##### rt1566 (realtime clock speed 8, 4 games each, not reproducible) $(date +%T)"
  SNAP=$S bash $R6/lg.sh fin_rt1566 ours tom,riffo,nashball,rusher 4 1566 --clock realtime --speed 8 | tail -1
  ( export $OFF; SNAP=$S bash $R6/lg.sh fb_rt1566 ours tom,riffo,nashball,rusher 4 1566 --clock realtime --speed 8 | tail -1 )
fi
if sel ident; then
  # the base (every v4.8 switch off) is v4.7 game by game: the v47 snapshot (main 4dc4457) on the same seeds
  for spec in "strong1556 $STRONG 120 1556" "main1557 $MAIN 96 1557" "nash1554 nash 120 1554"; do
    set -- $spec
    echo "##### ident v47_$1 $(date +%T)"
    SNAP=/home/linmengfdm/wt/_ra6_snap/v47 bash $R6/lg.sh v47_$1 ours "$2" $3 $4 > /dev/null
    (cd $O && python3 $R6/compare.py v47_$1.json fb_$1.json | grep -E '^  ALL')
  done
fi
echo "ALLDONE $want $(date +%T)"
exit 0
