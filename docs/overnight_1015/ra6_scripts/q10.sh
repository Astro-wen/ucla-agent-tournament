#!/usr/bin/env bash
# queue 10 (c10): split_fire, tanks walk 2 cells only to an isolated / paired enemy tank (split_pack 1). $1 = A / B
R6=/home/linmengfdm/r6
S="ARENA_RA_AT_TOWERS=0 ARENA_RA_AT_COMP=0 ARENA_RA_HF_KEEP=0 ARENA_RA_ANCHOR2=0 ARENA_RA_SPLIT_SLACK_HEAVY=2 ARENA_RA_SPLIT_SLACK_E3=0 ARENA_RA_SPLIT_ROCKETS=99 ARENA_RA_SPLIT_PACK=1"
if [ "$1" = A ]; then
  bash $R6/run1.sh PK_c10_nash1504 c10 nash 60 1504 $S
  bash $R6/run1.sh PK_c10_tom1501 c10 tom 120 1501 $S
else
  bash $R6/run1.sh PK_c10_tom1507 c10 tom 120 1507 $S
  bash $R6/run1.sh PK_c10_nash1509 c10 nash 60 1509 $S
fi
echo ALLDONE
