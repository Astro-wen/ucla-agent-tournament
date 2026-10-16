#!/usr/bin/env bash
# bg.sh <script> [args]: run a scratchpad script detached, output to ~/wt/_ra6_out/${OUTNAME:-<script>}.out
s=$1; shift
mkdir -p /home/linmengfdm/wt/_ra6_out
o=${OUTNAME:-${s%.sh}}
nohup setsid bash /home/linmengfdm/r6/$s "$@" </dev/null > /home/linmengfdm/wt/_ra6_out/$o.out 2>&1 &
sleep 2
echo "started $s -> $o.out"
ps aux | grep -c "[r]4/$s"
