#!/usr/bin/env bash
#
# S0-1 — Does the host's rsync do what the workspace mirror (C3) needs?
#
#   ./spike-s0-1-rsync.sh [ssh-alias]      default alias: pi
#
# Answers, with assertions rather than eyeballing:
#   1. which rsync implementation is on this Mac
#   2. is --exclude-from honoured
#   3. do deletions propagate
#   4. does --checksum catch a change that mtime+size cannot see   <-- the one that matters
#   5. is an unchanged tree a genuine no-op
#
# Throwaway. Cleans up after itself on both machines.

set -uo pipefail        # deliberately not -e: run every check, report at the end

HOST="${1:-pi}"
W="$(mktemp -d "${TMPDIR:-/tmp}/s0-1.XXXXXX")"
REMOTE="s0-1-spike"
PASS=0
FAIL=0

ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL=$((FAIL+1)); }
info() { printf '  \033[2m····\033[0m  %s\n' "$*"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$*"; }

cleanup() {
  rm -rf "$W"
  ssh "$HOST" "rm -rf ~/$REMOTE" 2>/dev/null
}
trap cleanup EXIT

# --------------------------------------------------------------------------
head_ "0 · preflight"

if ! command -v rsync >/dev/null; then
  bad "no rsync on this machine at all"
  exit 1
fi
if ! ssh -o ConnectTimeout=10 "$HOST" true 2>/dev/null; then
  bad "cannot ssh to '$HOST' — fix that first"
  exit 1
fi
ok "ssh to '$HOST' works"

# --------------------------------------------------------------------------
head_ "1 · which rsync is this"

VER_RAW="$(rsync --version 2>&1 | head -3)"
printf '%s\n' "$VER_RAW" | sed 's/^/        /'

if printf '%s' "$VER_RAW" | grep -qi 'openrsync'; then
  FLAVOR="openrsync"
elif printf '%s' "$VER_RAW" | grep -qi 'Andrew Tridgell\|rsync  version'; then
  FLAVOR="GNU rsync"
else
  FLAVOR="unknown"
fi
info "host implementation : $FLAVOR  ($(command -v rsync))"
info "target implementation: $(ssh "$HOST" 'rsync --version 2>&1 | head -1' || echo MISSING)"

if [ "$FLAVOR" = "unknown" ]; then
  info "could not classify — read the version block above yourself"
fi

# --------------------------------------------------------------------------
head_ "2 · build a scratch tree"

mkdir -p "$W/src/a" "$W/src/b"
printf 'hello\n'  > "$W/src/a/one.txt"     # 6 bytes — matters for check 4
printf 'world\n'  > "$W/src/b/two.txt"
printf 'object\n' > "$W/src/b/skip.o"
printf '*.o\n'    > "$W/excludes"

SYNC=(rsync -a -z --delete --exclude-from="$W/excludes")
info "flags under test: ${SYNC[*]#rsync }"

"${SYNC[@]}" "$W/src/" "$HOST:$REMOTE/" 2>"$W/err1"
if [ $? -ne 0 ]; then
  bad "initial sync failed:"; sed 's/^/        /' "$W/err1"; exit 1
fi
ok "initial sync completed"

REMOTE_FILES="$(ssh "$HOST" "cd ~/$REMOTE && find . -type f | sort")"
info "on target: $(printf '%s' "$REMOTE_FILES" | tr '\n' ' ')"

# --------------------------------------------------------------------------
head_ "3 · --exclude-from"

if printf '%s' "$REMOTE_FILES" | grep -q 'skip.o'; then
  bad "--exclude-from ignored — skip.o reached the target"
else
  ok "--exclude-from honoured — *.o did not transfer"
fi

# --------------------------------------------------------------------------
head_ "4 · --delete propagation"

rm "$W/src/b/two.txt"
"${SYNC[@]}" "$W/src/" "$HOST:$REMOTE/" >/dev/null 2>&1

if ssh "$HOST" "test -e ~/$REMOTE/b/two.txt"; then
  bad "--delete ignored — two.txt still on the target after local deletion"
else
  ok "--delete propagated — locally removed file is gone from the target"
fi

# --------------------------------------------------------------------------
head_ "5 · --checksum vs the mtime+size quick check"
#
# The Pi has no battery-backed clock, so its timestamps are unreliable
# (invariant I3). This check proves --checksum actually changes behaviour.
#
# Trick: replace the content with a DIFFERENT string of the SAME byte length,
# then restore the original mtime. Now size and mtime are both identical to
# what the target already has — the quick check has nothing to go on.

cp -p "$W/src/a/one.txt" "$W/ref"
printf 'HELLO\n' > "$W/src/a/one.txt"      # also 6 bytes
touch -r "$W/ref" "$W/src/a/one.txt"       # restore mtime

L_SIZE="$(stat -f '%z' "$W/src/a/one.txt" 2>/dev/null || stat -c '%s' "$W/src/a/one.txt")"
L_MTIME="$(stat -f '%m' "$W/src/a/one.txt" 2>/dev/null || stat -c '%Y' "$W/src/a/one.txt")"
R_META="$(ssh "$HOST" "stat -c '%s %Y' ~/$REMOTE/a/one.txt")"
info "local  size/mtime : $L_SIZE $L_MTIME"
info "target size/mtime : $R_META   (identical → quick check is blind)"

# 5a — informational: what happens WITHOUT --checksum
"${SYNC[@]}" -i "$W/src/" "$HOST:$REMOTE/" > "$W/out_nock" 2>&1
if grep -q 'one.txt' "$W/out_nock"; then
  info "without --checksum: transferred anyway (this implementation always compares content)"
else
  info "without --checksum: skipped — exactly the silent-stale-build bug I3 guards against"
fi
NOCK_CONTENT="$(ssh "$HOST" "cat ~/$REMOTE/a/one.txt")"

# 5b — the assertion: WITH --checksum the target must end up correct
"${SYNC[@]}" --checksum -i "$W/src/" "$HOST:$REMOTE/" > "$W/out_ck" 2>&1
CK_CONTENT="$(ssh "$HOST" "cat ~/$REMOTE/a/one.txt")"

if [ "$CK_CONTENT" = "HELLO" ]; then
  ok "--checksum caught a same-size, same-mtime content change"
else
  bad "--checksum did NOT update the target (still: '$CK_CONTENT') — C3 cannot be built as designed"
fi

if [ "$NOCK_CONTENT" != "HELLO" ] && [ "$CK_CONTENT" = "HELLO" ]; then
  info "confirmed: --checksum is load-bearing here, not decorative"
fi

# --------------------------------------------------------------------------
head_ "6 · unchanged tree is a no-op"

"${SYNC[@]}" --checksum -i "$W/src/" "$HOST:$REMOTE/" > "$W/out_noop" 2>&1
XFER="$(grep -c '^[<>]' "$W/out_noop")"
if [ "$XFER" -eq 0 ]; then
  ok "second identical sync transferred nothing"
else
  bad "second identical sync transferred $XFER file(s) — the fast path in P4-4 will be fighting this"
  sed 's/^/        /' "$W/out_noop"
fi

# --------------------------------------------------------------------------
head_ "verdict"
printf '  %d passed, %d failed\n\n' "$PASS" "$FAIL"

if [ "$FAIL" -eq 0 ]; then
  cat <<'EOF'
  C3 can be written as designed. Record in ARCHITECTURE.md §9:
    rsync flavour confirmed, flags -a -z --delete --checksum --exclude-from all verified.
EOF
  exit 0
else
  cat <<'EOF'
  C3 cannot be written as designed with this rsync. Options, in order:
    1. brew install rsync      then pin the absolute path in .perch.toml
                               (Homebrew installs GNU rsync 3.x alongside the system one)
    2. narrow C3's flag set    replace --exclude-from with repeated --exclude
    3. if --checksum is the failure: do not proceed. I3 is not negotiable —
       a silently stale build is the worst failure mode this tool can have.
  Record whichever you choose as an ADR before writing P0-3.
EOF
  exit 1
fi
