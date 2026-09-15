# Project brief

> **Provenance.** This is the original brief that motivated the project,
> written before `ARCHITECTURE.md` or a line of code existed. Moved here
> from the README in Phase 5 (T10) - the README is the front door for
> someone who has never seen this project, and a pre-implementation brief
> answering questions the code has since settled is development narrative,
> not onboarding material. Kept verbatim as the historical record of *why*,
> with one addition: each of the brief's own "Open questions" now has a
> pointer to how it was actually resolved.

## Context

Development happens on a Mac mini. The target is a Raspberry Pi 4 (aarch64, 2 GB RAM, Raspberry Pi OS) on the same network, reachable over SSH with key authentication.

The Pi cannot host the development environment. It is below the memory floor for Claude Code, and running a language server, an editor backend and a coding agent on 2 GB backed by an SD card degrades every one of them. The Mac has the RAM, the editor and the agent. The Pi has the ARM toolchain, the I2C bus, the GPIO lines and systemd — none of which exist on the Mac.

So the work is split across two machines by necessity, not preference. The question is how to make that split invisible while working.

## Problem

Code is authored on the Mac but can only be built and executed on the Pi. Today that boundary is manual: copy files across, open a second terminal, SSH in, build, read the output, come back, edit, repeat.

That breaks the edit–compile–run loop in three specific ways:

- **The agent is blind to results.** Claude Code writes code on the Mac and has no way to learn whether it compiled. It can only report what should work.
- **Errors don't map back.** A compiler error from the Pi names a path on the Pi (`/home/towhid/projects/x/src/main.c:12`). Neither VS Code's problem matcher nor the agent can jump to that file, because the file they can edit lives at a different path on a different machine.
- **State drifts.** Two copies of the source exist with no defined direction of truth, so "which version did I just build?" becomes a real question.

VS Code Remote-SSH solves the editing side by moving the editor to the Pi — which is exactly the thing that doesn't fit in 2 GB, and which puts the agent on the wrong machine.

## What "as if local" has to mean

The phrase needs a testable definition. The remote execution is transparent when all six hold:

| Property | Requirement |
|---|---|
| Streaming | Output appears line by line as it is produced, not buffered until the remote process exits |
| Ordering | stdout and stderr interleave in the order the target emitted them |
| Exit codes | The target's exit status is the local command's exit status — a failed build fails locally |
| Signals | Ctrl-C terminates the process on the target, not just the local wrapper |
| Path mapping | Diagnostics referencing target paths resolve to the corresponding file in the local workspace |
| Latency | Per-invocation overhead is small enough that the loop feels immediate — connection setup is amortized, not repaid each time |

## Goals

- A single command on the Mac synchronizes the workspace to the Pi, builds there, runs there, and returns output and exit status to the local terminal.
- The local workspace is the sole source of truth; the copy on the Pi is derived and disposable.
- Compiler and runtime diagnostics are actionable from the local machine — clickable in VS Code, and directly usable by an agent to locate and edit the right file.
- Artifacts produced on the target (binaries, logs, captures) can be retrieved into the workspace on request.
- The whole loop is usable from a non-interactive agent session: no prompts, no TTY requirement, no human in the middle.
- A target reboot or a dropped link is recovered on the next invocation without manual intervention.

## Non-goals

- Not a remote IDE. The editor stays local.
- Not cross-compilation. The Pi's own toolchain builds for the Pi.
- Not general-purpose file sync. One direction, on demand, scoped to one project.
- Not device fleet management, provisioning, or OTA updates. One target.
- Not a replacement for SSH access — an interactive shell on the Pi stays available for the things that genuinely need one.

## Constraints

- **Target:** aarch64, 2 GB RAM, SD-card storage. Nothing heavy may be installed on it; it runs the project's own dependencies and nothing else.
- **Network:** LAN only, intermittently available, no inbound connections to the Pi beyond SSH. The Pi's hostname resolves via mDNS; its IP is not stable.
- **Hardware coupling:** code touching I2C, GPIO or systemd is only meaningful on the target. Hardware access is by group membership (`i2c`, `gpio`), not root.
- **Clock:** the Pi has no battery-backed RTC, so timestamps before NTP sync are unreliable.

## Acceptance criteria

The system is working when, from the Mac, with no second terminal open:

- Introducing a syntax error locally and running the build command produces the Pi's gcc diagnostics, a non-zero exit status, and a clickable path into the local file.
- A program that prints once per second shows one line per second locally, not a burst at the end.
- Ctrl-C during a long-running remote program leaves no orphaned process on the Pi.
- Deleting a source file locally removes it from the target on the next build.
- Power-cycling the Pi requires no action beyond re-running the command.
- Claude Code, running unattended, can edit a file, build, read the failure, fix it, and build again — with no human input at any step.

## Open questions (as of this brief) — and how each was actually resolved

- **Sync trigger: explicit per command, or a watcher keeping the target continuously current?** Explicit — recorded as ADR-4 in `architecture/ARCHITECTURE.md` §9. Revisit only if measured sync time becomes the dominant term in loop latency.
- **Path mapping: rewrite diagnostics in transit, or mirror the workspace path on the target so no rewriting is needed?** Rewrite in transit — ADR-3. Mirroring the absolute path was rejected: it forces the same username and home layout on both machines and constrains where the project may live. `perch/diagnostics.py` (C5, Phase 3) is the result.
- **Concurrency: what happens when a build is triggered while a previous run is still executing on the target?** A run lock at `<remote_root>/.perch/run.lock`, holding the live invocation's process group id — a second invocation fails fast (exit 75, naming the holder) unless `--replace`. Landed in Phase 4 (P4-1); see `docs/PHASE-4-BUILD-AND-RUN.md`.

mDNS resolution (mentioned above as the Pi's expected hostname mechanism) turned out not to work reliably on the actual development LAN — the shipped setup uses a literal `HostName` in `~/.ssh/config` instead (see `docs/PHASE-1-BUILD-AND-RUN.md`'s risk register entry and the README's own prerequisites).
