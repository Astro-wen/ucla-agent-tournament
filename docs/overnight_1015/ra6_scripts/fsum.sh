#!/usr/bin/env bash
# fsum.sh: final-measurement summary (fb_* base vs fin_* candidate): per set, pooled core by opponent and by map,
# stress sets, identity vs main, health
cd ~/wt/_ra6_out
CORE="t2_1651 tom_1653 nb_1654 nash_1655 pro_1656 strong_1657 main_1658"
STRESS="cash_1660 lat_1661 fault_1662 lost_1663 win_1664 h2h_1665 rt_1666"
echo "== per set"
for x in $CORE $STRESS; do
  if [ -s fb_$x.json ] && [ -s fin_$x.json ]; then
    python3 ~/r6/compare.py fb_$x.json fin_$x.json | grep -E '^  ALL' | sed -e "s/^  ALL /  $x /" | cut -c1-200
  else echo "  $x: missing"; fi
done
args=()
for x in $CORE; do [ -s fb_$x.json ] && [ -s fin_$x.json ] && args+=(fb_$x.json fin_$x.json); done
echo "== core pooled by opponent / map"
BYMAP=1 python3 ~/r6/compare.py "${args[@]}" | sed -n '/POOLED/,$p' | grep -E '^  [a-zA-Z]' | cut -c1-200
echo "== identity: main snapshot vs base arm"
for x in nash_1655 main_1658 strong_1657; do
  [ -s m_$x.json ] && [ -s fb_$x.json ] && python3 ~/r6/compare.py m_$x.json fb_$x.json | grep -E '^  ALL' | sed -e "s/^  ALL /  $x /" | cut -c1-200
done
echo "== health (all sets)"
all=()
for x in $CORE $STRESS; do [ -s fb_$x.json ] && [ -s fin_$x.json ] && all+=(fb_$x.json fin_$x.json); done
python3 ~/r6/compare.py "${all[@]}" | sed -n '/POOLED/,$p' | grep health
python3 - "${all[@]}" <<'EOF'
import json, sys
for p in sys.argv[1:]:
    d = json.load(open(p))
    rows = d["rows"]
    lp = max((r["a"]["p99_ms"] for r in rows), default=0)
    inv = sum(r["a"]["invalid_actions"] for r in rows)
    err = sum(1 for r in rows if r["a"]["error"] or r["a"]["hung"])
    if inv or err:
        print("  PROBLEM", p, "invalid", inv, "errors", err)
print("  checked", len(sys.argv) - 1, "files")
EOF
