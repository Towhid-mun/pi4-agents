"""C4 - remote execution: streaming.

The component where "roughly right" is a bug (ARCHITECTURE.md §5/C4). This
ticket (P2-1) carries I5 alone: read stdout and stderr concurrently with
`selectors` and emit each line as it arrives, never buffering until exit.

Process groups, signal forwarding, stale-group reaping, pty mode and
indeterminate detection are P2-2 through P2-6 - not here yet. The exit code
is still ssh's own raw return value, which means the 127/255 ambiguity
documented in errors.py still applies at this stage; P2-2's marker/sentinel
protocol is what resolves it.
"""

import os
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
    """The single string handed to the shell on the target.

    remote_root is data and is quoted. `command` is deliberately NOT quoted:
    it is shell text by definition - it comes from [commands] in .perch.toml,
    where the user writes things like "make -j4 && ./run", and quoting it would
    turn the whole line into one argv element. Anything built from argv rather
    than from config must be joined with shlex.join before it gets here.
    """
    return f"cd {shlex.quote(remote_root)} && {command}"


def join(argv: list[str]) -> str:
    """Turn a local argv into one safely quoted remote shell string."""
    return shlex.join(argv)


def run(cfg: Config, command: str, session: Session) -> RunResult:
    """Run `command` in the remote project root, streaming output as it arrives.

    Reads stdout and stderr with `selectors` so neither stream can starve the
    other or deadlock on a full pipe - a `.communicate()` call in this path
    would be a defect (I5). Each stream keeps its own newline-buffered tail
    across chunk boundaries, flushed as a final unterminated line at EOF.
    """
    proc = session.popen(remote_command(cfg.remote_root, command))

    os.set_blocking(proc.stdout.fileno(), False)
    os.set_blocking(proc.stderr.fileno(), False)

    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ, data="stdout")
    sel.register(proc.stderr, selectors.EVENT_READ, data="stderr")

    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    open_streams = {"stdout", "stderr"}

    def emit(stream: str, line: str) -> None:
        # Trap 3: our own stdout/stderr are block-buffered when not a
        # terminal - exactly the case when an agent or an editor task is the
        # caller. Flush every line explicitly, or streaming is indistinguishable
        # from not streaming at all.
        out = sys.stdout if stream == "stdout" else sys.stderr
        out.write(line + "\n")
        out.flush()

    def drain(stream: str, fileobj) -> None:
        try:
            chunk = os.read(fileobj.fileno(), CHUNK_SIZE)
        except BlockingIOError:
            return
        if chunk == b"":
            sel.unregister(fileobj)
            open_streams.discard(stream)
            if buffers[stream]:
                emit(stream, bytes(buffers[stream]).decode("utf-8", errors="replace"))
                buffers[stream].clear()
            return
        buffers[stream].extend(chunk)
        while True:
            idx = buffers[stream].find(b"\n")
            if idx == -1:
                break
            line = bytes(buffers[stream][:idx]).decode("utf-8", errors="replace")
            del buffers[stream][: idx + 1]
            emit(stream, line)

    while open_streams:
        for key, _ in sel.select(timeout=0.5):
            drain(key.data, key.fileobj)
    sel.close()

    returncode = proc.wait()

    # A remote command that happens to exit 255 itself is legal (I6) but
    # indistinguishable here from an ssh-level failure - P2-2's sentinel
    # resolves this; not yet at this stage.
    return RunResult(exit_code=returncode)
