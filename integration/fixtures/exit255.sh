#!/bin/sh
# Exits 255 on purpose - the exact code ssh itself uses for its OWN
# connection-level failures. Checks that the exit sentinel (P2-2) correctly
# disambiguates a genuine remote 255 from a connection failure (P2-6).
echo "about to exit 255"
exit 255
