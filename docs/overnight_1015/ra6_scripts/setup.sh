#!/bin/bash
# setup.sh: ra6 scratch tooling (copied from ra5, paths rewritten), ~/r6 symlink, out/snap dirs
set -e
S=/mnt/c/Users/LinmengFDM/AppData/Local/Temp/claude/--wsl-localhost-ubuntu-24-04-home-linmengfdm-ucla-agent-tournament/0e5ee86a-2600-44fc-aaad-0c437e4b65cb/scratchpad
R5=$S/ra5
R6=$S/ra6
rm -f /home/linmengfdm/r6
ln -sfn $R6 /home/linmengfdm/r6
for f in $R5/*.py $R5/*.sh; do
  n=$(basename $f)
  n=${n/5/6}
  [ "$n" = setup.sh ] && continue
  [ -f $R6/$n ] && continue
  sed -e 's#_ra5_#_ra6_#g' -e 's#/r5#/r6#g' -e 's#wt/ra5#wt/ra6#g' -e 's#\$R5#$R6#g' -e 's#R5=#R6=#g' $f > $R6/$n
done
mkdir -p ~/wt/_ra6_out ~/wt/_ra6_snap
ls $R6 | tr '\n' ' '
