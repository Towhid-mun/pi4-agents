"""C6 - artifact retrieval.

Because C3 deletes (--delete propagates, I2: the target's copy is derived
and disposable), anything generated on the target inside the project
directory and never retrieved is lost on the very next sync. `pull()` is the
one function behind both the explicit `perch pull <glob> [dest]` verb and
cli.py's auto-pull of `config.artifacts` after a successful run
(ARCHITECTURE.md §6 step 7) - they call the exact same code, so a no-match
glob and the default destination behave identically in both places by
construction, not by two separately-maintained implementations (P4-2 traps
2 and 3).

P4-2.3 - the round trip (see docs/PHASE-4-BUILD-AND-RUN.md for the full
reasoning): a file pulled into the host workspace is now something the NEXT
sync will push right back to the target, at best wastefully and at worst
overwriting a fresher target-side build with a now-stale host copy - the
same failure class I3 exists to prevent, from the opposite direction.
DEFAULT_DEST_SUBDIR sidesteps this for the common case rather than merely
documenting it: it is `.perch/`-rooted, which config.BUILTIN_EXCLUDES
already protects unconditionally (added this phase for the run lock) - so
the default destination cannot be pushed back, ever, no user action
required. An explicit `dest` argument is honored wherever the caller points
it; if that is inside the workspace and not excluded, the round trip is
real and is the caller's informed choice (--help says so).
"""

import os
import shlex
import subprocess
from pathlib import Path

from perch.config import Config
from perch.errors import PullError
from perch.session import Session

RSYNC = "rsync"

# Rooted under .perch/, which is ALREADY in config.BUILTIN_EXCLUDES (P4-1,
# added there for the run lock) - see the module docstring for why that,
# specifically, is what makes the default destination round-trip-safe.
DEFAULT_DEST_SUBDIR = ".perch/artifacts"


def default_dest(cfg: Config) -> Path:
    return cfg.local_root / DEFAULT_DEST_SUBDIR


def list_command(remote_root: str, glob: str) -> str:
    """The remote shell command that expands `glob` inside remote_root.

    Trap 1: globs expand on the TARGET's shell, never locally - this is the
    whole reason this is a remote command at all rather than something
    resolved on the host. Pure - builds a string, runs nothing.

    The pattern is passed as a genuine argv element to the inner `bash -c`
    (its $1), not concatenated into the script text - so it can contain
    shell metacharacters without ever being able to break out of the
    listing script, while still being referenced UNQUOTED inside the `for`
    loop so it undergoes real glob expansion (quoting it there would
    defeat the entire point - it would list one literal, almost certainly
    nonexistent, filename instead of expanding).

    `shopt -s nullglob`: a pattern matching nothing expands to nothing, not
    to its own literal text - without this, a no-match glob would come back
    looking like exactly one (bogus) matched file instead of zero (trap 2's
    "matches nothing" case depends on this being real).
    """
    root = shlex.quote(remote_root)
    pattern = shlex.quote(glob)
    script = 'shopt -s nullglob; for f in $1; do printf "%s\\n" "$f"; done'
    return f"cd {root} && bash -c {shlex.quote(script)} bash {pattern}"


def pull_argv(cfg: Config, session: Session, matched: list[str], dest: Path) -> list[str]:
    """The exact rsync command line to retrieve `matched` (paths already
    resolved relative to remote_root by list_command/_expand_glob) into
    `dest`. Pure - runs nothing.

    `-R` (--relative), anchored at the `/./` marker in each source, so the
    files land under `dest` preserving their path relative to remote_root
    rather than flattened - predictable, and avoids collisions between
    same-named files pulled from different subdirectories.

    No --delete (a pull is purely additive/overwriting of the named files -
    it must never remove anything already in dest that the glob did not
    match). No --checksum: I3 is about the ongoing bidirectional-trust
    SYNC comparison (C3) deciding whether to skip a push - a pull is a
    one-shot, explicitly requested copy with no "skip if it looks
    unchanged" step to get wrong, so the concern I3 guards against does not
    apply here.
    """
    sources = [f"{cfg.host}:{cfg.remote_root}/./{path}" for path in matched]
    return [
        RSYNC,
        "-az", "-i", "-R",
        f"--rsh={session.rsh_command()}",
        *sources,
        f"{os.fspath(dest)}{os.sep}",
    ]


def _expand_glob(session: Session, remote_root: str, glob: str) -> list[str]:
    completed = session.run_capturing(list_command(remote_root, glob))
    if completed.returncode != 0:
        raise PullError(
            f"could not list files matching {glob!r} under {remote_root} "
            f"(exit {completed.returncode}): {completed.stderr.strip()}"
        )
    return [line for line in completed.stdout.splitlines() if line]


def pull(cfg: Config, session: Session, glob: str, dest: Path | None = None) -> list[str]:
    """Retrieve files matching `glob` from the project's remote root into
    `dest` (default: see default_dest/DEFAULT_DEST_SUBDIR above).

    A glob matching nothing is a NO-OP, not an error (trap 2): auto-pull
    runs after every successful run, and not every run necessarily produces
    every configured artifact every time - treating that as fatal would
    turn an optional artifact into a mandatory one. Returns the list of
    remote-relative paths retrieved (empty when nothing matched), so a
    caller can report accordingly without needing its own no-match logic.
    """
    if dest is None:
        dest = default_dest(cfg)
    matched = _expand_glob(session, cfg.remote_root, glob)
    if not matched:
        return []
    dest.mkdir(parents=True, exist_ok=True)
    argv = pull_argv(cfg, session, matched, dest)
    completed = _run(argv)
    if completed.returncode != 0:
        raise PullError(
            f"rsync exited {completed.returncode} retrieving {glob!r} from "
            f"{cfg.host}:{cfg.remote_root} into {dest}"
        )
    return matched


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, stdout=subprocess.DEVNULL)
    except FileNotFoundError as exc:
        raise PullError(f"{argv[0]} not found on this machine: {exc}") from exc
