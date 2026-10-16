cd ~/wt/_ra6_out
for a in fin fb; do
  echo "== $a"; tail -2 ${a}_rt1566.log
  python3 ~/r6/rows.py ${a}_rt1566.json | grep -v keys | cut -c1-130
done
