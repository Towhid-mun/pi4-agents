# Phase 4 — Durability

> **Provenance.** Drafted agent-side as Phase 4 tickets land, same convention
> as `docs/PHASE-3-BUILD-AND-RUN.md`: filled in from real, live behavior
> against the target, not written up front from what the code was expected
> to do. Anywhere this document fixes something `ARCHITECTURE.md` and
> `DEVELOPMENT-PLAN.md` leave open, that is this document's own decision,
> marked **(chosen here)**.

Phase 4 gate, verbatim from the working prompt:

1. Two concurrent `perch run` invocations: the second exits 75 naming the
   holder. With `--replace` it takes over and the first is confirmed gone.
2. A lock stranded by SIGKILL is detected as stale and reclaimed on the next run.
3. A binary built on the target arrives in the workspace automatically after a
   successful build, and the round-trip question from P4-2.3 is resolved and
   documented.
4. Every row of §8 provoked, with observed behaviour and exit code pasted.
5. The clock test: set the Pi's date to 2020, change a file's content without
   changing its size, sync, and confirm the target gets the new content.
6. If P4-4 was built: a no-change build measurably skips, one changed byte
   un-skips it, and an edit made directly on the target does not produce a
   false skip.

## T0 (blocking check, run before P4-1)

The run lock is specified at `<remote_root>/.perch/run.lock`. `mirror.py`
runs `rsync --delete`, and the host tree has no `.perch/` at all - so before
writing any lock code, confirmed live what actually happens:

```sh
ssh pi 'mkdir -p ~/perch-diag-capture/.perch && echo test > ~/perch-diag-capture/.perch/probe'
perch sync
ssh pi 'cat ~/perch-diag-capture/.perch/probe'
```

Without protection: `perch sync` printed `*deleting .perch/probe` /
`*deleting .perch`, and the probe was gone - confirmed, the trap is real.
`.perch/` in `config.BUILTIN_EXCLUDES` fixes it (added in P4-1): the same
probe, recreated, survives an unexcluded-except-this sync untouched. rsync's
`--delete` does not remove a receiver-side path an exclude rule protects,
without `--delete-excluded` (never passed here).

## What Phase 4 adds

```
perch/artifacts.py     C6 - artifact retrieval (pull, auto-pull)
--replace               take the run lock from a live holder
perch pull <glob> [dest]
```

`perch/executor.py` gains the run lock (`acquire_lock`/`release_lock`,
`lock_dir`/`lock_pgid_path`), bracketing both pipe and pty modes inside
`run()`'s own `try`/`finally`. `perch/config.py`'s `BUILTIN_EXCLUDES` gains
`.perch/` - load-bearing, not cosmetic (see T0 above and P4-2's round-trip
section below, which both depend on it).

## Design decisions this document fixes

**The round trip (P4-2.3), resolved before writing any pull code:** a file
pulled into the host workspace is something the next sync will push right
back to the target - at best wasteful, at worst overwriting a fresher
target-side build with a now-stale host copy. Rejected relying on the user
remembering to add pulled paths to `exclude` (works, but only if they
remember, every time, and does not help an explicit custom `dest`) and
dynamically auto-excluding whatever was pulled (implicit, still does not
cover a custom `dest`). Chosen instead: the default destination for both
auto-pull and explicit `perch pull <glob>` with no `dest` is
`.perch/artifacts/`, relative-path-preserved from `remote_root` - and
`.perch/` is *already* in `BUILTIN_EXCLUDES` (P4-1, for the run lock).
Anything under it cannot be pushed back, by construction, no user action
needed. An explicit `dest` is honored wherever given; `--help` and
`pull --help` both state that an unexcluded destination inside the
workspace will be pushed back on the next sync.

**A glob matching nothing (P4-2 trap 2):** a no-op, not an error, in both
auto-pull and explicit `pull` (the same `artifacts.pull()` function drives
both, so this holds by construction rather than by two maintained
implementations). Reasoning: auto-pull runs after every successful run, and
not every run necessarily produces every configured artifact every time -
treating an absent one as fatal would make an optional artifact mandatory.

**Run lock representation (P4-1):** a *directory*
(`<remote_root>/.perch/run.lock`), not a file - directory creation
(`mkdir`) is the one primitive that is atomic over a single ssh round trip
with no test-then-write race window. The holder's pgid lives inside it, at
`run.lock/pgid`, written by the same already-launched wrapper script that
computes `$PGID` for the existing marker protocol (P2-2) - no second ssh
round trip, and no window where the lock exists but names nobody, since
that write happens inside a script that, by construction, only ever runs
after `acquire_lock` has already succeeded.

## Failure matrix (P4-3)

Every row of `ARCHITECTURE.md` §8, provoked for real against the Pi. The
*detection and response* for every row already existed from earlier phases
(P1-2, P2-6, I3/I4 from Phase 0, P4-1) - P4-3's own work was verifying each
live and writing down how to provoke it again.

| # | Failure | Provoked how | Observed | Exit |
|---|---|---|---|---|
| 1 | Target unreachable | `.perch.toml` pointed at an ssh alias whose `HostName` is `203.0.113.1` (RFC 5737, routes nowhere); `perch build` | `perch: pi-unreachable-test: unreachable - check the target is powered on and reachable on the network`. Bounded: 11.4s total (2 attempts × ~5s `ConnectTimeout` + 1s backoff) - never hangs. | **69** |
| 2 | Target reboots mid-run | `perch exec sleep 60` backgrounded; 3s in, a *separate* connection ran `ssh pi 'sudo reboot'` | `perch: indeterminate - the channel closed without an exit status (the target likely rebooted or the network dropped mid-run). Not success, not failure. The next invocation reconnects unaided.` Confirmed the reboot was real (`uptime -s` matched, `up 0 min` right after). Confirmed "reconnects unaided": ~12s after the reboot began, `perch exec true` succeeded with no manual step - no stale-socket error, no re-auth. | **74** |
| 3 | Partial sync | Two provocations, both real: (a) `ssh pi 'touch ~/proj'` before `remote_root` had ever been created, so `mkdir -p` itself fails ("File exists"); (b) `sudo chattr +i` on a target-side file inside an otherwise-fine `remote_root`, so rsync's own write fails with `Operation not permitted` mid-transfer. (chmod-ing the whole `remote_root` directory read-only was tried first and does **not** work - rsync's `-a` restores the directory's permissions to match the source as part of the same transfer, since the local tree is normally-permissioned; recorded here so nobody re-tries it expecting it to fail.) | (a) `perch: could not create remote root perch-row3-test on pi (ssh exited 1)`. (b) `rsync: [receiver] rename ... -> "a.txt": Operation not permitted (1)` / `perch: rsync exited 23 syncing ... - nothing was executed on the target`. Both abort before anything runs (I4). | **73** |
| 4 | Orphaned remote process | `perch exec sleep 3` backgrounded; SIGKILL the **local** host process 1.5s in (remote group, confirmed via its recorded pgid, is still alive at that instant); waited for the remote `sleep` to finish naturally, confirmed dead via `pgrep -g`; ran a fresh invocation | Stale lock (dead pgid) reclaimed automatically, no `--replace` needed, lock directory clean afterward. | **0** (reclaim), or **75** naming the holder if it were still alive |
| 5 | Concurrent invocation | `perch exec sleep N` backgrounded; a second `perch exec true` ~1.5s later, while the first still holds the lock | `perch: pi: run lock is held by process group <pgid>. Use --replace to take over (this kills that process group first).` `--replace`: kills the holder (confirmed dead via `pgrep -g` after), takes over, exits 0; the pre-empted first invocation reports 74 (indeterminate) from its own point of view - its channel died out from under it, which genuinely is indeterminate, not a lie about confirmed-interrupted. | **75** (default) / **0** (`--replace`) |
| 6 | Target clock wrong | `sudo systemctl stop systemd-timesyncd`, `sudo date -s "2020-01-01"` on the target; local file rewritten with the **exact same byte count**, different content; `perch sync` while the target clock is still stuck at 2020 (confirmed via `date` immediately before and after) | New content transferred correctly (`<fc......` - checksum differs, not time) despite matching size and a target clock 6 years wrong - I3 holds. `systemd-timesyncd` restarted afterward, clock confirmed real again. | **0** |
| 7 | Target disk full | Simulated, never the real SD card (per the working prompt): `sudo mount -t tmpfs -o size=200k tmpfs /mnt/perch-tiny`, `remote_root` pointed inside it. Two provocations: (a) syncing a 500KB file into the 200KB tmpfs; (b) a `commands.build` of `dd if=/dev/zero of=bigfile bs=1k count=500` run *after* a successful small sync, so the failure is the **build's own**, not the mirror's. | (a) `rsync: [receiver] write failed ... No space left on device (28)` / `perch: rsync exited 11 ... - nothing was executed on the target` - verbatim (I8). (b) `dd: error writing 'bigfile': No space left on device` printed exactly as the target produced it, remote exit code passed through unchanged (I6). | (a) **73** (b) **1** (whatever `dd` itself returned - I6) |

## What still doesn't work

| Gap | Phase that fixes it |
|---|---|
| `.vscode/tasks.json`, `CLAUDE.md`, `perch init` | Phase 5 |
| A file watcher | Not planned - ADR-4 |
| Multi-level nested `make -C` cwd tracking is implemented but never exercised against a real nested build (Phase 3 carryover) | untested, not phase-gated |

## Troubleshooting

| Symptom | Likely cause | What to check |
|---|---|---|
| `perch build`/`exec`/`run` exits 75 unexpectedly | Another invocation (possibly from a different machine) genuinely holds the lock, or a previous one crashed hard enough that its process is still alive on the target | The error message names the holder's pgid - `ssh <host> "ps -o pid,pgid,cmd -g <pgid>"` to see what it is; `--replace` if you are sure it should not be there |
| Pulled artifacts keep reappearing in `git status` / look tracked | `.perch/artifacts/` is excluded from the *mirror*, not from git - add `.perch/` to `.gitignore` if using git | n/a - this is a host-side convention, not a perch behavior |
| `chmod`-ing the target directory does not provoke a sync failure | rsync's `-a` restores directory permissions to match the source as part of the same transfer - see failure matrix row 3's note | Use a real obstruction instead: an occupied path (`touch` where a directory needs to be) or an immutable file (`chattr +i`) |
