#!/usr/bin/env bash
# Keep `python -m agent` running for ONE agent key: auto-restart with backoff, log rotation under logs/, a health
# line every HEALTH_EVERY seconds, and a clean stop that leaves no worker or node process behind.
#
#   bash scripts/run_agent.sh start   [--name NAME | --env FILE] [--tournament] [-- AGENT_ARGS]
#                                                                  # background (setsid); returns once the agent is up
#   bash scripts/run_agent.sh stop    [--name NAME]                # SIGTERM to the agent tree, SIGKILL after STOP_TIMEOUT
#   bash scripts/run_agent.sh restart [--name NAME]
#   bash scripts/run_agent.sh status  [--name NAME]                # run loop pid + a health line now + the last logged one
#   bash scripts/run_agent.sh health  [--name NAME]                # one health line now
#   bash scripts/run_agent.sh logs    [--name NAME]                # tail -F the log
#   bash scripts/run_agent.sh run     [--name NAME | --env FILE]   # foreground loop: what systemd and tmux run
#   bash scripts/run_agent.sh tmux    [--name NAME]                # `run` inside tmux session arena-NAME
#   bash scripts/run_agent.sh install-systemd                      # ~/.config/systemd/user/arena-agent@.service
#   bash scripts/run_agent.sh check-tournament [--name NAME]       # python -m agent --check-tournament (CONNECTS to
#                                                                  # the platform: the owner runs it, once the key exists)
#
# Two modes. TOURNAMENT (the official competition): `python -m agent --tournament` with the Official Agent Key
# ALTRUAGENT_OFFICIAL_AGENT_KEY (eak_live_ + 64 hex). DISCOVERY: plain `python -m agent` with an sk_agent_ key
# (ALTRUAGENT_API_KEY; sparring, self-hosted test matches). The mode is --tournament (or `-- --tournament`), else
# ARENA_MODE=tournament|discovery, else tournament whenever an Official Agent Key is set (key file or environment),
# so a .env that holds both keys never quietly runs discovery on competition day. Every start logs the mode and
# every health line shows it (mode=...).
# NAME picks the key file: main -> .env, anything else -> .env.NAME (--name apocalypse -> .env.apocalypse).
# Without a key file the key must already be in the environment (Docker, systemd Environment=). Keys are never
# printed; health lines show a 10-hex-digit hash so two instances with the same key are recognisable.
# One key = one running instance per machine: a lock file per key hash under ARENA_LOCK_DIR (default /tmp), held by
# the run loop AND the running agent, so a second checkout, bundle or LOG_DIR cannot start the same sk_agent_ key;
# never run one sk_agent_ key on two machines. Tournament mode differs: the platform gives each seat to ONE runtime
# (a 30 s execution lease), so a second runtime with the same Official Agent Key is a hot standby, not a clash; it
# starts with a note (role=standby) instead of being refused; on another machine ARENA_ROLE=standby in its key file
# says so. The platform gives a NEW seat to whichever runtime claims it first, so a standby leaves new seats to the
# primary (ARENA_NEW_SEAT_DELAY_S, default 20 s for role=standby), then plays a seat only once its grant succeeds (no
# runtime holds its lease; checked at every poll, with no worker or agent session while the primary plays it), so it
# takes a seat over within ~10 s of the primary's lease lapsing. Each run loop hands its agent one
# random execution id (ARENA_EXECUTION_ID, never logged, fresh per loop), kept across its restarts of the agent: a
# restarted runtime resumes its own seats at once instead of waiting out the lease (30-45 s).
# The damage calculator is required: the start refuses (exit 2, loudly) when it cannot answer a real calc, unless
# ARENA_ALLOW_NO_CALC=1 (emergency only: without it the agent wins ~12% against itself with the calculator).
# So is the engine search's simulator while ARENA_BATTLE=search (the default battle policy): without it every battle
# decision would quietly play the classic policy, so the start refuses (exit 2, loudly) unless ARENA_BATTLE=classic.
#
# Files under LOG_DIR (default logs/, ignored by git): agent-NAME.out (+ .1 ... .LOG_KEEP, rotated at LOG_MAX_MB),
# agent-NAME.pid (the run loop), agent-NAME.state (the agent process and the loop's helpers), agent-NAME.lock.
# ARENA_ALLOW_NO_CALC=1 alone is enough when node cannot run: the engine search needs the same node, so the start
# then goes on with the classic battle policy (ARENA_BATTLE=classic for the agent), loudly. A simulator that is
# missing while node works still refuses (ARENA_BATTLE=classic is the switch for that).
# A LOG_DIR that cannot be written (read-only mount, another user's directory) refuses with exit 2 and says so.
# Agent recordings go to LOG_DIR unless ARENA_RECORD_DIR is set.
# Knobs (environment): PYTHON, LOG_DIR, LOG_MAX_MB=20, LOG_KEEP=10, HEALTH_EVERY=300, RESTART_DELAY=3,
# RESTART_MAX_DELAY=60, STOP_TIMEOUT=20, ARENA_LOCK_DIR=/tmp, ARENA_ALLOW_NO_CALC, ARENA_MODE, ARENA_ROLE,
# ARENA_DISCOVERY_S=10, ARENA_WORKER_COOLDOWN_S=10, ARENA_SEAT_BUSY_RETRY_S, ARENA_NEW_SEAT_DELAY_S
# (agent/__main__.py), ARENA_NODE, ARENA_CALC (agent/pokemon/calc.py), ARENA_BATTLE, ARENA_SHOWDOWN
# (agent/pokemon/policy.py, engine.py), ARENA_STATE, ARENA_STATE_DIR (a restarted worker's memory, under
# LOG_DIR/state by default), ARENA_SEARCH_MAX_PROCS, ARENA_SEARCH_MIN_FREE_MB (the machine's search processes; all in
# docs/restart_load.md), ARENA_HEALTH_PROBE=0 (a health line never starts a probe search: Docker's HEALTHCHECK).
# Needs bash >= 4.2 and ps; setsid and flock are used when present.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SELF="$ROOT/scripts/run_agent.sh"
cd "$ROOT" || exit 2
if [ "${BASH_VERSINFO[0]}" -lt 4 ] || { [ "${BASH_VERSINFO[0]}" -eq 4 ] && [ "${BASH_VERSINFO[1]}" -lt 2 ]; }; then
  echo "run_agent.sh needs bash >= 4.2 (found $BASH_VERSION)" >&2; exit 2
fi

LOG_DIR="${LOG_DIR:-logs}"
LOG_MAX_MB="${LOG_MAX_MB:-20}"
LOG_KEEP="${LOG_KEEP:-10}"
HEALTH_EVERY="${HEALTH_EVERY:-300}"
[[ "$HEALTH_EVERY" =~ ^[1-9][0-9]*$ ]] || HEALTH_EVERY=300
RESTART_DELAY="${RESTART_DELAY:-3}"
RESTART_MAX_DELAY="${RESTART_MAX_DELAY:-60}"
STOP_TIMEOUT="${STOP_TIMEOUT:-20}"
export ARENA_DISCOVERY_S="${ARENA_DISCOVERY_S:-10}"
export ARENA_WORKER_COOLDOWN_S="${ARENA_WORKER_COOLDOWN_S:-10}"
export PYTHONUNBUFFERED=1
if [ -z "${PYTHON:-}" ]; then
  if [ -x "$ROOT/.venv/bin/python" ]; then PYTHON="$ROOT/.venv/bin/python"; else PYTHON="$(command -v python3 || echo python3)"; fi
fi

ORIG_ARGS=("$@")
CMD="${1:-}"; [ $# -gt 0 ] && shift
NAME=""; ENV_FILE=""; AGENT_ARGS=(); MODE_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="${2:?--name needs a value}"; shift 2 ;;
    --env) ENV_FILE="${2:?--env needs a file}"; shift 2 ;;
    --tournament) MODE_ARG=tournament; shift ;;
    --) shift; AGENT_ARGS=("$@"); break ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [ -z "$NAME" ]; then
  if [ -n "$ENV_FILE" ]; then
    b="$(basename "$ENV_FILE")"
    case "$b" in .env) NAME=main ;; .env.*) NAME="${b#.env.}" ;; *) NAME="${b%.*}" ;; esac
  else
    NAME=main
  fi
fi
[[ "$NAME" =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "bad --name '$NAME'" >&2; exit 2; }
if [ -z "$ENV_FILE" ]; then
  if [ "$NAME" = main ]; then ENV_FILE=.env; else ENV_FILE=".env.$NAME"; fi
fi
for a in ${AGENT_ARGS[@]+"${AGENT_ARGS[@]}"}; do
  case "$a" in
    --claim*) echo "claim mode is one single-use seat: run it in a terminal, not under run_agent.sh" >&2; exit 2 ;;
    --tournament) MODE_ARG=tournament ;;
    --check-tournament) [ "$CMD" = check-tournament ] || {
      echo "--check-tournament is a one-shot check: bash scripts/run_agent.sh check-tournament [--name NAME]" >&2; exit 2; } ;;
  esac
done

mkdir -p "$LOG_DIR" 2>/dev/null
LOG="$LOG_DIR/agent-$NAME.out"
PIDFILE="$LOG_DIR/agent-$NAME.pid"
STATE="$LOG_DIR/agent-$NAME.state"
LOCK="$LOG_DIR/agent-$NAME.lock"
LOG_ABS="$(cd "$LOG_DIR" 2>/dev/null && pwd)" || LOG_ABS="$LOG_DIR"
RUN_ID="$NAME@$LOG_ABS"                              # ARENA_RUN_ID of the loop, its helpers and the agent
START_KNOBS='^(ARENA_CALC|ARENA_NODE|ARENA_ALLOW_NO_CALC|ARENA_BATTLE|ARENA_SHOWDOWN)$'   # what the start checks read
MODE=""; MODE_WHY=""; ROLE=""; CALC_GOOD=1
OFFICIAL_KEY_RE='^eak_live_[0-9a-f]{64}$'            # altruagent/official.py _KEY_PATTERN

now() { printf '%(%s)T' -1; }
say() { printf '[run_agent] %s\n' "$*"; }
fmt_dur() { local s=$1; printf '%dh%02dm' $((s / 3600)) $((s % 3600 / 60)); }
state_get() { [ -f "$STATE" ] && sed -n "s/^$1=//p" "$STATE" | tail -n 1; }
is_yes() { case "$(printf '%s' "${1:-}" | tr 'A-Z' 'a-z')" in 1|yes|true|on) return 0 ;; esac; return 1; }

load_env_file() {   # $1 file of KEY=VALUE lines (the dotenv subset .env.example uses), $2 optional key regex;
  local f="$1" only="${2:-}" line key val   # exported, never echoed; the real environment wins (python-dotenv)
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    key="${BASH_REMATCH[2]}"; val="${BASH_REMATCH[3]}"
    [ -n "$only" ] && ! [[ "$key" =~ $only ]] && continue
    if [[ "$val" =~ ^\"(.*)\"[[:space:]]*$ ]] || [[ "$val" =~ ^\'(.*)\'[[:space:]]*$ ]]; then
      val="${BASH_REMATCH[1]}"
    else
      val="${val%%[[:space:]]#*}"; val="${val%"${val##*[![:space:]]}"}"
    fi
    [ -n "${!key+x}" ] && continue
    export "$key=$val"
  done < "$f"
}

load_envs() {     # what the agent will see: the key file, then the calculator / battle knobs of the checkout's .env,
  [ -f "$ENV_FILE" ] && load_env_file "$ENV_FILE"   # which the SDK's load_dotenv() reads for every NAME (never overriding)
  if [ -f "$ROOT/.env" ] && [ "$(cd "$(dirname "$ENV_FILE")" 2>/dev/null && pwd)/$(basename "$ENV_FILE")" != "$ROOT/.env" ]; then
    load_env_file "$ROOT/.env" "$START_KNOBS"
  fi
}

official_key() { local k="${ALTRUAGENT_OFFICIAL_AGENT_KEY:-}"; printf '%s' "${k//[[:space:]]/}"; }   # (the SDK strips it)

resolve_mode() {  # MODE = tournament | discovery (see the header), MODE_WHY = how it was chosen; after load_envs
  MODE="$MODE_ARG"; MODE_WHY="--tournament"
  if [ -z "$MODE" ]; then
    case "$(printf '%s' "${ARENA_MODE:-}" | tr -d '[:space:]' | tr 'A-Z' 'a-z')" in
      tournament|official) MODE=tournament; MODE_WHY="ARENA_MODE" ;;
      discovery) MODE=discovery; MODE_WHY="ARENA_MODE" ;;
      "") ;;
      *) say "WARNING: ARENA_MODE=${ARENA_MODE} is neither tournament nor discovery: ignored" ;;
    esac
  fi
  if [ -z "$MODE" ]; then
    if [ -n "$(official_key)" ]; then MODE=tournament; MODE_WHY="ALTRUAGENT_OFFICIAL_AGENT_KEY is set"
    else MODE=discovery; MODE_WHY="no ALTRUAGENT_OFFICIAL_AGENT_KEY"; fi
  fi
}

key_hash() {      # of the key this mode uses
  local k="${ALTRUAGENT_API_KEY:-}"
  [ "$MODE" = tournament ] && k="$(official_key)"
  [ -n "$k" ] || { echo none; return; }
  if command -v sha256sum >/dev/null; then printf '%s' "$k" | sha256sum | cut -c1-10
  else printf '%s' "$k" | shasum -a 256 | cut -c1-10; fi
}

node_status() {   # cheap mirror of agent/pokemon/calc.py _node_bin() for the health line (the start runs a real calc)
  case "$(printf '%s' "${ARENA_CALC:-on}" | tr -d '[:space:]' | tr 'A-Z' 'a-z')" in 0|off|false|no) echo "OFF(ARENA_CALC)"; return ;; esac
  local bin="" e="${ARENA_NODE:-}"
  if [ -n "$e" ] && { [ -x "$e" ] && [ -f "$e" ] || command -v "$e" >/dev/null 2>&1; }; then bin="$e"
  elif [ -x "$ROOT/node/bin/node" ] && "$ROOT/node/bin/node" --version >/dev/null 2>&1; then bin="$ROOT/node/bin/node"
  else bin="$(command -v node || true)"; fi
  [ -n "$bin" ] || { echo "MISSING"; return; }
  [ -d "$ROOT/agent/js/node_modules/@smogon/calc" ] || { echo "NO-NODE_MODULES"; return; }
  echo "ok($("$bin" --version 2>/dev/null || echo '?'))"
}

battle_mode() {   # agent/pokemon/policy.py battle_mode(): the engine search unless ARENA_BATTLE names another policy
  case "$(printf '%s' "${ARENA_BATTLE:-search}" | tr -d '[:space:]' | tr 'A-Z' 'a-z')" in search) echo search ;; *) echo classic ;; esac
}

engine_status() {  # cheap mirror of agent/pokemon/engine.py available() for the health line (the start loads it)
  local b; b="$(state_get battle)"                  # (what the running loop gave its agent: see check_engine)
  [ -n "$b" ] || b="$(battle_mode)"
  [ "$b" = search ] || { echo "classic(ARENA_BATTLE)"; return; }
  [ -f "$ROOT/agent/js/search.js" ] || { echo "MISSING"; return; }
  if [ -d "$ROOT/agent/js/node_modules/pokemon-showdown" ] || [ -d "$ROOT/local/js/node_modules/pokemon-showdown" ] \
     || [ -n "${ARENA_SHOWDOWN:-}" ]; then search_live; else echo "NO-SIMULATOR"; fi
}

search_live() {   # agent/health.py: did the matches' battle decisions get searched answers (last 30 min), else
  local out t=() np=()  # a real probe search (every 15 min at most): ok(used=38/40) / DEGRADED(...) / idle(probe=1.9s)
  command -v timeout >/dev/null 2>&1 && t=(timeout 90)
  case "$(printf '%s' "${ARENA_HEALTH_PROBE:-1}" | tr -d '[:space:]' | tr 'A-Z' 'a-z')" in
    0|off|no|false) np=(--no-probe) ;;               # (Docker's HEALTHCHECK has 20 s: counters only, never a probe)
  esac
  # The counters are where the loop's agent keeps its match state: under ARENA_RECORD_DIR, which preflight sets to
  # LOG_DIR (a `status` shell has not run preflight). engine_status calls this only when the loop (or a start from
  # here) plays the search, whatever ARENA_BATTLE the asking shell has.
  out="$(cd "$ROOT" && ARENA_RECORD_DIR="${ARENA_RECORD_DIR:-$LOG_ABS}" ARENA_BATTLE=search ${t[@]+"${t[@]}"} \
         "$PYTHON" -c 'import sys; from agent.health import main; sys.exit(main())' ${np[@]+"${np[@]}"} 2>/dev/null \
         | tail -n 1)"
  echo "${out:-files-ok}"
}

cmdline_of() {    # $1 pid: its arguments, one per line ('' when gone)
  if [ -r "/proc/$1/cmdline" ]; then tr '\0' '\n' 2>/dev/null < "/proc/$1/cmdline"
  else ps -o args= -p "$1" 2>/dev/null | tr ' ' '\n'; fi
}

run_id_of() { tr '\0' '\n' 2>/dev/null < "/proc/$1/environ" | sed -n 's/^ARENA_RUN_ID=//p' | tail -n 1; }

agent_alive() {   # $1 pid: alive, a `-m agent` process, and started by a loop of this NAME + LOG_DIR (ARENA_RUN_ID;
  local pid="${1:-}" rid                              # an agent without one comes from an older run_agent.sh)
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null || return 1
  cmdline_of "$pid" | tr '\n' ' ' | grep -q -- '-m agent' || return 1
  rid="$(run_id_of "$pid")"
  [ -z "$rid" ] || [ "$rid" = "$RUN_ID" ]
}

loop_alive() {    # $1 pid: alive and a `run_agent.sh run` process of this NAME + LOG_DIR (the loop, its health loop or
  local pid="${1:-}" args rid                        # log writer), never some other process that reuses the pid
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null || return 1
  args="$(cmdline_of "$pid")"
  printf '%s\n' "$args" | grep -q 'run_agent\.sh$' && printf '%s\n' "$args" | grep -qxF run || return 1
  rid="$(run_id_of "$pid")"
  [ "$rid" = "$RUN_ID" ] && return 0
  [ -z "$rid" ] && printf '%s\n' "$args" | grep -qxF -- "$NAME"   # no /proc, or a loop of an older run_agent.sh
}

group_pids() { ps -eo pgid=,pid= 2>/dev/null | awk -v g="$1" '$1 == g { print $2 }'; }
kill_group() { [ -n "${1:-}" ] && kill -s "$2" -- "-$1" 2>/dev/null; return 0; }

kill_helpers() {  # the health loop and log writer of a run loop that is gone (SIGKILL, OOM): never left behind
  local p left=()
  for p in "$(state_get health)" "$(state_get writer)"; do
    [ "$p" != "$$" ] && [ "$p" != "${WRITER:-}" ] && loop_alive "$p" && left+=("$p")
  done
  [ ${#left[@]} -gt 0 ] || return 0
  sleep 1                                             # (a writer still draining the last lines gets a moment)
  for p in "${left[@]}"; do loop_alive "$p" && kill -KILL "$p" 2>/dev/null; done
  return 0
}

systemd_unit_of() {   # $1 pid: "unit scope" when it is the MAIN process of a systemd service (which would restart it)
  local unit main scope
  unit="$(sed -n 's#^.*/\([^/]*\.service\)$#\1#p' "/proc/$1/cgroup" 2>/dev/null | tail -n 1)"
  [ -n "$unit" ] && command -v systemctl >/dev/null || return 0
  for scope in --user --system; do
    main="$(systemctl "$scope" show -p MainPID --value "$unit" 2>/dev/null)"
    [ "$main" = "$1" ] && { echo "$unit $scope"; return 0; }
  done
  return 0
}

health_line() {
  local child rss=0 npy=0 nnode=0 ncalc=0 nfd=0 pid pgid rss_k comm args up="-" aup="-" st=down s
  child="$(state_get child)"
  if agent_alive "$child"; then
    st=up
    while read -r pgid pid rss_k comm args; do
      [ "$pgid" = "$child" ] || continue
      rss=$((rss + rss_k))
      case "$comm" in
        python*) case "$args" in *resource_tracker*) ;; *spawn_main*) npy=$((npy + 1)) ;; esac ;;   # match workers
        node*) nnode=$((nnode + 1)); case "$args" in *calc.js*) ncalc=$((ncalc + 1)) ;; esac ;;
      esac
      [ -d "/proc/$pid/fd" ] && nfd=$((nfd + $(ls "/proc/$pid/fd" 2>/dev/null | wc -l)))
    done < <(ps -eo pgid=,pid=,rss=,comm=,args= 2>/dev/null)
    s="$(state_get started)"; aup="$(fmt_dur $(( $(now) - ${s:-$(now)} )))"
  fi
  s="$(state_get run_started)"; [ -n "$s" ] && up="$(fmt_dur $(( $(now) - s )))"
  local age="-" m rec last_rec="-" newest disk logs_mb restarts
  if [ -f "$LOG" ]; then m="$(stat -c %Y "$LOG" 2>/dev/null || stat -f %m "$LOG" 2>/dev/null || echo 0)"; age=$(( $(now) - m ))s; fi
  rec="$(find "$LOG_DIR" -maxdepth 1 -type f -name '*.jsonl' 2>/dev/null | wc -l | tr -d ' ')"
  newest="$(ls -t "$LOG_DIR"/*.jsonl 2>/dev/null | while read -r f; do [ -L "$f" ] || { echo "$f"; break; }; done)"
  if [ -n "$newest" ]; then m="$(stat -c %Y "$newest" 2>/dev/null || stat -f %m "$newest" 2>/dev/null || echo 0)"; last_rec=$(( $(now) - m ))s; fi
  disk="$(df -Pk "$LOG_DIR" 2>/dev/null | awk 'NR==2 { printf "%.1fG", $4 / 1048576 }')"
  logs_mb="$(du -sk "$LOG_DIR" 2>/dev/null | awk '{ printf "%dM", $1 / 1024 }')"
  restarts="$(state_get restarts)"
  local mode="${MODE:-discovery}" role kh=""
  if loop_alive "$(state_get loop)"; then       # a running loop: the mode and key IT runs, as its state file says; a
    s="$(state_get mode)"; [ -n "$s" ] && mode="$s"  # `status` / `health` shell may see another ARENA_MODE or key file
    kh="$(state_get keyhash)"
  fi
  role="$(state_get role)"; [ "$mode" = tournament ] && mode="tournament(${role:-primary})"
  printf '[health] name=%s mode=%s agent=%s pid=%s run_up=%s agent_up=%s restarts=%s workers=%d node=%d calc_procs=%d rss=%dMB fds=%d last_output=%s records=%s last_record=%s disk_free=%s logs=%s calc=%s search=%s key=#%s\n' \
    "$NAME" "$mode" "$st" "${child:--}" "$up" "$aup" "${restarts:-0}" "$npy" "$nnode" "$ncalc" \
    $((rss / 1024)) "$nfd" "$age" "$rec" "$last_rec" "${disk:-?}" "${logs_mb:-?}" "$(node_status)" "$(engine_status)" \
    "${kh:-$(key_hash)}"
}

log_writer() {    # stdin -> timestamped lines in $LOG, rotated at LOG_MAX_MB; echoed to fd 3 if a tty or LOG_ECHO=1
  trap '' INT TERM HUP PIPE                  # never dies with a closed terminal: the file keeps getting written
  exec 8>&- 9>&-                             # (it ends when the last writer - loop, health loop, agent - is gone)
  local LC_ALL=C line size=0 max="${LOG_MAX_BYTES:-$((LOG_MAX_MB * 1024 * 1024))}" echo=0 i
  { [ -t 3 ] || [ "${LOG_ECHO:-0}" = 1 ]; } && echo=1
  [ -f "$LOG" ] && size="$(wc -c < "$LOG" | tr -d ' ')"
  exec 4>>"$LOG"
  while IFS= read -r line || [ -n "$line" ]; do
    printf -v line '%(%Y-%m-%d %H:%M:%S)T %s\n' -1 "$line"
    printf '%s' "$line" >&4
    if [ "$echo" = 1 ]; then printf '%s' "$line" >&3 2>/dev/null || echo=0; fi
    size=$((size + ${#line}))
    if [ "$size" -ge "$max" ]; then
      exec 4>&-
      rm -f "$LOG.$LOG_KEEP"
      for ((i = LOG_KEEP - 1; i >= 1; i--)); do [ -f "$LOG.$i" ] && mv -f "$LOG.$i" "$LOG.$((i + 1))"; done
      mv -f "$LOG" "$LOG.1"
      exec 4>>"$LOG"
      size=0
    fi
  done
  exec 4>&-
}

loud() {          # a line for the log AND the loop's own stdout (the journal under systemd, the terminal of `run`)
  say "$@"
  { [ -t 3 ] || [ "${LOG_ECHO:-0}" = 1 ]; } || printf '[run_agent] %s\n' "$*" >&3 2>/dev/null
  return 0
}

check_calc() {    # 0 = the calculator answers a real calc (agent/pokemon/calc.py probe()), or ARENA_ALLOW_NO_CALC=1
  local out rc line
  out="$("$PYTHON" -c 'import sys
from agent.pokemon import calc
good, why = calc.probe()
print("PROBE " + why)
sys.exit(0 if good else 1)' 2>&1)"; rc=$?
  while IFS= read -r line; do case "$line" in "PROBE "*|"") ;; *) say "calc probe: $line" ;; esac; done <<< "$out"
  line="$(printf '%s\n' "$out" | sed -n 's/^PROBE //p' | tail -n 1)"
  if [ "$rc" = 0 ]; then say "damage calculator: ok, $line"; CALC_GOOD=1; return 0; fi
  CALC_GOOD=0
  [ -n "$line" ] || line="the probe failed (exit $rc)"
  loud "******************************************************************************************"
  if is_yes "${ARENA_ALLOW_NO_CALC:-}"; then
    loud "WARNING: THE DAMAGE CALCULATOR CANNOT RUN ($line)."
    loud "  Starting anyway because ARENA_ALLOW_NO_CALC=1: the agent plays its heuristic, which wins only"
    loud "  ~12% against itself with the calculator. Emergency only; fix node and restart."
  else
    loud "ERROR: THE DAMAGE CALCULATOR CANNOT RUN ($line): NOT STARTING."
    loud "  Without it the agent wins only ~12% against itself with the calculator."
    loud "  Fix: bundles ship node/bin/node; else install node >= 18 or set ARENA_NODE, and run"
    loud "  (cd agent/js && npm ci --omit=dev) if node_modules is missing. python scripts/selfcheck.py shows"
    loud "  the details. Emergency only (the heuristic still beats an agent that is down): ARENA_ALLOW_NO_CALC=1."
  fi
  loud "******************************************************************************************"
  is_yes "${ARENA_ALLOW_NO_CALC:-}"
}

check_engine() {  # 0 = ARENA_BATTLE=classic, or the engine search's simulator loads (agent/pokemon/engine.py probe())
  local out rc line
  if [ "$(battle_mode)" != search ]; then say "battle policy: classic (ARENA_BATTLE=${ARENA_BATTLE:-}), no engine search"; return 0; fi
  out="$("$PYTHON" -c 'import sys
from agent.pokemon import engine
good, why = engine.probe()
print("ENGINE " + why)
sys.exit(0 if good else 1)' 2>&1)"; rc=$?
  while IFS= read -r line; do case "$line" in "ENGINE "*|"") ;; *) say "engine probe: $line" ;; esac; done <<< "$out"
  line="$(printf '%s\n' "$out" | sed -n 's/^ENGINE //p' | tail -n 1)"
  if [ "$rc" = 0 ]; then say "battle policy: engine search (ARENA_BATTLE=search), ${line:-ok}"; return 0; fi
  [ -n "$line" ] || line="the probe failed (exit $rc)"
  if is_yes "${ARENA_ALLOW_NO_CALC:-}" && [ "$CALC_GOOD" = 0 ]; then
    # node itself cannot run (the calculator failed too): the documented emergency switch covers the search as well
    export ARENA_BATTLE=classic
    loud "******************************************************************************************"
    loud "WARNING: THE ENGINE SEARCH CANNOT RUN EITHER ($line)."
    loud "  Starting anyway because ARENA_ALLOW_NO_CALC=1 and the calculator cannot run: battles play the classic"
    loud "  policy (ARENA_BATTLE=classic is set for the agent). Emergency only; fix node and restart."
    loud "******************************************************************************************"
    return 0
  fi
  loud "******************************************************************************************"
  loud "ERROR: ARENA_BATTLE=search BUT THE ENGINE SEARCH CANNOT RUN ($line): NOT STARTING."
  loud "  Without its simulator every battle decision would quietly play the classic policy (champion v3.2)."
  loud "  Fix: deploy a bundle (scripts/bundle.sh vendors the simulator into agent/js/node_modules) or, in a"
  loud "  git checkout, run (cd local/js && npm ci). python scripts/selfcheck.py shows the details."
  loud "  To run the classic policy on purpose: ARENA_BATTLE=classic."
  loud "******************************************************************************************"
  return 1
}

preflight() {     # 0 = go; everything it prints lands in the log
  local ok=0 perm free_k k
  if [ -f "$ENV_FILE" ]; then
    perm="$(stat -c %a "$ENV_FILE" 2>/dev/null || echo 600)"
    case "$perm" in *00) ;; *) say "WARNING: $ENV_FILE is readable by other users (mode $perm): chmod 600 $ENV_FILE" ;; esac
  elif [ -z "${ALTRUAGENT_API_KEY:-}" ] && [ -z "$(official_key)" ]; then
    loud "ERROR: no key file $ENV_FILE and no ALTRUAGENT_OFFICIAL_AGENT_KEY / ALTRUAGENT_API_KEY in the environment"; ok=1
  fi
  load_envs
  resolve_mode
  if [ "$MODE" = tournament ]; then
    k="$(official_key)"
    if [ -z "$k" ]; then
      loud "ERROR: tournament mode ($MODE_WHY) but ALTRUAGENT_OFFICIAL_AGENT_KEY is not set: generate the Official Agent Key in the tournament dashboard and put it in $ENV_FILE"; ok=1
    elif ! [[ "$k" =~ $OFFICIAL_KEY_RE ]]; then
      loud "ERROR: ALTRUAGENT_OFFICIAL_AGENT_KEY doesn't look like an Official Agent Key (eak_live_ followed by 64 hex characters): copy it again from the tournament dashboard"; ok=1
    fi
  else
    case "${ALTRUAGENT_API_KEY:-}" in ""|sk_agent_your_key_here) loud "ERROR: ALTRUAGENT_API_KEY is not set"; ok=1 ;; esac
  fi
  [ -n "${ALTRUAGENT_CONTROL_URL:-}" ] || export ALTRUAGENT_CONTROL_URL=https://api.altruagent-game.com
  [ -n "${ARENA_RECORD_DIR:-}" ] || export ARENA_RECORD_DIR="$LOG_ABS"   # (recordings next to the log, which health counts)
  if ! "$PYTHON" -c 'import agent.agent, altruagent' >/dev/null 2>&1; then
    loud "ERROR: $PYTHON cannot import agent.agent / altruagent (venv? pip install -r requirements.lock)"; ok=1
  elif ! check_calc; then
    ok=1
  elif ! check_engine; then
    ok=1
  fi
  free_k="$(df -Pk "$LOG_DIR" 2>/dev/null | awk 'NR==2 { print $4 }')"
  [ -n "$free_k" ] && [ "$free_k" -lt 1048576 ] && say "WARNING: less than 1 GB free under $LOG_DIR"
  say "mode $MODE ($MODE_WHY), control plane $ALTRUAGENT_CONTROL_URL, key #$(key_hash), python $("$PYTHON" -c 'import sys; print(sys.version.split()[0])' 2>/dev/null), recording ${ARENA_RECORD:-1} (dir ${ARENA_RECORD_DIR:-logs}), reasoning ${ARENA_REASONING:-safe}, discovery ${ARENA_DISCOVERY_S}s, worker cooldown ${ARENA_WORKER_COOLDOWN_S}s"
  return $ok
}

log_dir_ok() {    # LOG_DIR exists and takes a file (a read-only mount or another user's directory must not look like a
  mkdir -p "$LOG_DIR" 2>/dev/null                   # held lock: that was exit 3, which systemd never retries)
  if [ -d "$LOG_DIR" ] && ( : >> "$LOG_DIR/.write-test" ) 2>/dev/null; then rm -f "$LOG_DIR/.write-test"; return 0; fi
  echo "ERROR: LOG_DIR $LOG_DIR is not writable (read-only file system or another user's directory): set LOG_DIR to a writable directory" >&2
  return 1
}

new_execution_id() {   # 64 random hex digits, or nothing (the runtime then makes its own)
  od -An -tx1 -N32 /dev/urandom 2>/dev/null | tr -d ' \n'
}

finish() {        # $1 exit code: close the log pipe, give the writer up to 5 s to drain, exit
  local i
  exec >&- 2>&-
  for i in $(seq 1 25); do kill -0 "$WRITER" 2>/dev/null || break; sleep 0.2; done
  exit "$1"
}

write_state() {   # [child started]: what status / health / stop read (keyhash: the 10-digit hash health lines show)
  { printf 'run_started=%s\nrestarts=%s\nloop=%s\nhealth=%s\nwriter=%s\nmode=%s\nrole=%s\nbattle=%s\nkeyhash=%s\n' \
      "$RUN_STARTED" "$RESTARTS" "$$" "${HEALTH:-}" "$WRITER" "$MODE" "$ROLE" "$(battle_mode)" "$(key_hash)"
    [ $# -eq 2 ] && printf 'child=%s\nstarted=%s\n' "$1" "$2"; } > "$STATE"
}

stop_child() {    # $1 signal: the agent's whole process group, and the pid itself (in case setsid has not run yet)
  [ -n "$CHILD" ] || return 0
  kill_group "$CHILD" "$1"; kill -s "$1" "$CHILD" 2>/dev/null
  return 0
}

cmd_run() {
  # Re-exec once with ARENA_RUN_ID in the process environment: /proc/<pid>/environ of this loop, its helpers and
  # the agent then prove whose they are (stop / start never act on a stale pid that now names another process).
  [ "${ARENA_RUN_ID:-}" = "$RUN_ID" ] || ARENA_RUN_ID="$RUN_ID" exec "$BASH" "$SELF" "${ORIG_ARGS[@]}"
  log_dir_ok || exit 2
  exec 3>&1
  if ! exec 8>"$LOCK"; then
    echo "ERROR: cannot open the lock file $LOCK: set LOG_DIR to a writable directory" >&2; exit 2
  fi
  if command -v flock >/dev/null; then
    flock -n 8 || { echo "agent '$NAME' is already running (lock $LOCK held)" >&2; exit 3; }
  else
    local oldrun; oldrun="$(cat "$PIDFILE" 2>/dev/null)"
    if loop_alive "$oldrun"; then echo "agent '$NAME' is already running (pid $oldrun)" >&2; exit 3; fi
  fi
  exec > >(log_writer) 2>&1
  WRITER=$!
  trap '' PIPE
  local old; old="$(state_get child)"                # (we hold the name lock: any loop of this NAME here is dead)
  if agent_alive "$old"; then say "killing agent group $old left over from a run loop that died"; kill_group "$old" KILL; fi
  kill_helpers
  RUN_STARTED="$(now)"; RESTARTS=0; STOPPING=0; CHILD=""; HEALTH=""
  preflight || { say "preflight failed; exiting (code 2)"; finish 2; }
  local kh lockdir keylock; kh="$(key_hash)"
  lockdir="${ARENA_LOCK_DIR:-/tmp}"; keylock="$lockdir/arena-agent-key-$kh.lock"
  ( umask 000; : >> "$keylock" ) 2>/dev/null         # (shared by every user: opened read-only, flock needs no more)
  if ! exec 9<"$keylock"; then
    say "WARNING: cannot open $keylock: the per-key lock falls back to $LOG_DIR (this checkout only)"
    keylock="$LOG_DIR/.key-$kh.lock"; exec 9>"$keylock"
  fi
  [ "$MODE" = tournament ] && ROLE=primary
  if command -v flock >/dev/null && ! flock -w 5 9; then  # (-w: an old agent group just killed may still hold it)
    if [ "$MODE" = tournament ]; then                 # the platform's seat lease arbitrates: a hot standby, not a clash
      ROLE=standby
      loud "NOTE: another runtime on this machine already runs Official Agent Key #$kh (lock $keylock held): starting as a"
      loud "  HOT STANDBY. The platform lets one runtime play each seat (a 30 s execution lease); this one takes a seat"
      loud "  over ~30-40 s after the other stops renewing it. A standby belongs on ANOTHER machine: here it only costs CPU."
    else
      loud "ERROR: another instance on this machine already runs key #$kh (lock $keylock held): not starting"; finish 3
    fi
  fi
  if [ "$ROLE" = primary ] && [ "$(printf '%s' "${ARENA_ROLE:-}" | tr -d '[:space:]' | tr 'A-Z' 'a-z')" = standby ]; then
    ROLE=standby                                      # (the standby machine of a two-machine setup: its key file says so)
    loud "NOTE: ARENA_ROLE=standby: a HOT STANDBY for Official Agent Key #$kh (the primary runs on another machine)."
  fi
  if [ "$ROLE" = standby ]; then                      # the platform gives a new seat to whichever runtime claims it first
    export ARENA_NEW_SEAT_DELAY_S="${ARENA_NEW_SEAT_DELAY_S:-20}"
    loud "  The standby leaves a NEW seat to the primary for ${ARENA_NEW_SEAT_DELAY_S}s (ARENA_NEW_SEAT_DELAY_S; the primary"
    loud "  claims it within one 10 s poll), then plays a seat only when no runtime holds its lease (checked every poll)."
  fi
  local exec_id="" launch
  unset ARENA_EXECUTION_ID ARENA_EXECUTION_ID_OWNER    # (never one from a key file or the caller's environment)
  [ "$MODE" = tournament ] && exec_id="$(new_execution_id)"   # one per run loop, kept across agent restarts; never logged
  echo $$ > "$PIDFILE"
  write_state
  # Stop = SIGTERM to the agent's whole process group (python -m agent, its workers, their node calculators): a
  # background job of a non-interactive shell ignores SIGINT, and nothing needs a graceful exit (no match is ever
  # resigned on shutdown). Background sleeps get /dev/null as stdout and none of the locks (fds 3, 8, 9 closed), so
  # they never hold the log pipe or a lock after the loop is gone.
  trap 'STOPPING=1; stop_child TERM' TERM INT HUP
  ( trap - INT HUP; exec 3>&- 8>&- 9>&-
    sl=""; trap '[ -n "$sl" ] && kill "$sl" 2>/dev/null; exit 0' TERM    # (its sleep never outlives it either)
    parent=$$; tick=5; [ "$HEALTH_EVERY" -lt "$tick" ] && tick=$HEALTH_EVERY; waited=0
    while :; do
      sleep "$tick" > /dev/null 2>&1 & sl=$!
      wait "$sl"; sl=""
      kill -0 "$parent" 2>/dev/null || exit 0        # the loop is gone (SIGKILL, OOM): never outlive it
      waited=$((waited + tick))
      [ "$waited" -ge "$HEALTH_EVERY" ] && { waited=0; health_line; }
    done ) &
  HEALTH=$!
  write_state
  say "start name=$NAME mode=$MODE${ROLE:+ role=$ROLE} root=$ROOT pid=$$, health every ${HEALTH_EVERY}s, log $LOG (rotated at ${LOG_MAX_MB} MB, $LOG_KEEP kept), key lock $keylock"
  launch=("$PYTHON" -u -m agent)
  if [ "$MODE" = tournament ] && ! printf '%s\n' ${AGENT_ARGS[@]+"${AGENT_ARGS[@]}"} | grep -qxF -- --tournament; then
    launch+=(--tournament)
  fi
  launch+=(${AGENT_ARGS[@]+"${AGENT_ARGS[@]}"})
  local fast=0 delay started code t sleeper
  while [ "$STOPPING" != 1 ]; do
    started="$(now)"
    # The execution id goes to the agent's environment only (never argv, the log or the state file), just for the
    # spawn: python drops it from its own environment and honours it only when its parent is this loop.
    [ -n "$exec_id" ] && export ARENA_EXECUTION_ID="$exec_id" ARENA_EXECUTION_ID_OWNER="$$"
    # The agent keeps fd 9 (the key lock) for its whole life: a loop killed with SIGKILL leaves no unlocked agent.
    if command -v setsid >/dev/null; then
      setsid "${launch[@]}" 3>&- 8>&- < /dev/null &
    else
      set -m; "${launch[@]}" 3>&- 8>&- < /dev/null & set +m
    fi
    unset ARENA_EXECUTION_ID ARENA_EXECUTION_ID_OWNER
    CHILD=$!
    [ "$STOPPING" = 1 ] && stop_child TERM             # a stop that arrived before CHILD was set
    write_state "$CHILD" "$started"
    say "agent started pid=$CHILD (restart #$RESTARTS)"
    wait "$CHILD"; code=$?
    t=0
    while kill -0 "$CHILD" 2>/dev/null; do          # woken by a signal: STOP_TIMEOUT s to finish, then SIGKILL
      if [ "$t" -eq $((STOP_TIMEOUT * 5)) ]; then say "agent did not stop within ${STOP_TIMEOUT}s: SIGKILL"; stop_child KILL; fi
      sleep 0.2; t=$((t + 1))
    done
    wait "$CHILD" 2>/dev/null; code=$?
    sleep 0.3
    if [ -n "$(group_pids "$CHILD")" ]; then         # orphaned workers / node calculators are never kept
      say "killing leftover processes of agent group $CHILD: $(group_pids "$CHILD" | tr '\n' ' ')"
      kill_group "$CHILD" KILL
    fi
    [ "$STOPPING" = 1 ] && { say "agent stopped (code $code)"; break; }
    if [ $(( $(now) - started )) -lt 60 ]; then fast=$((fast + 1)); else fast=0; fi
    delay=$RESTART_DELAY
    for ((t = 1; t < fast && delay < RESTART_MAX_DELAY; t++)); do delay=$((delay * 2)); done
    [ "$delay" -gt "$RESTART_MAX_DELAY" ] && delay=$RESTART_MAX_DELAY
    RESTARTS=$((RESTARTS + 1))
    CHILD=""
    write_state
    say "agent exited with code $code after $(( $(now) - started ))s; restart #$RESTARTS in ${delay}s"
    sleep "$delay" > /dev/null 2>&1 3>&- 8>&- 9>&- &
    sleeper=$!
    wait "$sleeper" 2>/dev/null
    kill "$sleeper" 2>/dev/null
    [ "$STOPPING" = 1 ] && { say "stopped during the restart delay"; break; }
  done
  kill "$HEALTH" 2>/dev/null; wait "$HEALTH" 2>/dev/null
  health_line
  say "exit"
  rm -f "$PIDFILE"
  HEALTH=""; write_state
  finish 0
}

cmd_start() {
  local pid i; pid="$(cat "$PIDFILE" 2>/dev/null)"
  if loop_alive "$pid"; then echo "agent '$NAME' already running (run loop pid $pid)"; return 0; fi
  log_dir_ok || return 2
  local args=(run --name "$NAME" --env "$ENV_FILE"); [ -n "$MODE_ARG" ] && args+=(--tournament)
  [ ${#AGENT_ARGS[@]} -gt 0 ] && args+=(-- "${AGENT_ARGS[@]}")
  if command -v setsid >/dev/null; then setsid nohup bash "$SELF" "${args[@]}" > /dev/null 2>&1 < /dev/null &
  else nohup bash "$SELF" "${args[@]}" > /dev/null 2>&1 < /dev/null & fi
  for i in $(seq 1 150); do
    sleep 0.2
    pid="$(cat "$PIDFILE" 2>/dev/null)"
    if loop_alive "$pid" && agent_alive "$(state_get child)"; then cmd_status; return 0; fi
    [ "$i" -gt 10 ] && ! kill -0 $! 2>/dev/null && break     # the loop exited (preflight, lock): no point waiting
  done
  echo "agent '$NAME' did not come up; last log lines:"; tail -n 15 "$LOG" 2>/dev/null; return 1
}

cmd_stop() {
  local pid child i unit; pid="$(cat "$PIDFILE" 2>/dev/null)"; child="$(state_get child)"
  loop_alive "$pid" || pid=""                                  # a stale pidfile names somebody else's process
  if [ -n "$pid" ]; then
    unit="$(systemd_unit_of "$pid")"
    if [ -n "$unit" ]; then
      echo "agent '$NAME' runs under systemd, which would just restart it: systemctl ${unit#* } $CMD ${unit%% *}" >&2
      return 1
    fi
    kill -TERM "$pid"
    for i in $(seq 1 $(( (STOP_TIMEOUT + 10) * 5 ))); do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
    if kill -0 "$pid" 2>/dev/null; then echo "run loop $pid did not exit: SIGKILL"; kill -KILL "$pid"; sleep 0.2; fi
  fi
  if agent_alive "$child"; then kill_group "$child" TERM; kill -TERM "$child" 2>/dev/null; sleep 2; fi
  [ -n "$child" ] && [ -n "$(group_pids "$child")" ] && kill_group "$child" KILL
  kill_helpers
  rm -f "$PIDFILE"
  echo "agent '$NAME' stopped"
}

cmd_status() {
  local pid; pid="$(cat "$PIDFILE" 2>/dev/null)"
  load_envs; resolve_mode
  if loop_alive "$pid"; then echo "run loop: pid $pid"; else echo "run loop: not running"; fi
  health_line
  grep '\[health\]' "$LOG" 2>/dev/null | tail -n 1 | sed 's/^/last logged: /'
}

cmd_install_systemd() {
  local dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user" unit nodedir
  nodedir="$(dirname "$(command -v node 2>/dev/null || echo /usr/bin/node)")"
  mkdir -p "$dir"
  unit="$dir/arena-agent@.service"
  sed -e "s#__ROOT__#$ROOT#g" -e "s#__PATH__#$nodedir:/usr/local/bin:/usr/bin:/bin#g" "$ROOT/deploy/arena-agent@.service" > "$unit"
  systemctl --user daemon-reload 2>/dev/null || true
  echo "wrote $unit"
  echo "next:  systemctl --user enable --now arena-agent@main    (main -> .env; arena-agent@apocalypse -> .env.apocalypse)"
  echo "       sudo loginctl enable-linger $USER                 (keeps it running without a login session and after a reboot)"
  echo "       bash scripts/run_agent.sh status; journalctl --user -u arena-agent@main"
  echo "Tournament mode: put ALTRUAGENT_OFFICIAL_AGENT_KEY in the key file (.env for main): the unit then runs"
  echo "       python -m agent --tournament (ARENA_MODE=tournament|discovery in the key file forces a mode)"
}

cmd_check_tournament() {   # the SDK's own check, with the key file loaded like `run` does; CONNECTS to the platform
  load_envs
  [ -n "${ALTRUAGENT_CONTROL_URL:-}" ] || export ALTRUAGENT_CONTROL_URL=https://api.altruagent-game.com
  local extra=()
  for a in ${AGENT_ARGS[@]+"${AGENT_ARGS[@]}"}; do [ "$a" = --check-tournament ] || [ "$a" = --tournament ] || extra+=("$a"); done
  "$PYTHON" -u -m agent --check-tournament ${extra[@]+"${extra[@]}"} < /dev/null
}

case "$CMD" in
  run) cmd_run ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_stop && cmd_start ;;
  status) cmd_status ;;
  health) load_envs; resolve_mode; health_line ;;
  logs) touch "$LOG"; exec tail -n 50 -F "$LOG" ;;
  check-tournament) cmd_check_tournament ;;
  tmux)
    command -v tmux >/dev/null || { echo "tmux is not installed" >&2; exit 2; }
    tmux has-session -t "arena-$NAME" 2>/dev/null && { echo "tmux session arena-$NAME exists: tmux attach -t arena-$NAME"; exit 0; }
    envs=()                   # an already running tmux server would not see this shell's knobs: pass them on (never keys)
    for v in PYTHON PYTHONPATH PATH LOG_DIR LOG_MAX_MB LOG_KEEP HEALTH_EVERY RESTART_DELAY RESTART_MAX_DELAY STOP_TIMEOUT \
             ARENA_NODE ARENA_CALC ARENA_ALLOW_NO_CALC ARENA_BATTLE ARENA_SHOWDOWN ARENA_LOCK_DIR ARENA_REASONING ARENA_RECORD ARENA_RECORD_DIR \
             ARENA_DISCOVERY_S ARENA_WORKER_COOLDOWN_S ARENA_DISCOVERY_MAX_FAILURES ARENA_MODE ARENA_SEAT_BUSY_RETRY_S \
             ARENA_ROLE ARENA_NEW_SEAT_DELAY_S ARENA_STATE ARENA_STATE_DIR ARENA_SEARCH_MAX_PROCS \
             ARENA_SEARCH_MIN_FREE_MB ARENA_SEARCH_GRACE_S ARENA_SUMMARY ARENA_HEALTH_PROBE; do
      [ -n "${!v+x}" ] && envs+=(-e "$v=${!v}")
    done
    targs="run --name $(printf '%q' "$NAME") --env $(printf '%q' "$ENV_FILE")"; [ -n "$MODE_ARG" ] && targs+=" --tournament"
    if [ ${#AGENT_ARGS[@]} -gt 0 ]; then targs+=" --"; for a in "${AGENT_ARGS[@]}"; do targs+=" $(printf '%q' "$a")"; done; fi
    tmux new-session -d "${envs[@]}" -s "arena-$NAME" -c "$ROOT" "bash $(printf '%q' "$SELF") $targs"
    echo "started in tmux: tmux attach -t arena-$NAME   (Ctrl-b d detaches; bash scripts/run_agent.sh stop --name $NAME stops)" ;;
  install-systemd) cmd_install_systemd ;;
  *) sed -n '2,/^set -uo pipefail/p' "$SELF" | sed '$d'; exit 2 ;;
esac
