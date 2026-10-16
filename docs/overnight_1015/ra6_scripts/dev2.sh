#!/usr/bin/env bash
# dev.sh <snap-tag> <set> <cfg>...: dev-seed leagues (seeds 1501-1510, tuning only) of snapshot <snap-tag> for each
# config: OFF (every v4.8 switch off = v4.7), ALL (defaults), T (at_towers only), C (at_comp only), H (hf_keep only),
# V47 (the v47 snapshot = main, defaults). JSON + log in ~/wt/_ra6_out/<cfg>_<tag>_<name>.json.
# set: tom | core | ident
R6=/home/linmengfdm/r6
t=$1; set_=$2; shift 2
for v in $(env | grep -o '^ARENA_RA_[A-Z0-9_]*'); do unset $v; done
OFF="ARENA_RA_AT_TOWERS=0 ARENA_RA_AT_COMP=0 ARENA_RA_HF_KEEP=0 ARENA_RA_ANCHOR2=0"
MAIN=idle,rusher,turtle,llm
STRONG=tanks,swarm,pro,raider,hider
cfgenv() {
  case $1 in
    OFF) echo "$OFF";;
    ALL|V47) echo "";;
    T) echo "ARENA_RA_AT_COMP=0 ARENA_RA_HF_KEEP=0";;
    C) echo "ARENA_RA_AT_TOWERS=0 ARENA_RA_HF_KEEP=0";;
    H) echo "ARENA_RA_AT_TOWERS=0 ARENA_RA_AT_COMP=0";;
    TC) echo "ARENA_RA_HF_KEEP=0";;
    TH) echo "ARENA_RA_AT_COMP=0";;
    *) echo "$1" | tr ',' ' ';;
  esac
}
run() {   # cfg name opps games seed [extra]
  cfg=$1; name=$2; opps=$3; games=$4; seed=$5; shift 5
  S=/home/linmengfdm/wt/_ra6_snap/$t
  label=${cfg}_${t}_$name
  [ $cfg = V47 ] && { S=/home/linmengfdm/wt/_ra6_snap/v47; label=V47_$name; }
  echo "##### $label ($opps x$games seed $seed $*) $(date +%T)"
  ( e=$(cfgenv $cfg); [ -n "$e" ] && export $e; SNAP=$S bash $R6/lg.sh $label ours "$opps" $games $seed "$@" | tail -1 )
}
for cfg in "$@"; do
  case $set_ in
    tom) run $cfg tom1501 tom 120 1501;;
    tom2) run $cfg tom1507 tom 120 1507;;
    sf) run $cfg sftom1508 tom 60 1508 --maps shadowfiend-ii; run $cfg sfnash1509 nash,pro 60 1509 --maps shadowfiend-ii;;
    core)
      run $cfg nb1502 riffo,nashball 120 1502
      run $cfg pro1503 pro 60 1503
      run $cfg nash1504 nash 60 1504
      run $cfg strong1505 $STRONG 60 1505
      run $cfg main1506 $MAIN 48 1506;;
    ident)
      run $cfg strong1505 $STRONG 60 1505
      run $cfg main1506 $MAIN 48 1506
      run $cfg nash1504 nash 60 1504;;
  esac
done
echo "ALLDONE $set_ $* $(date +%T)"
