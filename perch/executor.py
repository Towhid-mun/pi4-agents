"""C4 - remote execution: streaming, process groups, exit sentinel.

The component where "roughly right" is a bug (ARCHITECTURE.md §5/C4). Carries
I5 (streaming, P2-1) and now the process-group/sentinel protocol (P2-2) that
signal forwarding (P2-3) and indeterminate detection (P2-6) both build on.
See ADR-5 for the wrapper script every command actually runs inside, and why.

Signal forwarding, stale-group reaping and pty mode are P2-3 through P2-5 -
not here yet.
"""

import os
import secrets
import selectors
import shlex
import sys
from dataclasses import dataclass

from perch.config import Config
from perch.session import Session

CHUNK_SIZE = 65536


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
    The payload: trap SIGPIPE/SIGHUP so a broken channel does not kill the
    group by accident (S0-2's finding), resolve the REAL pgid via `ps` rather
    than trusting $$ (setsid can fork when the caller is already a group
    leader), print it on a marked line before the program's own output
    begins, run the program (line-buffered via stdbuf when available), then
    print a second marked line carrying its real exit status - the sentinel
    that disambiguates a genuine remote 255 from ssh's own connection-level
    255 (P2-6 depends on this).
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


def run(cfg: Config, command: str, session: Session) -> RunResult:
    """Run `command` in the remote project root, streaming output as it arrives.

    Reads stdout and stderr with `selectors` (P2-1) so neither stream can
    starve or deadlock the other. Marker lines are stripped from user-visible
    output and used to capture the process group id and, from the exit
    sentinel, the command's REAL exit status - not ssh's own return code,
    which is ambiguous between a genuine remote 255 and a connection failure.

    If the stream ends without ever seeing the exit sentinel, this is
    indeterminate (P2-6) - not success, not failure.
    """
    token = new_marker_token()
    proc = session.popen(build_wrapped_command(cfg.remote_root, command, token))

    os.set_blocking(proc.stdout.fileno(), False)
    os.set_blocking(proc.stderr.fileno(), False)

    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ, data="stdout")
    sel.register(proc.stderr, selectors.EVENT_READ, data="stderr")

    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    open_streams = {"stdout", "stderr"}
    captured = {"pgid": None, "exit_code": None}

    def emit(stream: str, line: str) -> None:
        # Trap 3: our own stdout/stderr are block-buffered when not a
        # terminal. Flush every line explicitly.
        out = sys.stdout if stream == "stdout" else sys.stderr
        out.write(line + "\n")
        out.flush()

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

    while open_streams:
        for key, _ in sel.select(timeout=0.5):
            drain(key.data, key.fileobj)
    sel.close()

    returncode = proc.wait()

    if captured["exit_code"] is None:
        return RunResult(exit_code=returncode, indeterminate=True)
    return RunResult(exit_code=captured["exit_code"])
