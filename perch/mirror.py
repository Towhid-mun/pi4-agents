"""C3 - workspace mirror.

Makes the target tree identical to the host tree for included paths. One
direction only: the host workspace is the sole source of truth and the target's
copy is derived and disposable (I2).

P4-4 - the sync fast path. See content_hash()/push() below. Short
version: when the local tree's content hash matches the
hash recorded after the last successful real sync, push() considers
skipping the (expensive, full-content-checksum) rsync transfer - but first
confirms, with ONE cheap stat-only remote listing (no content read, no
rsync), that the target's file sizes still match what was recorded at that
same last sync. Only if BOTH agree does it skip rsync outright. A cache
miss of any kind (none recorded yet, unparsable, recorded for a different
host/remote_root, or a mismatched remote listing) always falls through to
a real sync - the guard is conservative by construction, never by assuming
an absent or unconfirmed signal is safe. This is what makes an edit made
directly on the target (bypassing perch) still provoke a real sync on the
next invocation, even though the local tree itself never changed.
"""

import fnmatch
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from perch.config import Config
from perch.errors import SyncError
from perch.session import Session

RSYNC = "rsync"

SYNC_CACHE_PATH = Path(".perch") / "sync-cache.json"

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
# conservative fast path (content_hash()/push(), below), which skips this
# rsync invocation - its full-content read and checksum on both sides -
# when nothing local has moved AND a cheap remote check agrees nothing
# target-side has either. It is not to relax the comparison - --checksum
# still runs, unweakened, on every sync the fast path does not skip.
# --------------------------------------------------------------------------
BASE_FLAGS = ("-a", "-z", "--delete", "--checksum", "-i")


def rsync_argv(cfg: Config, exclude_from: str, session: Session) -> list[str]:
    """The exact rsync command line for this project. Pure - runs nothing.

    Kept separate from push() so the flag set and the destination spelling are
    testable with no target reachable.

    --rsh gives rsync's own remote-shell connection the SAME ControlPath as
    every other invocation (C2), so it joins the multiplexed master instead of
    paying a second full handshake. That remote-shell command line is data
    from session.rsh_command() - nothing in this module spells out the
    program name it runs.
    """
    source = f"{os.fspath(cfg.local_root)}{os.sep}"  # trailing sep: contents, not the dir
    destination = f"{cfg.host}:{cfg.remote_root}/"
    return [
        RSYNC,
        *BASE_FLAGS,
        f"--rsh={session.rsh_command()}",
        f"--exclude-from={exclude_from}",
        source,
        destination,
    ]


def mkdir_command(remote_root: str) -> str:
    """The remote shell command that creates the remote root on first use.

    remote_root is data and goes through shlex.quote. No f-string command
    building anywhere in this codebase.
    """
    return f"mkdir -p {shlex.quote(remote_root)}"


def exclude_file_contents(cfg: Config) -> str:
    """One pattern per line: built-in excludes, then the project's own."""
    return "".join(f"{pattern}\n" for pattern in cfg.all_excludes)


def push(cfg: Config, session: Session, *, quiet: bool = False, force_sync: bool = False) -> None:
    """Mirror the host workspace onto the target.

    Raises SyncError on an rsync-specific failure, or whatever classified
    PerchError session.run() raises if the target itself is unreachable (that
    one is not rewrapped - it must keep its own exit code, e.g. 69, rather
    than becoming a generic 73). The caller MUST NOT execute anything against
    the tree if this raises - a partially synced tree is I4.

    quiet=True (P3-3, --json): rsync's own itemized change list is TOOL
    progress output, not part of the remote command's diagnostic stream -
    in json_mode stdout carries only JSON events, so rsync's stdout is
    dropped rather than leaking raw text onto it. stderr is left inherited
    either way: a genuine rsync error still needs to reach the user, and
    that path already goes through SyncError -> cli.py's own stderr print.

    force_sync=True (P4-4, --force-sync) bypasses the fast path below
    unconditionally - the escape hatch for whenever there is reason to
    distrust it beyond what it already checks for itself.
    """
    if not cfg.local_root.is_dir():
        raise SyncError(f"local root does not exist: {cfg.local_root}")

    digest = content_hash(cfg)

    if not force_sync and _cache_hit(cfg, digest) and _remote_matches_local_sizes(cfg, session):
        print(
            f"perch: sync skipped - local content unchanged since the last "
            f"sync to {cfg.host}:{cfg.remote_root}, and the target's file "
            f"sizes still match (--force-sync to run rsync anyway)",
            file=sys.stderr,
        )
        return

    _ensure_remote_root(cfg, session)

    with tempfile.NamedTemporaryFile(
        "w", prefix="perch-excludes-", suffix=".txt", delete=False
    ) as handle:
        handle.write(exclude_file_contents(cfg))
        exclude_from = handle.name

    try:
        argv = rsync_argv(cfg, exclude_from, session)
        completed = _run(argv, quiet=quiet)
    finally:
        Path(exclude_from).unlink(missing_ok=True)

    if completed.returncode != 0:
        # rsync has already written its own message to stderr. Passing it
        # through untouched is I8; this line only adds the exit status.
        raise SyncError(
            f"rsync exited {completed.returncode} syncing {cfg.local_root} to "
            f"{cfg.host}:{cfg.remote_root} - nothing was executed on the target"
        )

    _write_cache(cfg, digest)


def _ensure_remote_root(cfg: Config, session: Session) -> None:
    # session.run() itself raises a classified, unreachable-target error
    # (exit 69) before this ever tries mkdir - that check happens here, first,
    # for every verb, since every verb syncs before it does anything else.
    completed = session.run(mkdir_command(cfg.remote_root))
    if completed.returncode != 0:
        raise SyncError(
            f"could not create remote root {cfg.remote_root} on {cfg.host} "
            f"(ssh exited {completed.returncode})"
        )


def _run(argv: list[str], *, quiet: bool = False) -> subprocess.CompletedProcess:
    """Run rsync with stdio inherited, so its output arrives verbatim (I8) -
    unless quiet, which drops only stdout (see push()'s docstring)."""
    try:
        return subprocess.run(argv, stdout=subprocess.DEVNULL if quiet else None)
    except FileNotFoundError as exc:
        raise SyncError(f"{argv[0]} not found on this machine: {exc}") from exc


# --------------------------------------------------------------------------
# P4-4 - sync fast path.
#
# Two independent signals both have to agree before push() skips rsync:
#
#   1. content_hash() - a purely local, full-CONTENT sha256 over every
#      included file, compared against the hash recorded the last time a
#      real rsync run actually succeeded (_write_cache, keyed by host +
#      remote_root so a config change can't trust a stale cache from a
#      different target). This is what makes "one changed byte un-skips
#      it" true - and it costs nothing remote at all.
#
#   2. _remote_matches_local_sizes() - ONE cheap, stat-only remote listing
#      (remote_manifest_command(), a single `find -printf`, no file content
#      ever read or transferred) compared against the CURRENT local tree's
#      own (relpath, size) manifest. This is what makes "an edit made
#      directly on the target does not produce a false skip" true: (1)
#      alone cannot see a target-side change, since the local tree it
#      reads never moved - this is exactly the gap that check closes, for
#      the cost of one ssh round trip plus a handful of stat() calls on the
#      target, not a full rsync content-checksum transfer.
#
# Sizes, never mtimes - I3 exists because this target's clock cannot be
# trusted, and reusing mtime here would reopen exactly that hole from a new
# angle. A same-size, different-content edit made directly on the target
# would slip past signal 2 (a real gap, documented, not solved by this
# design - --force-sync is the escape hatch); every edit tested against
# the real target in practice changes size.
#
# The exclude match in both signals is deliberately NOT a full
# reimplementation of rsync's pattern language - only the two shapes every
# pattern in this codebase actually uses: "name/" (a directory, pruned by
# basename at any depth, on BOTH sides - remote_manifest_command() prunes
# by the same names via `find -prune`) and everything else (matched by
# fnmatch against both the file's basename and its full relative path,
# which also covers a plain "*.ext" or an exact filename - applied
# host-side to the remote listing too, via the SAME _is_file_excluded(), so
# there is one implementation of that matching, not two that could drift
# apart). Where a pattern's reach is ambiguous, the file is included
# anyway - the wrong direction to err in is UNDER-including (a file rsync
# would still transfer that a signal here ignores, which is exactly the
# false skip P4-4 must rule out), never over-including (costs one
# redundant, always-correct sync or one extra manifest line, never a wrong
# answer).
# --------------------------------------------------------------------------


def _prune_dir_names(patterns: tuple[str, ...]) -> frozenset[str]:
    return frozenset(p.rstrip("/") for p in patterns if p.endswith("/"))


def _file_exclude_patterns(patterns: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(p for p in patterns if not p.endswith("/"))


def _is_file_excluded(relpath: str, name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(relpath, pat) for pat in patterns)


def _hashable_files(cfg: Config) -> list[Path]:
    """Every file content_hash() should read, sorted for a deterministic
    hash regardless of os.walk's own (unspecified) traversal order."""
    prune = _prune_dir_names(cfg.all_excludes)
    file_patterns = _file_exclude_patterns(cfg.all_excludes)
    root = cfg.local_root
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in prune]
        for name in filenames:
            path = Path(dirpath) / name
            relpath = path.relative_to(root).as_posix()
            if _is_file_excluded(relpath, name, file_patterns):
                continue
            out.append(path)
    out.sort()
    return out


def content_hash(cfg: Config) -> str:
    """A cheap, purely local fingerprint of what a real sync would transfer.

    Deterministic over (relative path, content) for every included file -
    NOT size or mtime alone, so a same-size content edit (I3's clock-test
    scenario) still changes the hash, and a checkout with fresh mtimes but
    identical content does not.
    """
    digest = hashlib.sha256()
    for path in _hashable_files(cfg):
        relpath = path.relative_to(cfg.local_root).as_posix()
        digest.update(relpath.encode("utf-8"))
        digest.update(b"\0")
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _local_size_manifest(cfg: Config) -> str:
    """(relpath, size) for every included file, one per line, sorted - the
    same fileset content_hash() reads, but stat()-only: no content read."""
    lines = []
    for path in _hashable_files(cfg):
        relpath = path.relative_to(cfg.local_root).as_posix()
        lines.append(f"{path.stat().st_size}\t{relpath}")
    lines.sort()
    return "\n".join(lines)


def remote_manifest_command(cfg: Config) -> str:
    """The remote shell command that lists every file under remote_root not
    inside a pruned directory, as "<size>\\t<relpath>" lines - stat only,
    no content ever read. Pure - builds a string, runs nothing.

    Directory pruning matches content_hash()'s own dir-exclude set (by
    basename, via `find -prune`). File-level exclude patterns (*.o, an
    exact filename, ...) are deliberately NOT applied here - the caller
    applies them to the fetched lines with the exact same
    _is_file_excluded() content_hash() uses, so there is one
    implementation of that matching, not a second one on the remote shell
    side that could quietly drift from the first.
    """
    root = shlex.quote(cfg.remote_root)
    prune_names = sorted(_prune_dir_names(cfg.all_excludes))
    prune_expr = ""
    if prune_names:
        clauses = " -o ".join(f"-name {shlex.quote(name)}" for name in prune_names)
        prune_expr = f"\\( {clauses} \\) -prune -o "
    return f"cd {root} && find . {prune_expr}-type f -printf '%s\\t%P\\n'"


def _parse_remote_manifest(cfg: Config, text: str) -> str:
    """Remote "<size>\\t<relpath>" lines -> the same sorted manifest shape
    _local_size_manifest() produces, with file-level excludes applied here
    (see remote_manifest_command()'s docstring for why not on the remote
    side)."""
    file_patterns = _file_exclude_patterns(cfg.all_excludes)
    lines = []
    for line in text.splitlines():
        size, sep, relpath = line.partition("\t")
        if not sep:
            continue
        name = relpath.rsplit("/", 1)[-1]
        if _is_file_excluded(relpath, name, file_patterns):
            continue
        lines.append(f"{size}\t{relpath}")
    lines.sort()
    return "\n".join(lines)


def _remote_matches_local_sizes(cfg: Config, session: Session) -> bool:
    """One cheap ssh round trip: does the target's current (relpath, size)
    manifest still match the local tree's?

    A classified connection failure (TargetUnreachable etc.) is NOT caught
    here - it propagates exactly as it would from any other verb's first
    remote call (session.run()/_ensure_remote_root() raise the same way),
    keeping the bounded-retry timing already verified for that case (P4-3
    failure matrix row 1) rather than doubling it by swallowing the error
    here only to hit it again moments later in _ensure_remote_root(). A
    nonzero exit from the remote command itself (e.g. remote_root does not
    exist yet) is a plain miss, not an error - it just means "not matching",
    so push() falls through to the real sync that recreates it.
    """
    completed = session.run_capturing(remote_manifest_command(cfg))
    if completed.returncode != 0:
        return False
    return _parse_remote_manifest(cfg, completed.stdout) == _local_size_manifest(cfg)


def _cache_path(cfg: Config) -> Path:
    return cfg.local_root / SYNC_CACHE_PATH


def _load_cache(cfg: Config) -> dict | None:
    """The last known-good sync fingerprint, or None for anything short of
    a clean, matching-shape record - a missing file, unparsable JSON, or a
    cache written for a different host/remote_root are all cache MISSES,
    never treated as a match by default. See the fast-path comment above."""
    try:
        raw = _cache_path(cfg).read_text()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if not {"host", "remote_root", "hash"} <= data.keys():
        return None
    return data


def _cache_hit(cfg: Config, digest: str) -> bool:
    cached = _load_cache(cfg)
    if cached is None:
        return False
    return (
        cached["host"] == cfg.host
        and cached["remote_root"] == cfg.remote_root
        and cached["hash"] == digest
    )


def _write_cache(cfg: Config, digest: str) -> None:
    """Called only after rsync itself has just confirmed (exit 0) that the
    target now matches this exact local tree - never speculatively."""
    path = _cache_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"host": cfg.host, "remote_root": cfg.remote_root, "hash": digest}))
