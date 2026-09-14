"""C2 - session manager: owns the SSH connection and its reuse.

Every ssh invocation in the codebase goes through this module. Nothing else
may build an argv naming "ssh" as the program - route it through a Session.

See docs/PHASE-1-BUILD-AND-RUN.md for why the numbers below (ConnectTimeout,
ControlPersist, retry count) are what they are; that document owns the
rationale so it doesn't drift out of sync with a second copy here.
"""

import hashlib
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from perch.errors import PerchError, TargetUnreachable

SSH = "ssh"

DEFAULT_CONNECT_TIMEOUT = 5      # seconds
DEFAULT_CONTROL_PERSIST = "10m"
RECONNECT_ATTEMPTS = 2           # bounded backoff, total tries
RECONNECT_BACKOFF = 1.0          # seconds between attempts

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
class Classification:
    error: PerchError
    transient: bool  # worth a bounded retry (target might be mid-boot)


# Each ssh-level failure ssh can report on stderr, before the remote shell
# ever ran - so there is no ambiguity here with a remote command's own output
# (see docs/PHASE-1-BUILD-AND-RUN.md, "Classification").
_PATTERNS: tuple[tuple[tuple[str, ...], str, str, bool], ...] = (
    (
        ("Connection timed out", "Operation timed out", "Connection refused",
         "No route to host", "Could not resolve hostname",
         "Network is unreachable", "Host is down",
         "kex_exchange_identification"),
        "unreachable",
        "check the target is powered on and reachable on the network",
        True,
    ),
    (
        ("Permission denied",),
        "authentication failed",
        "check ssh-agent has the right key loaded",
        False,
    ),
    (
        ("Host key verification failed", "REMOTE HOST IDENTIFICATION HAS CHANGED"),
        "host key mismatch",
        "run `ssh-keygen -R <alias>` if the target was reimaged, then retry",
        False,
    ),
)


def classify_ssh_failure(host: str, stderr: str) -> Classification | None:
    """Turn ssh's own connection-level stderr into a classified failure.

    Returns None when exit 255 does not match a known ssh diagnostic - that
    case is a remote command that happens to have exited 255 itself, which is
    legal (I6) and must pass through unchanged, not be reported as unreachable.
    """
    for needles, kind, hint, transient in _PATTERNS:
        if any(needle in stderr for needle in needles):
            message = f"{host}: {kind} - {hint}"
            return Classification(error=TargetUnreachable(message), transient=transient)
    return None


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
        lines.
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

    def ensure_reachable(self) -> None:
        """Fail fast and classified before anything tries to run on the target.

        Raises a classified PerchError (never a raw exception) if the target
        cannot be reached, so a caller never needs its own connection-failure
        handling. On success, the control master this call establishes is
        reused by whatever runs next - see the module docstring.
        """
        last: Classification | None = None
        for attempt in range(1, RECONNECT_ATTEMPTS + 1):
            completed = self._run_capturing(self.ssh_argv("true"))
            if completed.returncode != 255:
                return
            classification = classify_ssh_failure(self.host, completed.stderr)
            if classification is None:
                # 255, but not a recognized ssh-level failure - treat the
                # connection as fine and let the caller's real command surface
                # whatever this actually was.
                return
            last = classification
            if not classification.transient or attempt == RECONNECT_ATTEMPTS:
                raise classification.error
            time.sleep(RECONNECT_BACKOFF)
        assert last is not None  # loop always returns or raises above
        raise last.error

    def run(self, remote_command: str, *, input: str | None = None) -> subprocess.CompletedProcess:
        """Run one command on the target, stdio inherited so output is verbatim (I8).

        Raises a classified PerchError if the target is unreachable, before
        ever attempting `remote_command` - never lets a raw connection failure
        (a Python OSError, an unclassified ssh exit) reach the caller.
        """
        self.ensure_reachable()
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

        For callers that need to parse the output (doctor's probe script)
        rather than show it to a human live.
        """
        self.ensure_reachable()
        argv = self.ssh_argv(remote_command)
        return self._run_capturing(argv, input=input)

    def popen(self, remote_command: str) -> subprocess.Popen:
        """Launch a long-running remote command without waiting for it (C4/P2-1).

        stdout and stderr are separate pipes (ADR-2, pipe mode); stdin is
        closed (I9 - nothing here is interactive). ensure_reachable() still
        runs first, so an unreachable target is still a fast, classified
        failure (P1-2) rather than a Popen that silently hangs or produces a
        confusing ssh-level error buried in the child's own stderr.

        The caller owns the Popen - reading it with selectors, waiting for
        it, killing it - none of that is C2's job. C2's job ends at handing
        back a correctly-constructed process.
        """
        self.ensure_reachable()
        argv = self.ssh_argv(remote_command)
        try:
            return subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise TargetUnreachable(f"ssh not found on this machine: {exc}") from exc

    def _run_capturing(self, argv: list[str], *, input: str | None = None) -> subprocess.CompletedProcess:
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
