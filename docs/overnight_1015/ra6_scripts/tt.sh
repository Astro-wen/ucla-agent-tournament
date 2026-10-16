#!/usr/bin/env bash
# tt.sh <snap-tag> <opp> <seed> <map> <seat> [every] [ENV=VAL...]: tomtrace.py (both sides) from a snapshot
t=$1; shift
W=/home/linmengfdm/wt/_ra6_snap/$t
cd $W
PYTHONPATH=$W ARENA_RECORD=0 nice -n 5 /home/linmengfdm/ucla-agent-tournament/.venv/bin/python /home/linmengfdm/r6/tomtrace.py "$@" 2>&1 | grep -v 'INFO\|ARENA_RA=off\|match-summary' | cut -c1-${CUT:-330}
