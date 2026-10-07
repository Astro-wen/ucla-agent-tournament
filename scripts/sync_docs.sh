#!/usr/bin/env bash
# Re-mirror the official AltruAgent skill docs into docs/platform/ (they change; re-run often).
# Topics as of skill v0.5.0 (2026-10-06): that day the platform retired the signup / claim / competition
# pages (01-signup 02-auth 03-competitions 08-commands 09-end-to-end avalon testing-seat-claim) and added
# official-agent. An unknown topic answers 404 with the live list ("Available: ..."), printed below, so a
# new or retired page shows up here; a local page that is no longer published is listed for `git rm`.
set -euo pipefail
cd "$(dirname "$0")/.."
BASE=https://api.altruagent-game.com
TOPICS="official-agent mcp werewolf pokemon redalert 04-tournaments 05-gameplay 06-messaging 07-errors reference"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -sf "$BASE/skill.md" > "$tmp/SKILL.md"          # everything is fetched first: a failed fetch changes nothing
for t in $TOPICS; do
  curl -sf "$BASE/skill/$t" > "$tmp/$t.md"
done
mv "$tmp"/*.md docs/platform/
echo "live topics: $(curl -s "$BASE/skill/list-topics" | grep -o 'Available: [^"]*' || echo '?')"
for f in docs/platform/*.md; do
  b=$(basename "$f" .md)
  case " SKILL $TOPICS " in
    *" $b "*) ;;
    *) echo "no longer published: $f (git rm it if the live list above agrees)" ;;
  esac
done
git --no-pager diff --stat -- docs/platform || true
