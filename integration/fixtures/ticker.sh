#!/bin/sh
# Prints once per second, N times (default 10). Uses the shell's own `echo`,
# which performs a raw write() with no libc FILE* buffering to defeat - this
# fixture proves the HOST-side reader (P2-1) delivers output as it arrives,
# independent of the remote-buffering problem (trap 4) that stdbuf mitigates
# for compiled/interpreted programs. See slow_printer.c for that half.
n="${1:-10}"
i=1
while [ "$i" -le "$n" ]; do
  echo "tick $i"
  i=$((i + 1))
  sleep 1
done
