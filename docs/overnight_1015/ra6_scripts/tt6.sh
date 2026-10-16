#!/usr/bin/env bash
# tt6.sh <code-dir> <opp> <seed> <map> <seat> [every] [ENV=VAL..]: tomtrace of one sim game (both sides), compact
D=$1; shift
cd $D
export PYTHONPATH=$D ARENA_RECORD=0
PY=/home/linmengfdm/ucla-agent-tournament/.venv/bin/python
timeout 900 $PY ~/r6/tomtrace.py "$@" 2>&1 | grep -v '^   \[' | sed -e 's/blds=.*att=/att=/' | cut -c1-300
