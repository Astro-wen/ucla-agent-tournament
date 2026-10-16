#!/usr/bin/env bash
# pool.sh <base-prefix> <new-prefix> <part>...: per-part compare + pooled, in ~/wt/_ra6_out (e.g. pool.sh OFF_c1 L_c1 nash1603 nb1604)
cd ~/wt/_ra6_out
b=$1; n=$2; shift 2
args=()
for x in "$@"; do
  if [ -s ${b}_$x.json ] && [ -s ${n}_$x.json ]; then
    python3 ~/r6/compare.py ${b}_$x.json ${n}_$x.json | grep -E '^  ALL' | sed -e "s/^  ALL /  $x /" | cut -c1-190
    args+=(${b}_$x.json ${n}_$x.json)
  else
    echo "  $x: missing"
  fi
done
if [ ${#args[@]} -gt 2 ]; then
  python3 ~/r6/compare.py "${args[@]}" | sed -n '/POOLED/,$p' | grep -E '^  [a-zA-Z]' | grep -v health | cut -c1-190
  python3 ~/r6/compare.py "${args[@]}" | sed -n '/POOLED/,$p' | grep -E 'health'
fi
