#!/usr/bin/env bash
# run1.sh <label> <snap-tag> <opps> <games> <seed> [ENV=VAL ...] [-- extra ra_platform args]: one league, any env
R6=/home/linmengfdm/r6
label=$1; t=$2; opps=$3; games=$4; seed=$5; shift 5
for v in $(env | grep -o '^ARENA_RA_[A-Z0-9_]*'); do unset $v; done
while [ $# -gt 0 ] && [ "$1" != "--" ]; do export "$1"; shift; done
[ "${1:-}" = "--" ] && shift
echo "##### $label snap $t ($opps x$games seed $seed $*) env: $(env | grep '^ARENA_RA_' | tr '\n' ' ') $(date +%T)"
SNAP=/home/linmengfdm/wt/_ra6_snap/$t bash $R6/lg.sh $label ours "$opps" $games $seed "$@" | tail -1
