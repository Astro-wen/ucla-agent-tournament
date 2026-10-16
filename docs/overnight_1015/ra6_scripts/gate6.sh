#!/bin/bash
# Gate of ra/tom-1015 (resumable: a step whose ".ok" marker exists is skipped). Output: /home/linmengfdm/wt/_ra6_gate.
# The worktree must not change meanwhile. Never touches the main checkout, its logs/ or .env.
set -u
WT=/home/linmengfdm/wt/ra6
O=${GATE_OUT:-/home/linmengfdm/wt/_ra6_gate}
PY=/home/linmengfdm/ucla-agent-tournament/.venv/bin/python
S=/home/linmengfdm/r6
BASE=867ec65
mkdir -p $O
step() { echo "=== $(date '+%F %T') $*"; }
done_() { touch "$O/$1.ok"; }
skip() { [ -f "$O/$1.ok" ] && { echo "--- skip $1 (done)"; return 0; }; return 1; }
cd $WT
export PYTHONPATH=$WT ARENA_RECORD=0
TIP=$(git rev-parse --short HEAD)
echo "HEAD $TIP champion $(cat champion/VERSION) dirty=$(git status --porcelain --untracked-files=no | wc -l)"
echo "agent/ changes since $BASE:"; git diff --stat $BASE HEAD -- agent/ | tail -n 3
echo "agent/ outside redalert since $BASE: $(git diff $BASE HEAD -- agent/ ':!agent/redalert' ':!champion' | wc -l) lines"
echo "altruagent/ vs $BASE: $(git diff $BASE HEAD -- altruagent/ | wc -l) lines of diff"
$PY -c "import agent, altruagent; print('agent from', agent.__file__, '| sdk from', altruagent.__file__)"

if ! skip help; then
  step "python -m agent --help"
  $PY -m agent --help > $O/help.txt 2>&1; echo "exit $?"
  grep -q -- '--tournament' $O/help.txt && grep -q -- '--match' $O/help.txt && ! grep -q -- '--claim' $O/help.txt && done_ help
fi
if ! skip pytest; then
  step "full pytest (worktree)"
  T0=$(date +%s)
  flock /tmp/arena-heavy.lock env ARENA_RECORD=0 $PY -m pytest -q -p no:cacheprovider > $O/pytest.log 2>&1; rc=$?
  echo "pytest rc=$rc ($(( $(date +%s) - T0 )) s)"; tail -n 3 $O/pytest.log
  grep -E '^(FAILED|ERROR)' $O/pytest.log | head -20
  [ $rc = 0 ] && done_ pytest
fi
if ! skip replay; then
  step "replay_real.py"
  flock /tmp/arena-heavy.lock $PY scripts/replay_real.py > $O/replay.log 2> $O/replay.err; rc=$?
  echo "replay rc=$rc"; tail -n 6 $O/replay.log
  [ $rc = 0 ] && done_ replay
fi
if ! skip nocalc; then
  step "replay_real.py --no-calc"
  flock /tmp/arena-heavy.lock $PY scripts/replay_real.py --no-calc > $O/nocalc.log 2> $O/nocalc.err; rc=$?
  echo "nocalc rc=$rc"; tail -n 6 $O/nocalc.log
  [ $rc = 0 ] && done_ nocalc
fi
if ! skip selfcheck; then
  step "selfcheck (worktree)"
  $PY scripts/selfcheck.py > $O/selfcheck.log 2>&1; rc=$?
  echo "selfcheck rc=$rc"; tail -n 4 $O/selfcheck.log
  [ $rc = 0 ] && grep -q 'selfcheck: ready' $O/selfcheck.log && done_ selfcheck
fi
if ! skip bundle; then
  step "bundle"
  rm -rf $O/dist $O/x
  flock /tmp/arena-heavy.lock nice -n 10 bash scripts/bundle.sh --out $O/dist > $O/bundle.log 2>&1; rc=$?
  echo "bundle rc=$rc"; tail -n 4 $O/bundle.log
  if [ $rc = 0 ]; then
    mkdir -p $O/x && tar xzf $O/dist/arena-agent-*.tar.gz -C $O/x
    B=$(ls -d $O/x/arena-agent-*)
    echo "bundle dir $B"; du -sh $O/dist/*.tar.gz; echo "files: $(find $B -type f | wc -l)"
    $PY /home/linmengfdm/exp20/scripts/mkvenv.py $B/requirements.lock $B/.venv | tail -n 1
    for f in agent altruagent data deploy scripts/run_agent.sh scripts/selfcheck.py; do
      diff -rq --exclude=node_modules --exclude=__pycache__ $WT/$f $B/$f > /dev/null && echo "same as tree: $f" || echo "DIFFERS: $f"
    done
    stat -c '%a %n' $B/scripts/run_agent.sh $B/scripts/bundle.sh
    echo "redalert modules: $(ls $B/agent/redalert/*.py | wc -l), counters: $(ls $B/agent/redalert/counters.py 2>/dev/null | wc -l), harv_first: $(grep -c harv_first $B/agent/redalert/params.py), harvest.py: $(ls $B/agent/redalert/harvest.py 2>/dev/null | wc -l), RA config: $(ls $B/data/red_alert_config.json 2>/dev/null | wc -l)"
    done_ bundle
  fi
fi
B=$(ls -d $O/x/arena-agent-* 2>/dev/null | head -1)
if ! skip bundle_check; then
  step "bundle selfcheck + replay + help + malformed key (clean env, PATH=/usr/bin:/bin)"
  mkdir -p $O/home; cd $B
  env -i PATH=/usr/bin:/bin HOME=$O/home LANG=C.UTF-8 $B/.venv/bin/python scripts/selfcheck.py > $O/bundle_selfcheck.log 2>&1; rc1=$?
  echo "bundle selfcheck rc=$rc1"; tail -n 5 $O/bundle_selfcheck.log
  env -i PATH=/usr/bin:/bin HOME=$O/home LANG=C.UTF-8 $B/.venv/bin/python scripts/replay_real.py > $O/bundle_replay.log 2>&1; rc2=$?
  echo "bundle replay rc=$rc2"; tail -n 3 $O/bundle_replay.log
  env -i PATH=/usr/bin:/bin HOME=$O/home LANG=C.UTF-8 ALTRUAGENT_OFFICIAL_AGENT_KEY=eak_live_FAKE_not_hex \
    ALTRUAGENT_CONTROL_URL=http://127.0.0.1:9 $B/.venv/bin/python -m agent --check-tournament > $O/bundle_check_bad.txt 2>&1
  echo "bundle check-tournament with a malformed key: exit $? (expect 1)"; tail -n 2 $O/bundle_check_bad.txt
  cd $WT
  [ $rc1 = 0 ] && [ $rc2 = 0 ] && grep -q 'selfcheck: ready' $O/bundle_selfcheck.log && done_ bundle_check
fi
if ! skip ra; then
  step "Red Alert assignments through the bundle's process tree (fake official platform, simulator, bot PLAYING)"
  flock /tmp/arena-heavy.lock bash $S/ra_e2e6.sh $B $O/ra_e2e > $O/ra_e2e.log 2>&1; rc=$?
  echo "ra e2e rc=$rc"; cut -c1-420 $O/ra_e2e.log
  [ $rc = 0 ] && done_ ra
fi
step "GATE DONE"
ls $O/*.ok
