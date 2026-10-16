#!/usr/bin/env bash
# queue 1: is the v4.7 attack gate too passive against tom? (v4.8 switches off; gate off = v4.6's attack decision)
R6=/home/linmengfdm/r6
OFF="ARENA_RA_AT_TOWERS=0 ARENA_RA_AT_COMP=0 ARENA_RA_HF_KEEP=0 ARENA_RA_ANCHOR2=0"
bash $R6/run1.sh G0_c2_tom1501 c2 tom 120 1501 $OFF ARENA_RA_ATTACK_GATE=0
bash $R6/run1.sh V46_c2_tom1501 c2 tom 120 1501 $OFF ARENA_RA_ATTACK_GATE=0 ARENA_RA_TRADE_RETREAT=0 ARENA_RA_RECOVER=0
echo ALLDONE
