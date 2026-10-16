# usage: bash bg.sh script.sh logfile   -- start a detached job
setsid nohup bash "$1" </dev/null >"$2" 2>&1 &
disown
sleep 1
echo started $!
