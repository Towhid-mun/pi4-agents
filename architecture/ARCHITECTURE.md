# ARCHITECTURE.md — Perch

> Written for an AI coding agent. Terse and prescriptive by intent.
> Read this before writing code. Where this file and a request conflict, say so
> rather than silently deviating.
>
> Add to `CLAUDE.md`: `Read ARCHITECTURE.md before any structural change.`

---

## 1. What this is

A remote build-and-run bridge. Source is authored on a **macOS workstation**;
it compiles, runs and fails on a **Raspberry Pi 4** over SSH; output, exit codes
and error locations come back to the workstation as if the build had been local.

**Host** — macOS, Apple silicon. Editor, agent, and the only real copy of the source.
**Target** — Raspberry Pi 4, aarch64, 2 GB RAM, Raspberry Pi OS, SD card storage.
**Channel** — SSH only. No new listening ports on the target.

The split exists because the target cannot host a development environment
(2 GB, SD card) and the host cannot host the hardware (no I2C, no GPIO, wrong
architecture). It is a fixed constraint, not a problem to engineer away.

---

## 2. Invariants

Violating any of these is a defect, regardless of whether tests pass.

| # | Invariant |
|---|---|
| **I1** | **Nothing is installed on the target.** No daemon, no agent, no runtime owned by this tool. Only the project's own source and its own build dependencies. |
| **I2** | **The host workspace is the sole source of truth.** The target's copy is derived and disposable. |
| **I3** | **Sync never depends on target timestamps.** The Pi has no battery-backed RTC; its clock is wrong on every boot until NTP lands. Compare by content. |
| **I4** | **Never execute against a partially synced tree.** A failed or incomplete sync aborts before the command runs. |
| **I5** | **Output streams; it is never buffered until process exit.** |
| **I6** | **The target's exit code is the tool's exit code.** Tool-level failures use a disjoint range (§7). |
| **I7** | **No orphans.** When the host invocation ends, no process it started is still alive on the target. |
| **I8** | **Remote errors pass through verbatim.** Do not reinterpret, summarize, or prettify a message produced on the target. Rewriting file paths (C5) is the only permitted transformation. |
| **I9** | **Every command works non-interactively.** No prompt, no TTY requirement, no human in the loop. An agent is a first-class caller. |
| **I10** | **No secrets in tool config.** Authentication is delegated entirely to SSH. |

---

## 3. Implementation language

**Python 3.11+, standard library only for the core.**

Rationale: no build step on the host; `subprocess`, `selectors`, `signal`, `pty`
and `shlex` cover every hard requirement in C4 directly; the agent can edit and
run it in one step. `rsync` and `ssh` are invoked as subprocesses — do not
reimplement either.

Third-party dependencies require justification in an ADR. `tomllib` is stdlib in
3.11 and is what parses the config.

---

## 4. Repository layout

```
perch/
├── ARCHITECTURE.md          this file
├── CLAUDE.md                agent instructions; points here
├── pyproject.toml           console_script entry point: perch
├── perch/
│   ├── __init__.py
│   ├── __main__.py          python -m perch
│   ├── cli.py               C7 — argument parsing, verb dispatch, exit codes
│   ├── config.py            C1 — config resolution
│   ├── session.py           C2 — SSH connection + multiplexing
│   ├── mirror.py            C3 — workspace sync
│   ├── executor.py          C4 — remote execution, streaming, signals
│   ├── diagnostics.py       C5 — path rewriting, structured events
│   ├── artifacts.py         C6 — retrieval
│   └── errors.py            exception → exit-code mapping
├── tests/
│   ├── fixtures/            recorded compiler output, sample trees
│   ├── test_config.py
│   ├── test_mirror.py
│   ├── test_diagnostics.py  runs offline against fixtures
│   └── test_executor.py     uses a local sshd or a fake transport
└── integration/
    └── test_against_pi.py   requires a reachable target; not run in CI
```

**Rule:** a module owns exactly one component. Cross-component logic goes in
`cli.py`, which orchestrates and owns no mechanism of its own.

---

## 5. Components

Each has an ID used in commit messages, tests and this file.

### C1 · `config.py` — Config resolver

Resolves project settings. Defers all connection detail to `~/.ssh/config`.

```python
@dataclass(frozen=True)
class Config:
    host: str              # ssh alias — resolved by ssh, not by us
    remote_root: str       # path on target, relative to $HOME unless absolute
    local_root: Path       # absolute path on host
    commands: dict[str, str]   # "build" | "test" | "run" -> shell command
    exclude: tuple[str, ...]
    artifacts: tuple[str, ...] # globs auto-pulled after a successful run
```

- **MUST NOT** store hostnames, ports, users, keys or passwords. Only the alias.
- **MUST** produce `(local_root, remote_root)` for C5; no other component derives it.
- Missing config is a tool error (§7), reported with the expected file path.

### C2 · `session.py` — Session manager

Owns the SSH connection and its reuse.

```python
class Session:
    def exec(self, argv: list[str], *, cwd: str, pty: bool = False) -> Process
    def push(self, spec: SyncSpec) -> None
    def pull(self, remote_glob: str, dest: Path) -> list[Path]
    def alive(self) -> bool
```

- **MUST** use `ControlMaster=auto` with a `ControlPath` under the user's
  `~/.ssh/` and a `ControlPersist` window, so the second and later invocations
  cost one round trip rather than a full handshake.
- **MUST** set an explicit `ConnectTimeout`. Never hang on a powered-off board.
- Reconnect with bounded backoff. A target that rebooted must require no manual step.

### C3 · `mirror.py` — Workspace mirror

Makes the target tree identical to the host tree for included paths.

- `rsync -az --delete --checksum` — **`--checksum`, not the default mtime+size**
  (I3). Document this at the call site so nobody "optimizes" it away.
- Deletions propagate. Excludes come from config plus a built-in list
  (`.git/`, `.venv/`, `__pycache__/`, `node_modules/`, `*.o`, `*.pyc`,
  `.DS_Store`, the tool's own config file).
- **MUST** create the remote root on first use.
- Non-zero from rsync raises; the caller aborts before executing (I4).
- Fast path: skip when a cheap local change-detect says nothing moved. Correctness
  first — a wrong skip is worse than a redundant sync.

### C4 · `executor.py` — Remote executor

The component where "roughly right" is a bug. Carries I5, I6, I7.

```python
@dataclass
class RunResult:
    exit_code: int
    interrupted: bool
    indeterminate: bool   # channel closed without a status — target rebooted
```

Two modes, per ADR-2:

**Pipes (default).** Used by `build`, `test`, and every agent invocation.
- stdout and stderr stay separate; no terminal control bytes in the output.
- **MUST** read both with `selectors` and emit per line as it arrives (I5).
  A `.communicate()` call in this path is a defect — it buffers.
- Signals are explicit: launch under `setsid`, capture the remote process group
  id on the first line of a control channel, install a local `SIGINT` handler
  that opens a **separate** connection and signals that group, then wait for
  confirmation before exiting 130.

**PTY (`--tty`).** For interactive remote programs.
- `ssh -tt`; signals arrive natively; streams merge and carry control characters.
- **MUST NOT** be the default, and **MUST NOT** be used when a structured output
  sink is open — the merged stream cannot be classified.

Also owns the **run lock**: a file under `<remote_root>/.perch/run.lock` holding
the process group id. A second invocation fails fast unless `--replace`. A stale
lock whose group is dead is reaped on the next run for the same project.

### C5 · `diagnostics.py` — Diagnostic mapper

Rewrites target paths to host paths in the output stream.

- **MUST** operate line by line on the live stream. Buffering to rewrite destroys I5.
- Handles at minimum the GCC/Clang form `path:line:col: severity: message` and
  the Python traceback form `File "path", line N`.
- Emits structured events when a sink is open:

```json
{"t": "stdout", "line": "..."}
{"t": "stderr", "line": "..."}
{"t": "diag", "file": "src/main.c", "line": 12, "col": 5, "severity": "error", "message": "..."}
{"t": "exit", "code": 1, "interrupted": false}
```

- A line it does not recognize passes through **unchanged** (I8).
- This module must be testable with no network: fixtures in, expected lines out.

### C6 · `artifacts.py` — Artifact retrieval

`pull` by path or glob; auto-pull of `config.artifacts` after exit code 0.

Because C3 deletes, anything generated on the target inside the project
directory is lost on the next sync unless retrieved or excluded. Say so in
`--help`.

### C7 · `cli.py` — Command surface

```
perch doctor                  target identity, arch, memory, disk, toolchain, device files
perch sync                    mirror only
perch build [args...]         sync + commands["build"]
perch test  [args...]         sync + commands["test"]
perch run   [args...]         sync + commands["run"]
perch exec  <cmd...>          sync + arbitrary command in remote root
perch pull  <glob> [dest]
perch logs  [unit]            journalctl -f on the target
perch shell                   interactive ssh in the remote root

--no-sync / -n                skip the mirror step (read-only pokes)
--tty                         pty mode (ADR-2)
--json                        structured event stream on stdout
--replace                     take the run lock from a live run
```

`cli.py` orchestrates the sequence in §6 and owns no mechanism.

### C8 · Integration (built last)

`.vscode/tasks.json` with a GCC problem matcher; `CLAUDE.md`; a permission
allowlist for the tool's own commands. **Thin** — consumes C7's contract, adds
no behaviour. Do not let editor-specific assumptions leak into C1–C7.

---

## 6. Invocation sequence

Fixed order. Steps 2 and 3 are usually free.

1. **Resolve** — merge project config with ssh config. Fail on an unresolvable target before doing anything else.
2. **Attach** — reuse the control socket, else connect and open one.
3. **Mirror** — push changes, propagate deletions, skip if unchanged. Abort on partial sync (I4).
4. **Claim** — take the run lock. Fail fast if held.
5. **Execute** — start in the remote project root under a tracked process group.
6. **Stream** — stdout/stderr → mapper → terminal and/or structured sink.
7. **Settle** — propagate exit status, auto-pull artifacts on success, release the lock.

---

## 7. Exit-code contract

| Range | Meaning |
|---|---|
| `0` | Remote command succeeded |
| `1–63` | Remote command failed — **passed through unchanged** |
| `64` | Config error (missing, malformed, unresolvable target) |
| `69` | Target unreachable |
| `70` | Internal tool error |
| `73` | Sync failed or incomplete |
| `74` | Run indeterminate — channel closed without a status |
| `75` | Run lock held by another invocation |
| `130` | Interrupted, **and** the remote process group confirmed dead |

`errors.py` owns this mapping. No other module chooses an exit code.

---

## 8. Failure behaviours

Implement each row explicitly; each is testable.

| Failure | Detected by | Required response |
|---|---|---|
| Target unreachable | connect timeout | Exit 69 immediately, naming the host. Never hang. |
| Target reboots mid-run | channel EOF without exit status | Exit 74. Report **indeterminate** — not success, not failure. Next run reconnects unaided. |
| Partial sync | rsync non-zero | Exit 73 before executing. |
| Orphaned remote process | stale lock, live pgid | Reap on next run for the same project, or exit 75 naming the holder. |
| Concurrent invocation | lock held | Exit 75 by default; `--replace` to take it. |
| Target clock wrong | assume always | Content-based sync only (I3). |
| Target disk full | rsync/build error | Surface the remote message verbatim (I8). |

---

## 9. Decisions already made

Do not relitigate these without an ADR. Recorded consequences are load-bearing.

**ADR-1 — No resident agent on the target.** Client-side orchestration over
plain SSH. *Rejected:* a daemon on the Pi — cleaner process control, at the cost
of an install step, client/daemon version skew, a supervision story, and a
resident process on a 2 GB board.

**ADR-2 — Pipes by default, pty on request.** A pty gets signals right and
stream separation wrong; pipes get separation right and signals wrong. The trade
cannot be won, so the mode is chosen by who is watching. *Consequence:* the
explicit signal path in C4 is real work and is not optional.

**ADR-3 — Rewrite paths in flight.** *Rejected:* mirroring the absolute
workspace path on the target so no rewriting is needed — cheaper, but forces the
same username and home layout on both machines and constrains where the project
may live.

**ADR-4 — Explicit sync, not a watcher.** Sync happens as part of a command, so
what ran is always what was on disk when you asked. *Revisit only if* measured
sync time becomes the dominant term in loop latency.

**ADR-5 — The remote command runs inside a small wrapper script, not bare.**
Every command C4 launches is wrapped as `setsid --wait bash -c '...'` with
`trap '' PIPE HUP` set before anything else runs, and (P2-1) `stdbuf -oL -eL`
around the user's own command when available. Three separate, empirically
justified reasons, recorded together because they are one script:

- *`setsid --wait`, not bare `setsid`.* Plain `setsid` forks and returns
  immediately when the caller is already a process-group leader, which can
  let the ssh channel report the command "done" before the detached child
  has run at all. `--wait` (util-linux ≥ 2.32; confirmed on target: 2.41)
  keeps the visible process alive until the real one exits, so the channel's
  lifetime always matches the command's.
- *`trap '' PIPE HUP`.* Confirmed by spike S0-2 (Phase 2 T0): with no trap,
  `kill -9`-ing the **local** ssh client kills the **remote** process group
  within about a second, on its very next write — not because sshd
  deliberately tears it down, but because bash's default SIGPIPE disposition
  terminates it the moment a write to the now-broken channel fails. Isolated
  by testing traps individually: `trap '' PIPE` alone was sufficient;
  `HUP` is kept too as a defensive no-cost second case for a graceful-looking
  disconnect that a differently-configured sshd might turn into SIGHUP
  instead. Without this, I7 ("no orphans") would hold **by accident** for a
  process that writes often, and silently fail for one that doesn't — e.g. a
  single long compile step with no output for 30s, disconnected mid-compile,
  would leave a genuine, undetected orphan. P2-3's explicit kill and P2-4's
  reaping are what actually make I7 hold; this trap is what makes that
  mechanism the only thing responsible for it, rather than an accident of
  timing.
- *`stdbuf -oL -eL`.* gcc, make and python3 detect a pipe on their stdout and
  switch to full block buffering, so output arrives in lumps regardless of
  how correct the host-side reader is. `stdbuf` forces line buffering via an
  `LD_PRELOAD` hook inherited down the process tree. It cannot help a program
  that manages its own buffering internally (rare among the build tools this
  project runs).

*Rejected:* leaving the command bare and accepting that Ctrl-C/disconnect
reliability depends on how chatty the program is — unacceptable, since I7 is
an invariant, not a best-effort.

*Consequence:* every remote command actually executed is
`setsid --wait bash -c 'trap "" PIPE HUP; ...; stdbuf -oL -eL sh -c "<cmd>"'`
(or without the `stdbuf` prefix on a target that lacks it), never `<cmd>`
directly. Nothing here is *installed* — `setsid` and `stdbuf` are part of
util-linux/coreutils, already present on Raspberry Pi OS — so I1 holds.

---

## 10. Build order

Each phase ends in something testable. Do not start a phase before its
predecessor's acceptance test passes.

- **Phase 0 — Walking skeleton.** C1 + naive C3 + naive C4. No streaming, no
  mapping, no locking.
  *Accept:* a local syntax error makes `perch build` exit non-zero.

- **Phase 1 — Transport.** C2, `doctor`.
  *Accept:* second invocation's overhead is a small fraction of the first's;
  power-cycling the target needs no manual step.

- **Phase 2 — Fidelity.** C4 proper; both modes from ADR-2.
  *Accept:* a program printing once per second prints once per second locally;
  Ctrl-C leaves nothing alive on the target (verify with `pgrep` over ssh).

- **Phase 3 — Legibility.** C5, `--json`.
  *Accept:* an agent edits, builds, reads the failure, fixes it and rebuilds,
  with no human input at any step.

- **Phase 4 — Durability.** C6, run lock, every row of §8.
  *Accept:* each failure row can be provoked deliberately and produces its
  stated response.

- **Phase 5 — Integration.** C8.
  *Accept:* one keystroke builds on the target; errors are clickable.

- **Phase 6 — The project that rides on it.** The sensor node. A **consumer** of
  this tool, not part of it. No code for it belongs in `perch/`.

---

## 11. Out of scope

Each would change the architecture rather than extend it. If asked for one, say
that first.

- **A remote IDE.** The editor stays local; moving it is what the 2 GB constraint forbids.
- **Cross-compilation.** The target's own toolchain builds for the target.
- **General bidirectional file sync.** One direction, on demand, one project.
  Bidirectional needs conflict resolution, which needs a policy nobody wants to own.
- **Multiple targets.** Identity, inventory and fan-out — a different system.
- **Deployment.** Installing a service on the Pi permanently is a separate
  concern from running a build there; a shared transport does not make them one tool.

---

## 12. Working rules for the agent

1. **Never build or run locally.** Different architecture, no hardware. Use the
   tool once it exists; before that, `ssh` explicitly and say that you are.
2. **Do not claim something works without running it on the target.** "Should
   compile" is not a result.
3. **One component per change.** A change touching C3 and C5 together usually
   means logic landed in the wrong module.
4. **Tests for C1, C3 and C5 run offline**, against fixtures. Keep them that way
   — they are the fast feedback loop.
5. **When an invariant blocks the simplest implementation, that is the
   invariant working.** Raise it; do not route around it.
6. **Record new decisions as ADRs in §9** with the alternative and what it costs.
