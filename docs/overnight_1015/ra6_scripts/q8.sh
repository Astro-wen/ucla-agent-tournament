#!/usr/bin/env bash
# queue 8 (c8): split_fire, tanks walk 2 cells to any vehicle, rockets only retarget in range (T2E0). $1 = A / B
R6=/home/linmengfdm/r6
S="ARENA_RA_AT_TOWERS=0 ARENA_RA_AT_COMP=0 ARENA_RA_HF_KEEP=0 ARENA_RA_ANCHOR2=0 ARENA_RA_SPLIT_SLACK_HEAVY=2 ARENA_RA_SPLIT_SLACK_E3=0"
if [ "$1" = A ]; then
  bash $R6/run1.sh TE_c8_tom1501 c8 tom 120 1501 $S
  bash $R6/run1.sh TE_c8_nb1502 c8 riffo,nashball 120 1502 $S
else
  bash $R6/run1.sh TE_c8_nash1504 c8 nash 60 1504 $S
  bash $R6/run1.sh TE_c8_tom1507 c8 tom 120 1507 $S
  bash $R6/run1.sh TE_c8_pro1503 c8 pro 60 1503 $S
fi
echo ALLDONE
