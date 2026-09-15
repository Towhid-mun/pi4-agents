"""C4 - remote execution: streaming, process groups, signals, pty.

The component where "roughly right" is a bug (ARCHITECTURE.md §5/C4). Carries
I5 (streaming, P2-1), the process-group/sentinel protocol (P2-2), signal
forwarding (P2-3), stale-group reaping (P2-4) and now pty mode (P2-5) - I7 in
full, and ADR-2's second mode. See ADR-5 for the wrapper script every
pipe-mode command actually runs inside, and why.

Two modes:

  run(..., tty=False)  Pipes (default). Everything above - selectors,
                        process group, signal forwarding, reaping.

  run(..., tty=True)   PTY. `ssh -tt`; the kernel's own pty line discipline
                        delivers signals to the remote foreground process
                        group natively, so none of the marker/pgid machinery
                        above is needed for it. The cost (ADR-2): stdout and
                        stderr merge into one stream, so this mode cannot be
                        combined with a structured (--json) sink.
"""

import hashlib
import json
import os
import secrets
import selectors
import shlex
import subprocess
import sys
import termios
import time
import tty as tty_module
from dataclasses import dataclass
from pathlib import Path

import signal as signal_module

from perch import diagnostics
from perch.config import Config
from perch.errors import InternalError, RunLockHeld, TargetUnreachable
from perch.session import Session

CHUNK_SIZE = 65536

# How long we wait, per escalation step, before giving up and reporting that
# the kill could not be confirmed. Bounded, per P2-3 - this is not allowed to
# hang forever.
TERM_GRACE_SECONDS = 5.0
KILL_GRACE_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 0.3

STATE_DIR = Path.home() / ".perch" / "state"


@dataclass
class RunResult:
    exit_code: int
    interrupted: bool = False
    indeterminate: bool = False  # channel closed without a status - target rebooted


def remote_command(remote_root: str, command: str) -> str:
    """The plain 'cd root && cmd' string, with no wrapper - the inner command
    the wrapper below runs once it has set up the process group."""
    return f"cd {shlex.quote(remote_root)} && {command}"


def join(argv: list[str]) -> str:
    """Turn a local argv into one safely quoted remote shell string."""
    return shlex.join(argv)


# --------------------------------------------------------------------------
# The wrapper (ADR-5, P2-2). Pure string-building - testable with no target.
# --------------------------------------------------------------------------

def new_marker_token() -> str:
    """A high-entropy token, unique per run, so a marker line cannot be
    forged by a program that happens to print something marker-shaped."""
    return secrets.token_hex(16)


def pgid_marker(token: str) -> str:
    return f"__PERCH_{token}_PGID_"


def exit_marker(token: str) -> str:
    return f"__PERCH_{token}_EXIT_"


def classify_line(line: str, token: str) -> tuple[str, str, str] | None:
    """(kind, value, leading) if a marker for this run's token appears in
    `line`, else None for an ordinary line of the program's own output.

    Searches for the marker anywhere in the line, not just at position 0:
    the marker is printed with a plain trailing newline and no separator of
    its own, so if the program's last line of output had no trailing newline,
    the marker can end up glued to the end of it. `leading` is that genuine
    partial content (empty in the overwhelmingly common case where the
    marker starts the line) - the caller should still emit it.
    """
    for marker, kind in ((pgid_marker(token), "pgid"), (exit_marker(token), "exit")):
        idx = line.find(marker)
        if idx != -1:
            return (kind, line[idx + len(marker):], line[:idx])
    return None


def build_wrapped_command(remote_root: str, command: str, token: str) -> str:
    """The exact remote command line. Pure - runs nothing.

    setsid --wait bash -c '<payload>' - see ARCHITECTURE.md ADR-5 for why
    each piece of the payload is there (--wait, the PIPE/HUP trap, stdbuf).

    Also records $PGID into the run lock's pgid file (P4-1), the same
    computation the marker protocol already needed, spent here for free:
    no extra ssh round trip, and no window where the lock directory exists
    but names nobody - the write happens inside the one script that already
    holds the lock by construction (run() acquires it before this is ever
    launched).
    """
    inner_quoted = shlex.quote(remote_command(remote_root, command))
    payload = (
        'trap "" PIPE HUP\n'
        'PGID=$(ps -o pgid= -p $$ | tr -d "[:space:]")\n'
        f'{_write_lock_pgid_snippet(remote_root)}\n'
        f'printf "%s\\n" "{pgid_marker(token)}$PGID"\n'
        "if command -v stdbuf >/dev/null 2>&1; then\n"
        f"  stdbuf -oL -eL sh -c {inner_quoted}\n"
        "else\n"
        f"  sh -c {inner_quoted}\n"
        "fi\n"
        "STATUS=$?\n"
        f'printf "%s\\n" "{exit_marker(token)}$STATUS"\n'
        'exit "$STATUS"\n'
    )
    return f"setsid --wait bash -c {shlex.quote(payload)}"


# --------------------------------------------------------------------------
# P2-4: stale process-group record. NOT the run lock (P4-1) - this only ever
# reacts to a CONFIRMED orphan of THIS project's own last recorded pgid. It
# never blocks or even notices a legitimately concurrent invocation; that
# distinction (and refusing with exit 75) is P4-1's job.
# --------------------------------------------------------------------------

def _project_key(cfg: Config) -> str:
    digest = hashlib.sha256(f"{cfg.host}|{cfg.remote_root}".encode()).hexdigest()[:16]
    return digest


def pgid_record_path(cfg: Config) -> Path:
    return STATE_DIR / _project_key(cfg) / "pgid"


def _read_pgid_record(cfg: Config) -> int | None:
    try:
        text = pgid_record_path(cfg).read_text().strip()
    except FileNotFoundError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _write_pgid_record(cfg: Config, pgid: int) -> None:
    path = pgid_record_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pgid))


def _clear_pgid_record(cfg: Config) -> None:
    pgid_record_path(cfg).unlink(missing_ok=True)


def reap_stale_group(cfg: Config, session: Session) -> None:
    """If a previous run for this project left a live group, kill it.

    Called at the start of every run. Silent no-op when there is nothing to
    reap (the overwhelmingly common case). Test by SIGKILLing the host
    process mid-run, which leaves an orphan by construction (ADR-5 makes the
    remote group immune to the connection simply dropping).
    """
    pgid = _read_pgid_record(cfg)
    if pgid is None:
        return
    try:
        alive = _group_alive(session, pgid)
    except TargetUnreachable:
        return  # can't check right now; the record is left for next time
    if not alive:
        _clear_pgid_record(cfg)
        return
    print(
        f"perch: reaping an orphaned process group ({pgid}) left by a "
        f"previous run of this project on {cfg.host}",
        file=sys.stderr,
    )
    if _terminate_group(session, pgid):
        _clear_pgid_record(cfg)
    else:
        raise InternalError(
            f"could not reap orphaned group {pgid} on {cfg.host} - it survived "
            f"SIGKILL. Investigate on the target directly (ps -o pgid={pgid})."
        )


# --------------------------------------------------------------------------
# Kill + confirm (P2-3). Always a SEPARATE connection (a fresh
# session.run_capturing() call) from whatever long-running Popen is being
# killed - the existing one may be exactly what is wedged. Multiplexing
# makes this cheap.
# --------------------------------------------------------------------------

def _group_alive(session: Session, pgid: int) -> bool:
    """Raises TargetUnreachable if we can't even check - never guess yes/no
    silently on a connectivity failure; let the caller decide what that means."""
    completed = session.run_capturing(f"pgrep -g {pgid}")
    return completed.returncode == 0


def _send_signal(session: Session, pgid: int, sig_name: str) -> None:
    try:
        session.run_capturing(f"kill -{sig_name} -- -{pgid}")
    except TargetUnreachable:
        pass  # the alive()-polling below will surface the same problem


def _poll_until_dead(session: Session, pgid: int, grace_seconds: float, should_escalate) -> bool:
    """False means "stop waiting" - either the deadline passed, or
    `should_escalate()` says a second Ctrl-C arrived. Checked every poll
    tick (~POLL_INTERVAL_SECONDS), not just once before this call started -
    a naive one-time check would block for the FULL grace period even after
    a second signal, since this function is what's running when it arrives.
    """
    deadline = time.monotonic() + grace_seconds
    while True:
        try:
            if not _group_alive(session, pgid):
                return True
        except TargetUnreachable:
            pass  # keep trying until the deadline; report the honest outcome then
        if should_escalate() or time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL_SECONDS)


def _terminate_group(
    session: Session,
    pgid: int,
    *,
    escalate_immediately: bool = False,
    should_escalate=lambda: False,
) -> bool:
    """TERM, wait, escalate to KILL if needed. True only once confirmed dead.

    `should_escalate` is re-checked throughout the TERM wait (not just at the
    start) so a second Ctrl-C during that wait cuts it short immediately
    rather than being noticed only once the first attempt's grace period
    elapses on its own.
    """
    if not escalate_immediately:
        _send_signal(session, pgid, "TERM")
        if _poll_until_dead(session, pgid, TERM_GRACE_SECONDS, should_escalate):
            return True
    _send_signal(session, pgid, "KILL")
    return _poll_until_dead(session, pgid, KILL_GRACE_SECONDS, lambda: False)


# --------------------------------------------------------------------------
# P4-1: the run lock. Mutual exclusion between INVOCATIONS (any host, any
# terminal) - do not conflate with P2-4's reap_stale_group above, which is
# this host's own crash recovery for its own last run. The lock lives
# entirely on the TARGET at <remote_root>/.perch/run.lock, so it also
# excludes a concurrent invocation from a different machine.
#
# T0 (Phase 4) confirmed the trap the ticket warned about is real: without
# .perch/ in BUILTIN_EXCLUDES (config.py), the mirror that runs before every
# command deletes this very lock out from under itself via --delete, on
# every invocation. Fixed there, not here - this module trusts the exclude
# already holds.
#
# Atomicity (trap 1): `mkdir` is the sole primitive. `test -e || write` is
# two round trips with a race in between; mkdir either creates the directory
# or fails, with no window, and nothing here tries to be cleverer than that.
# --------------------------------------------------------------------------

LOCK_STALE_RETRY_DELAY = 0.5  # one short wait for a just-claimed lock's pgid file to appear
LOCK_CLAIM_ATTEMPTS = 3       # bounded retries after reclaiming a confirmed-stale lock


def lock_dir(remote_root: str) -> str:
    return f"{remote_root.rstrip('/')}/.perch/run.lock"


def lock_pgid_path(remote_root: str) -> str:
    return f"{lock_dir(remote_root)}/pgid"


def _claim_command(remote_root: str) -> str:
    """Atomically create the lock directory, or report who already holds it.

    `mkdir -p .perch` first (idempotent - safe even if two invocations race
    on it, since -p treats "already exists" as success, not failure) so the
    parent exists on a target that has never taken this lock before. THEN
    the real exclusion boundary: a bare `mkdir` on the lock dir itself,
    which is what mkdir_p is not - either it creates the directory (nobody
    else has it) or it fails (somebody does), atomically, no window.
    On failure, print whatever pgid the current holder (if any) has managed
    to record - see lock_pgid_path's write-after-claim race, handled by the
    caller, not here.
    """
    root = shlex.quote(remote_root.rstrip("/"))
    ldir = shlex.quote(lock_dir(remote_root))
    pgid_path = shlex.quote(lock_pgid_path(remote_root))
    return (
        f"mkdir -p {root}/.perch 2>/dev/null; "
        f"if mkdir {ldir} 2>/dev/null; then echo CLAIMED; "
        f"else cat {pgid_path} 2>/dev/null || true; fi"
    )


def _release_command(remote_root: str) -> str:
    """Unconditionally remove the lock directory and everything in it."""
    return f"rm -rf {shlex.quote(lock_dir(remote_root))}"


def _write_lock_pgid_snippet(remote_root: str) -> str:
    """A shell fragment that records $PGID into the lock file, best-effort.

    Meant to be spliced into a script that has already computed $PGID -
    never a standalone command. Failure here (e.g. the lock directory
    vanished under us somehow) must not abort the run the lock was
    protecting - hence `|| true`.
    """
    return f'printf "%s" "$PGID" > {shlex.quote(lock_pgid_path(remote_root))} 2>/dev/null || true'


def _holder_pgid(session: Session, remote_root: str) -> int | None:
    """Read the lock's recorded pgid, tolerating the brief window between a
    competitor's mkdir succeeding and its own pgid write landing."""
    completed = session.run_capturing(f"cat {shlex.quote(lock_pgid_path(remote_root))} 2>/dev/null")
    text = completed.stdout.strip()
    try:
        return int(text)
    except ValueError:
        return None


def acquire_lock(cfg: Config, session: Session, *, replace: bool = False) -> None:
    """Claim the run lock for this project, or raise RunLockHeld (exit 75).

    Trap 2 (staleness): a lock whose recorded pgid is not found by
    `pgrep -g` is dead - the process that held it is gone, most likely
    because its host machine was killed before it could release (trap 3
    exists for exactly that). A dead holder's lock is reclaimed
    automatically, no --replace needed - --replace is only for a holder
    confirmed ALIVE (trap 4).

    Trap 4 (--replace): never removes a live holder's lock directly. It
    kills that process group first (reusing P2-3's _terminate_group),
    confirms it is dead, and only then proceeds exactly as the ordinary
    stale-reclaim path would.
    """
    completed = session.run_capturing(_claim_command(cfg.remote_root))
    output = completed.stdout.strip()
    if output == "CLAIMED":
        return

    # Lock dir already existed. `output` is either the holder's recorded
    # pgid, or empty - the latter meaning a competitor's mkdir landed but its
    # own pgid write has not yet, a race this narrow only because both
    # happen inside the same already-atomic wrapper script (see
    # build_wrapped_command). One short, bounded wait, then treat it as
    # genuinely unreadable rather than retry forever.
    pgid = _parse_pgid(output)
    if pgid is None:
        time.sleep(LOCK_STALE_RETRY_DELAY)
        pgid = _holder_pgid(session, cfg.remote_root)
    if pgid is None:
        raise RunLockHeld(
            f"{cfg.host}: run lock is held (holder's process group id has not "
            f"been recorded yet - another invocation is still starting up). "
            f"Retry, or use --replace to take over."
        )

    alive = _group_alive(session, pgid)
    if alive and not replace:
        raise RunLockHeld(
            f"{cfg.host}: run lock is held by process group {pgid}. "
            f"Use --replace to take over (this kills that process group first)."
        )
    if alive and replace:
        if not _terminate_group(session, pgid):
            raise InternalError(
                f"could not confirm process group {pgid} dead on {cfg.host} while "
                f"honouring --replace - it survived SIGKILL. Investigate on the "
                f"target directly (ps -o pgid={pgid})."
            )
        # Fall through: holder is now confirmed dead, same as the ordinary
        # stale case below - never delete a lock without that confirmation.

    _reclaim_stale_lock(session, cfg.remote_root)


def _reclaim_stale_lock(session: Session, remote_root: str) -> None:
    """Remove a lock whose holder is confirmed dead, then re-attempt the
    atomic claim. Bounded retries: the removal + re-claim is two round
    trips, not one, so a second invocation reclaiming at the same moment can
    still race here - loop rather than assume the first retry wins."""
    for attempt in range(1, LOCK_CLAIM_ATTEMPTS + 1):
        session.run_capturing(_release_command(remote_root))
        completed = session.run_capturing(_claim_command(remote_root))
        if completed.stdout.strip() == "CLAIMED":
            return
        # Someone else's claim won the re-attempt. Only worth looping if
        # THEIR holder is also already dead (another stale lock, unlikely
        # but possible under heavy contention); otherwise this is a live,
        # legitimate concurrent claim and belongs to acquire_lock's normal
        # "held" path, not a retry loop here.
        pgid = _parse_pgid(completed.stdout.strip())
        if pgid is not None and _group_alive(session, pgid):
            raise RunLockHeld(
                f"run lock was reclaimed by another invocation (process group "
                f"{pgid}) before this one could take it."
            )
    raise InternalError(
        f"could not claim the run lock at {lock_dir(remote_root)} after "
        f"{LOCK_CLAIM_ATTEMPTS} attempts - persistent contention or a target-side "
        f"problem removing/creating it."
    )


def _parse_pgid(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None


def release_lock(cfg: Config, session: Session) -> None:
    """Unconditionally remove the lock directory (trap 3: every path -
    success, failure, signal, exception - releases; callers wrap this in
    try/finally, never a conditional cleanup).

    Best-effort against connectivity: if the target is unreachable at this
    point (e.g. it rebooted mid-run - the indeterminate case), there is
    nothing more this invocation can do about its own lock. It is left for
    the NEXT invocation's staleness check to reclaim, exactly like P2-4's
    stale pgid record - never let a release failure mask or override the
    run's real outcome.
    """
    try:
        session.run_capturing(_release_command(cfg.remote_root))
    except TargetUnreachable:
        pass


# --------------------------------------------------------------------------
# Signal state. The OS handler only ever does this - a plain counter bump.
# All the actual work (opening a connection, sending a kill, polling) happens
# in the main read loop, never inside the handler itself (re-entrancy).
# --------------------------------------------------------------------------

class _SignalState:
    def __init__(self):
        self.count = 0

    def bump(self, signum, frame):
        self.count += 1


def _run_pipes(cfg: Config, command: str, session: Session, *, json_mode: bool = False) -> RunResult:
    """Run `command` in the remote project root, streaming output as it arrives.

    Reads stdout and stderr with `selectors` (P2-1) so neither stream can
    starve or deadlock the other. Marker lines are stripped from user-visible
    output and used to capture the process group id and, from the exit
    sentinel, the command's REAL exit status (P2-2) - not ssh's own return
    code, which is ambiguous between a genuine remote 255 and a connection
    failure.

    A local SIGINT or SIGTERM signals the captured group from a SEPARATE
    connection, waits for confirmation it is dead, then reports interrupted -
    never claiming that without confirmation (I7). A second signal escalates
    to SIGKILL immediately rather than waiting out the first grace period.

    Before any of that: if a previous run for this project left a live
    group (this host process was SIGKILLed mid-run, which the ADR-5 wrapper
    survives on purpose), reap it first (P2-4).

    json_mode (P3-3): every line becomes a `{"t": "stdout"/"stderr", ...}`
    event, a recognized primary diagnostic ALSO gets its own `diag` event,
    and a terminal `exit` event is always emitted before returning - in
    every outcome, not just a clean one. All of it goes on stdout, which in
    this mode carries nothing else at all; anything perch itself needs to
    say (P2-4's reaping notice, P2-6's indeterminate/interrupted wording)
    already goes to stderr regardless of mode.
    """
    reap_stale_group(cfg, session)
    resolver = diagnostics.PathResolver(cfg.local_root, cfg.remote_root)

    token = new_marker_token()
    proc = session.popen(build_wrapped_command(cfg.remote_root, command, token))

    os.set_blocking(proc.stdout.fileno(), False)
    os.set_blocking(proc.stderr.fileno(), False)

    sig_read_fd, sig_write_fd = os.pipe()
    os.set_blocking(sig_read_fd, False)
    os.set_blocking(sig_write_fd, False)  # set_wakeup_fd requires this on the write end too
    previous_wakeup_fd = signal_module.set_wakeup_fd(sig_write_fd)

    state = _SignalState()
    old_int = signal_module.signal(signal_module.SIGINT, state.bump)
    old_term = signal_module.signal(signal_module.SIGTERM, state.bump)

    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ, data="stdout")
    sel.register(proc.stderr, selectors.EVENT_READ, data="stderr")
    sel.register(sig_read_fd, selectors.EVENT_READ, data="signal")

    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    open_streams = {"stdout", "stderr"}
    captured = {"pgid": None, "exit_code": None}
    handled_signal_count = 0
    outcome = "normal"  # normal | interrupted_confirmed | interrupted_unconfirmed
    broken = {"stdout": False, "stderr": False}  # our OWN downstream consumer went away

    def for_display(line: str) -> tuple[str, diagnostics.Diagnostic | None]:
        """C5: strip ANSI, track the compiler's remote cwd (P3-2's
        `make -C subdir` trap), and rewrite a recognized diagnostic's path
        to a local one. A line that is not a diagnostic - the overwhelming
        common case - comes back with nothing touched but the ANSI strip,
        and no diagnostic. The diagnostic, when present, is what json_mode
        turns into a `diag` event (P3-3) - plain-text mode ignores it.
        """
        clean = diagnostics.strip_ansi(line)
        resolver.observe(clean)
        diag = diagnostics.parse_line(clean)
        if diag is None:
            return clean, None
        return diagnostics.rewrite_line(clean, diag, resolver), diag

    def write_json(event: dict) -> None:
        # Requirement 1: in json_mode EVERY event goes on stdout regardless
        # of which logical stream (stdout/stderr) its line came from -
        # "stdout carries ONLY JSON" means one stream, not two. Tracked
        # under the same "stdout" broken-pipe flag as plain-text mode uses.
        if broken["stdout"]:
            return
        try:
            sys.stdout.write(json.dumps(event) + "\n")
            sys.stdout.flush()  # requirement 2: flush every event
        except BrokenPipeError:
            broken["stdout"] = True
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            os.close(devnull)

    def emit(stream: str, line: str, *, had_newline: bool = True) -> None:
        display, diag = for_display(line)
        if json_mode:
            write_json(diagnostics.event_for_stream(stream, display))
            if diag is not None and diagnostics.is_event(diag):
                resolved = resolver.resolve(diag.file) or diag.file
                write_json(diagnostics.diag_event(diag, resolved))
            return
        # Trap 3: our own stdout/stderr are block-buffered when not a
        # terminal. Flush every line explicitly.
        if broken[stream]:
            return
        out = sys.stdout if stream == "stdout" else sys.stderr
        try:
            # had_newline=False for a genuine final unterminated line (I8:
            # fabricating a trailing newline the source never had is still
            # corrupting the output, just subtly - caught live against the
            # noise fixture, whose last line deliberately has none).
            out.write(display + ("\n" if had_newline else ""))
            out.flush()
        except BrokenPipeError:
            # Our own local consumer (e.g. `perch build | head`) went away.
            # That is not a failure of the remote command - keep draining
            # and let the real exit status settle normally, just stop trying
            # to display output nobody is reading. Also redirect the real fd
            # to /dev/null so the interpreter's own flush at shutdown doesn't
            # hit the same broken pipe and print a scary "Exception ignored".
            broken[stream] = True
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, out.fileno())
            os.close(devnull)

    def handle_line(stream: str, raw: bytes, *, had_newline: bool) -> None:
        line = raw.decode("utf-8", errors="replace")
        kind = classify_line(line, token)
        if kind is None:
            emit(stream, line, had_newline=had_newline)
            return
        marker_kind, value, leading = kind
        if leading:
            # A genuine final partial line, glued to the marker - the marker
            # itself supplied the only \n on the wire, so the user's own
            # content here never actually had one (that is WHY it got glued
            # in the first place; a properly terminated last line would have
            # been its own, separate, ordinary line instead).
            emit(stream, leading, had_newline=False)
        if marker_kind == "pgid":
            try:
                captured["pgid"] = int(value)
            except ValueError:
                emit(stream, line, had_newline=had_newline)  # garbled marker - show it rather than trust it
                return
            # Recorded immediately, not deferred to a clean exit - if THIS
            # host process gets SIGKILLed a moment from now, the next
            # invocation still has something to reap (P2-4).
            _write_pgid_record(cfg, captured["pgid"])
        elif marker_kind == "exit":
            try:
                captured["exit_code"] = int(value)
            except ValueError:
                pass

    def drain(stream: str, fileobj) -> None:
        try:
            chunk = os.read(fileobj.fileno(), CHUNK_SIZE)
        except BlockingIOError:
            return
        if chunk == b"":
            sel.unregister(fileobj)
            open_streams.discard(stream)
            if buffers[stream]:
                # Reached EOF with no trailing \n for this leftover - by
                # definition it never had one on the wire (if it had, it
                # would already have been split out and handled above).
                handle_line(stream, bytes(buffers[stream]), had_newline=False)
                buffers[stream].clear()
            return
        buffers[stream].extend(chunk)
        while True:
            idx = buffers[stream].find(b"\n")
            if idx == -1:
                break
            handle_line(stream, bytes(buffers[stream][:idx]), had_newline=True)
            del buffers[stream][: idx + 1]

    try:
        while open_streams:
            for key, _ in sel.select(timeout=0.2):
                if key.data == "signal":
                    try:
                        os.read(sig_read_fd, 4096)
                    except BlockingIOError:
                        pass
                else:
                    drain(key.data, key.fileobj)

            if state.count > handled_signal_count and captured["pgid"] is not None:
                baseline = handled_signal_count
                handled_signal_count = state.count
                escalate_now = state.count >= 2
                confirmed = _terminate_group(
                    session,
                    captured["pgid"],
                    escalate_immediately=escalate_now,
                    should_escalate=lambda: state.count > baseline + 1,
                )
                outcome = "interrupted_confirmed" if confirmed else "interrupted_unconfirmed"
                break
    finally:
        signal_module.signal(signal_module.SIGINT, old_int)
        signal_module.signal(signal_module.SIGTERM, old_term)
        signal_module.set_wakeup_fd(previous_wakeup_fd)
        sel.close()
        os.close(sig_read_fd)
        os.close(sig_write_fd)

    try:
        returncode = proc.wait(timeout=KILL_GRACE_SECONDS + 2)
    except subprocess.TimeoutExpired:
        proc.kill()
        returncode = proc.wait()

    if outcome == "interrupted_confirmed":
        result = RunResult(exit_code=returncode, interrupted=True)
    elif outcome == "interrupted_unconfirmed":
        # NEVER claim confirmed-dead (I7) without confirmation - this is
        # indeterminate, not interrupted, even though a human caused it.
        result = RunResult(exit_code=returncode, indeterminate=True)
    elif captured["exit_code"] is None:
        # Stream ended without the exit sentinel - P2-6. Not success, not
        # failure. Leave the pgid record alone: we don't know it's dead.
        result = RunResult(exit_code=returncode, indeterminate=True)
    else:
        _clear_pgid_record(cfg)
        result = RunResult(exit_code=captured["exit_code"])

    if json_mode:
        # Requirement 3: always emitted, in every outcome - a consumer must
        # never be left waiting to find out how the run ended. `code` here
        # is the raw remote/local value (ARCHITECTURE.md's own example,
        # {"t": "exit", "code": 1, ...} - a plausible remote failure code,
        # not a tool-level one); errors.exit_code_for_run's mapping is what
        # this PROCESS exits with, a separate concern from what this event
        # reports about the RUN.
        write_json(
            diagnostics.exit_event(
                result.exit_code, interrupted=result.interrupted, indeterminate=result.indeterminate
            )
        )
    return result


# --------------------------------------------------------------------------
# PTY mode (P2-5).
# --------------------------------------------------------------------------

def _pty_command(cfg: Config, command: str) -> str:
    """The remote command line for --tty mode: still no marker/exit-sentinel
    protocol (ADR-2 - the pty's own line discipline handles signals natively,
    so none of that machinery is needed here), but P4-1 still needs SOME
    pgid recorded in the run lock so a concurrent invocation can tell this
    one apart from a stale, dead holder. Computed the same way the pipe-mode
    wrapper does, then handed off to the real command with `exec` rather
    than staying around as its parent - `exec` replaces the shell in place
    (same pid, same pgid), so the interactive program itself, not a wrapper
    shell around it, ends up as the pty's foreground process, which is what
    native Ctrl-C delivery depends on.
    """
    inner = remote_command(cfg.remote_root, command)
    return (
        'PGID=$(ps -o pgid= -p $$ | tr -d "[:space:]"); '
        f"{_write_lock_pgid_snippet(cfg.remote_root)}; "
        f"exec sh -c {shlex.quote(inner)}"
    )


def _run_pty(cfg: Config, command: str, session: Session) -> RunResult:
    """`ssh -tt`, with the local terminal put in raw mode for the duration.

    No marker/exit-sentinel machinery here - ADR-2's trade for pty mode is
    that the kernel's own pty line discipline delivers signals to the remote
    foreground process group natively, so C4 does not need to do it. The
    cost is the one this mode is opt-in for: stdout and stderr merge into a
    single stream, so it cannot be classified (--json refuses the combination
    in cli.py).
    """
    session.ensure_reachable()
    argv = session.pty_argv(_pty_command(cfg, command))

    old_termios = None
    stdin_fd = sys.stdin.fileno()
    try:
        if sys.stdin.isatty():
            old_termios = termios.tcgetattr(stdin_fd)
            tty_module.setraw(stdin_fd)
        completed = subprocess.run(argv)
    finally:
        # MUST restore even if something above raised - an unrestored
        # terminal leaves the user's shell echo-less and they have to type
        # `reset` blind. Tested against the actual exception path, not just
        # the happy one: a broken ssh binary path raises FileNotFoundError
        # here and termios is still back to normal afterward.
        if old_termios is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_termios)

    return RunResult(exit_code=completed.returncode)


# --------------------------------------------------------------------------
# Public entry point.
# --------------------------------------------------------------------------

def run(
    cfg: Config,
    command: str,
    session: Session,
    *,
    tty: bool = False,
    json_mode: bool = False,
    replace: bool = False,
) -> RunResult:
    """Run `command` in the remote project root and propagate its outcome.

    Pipe mode (default) carries I5/I6/I7 in full - see _run_pipes. PTY mode
    (--tty) trades stream separation for native signal delivery (ADR-2) and
    is never the default. json_mode (P3-3) is a pipe-mode-only concept -
    cli.py already refuses --tty --json before this is ever called with both.

    P4-1: brackets BOTH modes in the run lock - Claim (ARCHITECTURE.md §6
    step 4) before Execute (step 5), release on every path out of Execute
    (trap 3 - success, failure, signal, exception all go through this same
    finally). acquire_lock raises RunLockHeld (exit 75) before either mode
    is ever entered if another invocation holds it and --replace was not
    given; nothing below this point may run without the lock.
    """
    acquire_lock(cfg, session, replace=replace)
    try:
        if tty:
            return _run_pty(cfg, command, session)
        return _run_pipes(cfg, command, session, json_mode=json_mode)
    finally:
        release_lock(cfg, session)
