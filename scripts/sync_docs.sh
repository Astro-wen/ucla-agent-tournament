#!/usr/bin/env bash
# Re-mirror the official AltruAgent skill docs into docs/platform/ (they change; re-run often).
set -euo pipefail
cd "$(dirname "$0")/.."
BASE=https://api.altruagent-game.com
curl -sf "$BASE/skill.md" > docs/platform/SKILL.md
for t in mcp werewolf pokemon 01-signup 02-auth 03-competitions 04-tournaments 05-gameplay \
         06-messaging 07-errors 08-commands 09-end-to-end reference avalon; do
  curl -sf "$BASE/skill/$t" > "docs/platform/$t.md"
done
git --no-pager diff --stat -- docs/platform || true
