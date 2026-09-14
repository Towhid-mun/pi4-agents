#!/bin/sh
# Alternates stdout/stderr, distinguishably, with a short sleep between each
# write - checks that both streams stay separated and each individually
# keeps its own order as it arrives (P2-1 gate item 2).
n="${1:-6}"
i=1
while [ "$i" -le "$n" ]; do
  if [ $((i % 2)) -eq 1 ]; then
    echo "OUT $i"
  else
    echo "ERR $i" >&2
  fi
  i=$((i + 1))
  sleep 0.3
done
