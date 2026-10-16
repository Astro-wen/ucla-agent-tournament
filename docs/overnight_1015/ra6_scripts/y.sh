cd ~/wt/_ra6_out
ls -la fb_* fin_* m_* 2>/dev/null | awk '{print $5, $8, $9}'
for f in fb_*.log fin_*.log m_*.log; do echo "$f games started: $(grep -c 'rule-based' $f)"; done
cat finA.out finB.out ident.out | cut -c1-200
tail -2 gate.out
uptime
