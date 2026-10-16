#!/usr/bin/env bash
# queue 3: split_fire alone (c4) against tom and the core dev seeds
R6=/home/linmengfdm/r6
S="ARENA_RA_AT_TOWERS=0 ARENA_RA_AT_COMP=0 ARENA_RA_HF_KEEP=0 ARENA_RA_ANCHOR2=0"
bash $R6/run1.sh S_c4_tom1501 c4 tom 120 1501 $S
bash $R6/run1.sh S_c4_nb1502 c4 riffo,nashball 120 1502 $S
bash $R6/run1.sh S_c4_pro1503 c4 pro 60 1503 $S
bash $R6/run1.sh S_c4_nash1504 c4 nash 60 1504 $S
bash $R6/run1.sh S_c4_strong1505 c4 tanks,swarm,pro,raider,hider 60 1505 $S
bash $R6/run1.sh S_c4_main1506 c4 idle,rusher,turtle,llm 48 1506 $S
echo ALLDONE
