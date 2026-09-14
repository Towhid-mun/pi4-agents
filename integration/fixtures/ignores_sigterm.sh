#!/bin/sh
# Ignores SIGTERM outright - checks that P2-3/P2-4 actually escalate to
# SIGKILL rather than waiting forever for a TERM that will never work.
trap '' TERM
echo "ignoring TERM, pid $$"
while true; do
  sleep 1
done
