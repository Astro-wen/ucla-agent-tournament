#!/usr/bin/env bash
# Build a self-contained tarball of the deployed agent for a machine we do not control (e.g. handed to the
# organizers): code, data, draft model, the damage calculator's node_modules (vendored; pure JavaScript), a pinned
# official Node.js build for Linux x64 (node/bin/node), the pinned Python requirements, the runbook scripts, the
# Dockerfile and one recorded real match for the self-check. Without node the agent wins only ~12% against itself
# with the calculator, so node ships by default. Never contains keys: .env files are excluded and the staged tree
# is scanned; the build fails on anything key-like.
#
#   bash scripts/bundle.sh                      # -> dist/arena-agent-<version>.tar.gz (+ .sha256), Node NODE_VERSION
#   bash scripts/bundle.sh --out /tmp/x         # another output directory
#   bash scripts/bundle.sh --node-tarball FILE  # offline: node-NODE_VERSION-linux-x64.tar.xz/.gz already downloaded
#                                               # (checked against the pinned SHA-256 like a download)
#   bash scripts/bundle.sh --node-dist DIR      # another extracted official Node >= 18 build (DIR/bin/node)
#   bash scripts/bundle.sh --no-node            # no node binary (the target must provide Node >= 18)
#   bash scripts/bundle.sh --no-node-modules    # leave agent/js/node_modules out (then: cd agent/js && npm ci)
#   bash scripts/bundle.sh --no-sample-logs     # leave the recorded match out (selfcheck then skips its replay)
#
# The Node tarball comes from https://nodejs.org/dist/ (cached in ~/.cache/arena-agent) and must match the SHA-256
# pinned below (from the release's SHASUMS256.txt); anything else aborts the build. On the target, the agent uses
# node/bin/node unless ARENA_NODE names another binary (agent/pokemon/calc.py _node_bin).
#
# On the target:  tar xzf arena-agent-*.tar.gz && cd arena-agent-* && cat BUNDLE_README.txt
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

NODE_VERSION=v22.23.3                 # Node 22 LTS (maintained until 2027-04); deploy/Dockerfile uses the same
NODE_SHA256_XZ=df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de   # node-v22.23.3-linux-x64.tar.xz
NODE_SHA256_GZ=1084aa36196bba4c3a5e69a1ee388a6e4ff729dad09445fbcd434b28fe3c24af   # node-v22.23.3-linux-x64.tar.gz
NODE_CACHE="${ARENA_NODE_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/arena-agent}"

OUT="$ROOT/dist"; NODE_MODULES=1; SAMPLE_LOGS=1; NODE_MODE=pinned; NODE_SRC=""
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --node-dist) NODE_MODE=dist; NODE_SRC="$2"; shift 2 ;;
    --node-tarball) NODE_MODE=tarball; NODE_SRC="$2"; shift 2 ;;
    --no-node) NODE_MODE=none; shift ;;
    --no-node-modules) NODE_MODULES=0; shift ;;
    --no-sample-logs) SAMPLE_LOGS=0; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }

VERSION="$(git describe --always --dirty 2>/dev/null || true)"
[ -n "$VERSION" ] || VERSION="$(date +%Y%m%d-%H%M)"
NAME="arena-agent-$VERSION"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
DEST="$STAGE/$NAME"
mkdir -p "$DEST"

# ---- the pinned Node tarball: cache or nodejs.org, SHA-256 checked -------------------------------------------------
node_tarball() {   # prints the path of a verified node-$NODE_VERSION-linux-x64 tarball
  local f want kind url
  if [ "$NODE_MODE" = tarball ]; then
    f="$NODE_SRC"
    case "$f" in
      *.tar.xz) want="$NODE_SHA256_XZ" ;;
      *.tar.gz) want="$NODE_SHA256_GZ" ;;
      *) echo "--node-tarball: expected node-$NODE_VERSION-linux-x64.tar.xz or .tar.gz" >&2; return 1 ;;
    esac
    [ -f "$f" ] || { echo "--node-tarball: $f not found" >&2; return 1; }
  else
    if command -v xz >/dev/null; then kind=xz; want="$NODE_SHA256_XZ"; else kind=gz; want="$NODE_SHA256_GZ"; fi
    f="$NODE_CACHE/node-$NODE_VERSION-linux-x64.tar.$kind"
    if ! [ -f "$f" ] || [ "$(sha256 "$f")" != "$want" ]; then
      url="https://nodejs.org/dist/$NODE_VERSION/node-$NODE_VERSION-linux-x64.tar.$kind"
      mkdir -p "$NODE_CACHE"
      echo "downloading $url" >&2
      if command -v curl >/dev/null; then
        curl -fsSL --retry 3 -o "$f.part" "$url" || { rm -f "$f.part"; echo "download failed: $url (offline? --node-tarball FILE)" >&2; return 1; }
      elif command -v wget >/dev/null; then
        wget -q -O "$f.part" "$url" || { rm -f "$f.part"; echo "download failed: $url (offline? --node-tarball FILE)" >&2; return 1; }
      else
        echo "neither curl nor wget: download $url yourself and pass --node-tarball FILE" >&2; return 1
      fi
      mv -f "$f.part" "$f"
    fi
  fi
  if [ "$(sha256 "$f")" != "$want" ]; then
    echo "NODE CHECKSUM MISMATCH: $f is not the official node-$NODE_VERSION-linux-x64 build (expected sha256 $want)" >&2
    return 1
  fi
  echo "$f"
}

# ---- what goes in (an allowlist: nothing else from the checkout is copied) ------------------------------------
INCLUDE=(agent altruagent data deploy pyproject.toml requirements.lock .env.example README.md GAMES.md docs/deploy.md
         scripts/run_agent.sh scripts/selfcheck.py scripts/check_connection.py scripts/replay_real.py)
for p in "${INCLUDE[@]}"; do
  [ -e "$p" ] || { echo "missing $p" >&2; exit 1; }
  mkdir -p "$DEST/$(dirname "$p")"
  cp -R "$p" "$DEST/$p"
done
# the calculator's node_modules: resolved (a checkout may link it), or left out on request
rm -rf "$DEST/agent/js/node_modules"
if [ "$NODE_MODULES" = 1 ]; then
  [ -d agent/js/node_modules/@smogon/calc ] || { echo "agent/js/node_modules missing: run (cd agent/js && npm ci) first" >&2; exit 1; }
  cp -RL agent/js/node_modules "$DEST/agent/js/node_modules"
fi
NODE_NOTE=""
case "$NODE_MODE" in
  pinned|tarball)
    tb="$(node_tarball)" || { echo "bundle NOT written (no verified Node build)" >&2; exit 1; }
    mkdir -p "$STAGE/nodex" "$DEST/node/bin"
    tar -C "$STAGE/nodex" -xf "$tb" "node-$NODE_VERSION-linux-x64/bin/node" "node-$NODE_VERSION-linux-x64/LICENSE"
    cp "$STAGE/nodex/node-$NODE_VERSION-linux-x64/bin/node" "$DEST/node/bin/node"
    cp "$STAGE/nodex/node-$NODE_VERSION-linux-x64/LICENSE" "$DEST/node/LICENSE"
    rm -rf "$STAGE/nodex"
    printf 'Node.js %s, linux-x64, official build from https://nodejs.org/dist/%s/ (%s sha256 %s)\n' "$NODE_VERSION" \
      "$NODE_VERSION" "$(basename "$tb")" "$(sha256 "$tb")" > "$DEST/node/VERSION.txt"
    NODE_NOTE="Node.js $NODE_VERSION (official linux-x64 build) is included in node/ and used by default." ;;
  dist)
    v="$("$NODE_SRC/bin/node" --version 2>/dev/null || true)"
    [ -n "$v" ] && [ "$(echo "$v" | cut -c2- | cut -d. -f1)" -ge 18 ] || { echo "--node-dist: $NODE_SRC/bin/node is not a node >= 18" >&2; exit 1; }
    mkdir -p "$DEST/node/bin"
    cp "$NODE_SRC/bin/node" "$DEST/node/bin/node"
    for f in LICENSE README.md; do [ -f "$NODE_SRC/$f" ] && cp "$NODE_SRC/$f" "$DEST/node/"; done
    printf 'Node.js %s from %s (--node-dist)\n' "$v" "$NODE_SRC" > "$DEST/node/VERSION.txt"
    NODE_NOTE="Node.js $v is included in node/ and used by default." ;;
  none)
    echo "WARNING: --no-node: the bundle has no node binary; the target must provide Node >= 18 or the agent will not start" >&2
    NODE_NOTE="NO node binary is included (built with --no-node): install Node.js >= 18, or set ARENA_NODE to one." ;;
esac
[ -f "$DEST/node/bin/node" ] && chmod 755 "$DEST/node/bin/node"
if [ "$SAMPLE_LOGS" = 1 ]; then          # one real match (both seats) for scripts/selfcheck.py's replay
  for d in yoriko apocalypse; do
    f="$(ls -S logs/$d/*.jsonl 2>/dev/null | tail -n 1 || true)"
    [ -n "$f" ] && { mkdir -p "$DEST/logs/$d"; cp "$f" "$DEST/logs/$d/"; }
  done
fi
find "$DEST" \( -name __pycache__ -o -name '*.pyc' -o -name .pytest_cache -o -name '.DS_Store' \) -prune -exec rm -rf {} +
find "$DEST" -name '*.smoke_test_backup' -delete
chmod +x "$DEST/scripts/run_agent.sh" "$DEST/scripts/selfcheck.py" "$DEST/scripts/check_connection.py" "$DEST/scripts/replay_real.py"

# ---- the bundled node must run the bundled calculator (when this machine can execute a linux-x64 binary) -----------
if [ -f "$DEST/node/bin/node" ] && [ "$NODE_MODULES" = 1 ] && [ "$(uname -sm)" = "Linux x86_64" ]; then
  req='{"id":1,"calcs":[{"attacker":{"species":"Pikachu","level":50},"defender":{"species":"Pikachu","level":50},"move":"Thunderbolt","field":{"gameType":"Doubles"}}]}'
  ans="$(printf '%s\n' "$req" | (cd "$DEST/agent/js" && NODE_PATH="$DEST/agent/js/node_modules" timeout 60 "$DEST/node/bin/node" calc.js) 2>&1 | head -n 1 || true)"
  case "$ans" in
    '{"id":1,"results":[{"min":'*) echo "bundled node $("$DEST/node/bin/node" --version) answers a calc with the bundled node_modules" ;;
    *) echo "the bundled node did not answer a calc: ${ans:0:200}" >&2; echo "bundle NOT written" >&2; exit 1 ;;
  esac
fi

cat > "$DEST/BUNDLE_README.txt" <<EOF
AltruAgent tournament agent, bundle $VERSION (built $(date -u +%Y-%m-%dT%H:%MZ)).
Contains no API key. Runbook: docs/deploy.md.

Needs: Linux x86-64, Python 3.11+ (tested 3.12). The damage calculator needs Node.js >= 18:
$NODE_NOTE
agent/js/node_modules is $([ "$NODE_MODULES" = 1 ] && echo "included" || echo "NOT included: run (cd agent/js && npm ci --omit=dev)").
WITHOUT THE CALCULATOR THE AGENT WINS ONLY ~12% AGAINST ITSELF WITH IT: selfcheck fails and run_agent.sh
refuses to start when it cannot run (ARENA_ALLOW_NO_CALC=1 overrides; emergency only).

Install and check (no network access to the platform needed):
  python3 -m venv .venv && .venv/bin/pip install -r requirements.lock
  .venv/bin/python scripts/selfcheck.py            # must end with "selfcheck: ready" and show
                                                   # "OK    damage calculator answers: node ..."

Run one agent key (restarts, logs/agent-main.out, a health line every 5 minutes; it must show calc=ok(...)):
  export ALTRUAGENT_API_KEY=...   (or put it in .env, mode 600; see .env.example)
  bash scripts/run_agent.sh start   # also: stop / status / logs; "run" = foreground; systemd: install-systemd
Always start through scripts/run_agent.sh (or Docker / systemd): plain "python -m agent" does not check the calculator.

Docker (the image brings its own Node $NODE_VERSION):
  docker build -f deploy/Dockerfile -t arena-agent . && \\
  docker run -d --restart unless-stopped -e ALTRUAGENT_API_KEY=... -v arena-logs:/data arena-agent
EOF

# ---- secret scan ---------------------------------------------------------------------------------------------------
fail=0
bad_files="$(find "$DEST" -name '.env*' ! -name .env.example; find "$DEST" \( -name '*.pem' -o -name '*.key' -o -name 'id_rsa*' -o -name 'id_ed25519*' \))"
if [ -n "$bad_files" ]; then echo "SECRET SCAN: key files in the bundle:" >&2; echo "$bad_files" >&2; fail=1; fi
PATTERN="sk_agent_[A-Za-z0-9_-]{8,}|seatclaim_[A-Za-z0-9_-]{8,}|sk-(proj-|ant-)?[A-Za-z0-9_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|ALTRUAGENT_API_KEY=[\"']?[A-Za-z0-9_-]{16,}"
hits="$(grep -rIlE "$PATTERN" "$DEST" --exclude-dir=node_modules --exclude-dir=node 2>/dev/null || true)"
for f in $hits; do
  if grep -IoE "$PATTERN" "$f" | grep -vqE '^(ALTRUAGENT_API_KEY=)?sk_agent_your_key_here$'; then
    echo "SECRET SCAN: key-like string in ${f#$DEST/}:" >&2
    grep -IoE "$PATTERN" "$f" | sed -E 's/(.{12}).*/\1.../' | sort -u >&2
    fail=1
  fi
done
nm_hits="$(grep -rIlE 'sk_agent_[A-Za-z0-9]{8,}|seatclaim_[A-Za-z0-9]{8,}' "$DEST/agent/js/node_modules" "$DEST/node" 2>/dev/null || true)"
[ -n "$nm_hits" ] && { echo "SECRET SCAN: platform tokens inside node_modules / node: $nm_hits" >&2; fail=1; }
[ "$fail" = 0 ] || { echo "bundle NOT written" >&2; exit 1; }

(cd "$DEST" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)
mkdir -p "$OUT"
TAR="$OUT/$NAME.tar.gz"
tar -C "$STAGE" --sort=name --owner=0 --group=0 --numeric-owner -czf "$TAR" "$NAME"
(cd "$OUT" && sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256")
echo "wrote $TAR ($(du -h "$TAR" | cut -f1), $(tar -tzf "$TAR" | grep -vc '/$') files; node: $NODE_MODE; secret scan clean)"
