#!/bin/sh
# Reads one line and echoes it back - the minimal "interactive remote
# program that reads a line" P2-5's done-test asks for.
printf 'name? '
read -r name
printf 'hello, %s\n' "$name"
