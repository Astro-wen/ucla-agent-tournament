#!/usr/bin/env bash
# queue 5: split_fire reach variants (c5): slack 0.5 and 0 (retarget only what is in range) vs nash and tom
R6=/home/linmengfdm/r6
S="ARENA_RA_AT_TOWERS=0 ARENA_RA_AT_COMP=0 ARENA_RA_HF_KEEP=0 ARENA_RA_ANCHOR2=0"
bash $R6/run1.sh S05_c5_nash1504 c5 nash 60 1504 $S ARENA_RA_SPLIT_SLACK=0.5
bash $R6/run1.sh S00_c5_nash1504 c5 nash 60 1504 $S ARENA_RA_SPLIT_SLACK=0
bash $R6/run1.sh S05_c5_tom1501 c5 tom 120 1501 $S ARENA_RA_SPLIT_SLACK=0.5
bash $R6/run1.sh S00_c5_tom1501 c5 tom 120 1501 $S ARENA_RA_SPLIT_SLACK=0
echo ALLDONE
