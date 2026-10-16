#!/usr/bin/env bash
# sum.sh <glob>: per-opponent ALL line of each league JSON matching the glob in ~/wt/_ra6_out
cd ~/wt/_ra6_out
for f in $1; do echo "$f"; python3 ~/r6/bymap.py $f | grep ALL; done
