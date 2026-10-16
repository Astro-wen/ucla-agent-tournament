#!/usr/bin/env bash
# ident7.sh: the main snapshot (867ec65) on the final seeds of nash / main / strong: must equal the base arm fb_*
R6=/home/linmengfdm/r6
for v in $(env | grep -o '^ARENA_RA_[A-Z0-9_]*'); do unset $v; done
S=/home/linmengfdm/wt/_ra6_snap/main
SNAP=$S bash $R6/lg.sh m_nash_1655 ours nash 120 1655 > /dev/null
SNAP=$S bash $R6/lg.sh m_main_1658 ours idle,rusher,turtle,llm 48 1658 > /dev/null
SNAP=$S bash $R6/lg.sh m_strong_1657 ours tanks,swarm,pro,raider,hider 60 1657 > /dev/null
echo ALLDONE $(date +%T)
