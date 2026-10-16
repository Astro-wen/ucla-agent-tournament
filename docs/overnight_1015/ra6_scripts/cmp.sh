#!/usr/bin/env bash
# cmp.sh <base-prefix> <new-prefix> <names...>: paired compare per name (ALL line + flips), then pooled
cd ~/wt/_ra6_out
b=$1; n=$2; shift 2
args=()
for x in "$@"; do
  if [ -s ${b}_$x.json ] && [ -s ${n}_$x.json ]; then
    echo "-- $x"
    python3 ~/r6/compare.py ${b}_$x.json ${n}_$x.json | grep -E '^  [a-zA-Z]|flip' | grep -v health
    args+=(${b}_$x.json ${n}_$x.json)
  fi
done
if [ ${#args[@]} -gt 2 ]; then python3 ~/r6/compare.py "${args[@]}" | sed -n '/POOLED/,$p' | grep -E 'POOLED|^  ALL|health'; fi
