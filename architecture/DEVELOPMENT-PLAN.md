# DEVELOPMENT-PLAN.md — Perch

> Companion to `ARCHITECTURE.md`. That file says what the system *is*; this one
> says what to build, in what order, and how each piece is proven done.
>
> Ticket IDs are stable. Use them in branch names and commit subjects:
> `P2-3: forward SIGINT to the remote process group`.

---

## 1. How to work this plan

- **One ticket per change.** A ticket touching two components usually means
  logic landed in the wrong module.
- **Do not start a phase before its predecessor's acceptance test passes.**
  Phases are gates, not labels.
- **Every ticket states its own done-test.** "Implemented" is not done; the
  stated observation is done.
- **Spikes are timeboxed and throwaway.** A spike answers one question and its
  code is deleted. If you want to keep it, that is a new ticket.

Global definition of done, applied to every ticket:

1. Offline tests pass (`tests/`), and the ticket's own done-test is observed.
2. No invariant from `ARCHITECTURE.md` §2 is violated.
3. `--help` reflects any new surface.
4. Any decision made along the way is recorded as an ADR in `ARCHITECTURE.md` §9.

Size is relative effort, not time: **S** a sitting · **M** a day's focus ·
**L** multiple sessions, expect to redo part of it.

---

## 2. Dependencies we take on

Everything here is either the Python standard library or a binary already
present on both machines. Adding anything else needs an ADR.

### Standard library

| Module | Used by | For |
|---|---|---|
| `tomllib` | C1 | parsing `.perch.toml` (stdlib from 3.11 — this is why 3.11 is the floor) |
| `dataclasses` | C1, C4 | frozen config and result types |
| `pathlib` | C1, C3, C6 | host paths; never used for remote paths (they are strings — remote is POSIX, host may not be) |
| `argparse` | C7 | verb dispatch and flags |
| `subprocess` | C2, C3, C4 | launching `ssh` and `rsync` |
| `selectors` | C4 | **the** mechanism for non-blocking dual-stream reads (I5) |
| `signal` | C4 | local interrupt handling |
| `pty`, `termios` | C4 | `--tty` mode only |
| `shlex` | C2, C4 | quoting commands that cross into a remote shell — every remote command string goes through this |
| `re` | C5 | diagnostic parsing |
| `fnmatch` | C3, C6 | exclude and artifact globs |
| `json` | C5, C7 | structured event stream |
| `hashlib` | C3 | local change detection for the sync fast path |
| `time`, `os`, `sys`, `errno` | — | ambient |

### External binaries

| Binary | Where | Why not a library |
|---|---|---|
| `ssh` | host | Delegating gets us `~/.ssh/config`, `ControlMaster` multiplexing, agent and key handling, and known-hosts for free. A Python SSH library would reimplement all of it worse, and would hold credentials we have an invariant against holding (I10). |
| `rsync` | both | Delta transfer, `--delete` semantics and `--checksum` are the exact behaviour C3 needs. Reimplementing is weeks of work for a worse result. |
| `journalctl`, `systemctl` | target | `logs` verb only. Invoked, never parsed. |

### Deliberately not taken

| Rejected | Instead | Reason |
|---|---|---|
| `paramiko` / `asyncssh` | subprocess `ssh` | loses ssh config, multiplexing and known-hosts; forces key material into our process |
| `watchdog` | explicit sync | ADR-4 |
| `click` / `typer` | `argparse` | stdlib-only rule; the surface is nine verbs |
| `pytest-xdist`, heavy test infra | plain `pytest` | the offline suite is small and must stay fast |

**Open risk:** recent macOS ships `openrsync` rather than GNU `rsync`, and flag
support differs. **Spike S0-1 resolves this before P0-3 is written.**

---

## 3. Spikes — do these first

Timeboxed, throwaway, answer one question each.

**S0-1 · Which rsync is on the host, and does it do what C3 needs?** `S`
Check `rsync --version` on the Mac. Confirm `-a`, `-z`, `--delete`,
`--checksum`, `--exclude-from` and `-e ssh` all behave. If the shipped binary is
`openrsync` and any of those differ, decide between requiring GNU rsync via
Homebrew or narrowing C3's flag set — and record it as an ADR.
*Answers:* whether C3 can be written as designed.

**S0-2 · Can we capture a remote process group id and kill it from a second connection?** `S`
By hand, no code in the repo. Start `setsid sleep 300` over ssh, capture the
pgid, disconnect, then kill the group over a fresh connection and confirm with
`pgrep`.
*Answers:* whether P2-2 and P2-3 are viable as designed. **This is the highest-risk
assumption in the architecture — resolve it before building toward it.**

---

## Phase 0 · Walking skeleton
**Gate:** a local syntax error makes `perch build` exit non-zero.

Deliberately crude. The point is to find out whether the shape is right before
any of it is made good.

**P0-1 · Project scaffold** `S` — *no deps*
`pyproject.toml` with a `perch` console script, package skeleton, `--version`,
and `errors.py` carrying the complete exit-code map from `ARCHITECTURE.md` §7 as
an exception hierarchy. No other module ever chooses an exit code.
*Done when:* `perch --version` prints, and `PerchError` subclasses map to their
documented codes in a unit test.

**P0-2 · Config resolution (C1)** `M` — *needs P0-1*
`.perch.toml` discovered by walking up from the working directory. Parse,
validate, apply defaults, return a frozen `Config`. Derive and expose the
`(local_root, remote_root)` pair — nothing else in the codebase derives it.
Reject unknown keys loudly; a typo in a config key must not fail silently.
*Done when:* a malformed config exits 64 naming the file and the offending key;
a valid one round-trips to the expected `Config` in a test.

**P0-3 · Naive mirror (C3)** `M` — *needs P0-2, S0-1*
`rsync` subprocess with `--delete` and `--checksum`. Built-in excludes plus
config excludes. Creates the remote root on first use. Non-zero raises.
Put a comment at the flag list saying why `--checksum` is there (I3) so nobody
removes it as an optimization.
*Done when:* a file deleted locally is gone from the target after `perch sync`;
touching a file without changing its content transfers nothing.

**P0-4 · Naive executor (C4-draft)** `S` — *needs P0-2*
`ssh host "cd <root> && <cmd>"`, inherit stdio, propagate the exit code.
Buffered and signal-naive **on purpose** — P2 replaces it. Mark it with a
`# REPLACED IN P2` comment so it is not mistaken for finished work.
*Done when:* `perch exec 'uname -m'` prints `aarch64` and exits 0.

**P0-5 · CLI wiring (C7-draft)** `S` — *needs P0-3, P0-4*
Verbs `sync`, `exec`, `build`, `test`, `run`. Sequence steps 1, 3, 5, 7 from
`ARCHITECTURE.md` §6. Abort before execute if the mirror failed (I4).
*Done when:* **the phase gate** — break a local `.c` file, run `perch build`,
get the Pi's compiler error and a non-zero exit status.

---

## Phase 1 · Transport
**Gate:** the second invocation's overhead is a small fraction of the first's, and power-cycling the target needs no manual step.

**P1-1 · Session manager (C2)** `M` — *needs P0-4*
`ControlMaster=auto`, a `ControlPath` derived per target under the user's
`~/.ssh/`, `ControlPersist`, explicit `ConnectTimeout`. All ssh invocations in
the codebase go through this one place.
*Done when:* after a first command, `ssh -O check` reports a live master, and a
second command visibly skips the handshake.

**P1-2 · Connection failure handling** `S` — *needs P1-1*
Classify: unreachable, auth failure, host key mismatch. Exit 69 for unreachable
with the host name and one actionable hint. Bounded backoff on reconnect. Never
hang on a powered-off board.
*Done when:* with the Pi unplugged, `perch build` exits 69 in under the
configured timeout — not after a TCP default.

**P1-3 · `doctor` verb** `M` — *needs P1-1*
Probe script sent over stdin (not quoted into the command line — it has to
survive two shells otherwise). Reports host identity, OS, arch, memory, disk,
`gcc`, `make`, `python3`, `rsync`, `libgpiod`, `/dev/i2c-*`, `/dev/gpiochip*`,
plus the local rsync flavour from S0-1 and the control-socket state.
*Done when:* `perch doctor` on a fresh Pi correctly reports which of the
toolchain and device files are missing.

**P1-4 · Latency harness** `S` — *needs P1-1*
A small script that times cold and warm invocations. It exists because it *is*
the phase gate; without it "feels fast" is the acceptance criterion.
*Done when:* it reports both numbers and the warm case is a small fraction of
the cold one.

---

## Phase 2 · Fidelity
**Gate:** a program printing once per second prints once per second locally, and Ctrl-C leaves nothing alive on the target.

The hard phase. Everything here is a guarantee that either holds or does not —
there is no partial credit. Budget accordingly.

**P2-1 · Streaming dual-stream reader** `L` — *needs P1-1*
`selectors`-based read of stdout and stderr, emitting per line as data arrives.
A `.communicate()` call in this path is a defect (I5). Handle partial lines at
chunk boundaries and a final unterminated line.
*Done when:* a remote program printing once per second produces one local line
per second; a 10 MB burst does not deadlock.

**P2-2 · Remote process group launch and capture** `M` — *needs S0-2, P2-1*
Launch under `setsid`; return the process group id to the host on a marked line
before the program's own output begins. The marker must be unambiguous — a
program that prints something marker-shaped must not confuse it.
*Done when:* the pgid reported matches `ps -o pgid=` on the target for that run.

**P2-3 · Signal forwarding** `L` — *needs P2-2*
Local `SIGINT` handler opens a **separate** connection, signals the captured
group, waits for confirmation that it is gone, then exits 130. Second Ctrl-C
escalates. Never exit 130 without confirmation — that is a lie about I7.
*Done when:* Ctrl-C during a long-running remote program is followed by
`pgrep -g <pgid>` over ssh returning nothing.

**P2-4 · Stale group reaping** `S` — *needs P2-3*
On start, if a previous run for this project left a recorded group that is still
alive, reap it or refuse with a message naming it.
*Done when:* killing the host process with `SIGKILL` mid-run leaves an orphan
that the next invocation cleans up.

**P2-5 · PTY mode** `M` — *needs P2-1*
`ssh -tt` path. Signals arrive natively; streams merge. Refuse to combine with
`--json` (the merged stream cannot be classified) with a clear message.
*Done when:* an interactive remote program that reads a line works under
`--tty`, and `--tty --json` exits 64 explaining why.

**P2-6 · Indeterminate run detection** `S` — *needs P2-1*
Channel EOF without an exit status → exit 74, reported as **indeterminate**.
Not success. Not failure.
*Done when:* rebooting the Pi mid-run produces exit 74 and that wording.

---

## Phase 3 · Legibility
**Gate:** an agent edits, builds, reads the failure, fixes it, and rebuilds — with no human input at any step.

This is the phase that turns the tool from usable-by-a-person into
usable-by-an-agent. All of it is testable offline.

**Build order actually used: P3-4, then P3-1, P3-2, P3-3** — inverted from the
numbering below, on instruction. The dependency as originally written
("P3-4 needs P3-1") assumed the corpus would be recorded to validate an
already-written parser; it was built the other way instead, so the parser
would be written against real recorded gcc/clang/Python output rather than
against a remembered guess at their format. P3-4 has no real dependency on
P3-1 - it only needs a reachable target to capture from, once, and never
again afterward.

**P3-4 · Fixture corpus and offline suite** `S` — *needs a reachable target,
once, to record from - not P3-1*
Recorded real output: a gcc error, a gcc warning, a linker error, a Python
traceback, a clean build, and one file of noise. These are the fast feedback
loop; keep them network-free forever.
*Done when:* the whole C1/C3/C5 suite runs with no target reachable.

**P3-1 · Diagnostic parsers (C5)** `M` — *needs P2-1, P3-4's fixtures*
GCC/Clang `path:line:col: severity: message`, and Python `File "path", line N`.
Streaming, line at a time. **An unrecognized line passes through byte-identical**
(I8) — this is the property most likely to be broken by a careless regex.
*Done when:* fixture files of real compiler output produce the expected parse,
and a fixture of unrelated output passes through unchanged.

**P3-2 · Path rewriting** `S` — *needs P3-1, P0-2*
Apply the `(local_root, remote_root)` pair to parsed diagnostics. Relative paths
resolve against the remote root before rewriting.
*Done when:* a target-side error path becomes a path that exists in the local
workspace, verified against a real build failure.

**P3-3 · Structured event stream** `M` — *needs P3-2*
`--json` emits the event schema from `ARCHITECTURE.md` §5/C5, one object per
line, including the terminal `exit` event. Stream stays flushed per event — a
consumer must be able to react before the process ends.
*Done when:* `perch build --json` on a failing build emits `diag` events with
local paths, then one `exit` event, and every line parses as JSON.

---

## Phase 4 · Durability
**Gate:** each row of the failure table can be provoked deliberately and produces its stated response.

**P4-1 · Run lock** `M` — *needs P2-2*
`<remote_root>/.perch/run.lock` holding the process group id. Acquire, release,
detect stale, `--replace` to take it. Exit 75 when held.
*Done when:* two concurrent `perch run` invocations — the second exits 75
naming the holder; with `--replace` it takes over and the first is gone.

**P4-2 · Artifact retrieval (C6)** `S` — *needs P1-1*
`perch pull <glob> [dest]`, and auto-pull of `config.artifacts` after exit 0.
`--help` must state plainly that the mirror deletes, so anything generated on
the target and not retrieved is lost on the next sync.
*Done when:* a binary built on the target lands in the local workspace
automatically after a successful build.

**P4-3 · Failure matrix** `M` — *needs P4-1*
Implement every row of `ARCHITECTURE.md` §8 explicitly, and write a provocation
test or documented manual procedure for each.
*Done when:* all seven rows demonstrated.

**P4-4 · Sync fast path** `M` — *needs P0-3*
Skip the mirror when a local content hash says nothing changed. **Correctness
first:** a wrong skip is far worse than a redundant sync, so the guard must be
conservative and the skip must be observable in verbose output.
*Done when:* a no-change `perch build` measurably skips the transfer, and
touching one byte reliably un-skips it.

---

## Phase 5 · Integration
**Gate:** one keystroke builds on the target and errors are clickable.

Thin, and last. Integration built earlier leaks editor assumptions back into the core.

**P5-1 · Editor tasks** `S` — *needs P3-2*
`.vscode/tasks.json`: build, test, run, doctor, with a GCC problem matcher and
paths relative to the workspace folder.
*Done when:* a failing build populates the Problems panel with entries that jump
to the right local line.

**P5-2 · Agent contract** `S` — *needs P3-3*
`CLAUDE.md` pointing at `ARCHITECTURE.md`, stating never to build locally, and a
permission allowlist so the agent is not interrupted mid-loop.
*Done when:* an unattended agent session completes an edit-build-fix-build cycle
without a single prompt.

**P5-3 · `perch init`** `S` — *needs P0-2*
Scaffolds `.perch.toml`, `.vscode/tasks.json` and the `CLAUDE.md` snippet into a
project, detecting the ssh alias from `~/.ssh/config` where it can.
*Done when:* a fresh directory goes from nothing to a working `perch doctor` in
one command plus editing one line.

---

## 4. Critical path

Most of the plan is parallelizable within a phase. This chain is not:

```
S0-2 → P0-4 → P1-1 → P2-1 → P2-2 → P2-3 → P3-1 → P3-3 → P5-2
```

Everything the agent-driven loop depends on runs through it. **P2-3 is the
single riskiest ticket** — it is the only one whose failure would force an
architecture change (an agent on the target, reopening ADR-1). S0-2 exists to
find that out on day one rather than in week three.

---

## 5. Risk register

| Risk | Affects | Signal it is happening | Response |
|---|---|---|---|
| Host `rsync` is `openrsync` with different flag support | P0-3 | S0-1 finds a missing flag | Require GNU rsync via Homebrew, or narrow C3's flags; record an ADR either way |
| Killing a remote process group from a second connection does not work reliably | P2-3 | S0-2 leaves survivors | Fall back to a pid-file plus a reaper on next run, and accept a weaker I7 — document the weakening |
| mDNS name resolution is flaky on the LAN | P1-2 | intermittent exit 69 with a working Pi | Allow a literal address in ssh config; make `doctor` report which resolution path was used |
| Marker line for pgid capture collides with program output | P2-2 | wrong pgid, failed kills | Use a marker with high-entropy content generated per run |
| Sync fast path skips a real change | P4-4 | a build that does not reflect an edit — corrosive, hard to spot | Make the skip loud in verbose mode; make the guard conservative; provide `--force-sync` |
| Scope creep toward deployment or a second board | all | requests that sound like extensions | `ARCHITECTURE.md` §11 — say it is out of scope first, then discuss |

---

## 6. Test strategy

| Layer | What | Runs where | Speed |
|---|---|---|---|
| Unit | C1 config, C3 exclude logic, C5 parsing and rewriting | offline, fixtures | must stay fast — this is the working loop |
| Contract | exit-code mapping, JSON event schema | offline | fast |
| Integration | C2 session, C4 execution, C6 retrieval | needs a reachable target | slow, not in CI |
| Provocation | each row of the failure matrix | needs a target you may reboot and unplug | manual, documented procedure |

Keep the offline suite network-free permanently. The moment a unit test needs
the Pi, the fast feedback loop is gone and nobody runs the tests.

---

## 7. Out of this plan

Phase 6 — the sensor node itself — is a **consumer** of this tool and has its
own plan. No code for it belongs in `perch/`. It begins when Phase 5's gate
passes, and its first act should be to prove the point: hardware work proceeding
without anyone thinking about which machine they are on.
