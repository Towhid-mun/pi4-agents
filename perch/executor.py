"""C4 (draft) - remote execution.

# REPLACED IN P2
#
# This is the walking-skeleton executor and it is deliberately crude. Every
# guarantee C4 is supposed to carry beyond what P1-1/P1-2 added is missing
# here:
#
#   I5 (streaming)  - stdio is inherited, so output happens to reach the
#                     terminal promptly, but stdout and stderr are NOT read
#                     separately and nothing is line-oriented. There is no
#                     selectors loop. C5 cannot classify this stream.
#   I7 (no orphans) - no setsid, no process group capture, no SIGINT handler.
#                     Ctrl-C kills the local ssh client and leaves whatever it
#                     started running on the target.
#   run lock        - not implemented. Two invocations will happily collide.
#   indeterminate   - a channel that closes without a status is not detected;
#                     RunResult.indeterminate is always False here.
#
# P2-1 through P2-6 replace this module wholesale. Do not mistake it for
# finished work, and do not build anything on its internals.
"""

import shlex
from dataclasses import dataclass

from perch.config import Config
from perch.session import Session


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
    """Run `command` in the remote project root and propagate its exit status.

    Connection failures are classified and raised by session.run() itself
    (exit 69, P1-2) before this command is even attempted. Once it does run,
    stdio is inherited, so the target's output reaches this terminal verbatim
    (I8) and its exit status becomes ours (I6).
    """
    completed = session.run(remote_command(cfg.remote_root, command))

    # A remote command that happens to exit 255 itself is legal (I6) and
    # session.run() already told the difference from a real connection
    # failure - by the time we're here, 255 just means 255.
    return RunResult(exit_code=completed.returncode)
