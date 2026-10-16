#!/usr/bin/env bash
# trace.sh <snap-tag> <opp> <seed> <map> <seat> [every] [ENV=VAL...]: trace.py from a snapshot (or ra3 worktree with tag 'wt')
S=/mnt/c/Users/LinmengFDM/AppData/Local/Temp/claude/--wsl-localhost-ubuntu-24-04-home-linmengfdm-ucla-agent-tournament/bdbc1af6-1104-4704-a606-458bc4b7d75e/scratchpad/ra3
t=$1; shift
W=/home/linmengfdm/wt/_ra3_snap/$t
[ "$t" = wt ] && W=/home/linmengfdm/wt/ra3
cd $W
PYTHONPATH=$W ARENA_RECORD=0 /home/linmengfdm/ucla-agent-tournament/.venv/bin/python $S/trace.py "$@" 2>&1 | grep -v 'INFO\|ARENA_RA=off' | cut -c1-${CUT:-360}
