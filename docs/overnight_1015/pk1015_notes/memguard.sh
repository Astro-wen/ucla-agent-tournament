# Kill my Pokemon panels (pattern _notes/runs) if MemAvailable drops below 1300 MB; log every 5 min.
i=0
while true; do
  m=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
  if [ "$m" -lt 1300 ]; then
    echo "$(date +%T) MemAvailable $m MB: killing panels"
    pkill -f "local.panel.*_notes/runs" ; sleep 30
  fi
  i=$((i+1)); [ $((i % 15)) -eq 0 ] && echo "$(date +%T) mem $m MB load $(cut -d' ' -f1 /proc/loadavg)"
  [ -f ~/wt/pk1015/_notes/STOP_GUARD ] && exit 0
  sleep 20
done
