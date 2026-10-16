#!/usr/bin/env bash
# queue 7 (c7): split_fire with light-vehicle slack 2 / heavy 0 + resume. $1 = A or B (two runners)
R6=/home/linmengfdm/r6
S="ARENA_RA_AT_TOWERS=0 ARENA_RA_AT_COMP=0 ARENA_RA_HF_KEEP=0 ARENA_RA_ANCHOR2=0"
if [ "$1" = A ]; then
  bash $R6/run1.sh RL_c7_tom1501 c7 tom 120 1501 $S
  bash $R6/run1.sh RL_c7_nb1502 c7 riffo,nashball 120 1502 $S
  bash $R6/run1.sh RL_c7_pro1503 c7 pro 60 1503 $S
else
  bash $R6/run1.sh RL_c7_nash1504 c7 nash 60 1504 $S
  bash $R6/run1.sh RL_c7_tom1507 c7 tom 120 1507 $S
  bash $R6/run1.sh RL_c7_sfmix1508 c7 pro,nash,nashball,riffo,tanks,swarm,raider,rusher 96 1508 $S -- --maps shadowfiend-ii
fi
echo ALLDONE
