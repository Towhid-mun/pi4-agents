#!/bin/sh
# Spawns a background child (itself with its own grandchild `sleep`), then
# idles - checks that killing the GROUP takes the whole tree, not just the
# top process (P2-3 gate item 3, and the process-spawns-children fixture
# DEVELOPMENT-PLAN.md asks for).
(
  while true; do
    sleep 5
  done
) &
echo "spawned child $!"
while true; do
  sleep 1
done
