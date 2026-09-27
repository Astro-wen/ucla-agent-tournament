#!/usr/bin/env bash
# Snapshot the current agent/ package as champion/agent so candidates can be laddered against it.
#   ./scripts/freeze_champion.sh        -> champion/ = copy of agent/ at HEAD (+ working tree)
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf champion
mkdir -p champion
cp -R agent champion/agent
rm -rf champion/agent/js/node_modules champion/agent/__pycache__ champion/agent/*/__pycache__
: > champion/__init__.py
git rev-parse --short HEAD > champion/VERSION 2>/dev/null || echo unknown > champion/VERSION
echo "champion frozen from agent/ @ $(cat champion/VERSION)"
