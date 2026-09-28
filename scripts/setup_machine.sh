#!/usr/bin/env bash
# Set up this checkout on a new Linux / macOS / WSL2 machine and verify it end to end.
#   ./scripts/setup_machine.sh
set -euo pipefail
cd "$(dirname "$0")/.."

need() { command -v "$1" >/dev/null || { echo "missing: $1 ($2)"; exit 1; }; }
need git "install git"
need node "install Node.js >= 18 (https://nodejs.org or: curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt-get install -y nodejs)"
need npm "comes with Node.js"
PY=$(command -v python3.12 || command -v python3.11 || command -v python3 || true)
[ -n "$PY" ] || { echo "missing: python3 >= 3.11"; exit 1; }
"$PY" -c 'import sys; assert sys.version_info >= (3, 11), f"Python >= 3.11 required, found {sys.version}"'
[ "$(node -v | cut -c2- | cut -d. -f1)" -ge 18 ] || { echo "Node >= 18 required, found $(node -v)"; exit 1; }
echo "python: $("$PY" --version) | node: $(node -v) | cores: $(getconf _NPROCESSORS_ONLN)"

echo "== venv"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e ".[dev]"

echo "== node packages (damage calculator + local simulator)"
(cd agent/js && npm install --no-audit --no-fund --silent)
(cd local/js && npm install --no-audit --no-fund --silent)

echo "== frozen copies run their OWN calc.js with the shared node_modules (ladder / benchmark / panel)"
for d in champion/agent/js versions/*/agent/js local/sparring/*/agent/js; do
  [ -f "$d/calc.js" ] || continue
  up=$(echo "$d" | sed 's#[^/][^/]*#..#g')        # relative, so the checkout can move: champion/agent/js -> ../../..
  [ -e "$d/node_modules" ] || ln -sfn "$up/agent/js/node_modules" "$d/node_modules"
done

echo "== scratch logs go to /dev/null"
mkdir -p logs
for n in local replay x sid; do ln -sf /dev/null "logs/$n.jsonl"; done

echo "== checks"
.venv/bin/python -m pytest -q 2>&1 | tail -1
.venv/bin/python scripts/replay_real.py | tail -1
.venv/bin/python -m local.ladder --games 60 | tail -2 || true
if [ -f .env ]; then
  .venv/bin/python scripts/check_connection.py | tail -2
else
  echo "no .env yet: copy the agent key files (see docs/HANDOFF.md), then run: .venv/bin/python scripts/check_connection.py"
fi
echo "== ready. Activate with: source .venv/bin/activate"
