#!/usr/bin/env bash
# queue 4: split_fire on a second tom dev seed; split_fire + at_comp; split_fire + hf_keep on the sf-ii mix
R6=/home/linmengfdm/r6
OFF="ARENA_RA_AT_TOWERS=0 ARENA_RA_AT_COMP=0 ARENA_RA_HF_KEEP=0 ARENA_RA_ANCHOR2=0 ARENA_RA_SPLIT_FIRE=0"
S="ARENA_RA_AT_TOWERS=0 ARENA_RA_AT_COMP=0 ARENA_RA_HF_KEEP=0 ARENA_RA_ANCHOR2=0"
bash $R6/run1.sh OFF_c4_tom1507 c4 tom 120 1507 $OFF
bash $R6/run1.sh S_c4_tom1507 c4 tom 120 1507 $S
bash $R6/run1.sh CS_c4_tom1501 c4 tom 120 1501 ARENA_RA_AT_TOWERS=0 ARENA_RA_HF_KEEP=0 ARENA_RA_ANCHOR2=0
bash $R6/run1.sh S_c4_sfmix1508 c4 pro,nash,nashball,riffo,tanks,swarm,raider,rusher 96 1508 $S -- --maps shadowfiend-ii
bash $R6/run1.sh HS_c4_sfmix1508 c4 pro,nash,nashball,riffo,tanks,swarm,raider,rusher 96 1508 ARENA_RA_AT_TOWERS=0 ARENA_RA_AT_COMP=0 ARENA_RA_ANCHOR2=0 -- --maps shadowfiend-ii
echo ALLDONE
