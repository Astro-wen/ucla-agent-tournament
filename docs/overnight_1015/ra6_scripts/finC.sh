#!/usr/bin/env bash
# finC.sh: a third runner for the final measurement, taking runner B's tail in reverse order (lg.sh skips a label that
# is done or running, so B skips these when it gets there). JOBS=4 to spare the machine.
export JOBS=4
R6=/home/linmengfdm/r6
for n in h2h1565 lat1567 lat1564 cash1563 sfmix1559 strong1556; do
  bash $R6/final.sh fin $n
done
echo "FINC DONE $(date +%T)"
