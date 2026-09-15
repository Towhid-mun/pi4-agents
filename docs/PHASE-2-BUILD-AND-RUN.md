# Phase 2 — Build and Run

What changed, how to verify each guarantee yourself, what still doesn't work,
and a troubleshooting table. Every command below was actually run against the
real Pi while writing this. See `docs/PHASE-0-BUILD-AND-RUN.md` and
`docs/PHASE-1-BUILD-AND-RUN.md` for what came before.

## What changed

`perch/executor.py` is rewritten wholesale (the walking-skeleton version from
Phase 0, marked `# REPLACED IN P2`, is gone). Every command now:

1. Streams stdout and stderr as two genuinely separate live streams (P2-1) -
   no `.communicate()`, no buffering until exit.
2. Runs inside a small wrapper (`setsid --wait bash -c '...'`) that captures
   its own process group id and reports it, then reports its real exit
   status on a second marked line at the end (P2-2). See ADR-5 in
   `ARCHITECTURE.md` for exactly what's in that wrapper and why.
3. Can be interrupted: local Ctrl-C (or SIGTERM) signals the remote group
   from a second connection, confirms it's dead, then exits 130 - never
   claims 130 without that confirmation (P2-3).
4. Reaps its own orphans: if a previous run for the same project left a live
   group (because the host process was killed outright), the next
   invocation kills it first, automatically (P2-4).
5. Has a `--tty` mode for interactive remote programs, which cannot be
   combined with `--json` (P2-5).
6. Reports **indeterminate** (exit 74), not success or failure, when the
   channel closes without ever producing that final exit-status line - the
   target most likely rebooted or the network dropped (P2-6).

New per-project state on the host: `~/.perch/state/<hash>/pgid`, used only by
P2-4's reaping. It is not the run lock (Phase 4) and never blocks a second
invocation - only reaping THIS project's own confirmed orphan.

New CLI flags on `exec`/`build`/`test`/`run`: `--tty`, `--json` (the latter
recognized now only so the combination can be refused - it does nothing on
its own until Phase 3).

## Prerequisites

Same as Phase 1 - see `docs/PHASE-1-BUILD-AND-RUN.md`. This document assumes
`.venv/bin` is on `PATH` and you're working from a project with a
`.perch.toml` pointed at `host = "pi"`.

## Test fixtures

`integration/fixtures/` holds small scripts the mirror pushes - never inline
these into an ssh command line. Each one exists for a specific guarantee:

| Fixture | Exercises |
|---|---|
| `ticker.sh` | Streaming, one line per second, via shell `echo` (no libc buffering layer to defeat - proves the READER, independent of trap 4) |
| `slow_printer.c` | The trap-4 remote-buffering problem itself - `printf` with no `fflush`, compiled with gcc, genuinely block-buffers without the `stdbuf` wrapper |
| `stdout_stderr_alternating.sh` | Stream separation and ordering |
| `spawns_children.sh` | The group kill takes children too |
| `ignores_sigterm.sh` | Escalation from TERM to KILL |
| `exit255.sh` | The exit sentinel disambiguates a genuine remote 255 from ssh's own connection-level 255 |
| `interactive.sh` | `--tty`: reads one line, echoes it back |

`integration/fixtures/.perch.toml` points at the real Pi (`remote_root =
"perch-integration-fixtures"`). `cd integration/fixtures && perch build`
pushes and compiles all of them.

## Verifying each guarantee

### 1. Streaming (P2-1)

```sh
cd integration/fixtures
while IFS= read -r line; do
  printf '%s  %s\n' "$(date +%T.%N | cut -c1-12)" "$line"
done < <(perch exec ./ticker.sh 5)
```

Expect five `tick N` lines roughly one second apart, not a burst at the end:

```
00:41:10.508  tick 1
00:41:11.570  tick 2
00:41:12.656  tick 3
00:41:13.542  tick 4
00:41:14.554  tick 5
```

### 2. Stream separation (P2-1)

```sh
perch exec ./stdout_stderr_alternating.sh 6 \
  1> >(while IFS= read -r l; do printf '%s  OUT  %s\n' "$(date +%T.%N|cut -c1-12)" "$l"; done) \
  2> >(while IFS= read -r l; do printf '%s  ERR  %s\n' "$(date +%T.%N|cut -c1-12)" "$l"; done)
wait
```

Expect `OUT 1`, `ERR 2`, `OUT 3`, ... each correctly labeled by its real
stream, in order, roughly 0.3s apart.

### 3. Process group capture (P2-2)

```sh
perch exec ./ticker.sh 8 &
sleep 2
PGID=$(cat ~/.perch/state/*/pgid)
echo "perch recorded: $PGID"
ssh pi "ps -o pgid= -p $PGID | tr -d ' '"
wait
```

The two numbers must match exactly.

### 4. The exit sentinel (P2-2/P2-6)

```sh
perch exec sh -c 'exit 255'; echo "exit: $?"
```

Expect `exit: 255` - a genuine remote 255, not misreported as a connection
failure (ssh's own connection-level failures also use 255, which is exactly
what the sentinel exists to disambiguate).

### 5. Ctrl-C confirms the group is dead (P2-3)

```sh
perch exec ./ticker.sh 60 &
LOCALPID=$!
sleep 3
kill -INT "$LOCALPID"       # Ctrl-C, from another terminal: just press it
wait "$LOCALPID"; echo "exit: $?"
```

Expect `exit: 130`. Then, from anywhere:

```sh
ssh pi 'pgrep -af "bash -c.*PIPE HUP"'
```

Expect nothing.

### 6. Escalation to KILL (P2-3)

```sh
perch exec ./ignores_sigterm.sh &
LOCALPID=$!
sleep 2
kill -INT "$LOCALPID"
wait "$LOCALPID"; echo "exit: $?"
```

Expect `exit: 130` after **about 5 seconds**, not instantly - that gap is
`TERM_GRACE_SECONDS` elapsing before KILL fires, proving escalation actually
happened rather than TERM quietly working on a script that traps it.

### 7. Stale-group reaping (P2-4)

```sh
perch exec sh -c 'sleep 30' &
LOCALPID=$!
sleep 3
PGID=$(cat ~/.perch/state/*/pgid)
kill -9 "$LOCALPID"          # SIGKILL the HOST process - no clean shutdown
sleep 1
ssh pi "pgrep -g $PGID -a"   # still alive - confirms the orphan exists
perch exec echo hello        # the NEXT invocation
```

Expect a `perch: reaping an orphaned process group (<PGID>)...` line before
`hello`, and `ssh pi "pgrep -g $PGID"` empty afterward.

### 8. `--tty` (P2-5)

Needs a real terminal (or a pty harness - see the P2-5 commit message for
the exact Python `pty` module approach used to verify this non-interactively
during development). From an actual terminal:

```sh
perch exec --tty ./interactive.sh
```

Type a name, press Enter, see it echoed back.

```sh
perch exec --tty --json true; echo "exit: $?"
```

Expect a one-line refusal and `exit: 64`.

### 9. Indeterminate (P2-6)

**Reboots the Pi.** Only do this if that's fine right now.

```sh
perch exec ./ticker.sh 120 &
LOCALPID=$!
sleep 4
ssh pi 'sudo reboot'          # from the SAME terminal is fine; it's a separate ssh connection
wait "$LOCALPID"; echo "exit: $?"
```

Expect the word **indeterminate** and `exit: 74`. Then, once the Pi is back
(`ssh -o ConnectTimeout=3 pi true` starts succeeding again):

```sh
perch exec echo "recovered fine"
```

Expect this to just work - no manual control-socket cleanup, nothing.

## What still doesn't work

| Gap | Phase that fixes it |
|---|---|
| Diagnostic parsing / path rewriting (a target-side error path is not yet rewritten to a local one) | Phase 3 (C5) |
| `--json` structured event stream (the flag exists only to be refused with `--tty`) | Phase 3 |
| The real run lock - two concurrent `perch run` invocations for the same project will collide; P2-4 only reaps a confirmed orphan from a *previous, ended* invocation, it has no concept of a legitimately concurrent one | Phase 4 (P4-1) |
| Artifact retrieval - anything a command produces on the target is still lost on the next sync unless the command itself both builds and uses it in one invocation | Phase 4 (P4-2) |
| The `interrupted_unconfirmed` path (TERM and KILL both fail to produce confirmed death, or the target goes unreachable mid-confirmation) reports indeterminate (74) rather than anything more specific - not deliberately provoked in this phase; the mechanism exists (see `_terminate_group`'s return value in `executor.py`) but was not forced in practice |

## Troubleshooting

| Symptom | Likely cause | What to check |
|---|---|---|
| Output arrives in one lump at the end instead of streaming | The REMOTE program buffers its own stdout (trap 4 - gcc/make/python detect a pipe and switch to block buffering) | `perch doctor` shows whether `stdbuf` exists on the target; `slow_printer.c` demonstrates the exact problem and the fix in one fixture |
| `perch build \| head` (or any pipe that closes early) crashes with `BrokenPipeError` | Should not happen - `executor.py`'s `emit()` catches this and redirects the fd to `/dev/null` | If you see this, it's a real regression - the guard is right at the top of the read loop's `emit()` closure |
| Ctrl-C exits 130 but a process is still visible on the target | This would be an I7 violation - should never happen if `_terminate_group` returned `True` | Check whether the confirmation polling itself hit `TargetUnreachable` (network dropped during the kill, not just during the run) - that path is designed to report **indeterminate**, not 130; if you see 130 anyway, that's a bug |
| A `perch exec` hangs with `--tty` when scripted (not from a real terminal) | Classic PTY nuance, not specific to this tool: if whatever drives the local pty puts it in raw mode *before* `ssh -tt` starts, ssh propagates that (including `ICRNL` off) to the *remote* pty too, so a bare `\r` never becomes a line terminator on either end | Send a real `\n`, not `\r`, when scripting input to a `--tty` session |
| `perch: reaping an orphaned process group...` appears on a run you didn't expect | A previous invocation for this project was killed outright (SIGKILL, laptop sleep during a run, etc.) and left a group alive - this is P2-4 working as intended | `rm -rf ~/.perch/state` to forget all recorded groups if you're certain nothing is actually running; otherwise let it reap |
| `could not reap orphaned group ... it survived SIGKILL` | Genuinely stuck target-side process (D-state, or the target became unreachable mid-reap) | `ssh pi "ps -o stat= -p <pgid>"` - a `D` state means it's blocked in an uninterruptible kernel wait (usually disk I/O) and nothing short of the process finishing or a reboot will clear it |
