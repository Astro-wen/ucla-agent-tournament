#!/usr/bin/env bash
# One-shot setup of a fresh Ubuntu cloud VM (x86 or Arm/Ampere) for simulation and competition-day hosting.
# Run from YOUR machine (not on the VM):
#   ./scripts/cloud_bootstrap.sh ubuntu@<vm-public-ip> [~/.ssh/oci_arena]
# It copies the repo (incl. logs/, data/, .env files) with rsync, installs Python 3.11+ and Node, creates the venv,
# runs npm install for both calculators and the full test suite on the VM.
set -euo pipefail
HOST="${1:?usage: $0 user@host [ssh_key]}"
KEY="${2:-$HOME/.ssh/oci_arena}"
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new"
cd "$(dirname "$0")/.."

echo "== packages"
$SSH "$HOST" 'set -e; sudo apt-get update -qq; sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  python3 python3-venv python3-pip rsync git tmux curl ca-certificates >/dev/null
  if ! command -v node >/dev/null || [ "$(node -v | cut -c2- | cut -d. -f1)" -lt 18 ]; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - >/dev/null; sudo apt-get install -y -qq nodejs >/dev/null
  fi
  python3 -c "import sys; assert sys.version_info >= (3, 11), sys.version" || {
    sudo apt-get install -y -qq software-properties-common >/dev/null; sudo add-apt-repository -y ppa:deadsnakes/ppa >/dev/null;
    sudo apt-get update -qq; sudo apt-get install -y -qq python3.12 python3.12-venv >/dev/null; }
  echo "node $(node -v), $(python3 --version), $(nproc) cores, $(free -g | awk "/Mem/{print \$2}") GB RAM"'

echo "== code (rsync, excluding venv / node_modules / worktrees)"
rsync -az --delete -e "$SSH" \
  --exclude .venv --exclude 'node_modules' --exclude '.claude' --exclude '__pycache__' --exclude '*.jsonl.tmp' \
  ./ "$HOST:~/arena/"

echo "== venv, npm, tests"
$SSH "$HOST" 'set -e; cd ~/arena
  PY=$(command -v python3.12 || command -v python3)
  [ -d .venv ] || $PY -m venv .venv
  .venv/bin/pip install -q --upgrade pip >/dev/null
  .venv/bin/pip install -q -e ".[dev]" >/dev/null
  (cd agent/js && npm install --no-audit --no-fund --silent)
  (cd local/js && npm install --no-audit --no-fund --silent)
  find logs -maxdepth 1 -type l -delete 2>/dev/null || true
  for n in local replay x sid; do ln -sf /dev/null logs/$n.jsonl; done
  .venv/bin/python -m pytest -q 2>&1 | tail -1
  .venv/bin/python scripts/replay_real.py | tail -1
  .venv/bin/python -m local.ladder --games 200 | tail -2'
echo "== done: ssh -i $KEY $HOST 'cd ~/arena && .venv/bin/python -m local.benchmark'"
