#!/usr/bin/env bash
# dev3.sh <snap-tag> <set> <cfg>...: more dev-seed leagues (tuning only; seeds 1508-1510). Never edit while running.
# set: sfmix (shadowfiend-ii only, the opponents that set off the early-army sign: hf_keep) | core2
R6=/home/linmengfdm/r6
t=$1; set_=$2; shift 2
for v in $(env | grep -o '^ARENA_RA_[A-Z0-9_]*'); do unset $v; done
OFF="ARENA_RA_AT_TOWERS=0 ARENA_RA_AT_COMP=0 ARENA_RA_HF_KEEP=0 ARENA_RA_ANCHOR2=0"
MAIN=idle,rusher,turtle,llm
STRONG=tanks,swarm,pro,raider,hider
cfgenv() {
  case $1 in
    OFF) echo "$OFF";;
    ALL) echo "";;
    T) echo "ARENA_RA_AT_COMP=0 ARENA_RA_HF_KEEP=0";;
    C) echo "ARENA_RA_AT_TOWERS=0 ARENA_RA_HF_KEEP=0";;
    H) echo "ARENA_RA_AT_TOWERS=0 ARENA_RA_AT_COMP=0";;
    CH) echo "ARENA_RA_AT_TOWERS=0";;
    *) echo "$1" | tr ',' ' ';;
  esac
}
run() {   # cfg name opps games seed [extra]
  cfg=$1; name=$2; opps=$3; games=$4; seed=$5; shift 5
  S=/home/linmengfdm/wt/_ra6_snap/$t
  label=${cfg}_${t}_$name
  echo "##### $label ($opps x$games seed $seed $*) $(date +%T)"
  ( e=$(cfgenv $cfg); [ -n "$e" ] && export $e; SNAP=$S bash $R6/lg.sh $label ours "$opps" $games $seed "$@" | tail -1 )
}
for cfg in "$@"; do
  case $set_ in
    sfmix) run $cfg sfmix1508 pro,nash,nashball,riffo,tanks,swarm,raider,rusher 96 1508 --maps shadowfiend-ii;;
    core2)
      run $cfg nb1502 riffo,nashball 120 1502
      run $cfg pro1503 pro 60 1503
      run $cfg nash1504 nash 60 1504
      run $cfg strong1505 $STRONG 60 1505
      run $cfg main1506 $MAIN 48 1506;;
  esac
done
echo "ALLDONE $set_ $*"
exit 0
