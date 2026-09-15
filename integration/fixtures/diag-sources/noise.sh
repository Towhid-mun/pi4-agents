#!/bin/sh
# Deliberately diagnostic-shaped noise: colons, numbers, absolute-looking
# paths - none of it is an actual compiler diagnostic. This is what proves
# I8 (pass-through fidelity).
echo "12:34:56 INFO starting up"
echo "/usr/bin/gcc: this looks like a compiler but is not one: 404"
echo "src/main.c: this has a colon but no line:col shape at all"
echo "error: standalone word 'error' with no file prefix"
echo "/home/towhid/perch-diag-capture/data.bin: 42:17 not a diagnostic, just numbers"
echo "warning level: 3, retry count: 12"
echo "config.yaml:not-a-number:also-not-a-number: weird but not gcc shaped"
printf 'no newline at all, just a bare colon: and done'
