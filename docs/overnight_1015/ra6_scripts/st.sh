#!/usr/bin/env bash
# st.sh: status of the dev queues
cd ~/wt/_ra6_out
for q in q_*.out; do echo "$q: $(grep '^#####' $q | tail -1 | cut -c1-90) $(tail -1 $q | grep -o 'ALLDONE.*')"; done
uptime; free -m | sed -n 2p
