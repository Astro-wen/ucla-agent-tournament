#!/usr/bin/env bash
# c6.sh <base.json> <new.json>...: paired compare (by map and opponent) of each existing new vs base, in ~/wt/_ra6_out
cd ~/wt/_ra6_out
b=$1; shift
for n in "$@"; do
  if [ -s "$n" ] && [ -s "$b" ]; then
    echo "== $n vs $b"
    BYMAP=1 python3 ~/r6/compare.py "$b" "$n" | grep -E '^  [a-zA-Z]' | grep -v health | sed -e 's/(\([0-9.]*%\), [0-9.-]*)/(\1)/g' | cut -c1-200
  fi
done
true
