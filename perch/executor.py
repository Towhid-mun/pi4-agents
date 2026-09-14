"""C4 - remote execution: streaming, process groups, signals, stale reaping.

The component where "roughly right" is a bug (ARCHITECTURE.md §5/C4). Carries
I5 (streaming, P2-1), the process-group/sentinel protocol (P2-2), signal
forwarding (P2-3), and now stale-group reaping (P2-4) - I7. See ADR-5 for the
wrapper script every command actually runs inside, and why.

Pty mode is P2-5 - not here yet.
"""

import hashlib
import os
import secrets
import selectors
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import signal as signal_module

from perch.config import Config
from perch.errors import InternalError, TargetUnreachable
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
    """
    inner_quoted = shlex.quote(remote_command(remote_root, command))
    payload = (
        'trap "" PIPE HUP\n'
        'PGID=$(ps -o pgid= -p $$ | tr -d "[:space:]")\n'
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
# Signal state. The OS handler only ever does this - a plain counter bump.
# All the actual work (opening a connection, sending a kill, polling) happens
# in the main read loop, never inside the handler itself (re-entrancy).
# --------------------------------------------------------------------------

class _SignalState:
    def __init__(self):
        self.count = 0

    def bump(self, signum, frame):
        self.count += 1


def run(cfg: Config, command: str, session: Session) -> RunResult:
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
    """
    reap_stale_group(cfg, session)

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

    def emit(stream: str, line: str) -> None:
        # Trap 3: our own stdout/stderr are block-buffered when not a
        # terminal. Flush every line explicitly.
        if broken[stream]:
            return
        out = sys.stdout if stream == "stdout" else sys.stderr
        try:
            out.write(line + "\n")
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

    def handle_line(stream: str, raw: bytes) -> None:
        line = raw.decode("utf-8", errors="replace")
        kind = classify_line(line, token)
        if kind is None:
            emit(stream, line)
            return
        marker_kind, value, leading = kind
        if leading:
            emit(stream, leading)  # a genuine final partial line, glued to the marker
        if marker_kind == "pgid":
            try:
                captured["pgid"] = int(value)
            except ValueError:
                emit(stream, line)  # garbled marker - show it rather than trust it
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
                handle_line(stream, bytes(buffers[stream]))
                buffers[stream].clear()
            return
        buffers[stream].extend(chunk)
        while True:
            idx = buffers[stream].find(b"\n")
            if idx == -1:
                break
            handle_line(stream, bytes(buffers[stream][:idx]))
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
        return RunResult(exit_code=returncode, interrupted=True)
    if outcome == "interrupted_unconfirmed":
        # NEVER claim confirmed-dead (I7) without confirmation - this is
        # indeterminate, not interrupted, even though a human caused it.
        return RunResult(exit_code=returncode, indeterminate=True)
    if captured["exit_code"] is None:
        # Stream ended without the exit sentinel - P2-6. Not success, not
        # failure. Leave the pgid record alone: we don't know it's dead.
        return RunResult(exit_code=returncode, indeterminate=True)
    _clear_pgid_record(cfg)
    return RunResult(exit_code=captured["exit_code"])
