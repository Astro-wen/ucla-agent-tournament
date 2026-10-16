#!/usr/bin/env bash
# Export the competition code of ONE commit of this (private) repository into a directory, for a separate public
# repository (Official Rules section 8; option C of the 10-08 pre-publication scan: this repository stays private and a
# clean public repository carries only what operates the agent). Everything comes from `git archive <commit>`, never
# from the working tree, so an untracked .env or scratch file can never be exported. It never pushes, never creates or
# changes a GitHub repository and never contacts the platform: committing and pushing the export is the owner's step.
#
#   bash scripts/publish_sync.sh --out DIR [--commit REV]  # export + scan + tree hash + install check
#   bash scripts/publish_sync.sh --out CLONE --replace ...  # sync into a clone of the public repository: everything
#                                                          # in CLONE except CLONE/.git is replaced by the export
#     --no-verify      export, scan and tree hash only (no venv, no npm, no selfcheck, no tests)
#     --no-tests       install check without running the exported tests
#     --npm link       link this checkout's node_modules instead of `npm ci` (offline; the venv is still a clean one)
#   bash scripts/publish_sync.sh --list [--commit REV]      # what would be exported (paths and tests), nothing else
#   bash scripts/publish_sync.sh --scan-only DIR            # only the secret / sensitive-string scan of DIR
#
# What is exported (the same set scripts/bundle.sh ships, plus the tests that run on their own): agent/ altruagent/
# data/ deploy/, the run / check scripts and bundle.sh, local/js/package*.json (the pin of the pokemon-showdown package
# the engine search loads; nothing else of local/), docs/deploy.md and docs/restart_load.md (the runbook bundle.sh
# ships), pyproject.toml requirements.lock .env.example .gitignore .gitattributes .dockerignore GAMES.md NOTICE, one
# recorded real match (both seats, the one bundle.sh picks) for selfcheck's replay, PUBLIC_README.md as README.md, and
# the tests whose imports and paths are all inside the export (and whose imported test modules are exported too),
# minus NON_ISOLATED_TESTS (they fail outside this repository). Not exported: this script, CLAUDE.md, the other docs, local/ code, champion/, versions/, examples/,
# the other scripts, recordings, history.
#
# The scan (any finding = NOT READY): key files (.env*, *.pem, *.key, id_rsa ...); key-like strings (Official Agent
# Keys, sk_agent_ / seatclaim_ tokens, model-provider, GitHub, AWS, Google, Slack, Hugging Face keys, private keys,
# JWTs, KEY=value lines), except the known placeholders and test fakes; e-mail addresses other than no-reply and
# example ones, and the private-correspondence names; wording about the suspected platform issue that is reported
# privately to the organizers (Official Rules section 7); local machine paths and user names. The README check: every
# repository path the exported README names must exist in the export. The tree hash printed at the end is what
# `git rev-parse HEAD^{tree}` prints in the public repository once exactly these files are committed (`git add -A`);
# the exported code subtrees are checked equal to the commit's.
#
# The install check runs in a copy of the export (the export itself stays clean): a fresh venv from
# requirements.lock (+ pytest), npm ci of agent/js and local/js, `import agent` must resolve to the copy, then
# scripts/selfcheck.py must end with "selfcheck: ready" and the exported tests must pass, all with a minimal
# environment (PATH, HOME, LANG; ARENA_RECORD=0; no key). It needs PyPI and the npm registry (or --npm link).
#
# Exit: 0 READY, 2 usage / refused, 3 scan or README findings, 4 install check failed, 1 anything else.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

EXPORT_PATHS=(agent altruagent assets data deploy
  scripts/run_agent.sh scripts/selfcheck.py scripts/check_connection.py scripts/replay_real.py scripts/bundle.sh
  local/js/package.json local/js/package-lock.json
  docs/deploy.md docs/restart_load.md
  pyproject.toml requirements.lock .env.example .gitignore .gitattributes .dockerignore GAMES.md NOTICE)
PUBLIC_README=PUBLIC_README.md          # exported as README.md
# tests that pass in this repository but not in the export (they read files the export does not have); found by the
# install check, which lists any other failing test file
NON_ISOLATED_TESTS=()

usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; }
die() { echo "publish_sync: $*" >&2; exit 2; }

COMMIT_REV=HEAD; OUT=""; REPLACE=0; VERIFY=1; TESTS=1; NPM=ci; MODE=export; SCAN_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --commit) [ $# -ge 2 ] || die "--commit needs a revision"; COMMIT_REV="$2"; shift 2 ;;
    --out) [ $# -ge 2 ] || die "--out needs a directory"; OUT="$2"; shift 2 ;;
    --replace) REPLACE=1; shift ;;
    --no-verify) VERIFY=0; shift ;;
    --no-tests) TESTS=0; shift ;;
    --npm) [ $# -ge 2 ] || die "--npm needs ci or link"; NPM="$2"; shift 2
           case "$NPM" in ci|link) ;; *) die "--npm: ci or link, not $NPM" ;; esac ;;
    --list) MODE=list; shift ;;
    --scan-only) [ $# -ge 2 ] || die "--scan-only needs a directory"; MODE=scan; SCAN_DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

# ---- the scan ------------------------------------------------------------------------------------------------------
KEY_RE='eak_live_[0-9A-Fa-f]{16,}|sk_agent_[A-Za-z0-9_-]{8,}|seatclaim_[A-Za-z0-9_-]{8,}|sk-(proj-|ant-|live-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|xox[abprs]-[A-Za-z0-9-]{10,}|hf_[A-Za-z0-9]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}|(ALTRUAGENT_OFFICIAL_AGENT_KEY|ALTRUAGENT_API_KEY|OPENAI_API_KEY|NEBIUS_API_KEY|ANTHROPIC_API_KEY|GITHUB_TOKEN)=["'\'']?[A-Za-z0-9_-]{12,}'
# placeholders and the tests' fakes (a match equal to one of these whole is not a finding)
KEY_ALLOW_RE='^((ALTRUAGENT_OFFICIAL_AGENT_KEY=)?eak_live_your_key_here|(ALTRUAGENT_API_KEY=)?sk_agent_(your_key_here|test[A-Za-z0-9_]*|FAKE[A-Za-z0-9_-]*|old_platform_key|wrong_kind|OTHERKEY[0-9]*|REALLOOKINGKEY[0-9]*|REPLACE_ME|oops)|seatclaim_(cli_secret_token|abc123secret|should_be_ignored|token_is_rejected)[A-Za-z0-9_-]*|sk-test-DO-NOT-LEAK-1234567890)$'
EMAIL_RE='[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}'
EMAIL_ALLOW_RE='(@users\.noreply\.github\.com|^noreply@|^no-reply@|@example\.(com|org|net)|^git@github\.com)$'
PRIVATE_RE='\b(guang|sandra)\b|回信|私人邮件|private (e-?mail|reply|correspondence)'
ISSUE_RE='spectat[a-z]* (feed|stream|state|endpoint|api|sse)|/spectate|dm recipients?|recipients? of (every|each) dm|who messaged whom|spectators see a move after|confirm spectators|before both (sides|players) (have )?(chosen|moved)|sse (message|event)|观战(数据流|接口)|私信(内容|的收件人)'
PATH_RE='/home/[a-z][a-z0-9_-]*/|/mnt/[a-z]/|[A-Za-z]:\\+Users\\+|/Users/[A-Za-z]|linmengfdm|siyuan|~/exp[0-9]|~/wt/|_compliance_out|scratchpad|wsl\.localhost|/tmp/claude'
BAD_FILES_FIND=( \( -name '.env' -o -name '.env.*' ! -name '.env.example' -o -name '*.pem' -o -name '*.key' -o -name '*.p12'
                    -o -name 'id_rsa*' -o -name 'id_ed25519*' -o -name '.npmrc' -o -name '.pypirc' -o -name '.netrc' \) )

mask() { sed -E 's/^(.{10}).*/\1.../'; }

scan_tree() {   # DIR -> prints findings and leaves their number in SCAN_FINDINGS
  local d="$1" n=0 f line hit
  SCAN_FINDINGS=0
  while IFS= read -r f; do
    echo "  KEY FILE  ${f#"$d"/}"; n=$((n + 1))
  done < <(find "$d" -path "$d/.git" -prune -o -type f "${BAD_FILES_FIND[@]}" -print 2>/dev/null)
  while IFS= read -r line; do            # path:line:match (one per match)
    hit="${line#*:*:}"
    if grep -qE "$KEY_ALLOW_RE" <<<"$hit"; then continue; fi
    echo "  KEY       ${line%%:*}:$(printf '%s' "${line#*:}" | cut -d: -f1)  $(printf '%s' "$hit" | mask)"; n=$((n + 1))
  done < <(cd "$d" && grep -rIonE --exclude-dir=.git --exclude-dir=node_modules "$KEY_RE" . 2>/dev/null | sed 's#^\./##')
  while IFS= read -r line; do
    hit="${line#*:*:}"
    if grep -qiE "$EMAIL_ALLOW_RE" <<<"$hit"; then continue; fi
    echo "  EMAIL     ${line%:*}  ${hit%%@*}@..."; n=$((n + 1))
  done < <(cd "$d" && grep -rIonE --exclude-dir=.git --exclude-dir=node_modules --exclude=package-lock.json "$EMAIL_RE" . 2>/dev/null | sed 's#^\./##')
  # (package-lock.json: npm's own metadata of third-party packages, e.g. a maintainer's public contact in a notice)
  while IFS= read -r line; do
    echo "  PRIVATE   ${line%%:*}:$(printf '%s' "${line#*:}" | cut -d: -f1)"; n=$((n + 1))
  done < <(cd "$d" && grep -rIniE --exclude-dir=.git --exclude-dir=node_modules "$PRIVATE_RE" . 2>/dev/null | sed 's#^\./##')
  while IFS= read -r line; do
    echo "  ISSUE     ${line%%:*}:$(printf '%s' "${line#*:}" | cut -d: -f1)  (wording about the privately reported platform issue)"; n=$((n + 1))
  done < <(cd "$d" && grep -rIniE --exclude-dir=.git --exclude-dir=node_modules "$ISSUE_RE" . 2>/dev/null | sed 's#^\./##')
  while IFS= read -r line; do
    echo "  PATH      ${line%%:*}:$(printf '%s' "${line#*:}" | cut -d: -f1)  $(grep -oiE "$PATH_RE" <<<"${line#*:*:}" | sed -n 1p)"; n=$((n + 1))
  done < <(cd "$d" && grep -rIniE --exclude-dir=.git --exclude-dir=node_modules "$PATH_RE" . 2>/dev/null | sed 's#^\./##')
  SCAN_FINDINGS=$n
}

readme_paths() {   # DIR -> prints every repository path its README.md names in backticks that the export lacks
  local d="$1" p
  [ -f "$d/README.md" ] || { echo "  README.md missing"; return 0; }
  while IFS= read -r p; do
    case "$p" in
      agent/*|altruagent/*|data/*|deploy/*|scripts/*|docs/*|local/*|tests/*|logs/*) ;;
      *) continue ;;
    esac
    case "$p" in *'*'*|*'<'*|*'>'*|*'...'*|logs/*.out) continue ;; esac     # (patterns, placeholders, run-time logs)
    [ -e "$d/$p" ] || echo "  README names $p, which is not in the export"
  done < <(grep -oE '`[^` ]+`' "$d/README.md" | tr -d '`' | sed -E 's/[.,;:)]+$//' | sort -u || true)
  return 0
}

if [ "$MODE" = scan ]; then
  [ -d "$SCAN_DIR" ] || die "--scan-only: $SCAN_DIR is not a directory"
  scan_tree "$(cd "$SCAN_DIR" && pwd)"
  if [ "$SCAN_FINDINGS" -eq 0 ]; then echo "scan: clean"; exit 0; fi
  echo "scan: $SCAN_FINDINGS finding(s)"; exit 3
fi

# ---- the commit and what it exports --------------------------------------------------------------------------------
cd "$ROOT"
git rev-parse --git-dir >/dev/null 2>&1 || die "$ROOT is not a git checkout"
COMMIT="$(git rev-parse --verify --quiet "$COMMIT_REV^{commit}")" || die "no such commit: $COMMIT_REV"
SUBJECT="$(git log -1 --format=%s "$COMMIT")"
for p in "${EXPORT_PATHS[@]}" "$PUBLIC_README"; do
  git cat-file -e "$COMMIT:$p" 2>/dev/null || die "$p is not in commit ${COMMIT:0:12} (every exported path must be)"
done

EXPORTED_SCRIPTS_RE='scripts/(run_agent\.sh|selfcheck\.py|check_connection\.py|replay_real\.py|bundle\.sh)'
# (here-strings and grep -c below, never `producer | grep -q`: with pipefail an early grep exit can fail the producer)
blob_has() { git cat-file blob "$COMMIT:$1" | grep -cF -- "$2" >/dev/null; }     # COMMIT's file $1 contains $2
select_tests() {   # prints the test files of COMMIT whose imports and paths are all inside the export
  local t body s bad
  while IFS= read -r t; do
    for s in "${NON_ISOLATED_TESTS[@]+"${NON_ISOLATED_TESTS[@]}"}"; do [ "$t" = "$s" ] && continue 2; done
    body="$(git cat-file blob "$COMMIT:$t")"
    # imports of code that is not exported
    grep -qE '^\s*(from|import)\s+(local|champion|versions|examples|scripts)([. ]|$)' <<<"$body" && continue
    # paths that are not exported: local/ champion/ versions/ examples/ (as a path, a quoted name or a quoted dotted
    # module such as "examples.smoke_agent"), recordings, CLAUDE.md, HANDOFF, a "docs" directory, docs other than the
    # two exported ones, scripts other than the exported ones
    grep -qE '(^|[^A-Za-z0-9_.-])(local|champion|versions|examples)/|["'\''](local|champion|versions|examples)[./"'\'']|["'\''](logs|docs)["'\'']|logs/|CLAUDE\.md|HANDOFF' <<<"$body" && continue
    bad="$(grep -oE 'docs/[A-Za-z0-9_./-]*' <<<"$body" | grep -vxE 'docs/(deploy|restart_load)\.md' || true)"
    [ -n "$bad" ] && continue
    bad="$(grep -oE 'scripts/[A-Za-z0-9_.-]+' <<<"$body" | grep -vxE "$EXPORTED_SCRIPTS_RE" || true)"
    [ -n "$bad" ] && continue
    echo "$t"
  done < <(git ls-tree -r --name-only "$COMMIT" -- tests | grep -E '(^|/)test_[^/]*\.py$' || true)
}
test_deps() {   # a test file -> the test files it imports (from test_x / .test_x / tests.strategy.test_x import ...,
  local t="$1" dir body m                     # from tests.strategy import test_x)
  dir="$(dirname "$t")"
  body="$(git cat-file blob "$COMMIT:$t")"
  while IFS= read -r m; do
    [ -n "$m" ] || continue
    if git cat-file -e "$COMMIT:$dir/$m.py" 2>/dev/null; then echo "$dir/$m.py"; else echo "tests/$m.py"; fi
  done < <(grep -oE '^\s*(from|import)\s+test_[A-Za-z0-9_]+' <<<"$body" | awk '{ print $2 }' || true)
  { grep -oE '^\s*from\s+\.test_[A-Za-z0-9_]+' <<<"$body" | sed -E "s#.*\.(test_[A-Za-z0-9_]+)#$dir/\1.py#"
    grep -oE '^\s*from\s+tests(\.[A-Za-z0-9_]+)*\.test_[A-Za-z0-9_]+\s+import' <<<"$body" | awk '{ print $2 }' | tr . / | sed 's/$/.py/'
    grep -oE '^\s*from\s+tests(\.[A-Za-z0-9_]+)*\s+import\s+test_[A-Za-z0-9_]+' <<<"$body" | awk '{ print $2 "." $4 }' | tr . / | sed 's/$/.py/'
  } || true
}
in_tests() { local x; for x in "${TEST_FILES[@]+"${TEST_FILES[@]}"}"; do [ "$x" = "$1" ] && return 0; done; return 1; }
mapfile -t TEST_FILES < <(select_tests)
changed=1                                   # drop tests that import a test that is not exported, until none does
while [ "$changed" = 1 ]; do
  changed=0; keep=()
  for t in "${TEST_FILES[@]+"${TEST_FILES[@]}"}"; do
    ok=1
    while IFS= read -r dep; do
      [ -n "$dep" ] || continue
      in_tests "$dep" || { ok=0; break; }
    done < <(test_deps "$t")
    if [ "$ok" = 1 ]; then keep+=("$t"); else changed=1; fi
  done
  TEST_FILES=("${keep[@]+"${keep[@]}"}")
done
TEST_SUPPORT=()
if [ "${#TEST_FILES[@]}" -gt 0 ]; then
  git cat-file -e "$COMMIT:tests/conftest.py" 2>/dev/null && TEST_SUPPORT+=(tests/conftest.py)
  if grep -q '^tests/strategy/' <<<"$(printf '%s\n' "${TEST_FILES[@]}")"; then
    git cat-file -e "$COMMIT:tests/strategy/__init__.py" 2>/dev/null && TEST_SUPPORT+=(tests/strategy/__init__.py)
    while IFS= read -r f; do        # fixtures a selected test names
      b="$(basename "$f")"
      for t in "${TEST_FILES[@]}"; do
        if blob_has "$t" "$b"; then TEST_SUPPORT+=("$f"); break; fi
      done
    done < <(git ls-tree -r --name-only "$COMMIT" -- tests/strategy/data)
  fi
fi
# the sample match: the largest session recorded by both seats with a draft (the one scripts/bundle.sh ships)
SAMPLE=()
best=""; best_size=0
while IFS= read -r f; do
  b="$(basename "$f")"
  git cat-file -e "$COMMIT:logs/apocalypse/$b" 2>/dev/null || continue
  blob_has "$f" '"phase": "draft"' || continue
  blob_has "logs/apocalypse/$b" '"phase": "draft"' || continue
  s=$(( $(git cat-file -s "$COMMIT:$f") + $(git cat-file -s "$COMMIT:logs/apocalypse/$b") ))
  if [ "$s" -gt "$best_size" ]; then best="$b"; best_size=$s; fi
done < <(git ls-tree -r --name-only "$COMMIT" -- logs/yoriko | grep '\.jsonl$' || true)
[ -n "$best" ] && SAMPLE=("logs/yoriko/$best" "logs/apocalypse/$best")

ALL_PATHS=("${EXPORT_PATHS[@]}" "${SAMPLE[@]+"${SAMPLE[@]}"}" "${TEST_FILES[@]+"${TEST_FILES[@]}"}" "${TEST_SUPPORT[@]+"${TEST_SUPPORT[@]}"}")

if [ "$MODE" = list ]; then
  echo "commit $COMMIT ($SUBJECT)"
  git ls-tree -r --name-only "$COMMIT" -- "${ALL_PATHS[@]}"
  echo "README.md  (from $PUBLIC_README)"
  echo "-- ${#TEST_FILES[@]} test files, sample match: ${best:-none}"
  exit 0
fi

# ---- export --------------------------------------------------------------------------------------------------------
[ -n "$OUT" ] || die "--out DIR is required (see --help)"
OUT="$(realpath -m -- "$OUT")"                      # (checked before anything is created)
ROOT_REAL="$(realpath -- "$ROOT")"
case "$OUT/" in
  "$ROOT"/*|"$ROOT_REAL"/*|/|/home/|/tmp/) die "refusing --out $OUT (inside this repository or a system directory)" ;;
esac
case "$ROOT_REAL/" in "$OUT"/*) die "refusing --out $OUT (it contains this repository)" ;; esac
[ "$OUT" = "$(realpath -m -- "${HOME:-/nonexistent}")" ] && die "refusing --out $OUT (the home directory)"
mkdir -p "$OUT"
if [ -n "$(ls -A "$OUT" | grep -vx '.git' || true)" ]; then
  [ "$REPLACE" = 1 ] || die "$OUT is not empty (use --replace to replace everything in it except .git)"
  find "$OUT" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
fi
git archive --format=tar "$COMMIT" -- "${ALL_PATHS[@]}" | tar -x -C "$OUT"
git show "$COMMIT:$PUBLIC_README" > "$OUT/README.md"

FILES="$(cd "$OUT" && find . -path ./.git -prune -o -type f -print | wc -l | tr -d ' ')"
BYTES="$(cd "$OUT" && find . -path ./.git -prune -o -type f -printf '%s\n' | awk '{ s += $1 } END { print s + 0 }')"
echo "exported commit $COMMIT ($SUBJECT)"
echo "into $OUT: $FILES files, $BYTES bytes ($(awk -v b="$BYTES" 'BEGIN { printf "%.1f MB", b / 1048576 }')), ${#TEST_FILES[@]} test files, sample match ${best:-none}"
echo "top level: $(cd "$OUT" && ls -A | grep -vx '.git' | tr '\n' ' ')"

# ---- scan and README check -----------------------------------------------------------------------------------------
echo "== scan"
scan_tree "$OUT"
[ "$SCAN_FINDINGS" -eq 0 ] && echo "  clean"
echo "== README paths"
README_MISSING="$(readme_paths "$OUT")"
if [ -n "$README_MISSING" ]; then echo "$README_MISSING"; else echo "  every path the README names is in the export"; fi
large="$(cd "$OUT" && find . -path ./.git -prune -o -type f -size +5M -print)"
[ -n "$large" ] && echo "  NOTE files over 5 MB: $large"

# ---- tree hash (what the public repository records after `git add -A`) ---------------------------------------------
echo "== tree hash"
TMPG="$(mktemp -d)"
cleanup() { rm -rf "$TMPG" ${WORK:+"$WORK"}; }
trap cleanup EXIT
git init -q "$TMPG/repo"
G=(git -c core.autocrlf=false -c core.fileMode=true --git-dir="$TMPG/repo/.git" --work-tree="$OUT")
"${G[@]}" add -A
TREE="$("${G[@]}" write-tree)"
IGNORED="$("${G[@]}" ls-files --others --ignored --exclude-standard | sed -n '1,20p')"
echo "  tree $TREE"
TREE_PROBLEMS=0
if [ -n "$IGNORED" ]; then
  echo "  exported files the exported .gitignore ignores (a public commit would leave them out):"; echo "$IGNORED" | sed 's/^/    /'
  TREE_PROBLEMS=1
fi
for p in "${EXPORT_PATHS[@]}" "${SAMPLE[@]+"${SAMPLE[@]}"}" "${TEST_FILES[@]+"${TEST_FILES[@]}"}" "${TEST_SUPPORT[@]+"${TEST_SUPPORT[@]}"}"; do
  a="$(git rev-parse "$COMMIT:$p")"; b="$("${G[@]}" rev-parse "$TREE:$p" 2>/dev/null || echo missing)"
  if [ "$a" != "$b" ]; then echo "  DIFFERENT from the commit: $p ($a vs $b)"; TREE_PROBLEMS=1; fi
done
[ "$TREE_PROBLEMS" = 0 ] && echo "  every exported path is identical to commit ${COMMIT:0:12} (same blobs and modes)"

# ---- install check (in a copy) -------------------------------------------------------------------------------------
VERIFY_RESULT="skipped (--no-verify)"; VERIFY_OK=1
if [ "$VERIFY" = 1 ]; then
  VERIFY_RESULT="running"
  echo "== install check (clean venv, ${NPM} node_modules, selfcheck$([ "$TESTS" = 1 ] && echo ", ${#TEST_FILES[@]} test files"))"
  WORK="$(mktemp -d)"; CHECK="$WORK/check"; mkdir -p "$CHECK" "$WORK/home" "$WORK/locks" "$WORK/state" "$WORK/tmp"
  cp -a "$OUT/." "$CHECK/"; rm -rf "$CHECK/.git"
  PY="${PYTHON:-python3}"
  VERIFY_OK=0
  step() { echo "  - $*"; }
  if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    VERIFY_RESULT="failed: $PY is older than 3.11"
  elif ! "$PY" -m venv "$WORK/venv" >"$WORK/venv.log" 2>&1; then
    VERIFY_RESULT="failed: python -m venv ($(tail -1 "$WORK/venv.log"))"
  elif step "pip install -r requirements.lock" && ! "$WORK/venv/bin/pip" install -q --disable-pip-version-check -r "$CHECK/requirements.lock" >"$WORK/pip.log" 2>&1; then
    VERIFY_RESULT="failed: pip install -r requirements.lock ($(tail -1 "$WORK/pip.log"))"
  elif [ "$TESTS" = 1 ] && [ "${#TEST_FILES[@]}" -gt 0 ] && ! "$WORK/venv/bin/pip" install -q --disable-pip-version-check pytest >>"$WORK/pip.log" 2>&1; then
    VERIFY_RESULT="failed: pip install pytest ($(tail -1 "$WORK/pip.log"))"
  else
    npm_ok=1
    if [ "$NPM" = ci ]; then
      step "npm ci (agent/js --omit=dev, local/js)"
      (cd "$CHECK/agent/js" && npm ci --omit=dev --no-audit --no-fund --loglevel=error) >"$WORK/npm.log" 2>&1 || npm_ok=0
      [ "$npm_ok" = 1 ] && { (cd "$CHECK/local/js" && npm ci --no-audit --no-fund --loglevel=error) >>"$WORK/npm.log" 2>&1 || npm_ok=0; }
    else
      step "link node_modules from $ROOT"
      for j in agent/js local/js; do
        [ -d "$ROOT/$j/node_modules" ] && ln -s "$(cd "$ROOT/$j/node_modules" && pwd -P)" "$CHECK/$j/node_modules" || npm_ok=0
      done
    fi
    CLEAN=(env -i PATH="$WORK/venv/bin:/usr/local/bin:/usr/bin:/bin" HOME="$WORK/home" LANG=C.UTF-8 TMPDIR="$WORK/tmp"
           ARENA_RECORD=0 ARENA_LOCK_DIR="$WORK/locks" ARENA_STATE_DIR="$WORK/state")
    if [ "$npm_ok" != 1 ]; then
      VERIFY_RESULT="failed: node_modules ($(tail -2 "$WORK/npm.log" 2>/dev/null | tr '\n' ' '))"
    else
      where="$(cd "$CHECK" && "${CLEAN[@]}" python -c 'import agent, altruagent; print(agent.__file__); print(altruagent.__file__)' 2>&1 || true)"
      case "$where" in
        "$CHECK/agent/"*) ;;
        *) VERIFY_RESULT="failed: import agent resolves outside the export copy: $where" ;;
      esac
      if [ "${VERIFY_RESULT#failed}" = "$VERIFY_RESULT" ]; then
        step "scripts/selfcheck.py"
        (cd "$CHECK" && "${CLEAN[@]}" python scripts/selfcheck.py) >"$WORK/selfcheck.log" 2>&1 || true
        grep -E 'FAIL|WARN|selfcheck:|replayed|engine search|damage calculator' "$WORK/selfcheck.log" | sed -n '1,20s/^/      /p' || true
        if ! grep -q '^selfcheck: ready' "$WORK/selfcheck.log"; then
          VERIFY_RESULT="failed: selfcheck is not ready"
        elif [ "$TESTS" = 1 ] && [ "${#TEST_FILES[@]}" -gt 0 ]; then
          step "python -m pytest -q tests (${#TEST_FILES[@]} files)"
          (cd "$CHECK" && "${CLEAN[@]}" python -m pytest -q -p no:cacheprovider --tb=line -rf tests) >"$WORK/pytest.log" 2>&1 && prc=0 || prc=$?
          summary="$(grep -E '(passed|failed|error)' "$WORK/pytest.log" | tail -n 1 || true)"
          echo "      $summary"
          if [ "$prc" != 0 ]; then
            grep -E '^(FAILED|ERROR) ' "$WORK/pytest.log" | sed -E 's/::.*//' | sort | uniq -c | sed -n '1,30s/^/      /p' || true
            VERIFY_RESULT="failed: exported tests ($summary)"
          else
            VERIFY_OK=1; VERIFY_RESULT="passed (selfcheck ready; tests: $summary)"
          fi
        else
          VERIFY_OK=1; VERIFY_RESULT="passed (selfcheck ready; tests not run)"
        fi
      fi
    fi
  fi
  if [ -n "${KEEP_WORK:-}" ]; then echo "  (work directory kept: $WORK)"; WORK=""; fi
fi

# ---- verdict -------------------------------------------------------------------------------------------------------
echo "== result"
echo "  scan: $([ "$SCAN_FINDINGS" -eq 0 ] && echo clean || echo "$SCAN_FINDINGS finding(s)")"
echo "  README paths: $([ -z "$README_MISSING" ] && echo ok || echo "$(printf '%s\n' "$README_MISSING" | wc -l | tr -d ' ') missing")"
echo "  tree: $TREE$([ "$TREE_PROBLEMS" = 0 ] || echo " (problems above)")"
echo "  install check: $VERIFY_RESULT"
rc=0
[ "$SCAN_FINDINGS" -eq 0 ] && [ -z "$README_MISSING" ] && [ "$TREE_PROBLEMS" = 0 ] || rc=3
[ "$rc" = 0 ] && [ "$VERIFY_OK" != 1 ] && rc=4
if [ "$rc" = 0 ]; then
  echo "READY: commit $OUT to the public repository's main, e.g. \"Sync from the development repository at ${COMMIT:0:12}\";"
  echo "after the commit, git rev-parse HEAD^{tree} there must print $TREE. This script pushes nothing."
else
  echo "NOT READY (exit $rc): fix the findings above in this repository, commit, and run it again."
fi
exit "$rc"
