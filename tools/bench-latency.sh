#!/usr/bin/env bash
#
# P1-4 - latency harness. This script IS the Phase 1 gate for "feels fast":
# without a number, "feels fast" is not a result.
#
#   tools/bench-latency.sh [ssh-alias] [warm-runs]
#     default alias:     pi
#     default warm runs: 7
#
# Kills any existing perch control master for the alias first, so "cold" is
# honest - it must pay a real handshake, not reuse a socket left over from an
# earlier run. Times one cold invocation, then N warm invocations back to
# back, and reports the median of the warm runs plus the warm/cold ratio.
#
# Uses the real `perch` CLI (sync, against a disposable scratch project),
# not raw ssh - the number this script reports is the number a user of the
# tool actually experiences.

set -uo pipefail   # deliberately not -e: report what we have even if one run misbehaves

ALIAS="${1:-pi}"
WARM_RUNS="${2:-7}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -x "$REPO_ROOT/.venv/bin/perch" ]; then
  PERCH="$REPO_ROOT/.venv/bin/perch"
elif command -v perch >/dev/null 2>&1; then
  PERCH="$(command -v perch)"
else
  echo "no perch found - looked for $REPO_ROOT/.venv/bin/perch and PATH" >&2
  exit 1
fi

# Same derivation as session.control_path_for(): sha256 of the alias, first
# 16 hex chars. Kept in sync with perch/session.py by the offline test suite
# (tests/test_session.py) - if that hash ever changes shape, this breaks
# loudly (no matching socket to kill) rather than silently.
HASH="$(printf '%s' "$ALIAS" | shasum -a 256 | cut -c1-16)"
CONTROL_PATH="$HOME/.ssh/perch-${HASH}.sock"

W="$(mktemp -d "${TMPDIR:-/tmp}/perch-bench.XXXXXX")"
REMOTE_ROOT="perch-bench-$$"

cleanup() {
  ssh -o ControlPath="$CONTROL_PATH" -O exit "$ALIAS" >/dev/null 2>&1
  ssh -o ConnectTimeout=5 "$ALIAS" "rm -rf ~/$REMOTE_ROOT" >/dev/null 2>&1
  rm -rf "$W"
}
trap cleanup EXIT

cat > "$W/.perch.toml" <<EOF
host = "$ALIAS"
remote_root = "$REMOTE_ROOT"

[commands]
build = "true"
EOF

cd "$W"

echo "target: $ALIAS   perch: $PERCH   warm runs: $WARM_RUNS"
echo

# --------------------------------------------------------------------------
echo "0 - ensure a cold start"
# Kill any master left over from earlier use of this alias, so the first
# timed invocation really does pay a fresh handshake.
if [ -S "$CONTROL_PATH" ]; then
  ssh -o ControlPath="$CONTROL_PATH" -O exit "$ALIAS" >/dev/null 2>&1
  echo "  killed a pre-existing control master"
else
  echo "  no pre-existing control master - already cold"
fi
rm -f "$CONTROL_PATH"

# --------------------------------------------------------------------------
echo
echo "1 - cold invocation (no control master yet)"
COLD_START=$(date +%s.%N)
if ! "$PERCH" sync >/dev/null 2>"$W/cold.err"; then
  echo "  cold invocation FAILED:"; sed 's/^/    /' "$W/cold.err"
  exit 1
fi
COLD_END=$(date +%s.%N)
COLD_MS=$(echo "($COLD_END - $COLD_START) * 1000" | bc)
printf "  %.0f ms\n" "$COLD_MS"

# --------------------------------------------------------------------------
echo
echo "2 - $WARM_RUNS warm invocations (control master reused)"
WARM_TIMES=()
for i in $(seq 1 "$WARM_RUNS"); do
  START=$(date +%s.%N)
  if ! "$PERCH" sync >/dev/null 2>"$W/warm.err"; then
    echo "  warm invocation $i FAILED:"; sed 's/^/    /' "$W/warm.err"
    exit 1
  fi
  END=$(date +%s.%N)
  MS=$(echo "($END - $START) * 1000" | bc)
  WARM_TIMES+=("$MS")
  printf "  run %d: %.0f ms\n" "$i" "$MS"
done

# Median of the warm runs (odd count by default - avoids an averaging tie).
SORTED=($(printf '%s\n' "${WARM_TIMES[@]}" | sort -n))
COUNT=${#SORTED[@]}
MID=$((COUNT / 2))
if [ $((COUNT % 2)) -eq 1 ]; then
  MEDIAN="${SORTED[$MID]}"
else
  MEDIAN=$(echo "(${SORTED[$((MID-1))]} + ${SORTED[$MID]}) / 2" | bc)
fi

RATIO=$(echo "scale=1; $MEDIAN * 100 / $COLD_MS" | bc)

echo
echo "verdict"
printf "  cold:          %.0f ms\n" "$COLD_MS"
printf "  warm (median): %.0f ms\n" "$MEDIAN"
printf "  warm/cold:     %s%%\n" "$RATIO"
