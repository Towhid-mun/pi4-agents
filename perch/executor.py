"""C4 (draft) - remote execution.

# REPLACED IN P2
#
# This is the walking-skeleton executor and it is deliberately crude. Every
# guarantee C4 is supposed to carry is missing here:
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
import subprocess
from dataclasses import dataclass

from perch.config import Config
from perch.errors import TargetUnreachable

SSH = "ssh"


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


def ssh_argv(cfg: Config, command: str) -> list[str]:
    """The exact ssh command line. Pure - runs nothing.

    P1-1 replaces this: every ssh invocation moves into C2 and gains
    ControlMaster, ControlPath, ControlPersist and an explicit ConnectTimeout.
    Until then there is no multiplexing and no timeout, which is why a
    powered-off board hangs instead of exiting 69.
    """
    return [SSH, cfg.host, remote_command(cfg.remote_root, command)]


def join(argv: list[str]) -> str:
    """Turn a local argv into one safely quoted remote shell string."""
    return shlex.join(argv)


def run(cfg: Config, command: str) -> RunResult:
    """Run `command` in the remote project root and propagate its exit status.

    stdio is inherited, so the target's output reaches this terminal verbatim
    (I8) and its exit status becomes ours (I6).
    """
    argv = ssh_argv(cfg, command)
    try:
        completed = subprocess.run(argv)
    except FileNotFoundError as exc:
        raise TargetUnreachable(f"ssh not found on this machine: {exc}") from exc

    # ssh reports its own failures as 255, which is indistinguishable here from
    # a remote command that genuinely exited 255. I6 says the remote status
    # wins, so it passes through untouched. P1-2 classifies connection failures
    # properly and turns the unreachable case into exit 69.
    return RunResult(exit_code=completed.returncode)
