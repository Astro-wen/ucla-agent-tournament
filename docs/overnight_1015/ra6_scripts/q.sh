cd ~/wt/ra6
for s in "$@"; do echo "######## $s"; python3 ~/r6/build.py $s 120 180 240 285 300 330 360 420 480 600 | cut -c1-420; done
