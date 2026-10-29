#!/bin/sh
# Background evidence collector for compat tests (a `setup` command):
#   sh compat/support/watch.sh <name> '<command>'
# Runs <command> every 5s for up to 40 minutes and keeps its latest non-empty output in
# $COMPAT_OUT/watch-<name>.log, so a `debug` command can show what a crash-looping pod
# said even after Flux has uninstalled the release.
name=$1 cmd=$2 log="$COMPAT_OUT/watch-$1.log"
nohup sh -c '
  i=0
  while [ $i -lt 480 ]; do
    out=$(sh -c "$1" 2>/dev/null)
    [ -n "$out" ] && printf "%s\n" "$out" > "$2"
    i=$((i+1)); sleep 5
  done' watch "$cmd" "$log" >/dev/null 2>&1 &
echo "watching $name -> $log"
