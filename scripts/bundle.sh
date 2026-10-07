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
#   bash scripts/bundle.sh --no-showdown        # leave the engine search's simulator out (then the bundle runs only
#                                               # with ARENA_BATTLE=classic: selfcheck fails, run_agent.sh refuses)
#   bash scripts/bundle.sh --no-sample-logs     # leave the recorded match out (selfcheck then skips its replay)
#   bash scripts/bundle.sh --wheels DIR         # ship a wheelhouse for an offline install (DIR/*.whl, made on a machine
#                                               # with network: see BUNDLE_README.txt); the target then needs no PyPI
#
# The sample match is the largest session recorded in BOTH logs/yoriko and logs/apocalypse with a draft: both seats of
# one whole real match (draft, team preview, ~45 battle decisions), which selfcheck and replay_real.py replay.
# The bundle carries its own .dockerignore (the Dockerfile builds from the bundle directory): .env files, .venv, logs,
# the node/ build (the image has its own) and wheels stay out of the build context.
#
# The engine-search battle policy (ARENA_BATTLE=search, agent/js/search.js) runs the Pokemon Showdown simulator: a
# trimmed copy of the pokemon-showdown package the local simulator already pins (local/js/package-lock.json, MIT
# license, shipped with it) goes to agent/js/node_modules/pokemon-showdown: dist/sim, dist/lib, dist/config, the
# top-level dist/data files (no mods' data, no random-battle sets: empty mod directories only, formats.js names
# them) and its two runtime dependencies (ts-chacha20, sql-template-strings), about 8 MB instead of 145 MB.
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

OUT="$ROOT/dist"; NODE_MODULES=1; SAMPLE_LOGS=1; NODE_MODE=pinned; NODE_SRC=""; SHOWDOWN=1; WHEELS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --wheels) WHEELS="$2"; shift 2 ;;
    --node-dist) NODE_MODE=dist; NODE_SRC="$2"; shift 2 ;;
    --node-tarball) NODE_MODE=tarball; NODE_SRC="$2"; shift 2 ;;
    --no-node) NODE_MODE=none; shift ;;
    --no-node-modules) NODE_MODULES=0; shift ;;
    --no-showdown) SHOWDOWN=0; shift ;;
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
         docs/restart_load.md
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
  rm -rf "$DEST/agent/js/node_modules/pokemon-showdown"          # (re)vendored trimmed below
fi
# the engine search's simulator: a trimmed pokemon-showdown (see the header) next to the calculator
SHOWDOWN_NOTE="NOT included (built with --no-showdown or --no-node-modules): selfcheck fails and run_agent.sh refuses to start unless ARENA_BATTLE=classic"
if [ "$NODE_MODULES" = 1 ] && [ "$SHOWDOWN" = 1 ]; then
  PS_SRC=""
  for d in agent/js/node_modules/pokemon-showdown local/js/node_modules/pokemon-showdown; do
    [ -f "$d/package.json" ] && [ -d "$d/dist/sim" ] && { PS_SRC="$d"; break; }
  done
  [ -n "$PS_SRC" ] || { echo "pokemon-showdown missing: run (cd local/js && npm ci) first, or pass --no-showdown" >&2; exit 1; }
  PSD="$DEST/agent/js/node_modules/pokemon-showdown"
  mkdir -p "$PSD/dist/data/mods"
  cp -L "$PS_SRC/package.json" "$PS_SRC/LICENSE" "$PSD/"
  cp -RL "$PS_SRC/dist/sim" "$PS_SRC/dist/lib" "$PS_SRC/dist/config" "$PSD/dist/"
  cp -RL "$PS_SRC/dist/data/text" "$PSD/dist/data/"
  cp -L "$PS_SRC"/dist/data/*.js "$PSD/dist/data/"
  for m in "$PS_SRC"/dist/data/mods/*/; do mkdir -p "$PSD/dist/data/mods/$(basename "$m")"; done
  find "$PSD" \( -name '*.map' -o -name '*.d.ts' \) -delete
  for dep in ts-chacha20 sql-template-strings; do
    [ -d "$(dirname "$PS_SRC")/$dep" ] || { echo "pokemon-showdown dependency $dep missing next to $PS_SRC" >&2; exit 1; }
    rm -rf "$DEST/agent/js/node_modules/$dep"
    cp -RL "$(dirname "$PS_SRC")/$dep" "$DEST/agent/js/node_modules/$dep"
  done
  SHOWDOWN_NOTE="included (pokemon-showdown $(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$PSD/package.json" | head -1), MIT, trimmed)"
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
SAMPLE_NOTE="none (--no-sample-logs)"
if [ "$SAMPLE_LOGS" = 1 ]; then          # one whole real match, both seats, for selfcheck's and replay_real.py's replay
  best=""; best_size=0
  for f in logs/yoriko/*.jsonl; do
    [ -f "$f" ] || continue
    g="logs/apocalypse/$(basename "$f")"
    [ -f "$g" ] && grep -q '"phase": "draft"' "$f" && grep -q '"phase": "draft"' "$g" || continue
    s=$(( $(wc -c < "$f") + $(wc -c < "$g") ))
    [ "$s" -gt "$best_size" ] && { best="$(basename "$f")"; best_size=$s; }
  done
  if [ -n "$best" ]; then
    mkdir -p "$DEST/logs/yoriko" "$DEST/logs/apocalypse"
    cp "logs/yoriko/$best" "$DEST/logs/yoriko/"; cp "logs/apocalypse/$best" "$DEST/logs/apocalypse/"
    SAMPLE_NOTE="both seats of real match ${best%.jsonl} ($(cat "logs/yoriko/$best" "logs/apocalypse/$best" | grep -c '"kind": "action"' || true) decisions)"
  else
    echo "WARNING: no match recorded in both logs/yoriko and logs/apocalypse: no sample match (selfcheck skips its replay)" >&2
  fi
fi
WHEELS_NOTE=""
if [ -n "$WHEELS" ]; then                # a wheelhouse made elsewhere (pip download ...): an offline install
  ls "$WHEELS"/*.whl >/dev/null 2>&1 || { echo "--wheels: no .whl files in $WHEELS" >&2; exit 1; }
  mkdir -p "$DEST/wheels"
  cp "$WHEELS"/*.whl "$DEST/wheels/"
  WHEELS_NOTE="wheels/ holds $(ls "$DEST/wheels" | wc -l | tr -d ' ') wheels: install offline with
  python3 -m venv .venv && .venv/bin/pip install --no-index --find-links wheels -r requirements.lock"
fi
cat > "$DEST/.dockerignore" <<'EOF'
# the build context of deploy/Dockerfile in this bundle: never keys, and none of the local bulk (the image installs
# its own Python packages and brings its own node)
.env
.env.*
!.env.example
.venv
logs
node
wheels
dist
**/__pycache__
**/*.pyc
EOF
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
  if [ "$SHOWDOWN" = 1 ]; then           # the engine search loads the vendored simulator (and nothing outside the bundle)
    ans="$(printf '%s\n' '{"id":1,"cmd":"warm"}' | (cd "$DEST/agent/js" && env -u ARENA_SHOWDOWN NODE_PATH="$DEST/agent/js/node_modules" timeout 120 "$DEST/node/bin/node" search.js) 2>&1 | head -n 1 || true)"
    case "$ans" in
      '{"id":1,"ok":true'*) echo "bundled node runs the engine search with the vendored pokemon-showdown" ;;
      *) echo "the bundled node did not start the engine search: ${ans:0:200}" >&2; echo "bundle NOT written" >&2; exit 1 ;;
    esac
  fi
fi

cat > "$DEST/BUNDLE_README.txt" <<EOF
AltruAgent tournament agent, bundle $VERSION (built $(date -u +%Y-%m-%dT%H:%MZ)).
Contains no key. Runbook: docs/deploy.md (the official tournament: section "比赛日手册").

Needs: Linux x86-64 with glibc >= 2.28 for the bundled node (Ubuntu >= 18.10, Debian >= 10, RHEL/Rocky/Alma >= 8,
Amazon Linux 2023; NOT Amazon Linux 2, CentOS 7, Alpine/musl or arm64 such as AWS Graviton: those need their own
Node >= 18 via ARENA_NODE), Python 3.11+ (tested 3.12) with its venv module (Ubuntu/Debian: apt install python3-venv).
The damage calculator and the engine search need Node.js >= 18:
$NODE_NOTE
agent/js/node_modules is $([ "$NODE_MODULES" = 1 ] && echo "included" || echo "NOT included: run (cd agent/js && npm ci --omit=dev)").
The engine search's simulator (the default battle policy, ARENA_BATTLE=search; ARENA_BATTLE=classic = the rollback): $SHOWDOWN_NOTE.
Sample match for the replay checks: $SAMPLE_NOTE.
WITHOUT THE CALCULATOR THE AGENT WINS ONLY ~12% AGAINST ITSELF WITH IT: selfcheck fails and run_agent.sh
refuses to start when it cannot run (ARENA_ALLOW_NO_CALC=1 overrides, emergency only; when node itself cannot run it
covers the engine search too: the agent then plays the classic battle policy).

Run it FROM THIS DIRECTORY. Never "pip install ." (a non-editable install drops data/, the draft model and the
JavaScript: the agent would play blind); only the pinned requirements go into the venv.

Install and check (no network access to the platform needed; PyPI is, unless the bundle has wheels/):
  python3 -m venv .venv && .venv/bin/pip install -r requirements.lock
${WHEELS_NOTE:-  (offline: on a machine with network and the same Python, pip download -r requirements.lock -d wheels
   --only-binary=:all: --platform manylinux2014_x86_64 --python-version 3.12, copy wheels/ here, then
   .venv/bin/pip install --no-index --find-links wheels -r requirements.lock)}
  .venv/bin/python scripts/selfcheck.py            # must end with "selfcheck: ready" and show
                                                   # "OK    damage calculator answers: node ..." and
                                                   # "OK    battle policy: engine search ..."
  .venv/bin/python scripts/replay_real.py          # the sample match through the agent: 0 failures

Play (the Official Agent Key from the tournament dashboard's Agent Configuration page, the agent set to Self-hosted;
every game, test matches and tournament games alike, arrives as an assignment of that key):
  put ALTRUAGENT_OFFICIAL_AGENT_KEY=eak_live_... in .env (mode 600; see .env.example)
  bash scripts/run_agent.sh check-tournament       # once, contacts the platform: every line must show a check mark
  bash scripts/run_agent.sh start                  # runs python -m agent --tournament --match (health: mode=tournament)
Keep it running while the event runs: a tournament game the agent does not connect to within its window (4 minutes,
Red Alert 4.5) is a loss. A second machine with the same key can be a hot standby: add ARENA_ROLE=standby to its .env.
It plays nothing while the primary plays (new seats are left to the primary) and takes a seat over ~30-40 s after the
primary stops renewing it. (The old sk_agent_ keys and Testing claim codes were retired by the platform on 10-06.)

Health line every 5 minutes in logs/agent-main.out: agent=up, calc=ok(...), search=ok. Also: stop / status / logs;
"run" = foreground; systemd: install-systemd. Start through scripts/run_agent.sh (or Docker / systemd): a plain
"python -m agent --tournament --match" gets no restarts and no log rotation (it does warn loudly when the calculator
or the engine search cannot run).

Docker (the image brings its own Node $NODE_VERSION; this directory's .dockerignore keeps keys and bulk out):
  docker build -f deploy/Dockerfile -t arena-agent .
  docker run -d --restart unless-stopped --env-file .env --log-opt max-size=20m --log-opt max-file=3 \\
      -v arena-logs:/data arena-agent
EOF

# ---- secret scan ---------------------------------------------------------------------------------------------------
fail=0
bad_files="$(find "$DEST" -name '.env*' ! -name .env.example; find "$DEST" \( -name '*.pem' -o -name '*.key' -o -name 'id_rsa*' -o -name 'id_ed25519*' \))"
if [ -n "$bad_files" ]; then echo "SECRET SCAN: key files in the bundle:" >&2; echo "$bad_files" >&2; fail=1; fi
PATTERN="sk_agent_[A-Za-z0-9_-]{8,}|seatclaim_[A-Za-z0-9_-]{8,}|eak_live_[0-9A-Fa-f]{16,}|sk-(proj-|ant-)?[A-Za-z0-9_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|ALTRUAGENT_API_KEY=[\"']?[A-Za-z0-9_-]{16,}|ALTRUAGENT_OFFICIAL_AGENT_KEY=[\"']?[A-Za-z0-9_-]{16,}"
hits="$(grep -rIlE "$PATTERN" "$DEST" --exclude-dir=node_modules --exclude-dir=node 2>/dev/null || true)"
for f in $hits; do
  # (the placeholders of .env.example are no keys)
  if grep -IoE "$PATTERN" "$f" | grep -vqE '^((ALTRUAGENT_API_KEY=)?sk_agent_your_key_here|ALTRUAGENT_OFFICIAL_AGENT_KEY=eak_live_your_key_here)$'; then
    echo "SECRET SCAN: key-like string in ${f#$DEST/}:" >&2
    grep -IoE "$PATTERN" "$f" | sed -E 's/(.{12}).*/\1.../' | sort -u >&2
    fail=1
  fi
done
nm_hits="$(grep -rIlE 'sk_agent_[A-Za-z0-9]{8,}|seatclaim_[A-Za-z0-9]{8,}|eak_live_[0-9a-f]{16,}' "$DEST/agent/js/node_modules" "$DEST/node" 2>/dev/null || true)"
[ -n "$nm_hits" ] && { echo "SECRET SCAN: platform tokens inside node_modules / node: $nm_hits" >&2; fail=1; }
[ "$fail" = 0 ] || { echo "bundle NOT written" >&2; exit 1; }

(cd "$DEST" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)
mkdir -p "$OUT"
TAR="$OUT/$NAME.tar.gz"
tar -C "$STAGE" --sort=name --owner=0 --group=0 --numeric-owner -czf "$TAR" "$NAME"
(cd "$OUT" && sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256")
echo "wrote $TAR ($(du -h "$TAR" | cut -f1), $(tar -tzf "$TAR" | grep -vc '/$') files; node: $NODE_MODE; secret scan clean)"
