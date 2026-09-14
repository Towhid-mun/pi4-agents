"""C2 - session manager: owns the SSH connection and its reuse.

Every ssh invocation in the codebase goes through this module. Nothing else
may build an argv naming "ssh" as the program - route it through a Session.

See docs/PHASE-1-BUILD-AND-RUN.md for why the numbers below (ConnectTimeout,
ControlPersist) are what they are; that document owns the rationale so it
doesn't drift out of sync with a second copy here.

Connection-failure classification (unreachable vs. auth vs. host-key, with
bounded retry) is P1-2, not here - this ticket is multiplexing only.
"""

import hashlib
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from perch.errors import TargetUnreachable

SSH = "ssh"

DEFAULT_CONNECT_TIMEOUT = 5      # seconds
DEFAULT_CONTROL_PERSIST = "10m"

CONTROL_SOCKET_DIR = Path.home() / ".ssh"


def control_path_for(alias: str, *, socket_dir: Path = CONTROL_SOCKET_DIR) -> Path:
    """A short, stable ControlPath under ~/.ssh/, derived from the alias.

    Deliberately NOT ssh's own %r@%h:%p expansion (user@host:port) - that
    string's length depends on the username and hostname, and combined with a
    long ~/.ssh/ path it can exceed the ~104-byte limit macOS puts on AF_UNIX
    socket paths. A fixed-width hash makes the FILENAME constant regardless of
    alias, username or hostname length - it does not, and cannot, bound an
    already-long ~/.ssh/ itself, which is a separate, unbounded concern.
    """
    digest = hashlib.sha256(alias.encode()).hexdigest()[:16]
    return socket_dir / f"perch-{digest}.sock"


@dataclass
class Session:
    """Owns one target's SSH connection and its reuse across invocations."""

    host: str
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT
    control_persist: str = DEFAULT_CONTROL_PERSIST
    control_socket_dir: Path = field(default=CONTROL_SOCKET_DIR)

    @property
    def control_path(self) -> Path:
        return control_path_for(self.host, socket_dir=self.control_socket_dir)

    def base_ssh_options(self) -> list[str]:
        """The -o flags every ssh invocation carries.

        Passed on the command line, not read from ~/.ssh/config: a -o flag on
        the command line overrides a same-named directive in a config file, so
        this multiplexing setup does not depend on - and cannot be silently
        defeated by - whatever the user's own ssh config says. It must work
        identically whether or not ~/.ssh/config has its own ControlMaster
        lines. Verified by hand: commenting out this machine's own
        ControlMaster/ControlPath/ControlPersist in ~/.ssh/config and
        confirming perch still multiplexes (see
        docs/PHASE-1-BUILD-AND-RUN.md).
        """
        return [
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={self.control_path}",
            "-o", f"ControlPersist={self.control_persist}",
            "-o", f"ConnectTimeout={self.connect_timeout}",
            "-o", "BatchMode=yes",  # I9: never prompt for a passphrase or host key
        ]

    def ssh_argv(self, remote_command: str) -> list[str]:
        """The exact ssh command line for one remote command. Pure - runs nothing."""
        return [SSH, *self.base_ssh_options(), self.host, remote_command]

    def rsh_command(self) -> str:
        """The ssh command line for rsync's --rsh, as one shell string.

        Giving rsync the same ControlPath means its connection joins the same
        multiplexed master instead of paying a second handshake.
        """
        return shlex.join([SSH, *self.base_ssh_options()])

    def alive(self) -> bool:
        """True if a control master for this host is already running."""
        if not self.control_path.exists():
            return False
        completed = subprocess.run(
            [SSH, "-O", "check", "-o", f"ControlPath={self.control_path}", self.host],
            capture_output=True,
            text=True,
        )
        return completed.returncode == 0

    def run(self, remote_command: str, *, input: str | None = None) -> subprocess.CompletedProcess:
        """Run one command on the target, stdio inherited so output is verbatim (I8).

        Connection failures still surface as ssh's own exit 255 here,
        undifferentiated from a remote command that happens to exit 255
        itself - P1-2 replaces that with classification and a bounded retry.
        """
        argv = self.ssh_argv(remote_command)
        try:
            return subprocess.run(
                argv,
                input=input,
                stdin=subprocess.DEVNULL if input is None else None,
            )
        except FileNotFoundError as exc:
            raise TargetUnreachable(f"ssh not found on this machine: {exc}") from exc

    def run_capturing(self, remote_command: str, *, input: str | None = None) -> subprocess.CompletedProcess:
        """Like run(), but captures stdout/stderr as text instead of inheriting.

        For callers that need to parse the output (doctor's probe script,
        P1-3) rather than show it to a human live.
        """
        argv = self.ssh_argv(remote_command)
        try:
            return subprocess.run(
                argv,
                input=input,
                stdin=subprocess.DEVNULL if input is None else None,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise TargetUnreachable(f"ssh not found on this machine: {exc}") from exc
