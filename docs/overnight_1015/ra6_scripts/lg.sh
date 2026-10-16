#!/usr/bin/env bash
# lg.sh <label> <a-spec> <b-specs> <games> <seed> [extra ra_platform args...]
# local.ra_platform from a frozen code dir ($SNAP, required), JSON + log in ~/wt/_ra6_out. Pure-Python RA leagues run
# without the heavy lock at JOBS (default 6) while MemAvailable >= 2.5 GB; otherwise they take the lock.
# Env for the agent under test goes through (e.g. ARENA_RA_ATTACK_GATE=1).
set -u
W=${SNAP:?set SNAP to a snapshot dir}
OUT=/home/linmengfdm/wt/_ra6_out
mkdir -p $OUT
label=$1; a=$2; b=$3; games=$4; seed=$5; shift 5
if [ -s $OUT/$label.json ] || { [ -f $OUT/$label.log ] && ! grep -q '^exit' $OUT/$label.log; }; then
  echo "skip $label (done or running)"; exit 0
fi
if [ -f $OUT/SKIP ] && grep -qx "${label%%_*}" $OUT/SKIP; then echo "skip $label (SKIP list)"; exit 0; fi
cd $W
export PYTHONPATH=$W
export ARENA_RECORD=0
PY=/home/linmengfdm/ucla-agent-tournament/.venv/bin/python
JOBS=${JOBS:-6}
$PY -c "import agent, local; assert agent.__file__.startswith('$W'), agent.__file__; assert local.__file__.startswith('$W')" || { echo "WRONG CODE PATH"; exit 3; }
echo "code $W env $(env | grep '^ARENA_RA_' | tr '\n' ' ')" > $OUT/$label.log
mem=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
if [ "$mem" -ge 2500 ]; then LOCK=""; else LOCK="flock /tmp/arena-heavy.lock"; echo "mem ${mem}MB: taking the lock" >> $OUT/$label.log; fi
T0=$(date +%s)
$LOCK nice -n 5 $PY -m local.ra_platform --a "$a" --b "$b" --games $games --seed $seed --jobs $JOBS \
  --json $OUT/$label.json --quiet "$@" >> $OUT/$label.log 2>&1
echo "exit $? ($(( $(date +%s) - T0 )) s)" >> $OUT/$label.log
tail -2 $OUT/$label.log
