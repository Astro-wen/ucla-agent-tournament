#!/usr/bin/env bash
# final7.sh <tag> <part> [names...]: FINAL measurement on fresh seeds 1651-1666 (never used before) of snapshot <tag>
# (the candidate commit, v4.9 defaults on): base = the same snapshot with every v4.9 switch off (= main),
# new = defaults. fb_<name>.json (base), <tag>_<name>.json (new). part A / B split the sets over two queues.
R6=/home/linmengfdm/r6
O=/home/linmengfdm/wt/_ra6_out
t=$1; part=$2; shift 2
want="$*"
[ -d /home/linmengfdm/wt/_ra6_snap/$t ] || { echo "no snapshot $t"; exit 2; }
for v in $(env | grep -o '^ARENA_RA_[A-Z0-9_]*'); do unset $v; done
OFF="ARENA_RA_RALLY_STEP=0 ARENA_RA_PRIOR_HOLD=0"
MAIN=idle,rusher,turtle,llm
STRONG=tanks,swarm,pro,raider,hider
S=/home/linmengfdm/wt/_ra6_snap/$t
run() {   # part name opps games seed [extra]
  p=$1; name=$2; opps=$3; games=$4; seed=$5; shift 5
  [ "$p" = "$part" ] || return
  if [ -n "$want" ] && [[ " $want " != *" $name "* ]]; then return; fi
  echo "##### $name ($opps x$games seed $seed $*) $(date +%T)"
  ( export $OFF; SNAP=$S bash $R6/lg.sh fb_$name ours "$opps" $games $seed "$@" > /dev/null )
  SNAP=$S bash $R6/lg.sh ${t}_$name ours "$opps" $games $seed "$@" > /dev/null
  (cd $O && BYMAP=1 python3 $R6/compare.py fb_$name.json ${t}_$name.json | grep -E '^  [A-Za-z]' | grep -v health | cut -c1-200)
}
run A t2_1651 tom2,tom2p 240 1651
run B tom_1653 tom 60 1653
run B nb_1654 riffo,nashball 120 1654
run A nash_1655 nash 120 1655
run B pro_1656 pro 60 1656
run B strong_1657 $STRONG 60 1657
run A main_1658 $MAIN 48 1658
run B cash_1660 $MAIN,$STRONG 27 1660 --cash 10000
run A lat_1661 tom2,nash,nashball 30 1661 --latency-ticks 25
run B fault_1662 $MAIN,tom2 15 1662 --fault-rate 0.05
run A lost_1663 $MAIN,tom2 15 1663 --lost-rate 0.01
run B win_1664 $MAIN,tom2 10 1664 --pacing windows
run A h2h_1665 champion.agent.agent:create_agent 48 1665
if [ "$part" = B ] && { [ -z "$want" ] || [[ " $want " == *" rt_1666 "* ]]; }; then
  echo "##### rt_1666 (realtime clock speed 8, 1 game each, not reproducible) $(date +%T)"
  SNAP=$S bash $R6/lg.sh ${t}_rt_1666 ours tom2,nash,nashball,rusher 4 1666 --clock realtime --speed 8 | tail -1
  ( export $OFF; SNAP=$S bash $R6/lg.sh fb_rt_1666 ours tom2,nash,nashball,rusher 4 1666 --clock realtime --speed 8 | tail -1 )
fi
echo ALLDONE $part $(date +%T)
