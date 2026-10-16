#!/usr/bin/env bash
# snap4.sh <tag> [src]: freeze code (agent, local, champion, altruagent, data) of the ra4 worktree (or src) for leagues.
set -e
W=${2:-/home/linmengfdm/wt/ra6}
D=/home/linmengfdm/wt/_ra6_snap/$1
rm -rf $D
mkdir -p $D
cd $W
cp -a agent local champion altruagent data $D/
rm -rf $D/agent/js/node_modules $D/local/js/node_modules $D/champion/agent/js/node_modules 2>/dev/null || true
find $D -name __pycache__ -type d -prune -exec rm -rf {} +
echo "$D ($(cd $W && git rev-parse --short HEAD) + worktree; diff sha $(git diff -- agent local | sha1sum | cut -c1-8))" | tee $D/SNAPINFO
