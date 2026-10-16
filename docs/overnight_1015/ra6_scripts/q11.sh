#!/usr/bin/env bash
# queue 11 (c11): split_fire, tanks walk 2 cells to enemy tanks only while tanks are < 50% of the enemy army seen
# (rockets never walk). $1 = A / B
R6=/home/linmengfdm/r6
S="ARENA_RA_AT_TOWERS=0 ARENA_RA_AT_COMP=0 ARENA_RA_HF_KEEP=0 ARENA_RA_ANCHOR2=0 ARENA_RA_SPLIT_SLACK_HEAVY=2 ARENA_RA_SPLIT_SLACK_E3=0 ARENA_RA_SPLIT_ROCKETS=99 ARENA_RA_SPLIT_PACK=99 ARENA_RA_SPLIT_TANK_SHARE=0.5"
if [ "$1" = A ]; then
  bash $R6/run1.sh TS_c11_nash1504 c11 nash 60 1504 $S
  bash $R6/run1.sh TS_c11_tom1501 c11 tom 120 1501 $S
  bash $R6/run1.sh TS_c11_pro1503 c11 pro 60 1503 $S
else
  bash $R6/run1.sh TS_c11_nash1509 c11 nash 60 1509 $S
  bash $R6/run1.sh TS_c11_tom1507 c11 tom 120 1507 $S
  bash $R6/run1.sh TS_c11_nb1502 c11 riffo,nashball 120 1502 $S
fi
echo ALLDONE
