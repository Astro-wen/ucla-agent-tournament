#!/bin/bash
# ra_e2e.sh <bundle dir> <out dir> (from ra-finish): two Red Alert assignments (next to one VGC match and one Werewolf
# table) through the bundle's whole process tree: run_agent.sh -> python -m agent --tournament --match -> one worker
# per seat, against the LOCAL fake platform's official control plane (FAKE key, own LOG_DIR / lock dir, run_agent name
# "ra-e2e", never "main"). The Red Alert seats are played on the local simulator (realtime clock, --ra-speed) against
# a scripted rusher: our bot must PLAY (order batches), not WAIT. Exit 0 only when every check holds.
set -u
B=$1; E=$2
WT=/home/linmengfdm/wt/ra6
PY=/home/linmengfdm/ucla-agent-tournament/.venv/bin/python
rm -rf "$E"; mkdir -p "$E/home"
PORT=$($PY -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
KEY=$(cd $WT && $PY -c 'from local.fake_platform import DEFAULT_OFFICIAL_KEY as k; print(k)')
case "$KEY" in *fa4efa4efa4e*) ;; *) echo "refusing: not the fake platform's well-known key"; exit 2;; esac
cd $WT
env PYTHONPATH=$WT ARENA_RECORD=0 setsid $PY -m local.fake_platform --port $PORT --official --concurrent 1 \
  --max-matches 1 --opponent baseline --werewolf 1 --max-werewolf 1 --red-alert 2 --ra-seconds 300 --ra-speed 6 \
  --ra-opponent rusher --stats $E/platform.json --stats-every 2 > $E/platform.out 2>&1 < /dev/null &
PLAT=$!
sleep 3
printf 'ALTRUAGENT_OFFICIAL_AGENT_KEY=%s\nALTRUAGENT_CONTROL_URL=http://127.0.0.1:%s\n' "$KEY" "$PORT" > $E/ra-e2e.env
chmod 600 $E/ra-e2e.env
ra() { (cd / && env -i PATH=/usr/bin:/bin LANG=C.UTF-8 HOME=$E/home LOG_DIR=$E/logs PYTHON=$B/.venv/bin/python \
        ARENA_LOCK_DIR=$E HEALTH_EVERY=15 bash $B/scripts/run_agent.sh "$1" --env $E/ra-e2e.env); }
done_yet() {
  $PY - "$E/platform.json" <<'EOF'
import json, sys
try:
    t = json.load(open(sys.argv[1]))["matches"]
except Exception:
    sys.exit(1)
ra = [m for m in t if m["game_type"] == "red_alert"]
sys.exit(0 if len(ra) == 2 and len(t) == 4 and all(m["terminal"] for m in t) else 1)
EOF
}
echo "start $(date +%T)"; ra start | tail -n 3
for i in $(seq 1 180); do sleep 5; done_yet && break; done
echo "all matches over (or timeout) $(date +%T) after ${i} polls"
ra status | tail -n 3
sleep 1
ra stop | tail -n 2
kill -TERM $PLAT 2>/dev/null; sleep 3
L=$E/logs/agent-ra-e2e.out
echo "log lines: $(wc -l < $L)"
for pat in "mode tournament (python -m agent --tournament --match" "red_alert: the rule-based bot plays this match" \
           "real-time game 'red_alert'" "\[match [^]]* seat=[^]]*\] starting" "Match assigned: red_alert" \
           "red_alert decision failed" "match failed" "unexpected error" "Traceback" "\[run_agent\] agent exited" \
           "\[match-summary\]" "game=red_alert" "\[ra\] slow frame" "\[ra\] first refusal" "eak_live_"; do
  echo "count [$pat]: $(grep -c "$pat" $L)"
done
echo "--- Red Alert lines"
grep -E "Match assigned: red_alert|rule-based bot|game=red_alert|\[ra\]" $L | tail -n 16 | cut -c1-420
$PY - $E/platform.json $L <<'EOF'
import json, re, sys
st = json.load(open(sys.argv[1]))
log = open(sys.argv[2], errors="replace").read()
ok = True
ra = [m for m in st["matches"] if m["game_type"] == "red_alert"]
for m in st["matches"]:
    print({k: m.get(k) for k in ("game_type", "terminal", "reason", "decisions", "batches", "views", "invalid_actions",
                                 "race_errors", "rejected_orders", "timer", "error", "opponent_error", "win",
                                 "game_seconds", "military")})
for m in ra:
    good = m["terminal"] and m.get("batches", 0) >= 20 and m.get("invalid_actions") == 0 and not m.get("error")
    print(f"red_alert match {m.get('n')}: {'PLAYED' if good else 'PROBLEM'} batches={m.get('batches')} "
          f"invalid={m.get('invalid_actions')} rejected={m.get('rejected_orders')} reason={m.get('reason')} "
          f"win={m.get('win')} game_s={m.get('game_seconds')}")
    ok &= bool(good)
t = st["totals"]
print({k: t.get(k) for k in ("matches_done", "invalid_actions", "timer_decisions", "game_config_calls", "red_alert_matches")})
off = st["official"]
print("official", {k: off.get(k) for k in ("grants", "seat_busy", "moves_by_holder", "moves_by_non_holder", "connected",
                                           "no_shows")})
seats = [(s["game_type"], s["context"], s["connected_s"], s["no_show"]) for s in off["seats"].values()]
print("seats", seats)
starts = re.findall(r"\[match [^\]]* seat=[^\]]*\] starting", log)
print(f"worker starts: {len(starts)} for {len(seats)} seats (more = a restarted worker)")
ok &= len(starts) == len(seats)
summ = [ln for ln in log.splitlines() if "[match-summary]" in ln and "game=red_alert" in ln]
print(f"red_alert match-summary lines: {len(summ)}")
for ln in summ:
    b = re.search(r"batches=(\d+)", ln)
    inv = re.search(r"invalid_action=(\d+)", ln)
    print("  ", ln[ln.find("[match-summary]"):][:420])
    ok &= bool(b and int(b.group(1)) > 0 and inv and inv.group(1) == "0")
ok &= len(summ) == 2 and off.get("no_shows", 0) == 0 and t.get("invalid_actions", 0) == 0
ok &= all(re.search(r"exceptions=0 ", ln) for ln in summ)
# Tracebacks: the only known one is the v4.4-draft Pokemon drafter's logged fallback ("drafter failed; keeping the
# card_score pick", agent/pokemon/drafter.py line 761/765 StopIteration on the fake platform's draft order; agent/pokemon is
# byte-identical to main b95a20f). Any other Traceback fails the check.
blocks = log.split("Traceback (most recent call last):")[1:]
known = sum(1 for b in blocks if 'drafter.py", line 76' in "\n".join(b.splitlines()[:14]))
print(f"Tracebacks: {len(blocks)} (known Pokemon drafter fallback: {known}, other: {len(blocks) - known})")
ok &= len(blocks) == known
ok &= "match failed" not in log and "red_alert decision failed" not in log
ok &= "eak_live_" not in log
print("RA E2E", "OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
EOF
rc=$?
echo "leftover processes with LOG_DIR=$E: $(grep -l "LOG_DIR=$E" /proc/[0-9]*/environ 2>/dev/null | wc -l)"
exit $rc
