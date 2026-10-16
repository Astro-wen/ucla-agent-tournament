#!/usr/bin/env bash
# rc.sh: recall7 (real recordings) of the c4 snapshot: defaults vs N / H / L / combined
cd /home/linmengfdm/wt/_ra6_snap/c4
export RA_ROOT=$PWD PYTHONPATH=$PWD ARENA_RECORD=0
nice -n 5 /home/linmengfdm/ucla-agent-tournament/.venv/bin/python ~/r6/recall7.py base= N=ARENA_RA_NO3X_EARLY=1 H=ARENA_RA_PRIOR_HOLD=1 C=ARENA_RA_NO3X_EARLY=1,ARENA_RA_PRIOR_HOLD=1,ARENA_RA_EA_LAZY=1 2>&1 | grep -v 'rule-based\|refusal'
