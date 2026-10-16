#!/usr/bin/env bash
# queue 9 (c9): split_fire, tanks walk 2 to light vehicles and to enemy tanks with <= 2 rockets near them, rockets
# only retarget in range (TR). $1 = A / B
R6=/home/linmengfdm/r6
S="ARENA_RA_AT_TOWERS=0 ARENA_RA_AT_COMP=0 ARENA_RA_HF_KEEP=0 ARENA_RA_ANCHOR2=0 ARENA_RA_SPLIT_SLACK_HEAVY=2 ARENA_RA_SPLIT_SLACK_E3=0 ARENA_RA_SPLIT_ROCKETS=2"
if [ "$1" = A ]; then
  bash $R6/run1.sh TR_c9_nash1504 c9 nash 60 1504 $S
  bash $R6/run1.sh TR_c9_tom1501 c9 tom 120 1501 $S
else
  bash $R6/run1.sh TR_c9_tom1507 c9 tom 120 1507 $S
  bash $R6/run1.sh TR_c9_nb1502 c9 riffo,nashball 120 1502 $S
fi
echo ALLDONE
