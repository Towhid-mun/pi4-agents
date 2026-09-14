"""C3 - workspace mirror.

Makes the target tree identical to the host tree for included paths. One
direction only: the host workspace is the sole source of truth and the target's
copy is derived and disposable (I2).

REPLACED IN P4-4: there is no fast path here yet. Every invocation runs rsync.
"""

import os
import shlex
import subprocess
import tempfile
from pathlib import Path

from perch.config import Config
from perch.errors import SyncError

RSYNC = "rsync"
SSH = "ssh"

# --------------------------------------------------------------------------
# The flag set. Do not change without reading this comment.
#
#   -a              archive: recurse, preserve modes, links and times
#   -z              compress in transit
#   --delete        deletions propagate; the target is a mirror, not a union
#   --checksum      compare by CONTENT, never by mtime+size.
#   -i              itemize what changed, so a sync is never silently opaque
#
# --checksum is INVARIANT I3, not an option and not a performance mistake
# somebody forgot to clean up. The Raspberry Pi has no battery-backed RTC, so
# its clock is wrong on every boot until NTP lands. rsync's default quick check
# is mtime+size; with a wrong clock on one side it will decide a changed file
# is unchanged and skip it, and you get a build of stale source with no
# indication anything is wrong. Spike S0-1 demonstrated exactly that on this
# pair of machines: without --checksum a same-size, same-mtime edit did not
# transfer. A silently stale build is the worst failure this tool can have.
#
# If --checksum ever looks like the reason a sync is slow, the answer is the
# conservative local-hash fast path in P4-4, which skips running rsync at all.
# It is not to relax the comparison.
# --------------------------------------------------------------------------
BASE_FLAGS = ("-a", "-z", "--delete", "--checksum", "-i")


def rsync_argv(cfg: Config, exclude_from: str) -> list[str]:
    """The exact rsync command line for this project. Pure - runs nothing.

    Kept separate from push() so the flag set and the destination spelling are
    testable with no target reachable.
    """
    source = f"{os.fspath(cfg.local_root)}{os.sep}"  # trailing sep: contents, not the dir
    destination = f"{cfg.host}:{cfg.remote_root}/"
    return [
        RSYNC,
        *BASE_FLAGS,
        f"--exclude-from={exclude_from}",
        source,
        destination,
    ]


def mkdir_argv(cfg: Config) -> list[str]:
    """The command that creates the remote root on first use.

    The remote path crosses into a shell on the target, so it goes through
    shlex.quote. No f-string command building anywhere in this codebase.
    """
    remote_command = f"mkdir -p {shlex.quote(cfg.remote_root)}"
    return [SSH, cfg.host, remote_command]


def exclude_file_contents(cfg: Config) -> str:
    """One pattern per line: built-in excludes, then the project's own."""
    return "".join(f"{pattern}\n" for pattern in cfg.all_excludes)


def push(cfg: Config) -> None:
    """Mirror the host workspace onto the target.

    Raises SyncError on any failure. The caller MUST NOT execute anything
    against the tree if this raises - a partially synced tree is I4.
    """
    if not cfg.local_root.is_dir():
        raise SyncError(f"local root does not exist: {cfg.local_root}")

    _ensure_remote_root(cfg)

    with tempfile.NamedTemporaryFile(
        "w", prefix="perch-excludes-", suffix=".txt", delete=False
    ) as handle:
        handle.write(exclude_file_contents(cfg))
        exclude_from = handle.name

    try:
        argv = rsync_argv(cfg, exclude_from)
        completed = _run(argv)
    finally:
        Path(exclude_from).unlink(missing_ok=True)

    if completed.returncode != 0:
        # rsync has already written its own message to stderr. Passing it
        # through untouched is I8; this line only adds the exit status.
        raise SyncError(
            f"rsync exited {completed.returncode} syncing {cfg.local_root} to "
            f"{cfg.host}:{cfg.remote_root} - nothing was executed on the target"
        )


def _ensure_remote_root(cfg: Config) -> None:
    completed = _run(mkdir_argv(cfg))
    if completed.returncode != 0:
        raise SyncError(
            f"could not create remote root {cfg.remote_root} on {cfg.host} "
            f"(ssh exited {completed.returncode})"
        )


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    """Run a subprocess with stdio inherited, so its output arrives verbatim (I8)."""
    try:
        return subprocess.run(argv)
    except FileNotFoundError as exc:
        raise SyncError(f"{argv[0]} not found on this machine: {exc}") from exc
