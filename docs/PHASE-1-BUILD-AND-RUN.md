# Phase 1 — Build and Run

> **Provenance.** This document did not exist before the code, as the
> convention set by `docs/PHASE-0-BUILD-AND-RUN.md` intends. No separate
> author supplied it, so it was drafted by the implementing agent directly
> from `ARCHITECTURE.md` (§5/C2, §6, §7, §8) and `DEVELOPMENT-PLAN.md`
> (P1-1..P1-4) *before* writing any Phase 1 code, so it could still serve as
> a spec-first target. Anywhere it fixes a concrete number or output shape
> that the source documents leave open, that is this document's own decision,
> not a derived fact — those spots are marked **(chosen here)**. Treat this
> whole file as provisional until it's been read and corrected.

Phase 1 gate, verbatim from the working prompt:

1. `tools/bench-latency.sh pi` shows warm overhead a small fraction of cold.
2. `perch doctor` produces the full report against the real Pi.
3. The unreachable-target procedure below exits 69 within the timeout.
4. `perch doctor` still works after `ssh pi 'sudo reboot'` and a wait, with no
   manual step.
5. Every command in this document has been executed and works as written.

## What Phase 1 adds

One new module, one new verb, one new script:

```
perch/session.py     C2 — owns every ssh invocation in the codebase
perch doctor          new verb — target identity, toolchain, device files
tools/bench-latency.sh — cold vs warm invocation overhead
```

Nothing else changes shape. `mirror.py` and `executor.py` stop building their
own `["ssh", ...]` argv and instead ask a `Session` for one. No streaming, no
signals, no run lock, no JSON — those are still later phases.

## Session manager (C2) — design decisions this doc fixes

`ARCHITECTURE.md` requires `ControlMaster=auto`, a `ControlPath` under
`~/.ssh/`, `ControlPersist`, and an explicit `ConnectTimeout`, all passed by
the tool itself rather than relied on from the user's own `~/.ssh/config`.
Concrete values:

| Setting | Value | Why **(chosen here)** |
|---|---|---|
| `ConnectTimeout` | `5` seconds | LAN, a known static IP, no mDNS involved — 5s is generous for a live target and short enough that "the Pi is off" resolves quickly instead of waiting out a ~75s OS-level TCP default. |
| `ControlPersist` | `10m` | Matches the order of magnitude a normal edit-build-run session runs in; long enough that consecutive commands in one sitting share the master. |
| Reconnect attempts | `2` total, `1s` backoff between | "Bounded" per P1-1; worst case for a genuinely dead target is ~`2 × ConnectTimeout + backoff` ≈ 11s — still far under the TCP default, and the retry exists for the boot-window case (P1-2), not to multiply the wait on a target that is simply off. |
| `ControlPath` | `~/.ssh/perch-<sha256(alias)[:16]>.sock` | A hash of the alias, not `%r@%h:%p` — that expands to `user@host:port`, which combined with a long `~/.ssh/` can exceed the ~104-byte `AF_UNIX` path cap on macOS. A short fixed-width hash never does, regardless of alias or username length. |
| `BatchMode` | `yes` | I9: a hung passphrase or host-key prompt is exactly the kind of interactive stall a non-interactive agent must never hit. |

Every one of these is a `-o` flag on the ssh command line, which overrides a
same-named directive in `~/.ssh/config` — so the tool's own multiplexing
setup does not depend on (and is not satisfied by coincidence from) whatever
the user's ssh config happens to already have. This must hold even with the
user's own `ControlMaster`/`ControlPath`/`ControlPersist` lines in
`~/.ssh/config` commented out.

**Classification (P1-2).** ssh reports its own connection-level failures as
exit 255. A remote command that itself happens to exit 255 is rare but legal
(I6) and must not be misreported as a tool failure. The discriminator: ssh's
*own* diagnostics land on stderr *before* the remote shell ever runs, so for
a genuine connection failure stderr contains nothing else. Patterns
classified **(chosen here)**, each ending in exit 69:

| Stderr contains | Classified as | Hint appended |
|---|---|---|
| `Connection timed out`, `Operation timed out`, `Connection refused`, `No route to host`, `Could not resolve hostname` | unreachable | "check the target is powered on and on the network" |
| `Permission denied` | auth failure | "check ssh-agent has the right key loaded" |
| `Host key verification failed`, `REMOTE HOST IDENTIFICATION HAS CHANGED` | host key mismatch | "run `ssh-keygen -R <alias>` if the target was reimaged, then retry" |

Anything else at exit 255 is treated as the remote command's own exit status
and passed through unchanged, per I6 — this is the same tradeoff already
recorded in `errors.py` for the 127/130 collision.

Only the *unreachable* classification retries (the target might be mid-boot);
auth failure and host key mismatch do not, since retrying changes nothing.

**Exit code.** `ARCHITECTURE.md` §7 has exactly one code for "can't talk to
the target" — 69 — and no separate code for auth or host-key failures. All
three classifications above exit 69; only the printed message differs. If
this reads as wrong once you've seen it in practice, say so — it's the kind
of thing that becomes an ADR.

**Never a traceback.** Every connection-level failure raises a `PerchError`
subclass with a one-line message; `cli.py`'s existing `except
errors.PerchError` handler prints it and returns the mapped code. No new
exception type should ever reach that handler unclassified.

## `perch doctor`

Read-only. Does **not** sync — resolves config to learn `host`, then probes
the target directly. A project need not even have valid `[commands]` for
`doctor` to run.

The probe is one POSIX `sh` script sent over `ssh <alias> sh -s` via stdin,
not interpolated into the command line — an interpolated script has to
survive quoting through two shells (local shell building the ssh argv, then
the remote shell parsing it) and that is exactly the kind of thing that looks
fine until a path has a space in it.

Missing tools or device files report as `MISSING` and do **not** affect the
exit code — a Pi with no `libgpiod` is a fact `doctor` reports, not a failure
`doctor` has. `doctor` exits non-zero only when the target itself is
unreachable (the same 69 as everywhere else).

Output shape **(chosen here — DEVELOPMENT-PLAN.md names the fields, not the
layout)**:

```
$ perch doctor
target             pi
reachable          yes
control socket     ~/.ssh/perch-3f1a9c2e8b47d016.sock (already live)

os                 Debian GNU/Linux 13 (trixie)
kernel             6.6.51+rpt-rpi-v8
architecture       aarch64
memory             1.9Gi total, 1.4Gi available
disk (/)           24G free of 29G

gcc                gcc (Debian 12.2.0-14) 12.2.0
make               GNU Make 4.3
python3            Python 3.11.2
rsync              rsync  version 3.2.7  protocol version 31
libgpiod           present
/dev/i2c-*         /dev/i2c-1
/dev/gpiochip*     /dev/gpiochip0 /dev/gpiochip4

host rsync         openrsync: protocol version 29   (this Mac, from S0-1)
```

A missing item prints as e.g. `gcc                MISSING` in place of the
version string; `/dev/i2c-*`/`/dev/gpiochip*` print `MISSING` when the glob
matches nothing.

## Unreachable-target procedure

The real Pi cannot be used for this — it's genuinely reachable. Point a
scratch project at an ssh alias whose address nothing answers on:

```sh
cat >> ~/.ssh/config <<'EOF'

Host pi-unreachable
    HostName 10.0.0.250
    User towhid
EOF
```

`10.0.0.250` must be an address on your LAN with no host listening (confirm
with `arp -a` / a quick `ping -c1 -t1` showing no reply — a silently dropped
SYN is exactly the case `ConnectTimeout` exists for; a `Connection refused`
from a live-but-closed port is the auth/unreachable classifier's other path
and both are acceptable here). Then:

```sh
mkdir -p /tmp/perch-unreachable && cd /tmp/perch-unreachable
cat > .perch.toml <<'EOF'
host = "pi-unreachable"
remote_root = "x"
EOF
time perch doctor
echo "exit: $?"
```

Expected: a one-line message naming `pi-unreachable`, no Python traceback,
exit 69, and a wall-clock time on the order of the configured `ConnectTimeout`
× retry count — **not** the OS default (tens of seconds to a few minutes).
Remove the scratch `Host pi-unreachable` block afterward.

## Reboot procedure (gate item 4)

```sh
ssh pi 'sudo reboot'
# wait ~30-45s for the Pi to come back
perch doctor   # from any project pointed at host = "pi"
```

No manual step in between — no `ssh -O exit`, no deleting the stale control
socket by hand. `ControlMaster=auto` transparently starts a fresh master when
the old one's socket is dead; that's the property this step is checking.

## Latency harness (P1-4)

```sh
tools/bench-latency.sh pi
```

Kills any existing control master for the alias first (so "cold" is
honest), times one cold invocation, then times N warm invocations
back-to-back and reports the median plus the warm/cold ratio. It exists
because it *is* the phase gate — "feels fast" is not a number.

## Offline test suite

Unchanged expectation from Phase 0 — still runs with no target reachable and
with `ssh`/`rsync` absent from `PATH`:

```sh
.venv/bin/python -m unittest discover -s tests -t .
```
