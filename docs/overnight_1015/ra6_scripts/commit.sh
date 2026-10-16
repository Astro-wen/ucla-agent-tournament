#!/bin/bash
# commit.sh <msgfile>: commit the ra5 worktree's tracked changes (agent/, local/, tests/, docs/, champion/) in the owner's
# name; never the node_modules symlinks, .env or logs
set -e
cd /home/linmengfdm/wt/ra6
git add agent local tests docs champion 2>/dev/null || true
git reset -q -- agent/js/node_modules local/js/node_modules champion/agent/js/node_modules 2>/dev/null || true
if git diff --cached --name-only | grep -E '(^|/)\.env|^logs/|node_modules'; then echo "REFUSING: .env, logs or node_modules staged"; exit 1; fi
git diff --cached --stat | tail -3
git commit -q -F "$1"
git log --oneline -1
git log -1 --format='%an <%ae>%n%b' | grep -i -E 'claude|co-authored' && { echo "ATTRIBUTION FOUND"; exit 1; } || true
