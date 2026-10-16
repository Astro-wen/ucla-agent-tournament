#!/usr/bin/env bash
# kq.sh <pattern>: kill a dev queue (its whole process group: bg.sh started it with setsid), by a pattern of the
# dev6.sh / dev7.sh command line
pat="$1"
for pg in $(for pid in $(pgrep -f "dev[67].sh $pat"); do ps -o pgid= -p $pid; done | sort -u); do
  echo "killing process group $pg"
  kill -TERM -- -$pg
done
sleep 2; pgrep -fa "dev[67].sh" | cut -c1-120
