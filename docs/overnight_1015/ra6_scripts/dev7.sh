#!/usr/bin/env bash
# dev6.sh <snap-tag> <set> <cfg>...: dev-seed leagues (seeds 1601-1620, tuning only) of snapshot <snap-tag>.
# cfg = OFF (every v4.9 switch off = main) or a comma list of ENV=VAL (label = cfg name before ':' if given as
# NAME:ENV=VAL,ENV=VAL). JSON + log in ~/wt/_ra6_out/<name>_<tag>_<set-part>.json.
R6=/home/linmengfdm/r6
t=$1; set_=$2; shift 2
for v in $(env | grep -o '^ARENA_RA_[A-Z0-9_]*'); do unset $v; done
MAIN=idle,rusher,turtle,llm
STRONG=tanks,swarm,pro,raider,hider
run() {   # name envs part opps games seed [extra]
  name=$1; envs=$2; part=$3; opps=$4; games=$5; seed=$6; shift 6
  S=/home/linmengfdm/wt/_ra6_snap/$t
  label=${name}_${t}_$part
  echo "##### $label ($opps x$games seed $seed $*) env [$envs] $(date +%T)"
  ( [ -n "$envs" ] && export $(echo "$envs" | tr ',' ' '); SNAP=$S bash $R6/lg.sh $label ours "$opps" $games $seed "$@" | tail -1 )
}
for cfg in "$@"; do
  if [ "$cfg" = OFF ]; then name=OFF; envs=""; else name=${cfg%%:*}; envs=${cfg#*:}; fi
  case $set_ in
    t2) run $name "$envs" t2a1601 tom2,tom2p 60 1601;;
    t2b) run $name "$envs" t2b1602 tom2,tom2p 60 1602;;
    t2c) run $name "$envs" t2c1611 tom2,tom2p 60 1611;;
    core)
      run $name "$envs" nash1603 nash 30 1603
      run $name "$envs" nb1604 riffo,nashball 60 1604
      run $name "$envs" pro1605 pro 30 1605
      run $name "$envs" strong1606 $STRONG 60 1606
      run $name "$envs" main1607 $MAIN 48 1607
      run $name "$envs" tom1608 tom 30 1608;;
    nash)
      run $name "$envs" nash1603 nash 30 1603
      run $name "$envs" nash1613 nash 30 1613;;
    tomx)
      run $name "$envs" tom1608 tom 30 1608
      run $name "$envs" nb1604 riffo,nashball 60 1604;;
    core2)
      run $name "$envs" nash1613 nash 30 1613
      run $name "$envs" nb1614 riffo,nashball 60 1614
      run $name "$envs" strong1616 $STRONG 60 1616
      run $name "$envs" rusher1617 rusher,swarm 30 1617;;
  esac
done
echo "ALLDONE $set_ $* $(date +%T)"
