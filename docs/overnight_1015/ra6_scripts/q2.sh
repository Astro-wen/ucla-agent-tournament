#!/usr/bin/env bash
# queue 2: at_towers v3 (in-base placement) alone, and with a looser trigger (any tank share), against tom
R6=/home/linmengfdm/r6
bash $R6/run1.sh T3_c3_tom1501 c3 tom 120 1501 ARENA_RA_AT_COMP=0 ARENA_RA_HF_KEEP=0 ARENA_RA_ANCHOR2=0
bash $R6/run1.sh T3s0_c3_tom1501 c3 tom 120 1501 ARENA_RA_AT_COMP=0 ARENA_RA_HF_KEEP=0 ARENA_RA_ANCHOR2=0 ARENA_RA_AT_TANK_SHARE=0
echo ALLDONE
