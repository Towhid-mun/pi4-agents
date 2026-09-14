"""Exception hierarchy and the exit-code contract (ARCHITECTURE.md §7).

This module is the ONLY place in the codebase that chooses an exit code. No
other module may contain a numeric exit status, and no other module may call
sys.exit with a literal. Everything else raises a PerchError subclass and lets
cli.py translate it here.

The contract:

    0        remote command succeeded
    1-63     remote command failed - passed through unchanged
    64       config error (missing, malformed, unresolvable target)
    69       target unreachable
    70       internal tool error
    73       sync failed or incomplete
    74       run indeterminate - channel closed without a status
    75       run lock held by another invocation
    130      interrupted, and the remote process group confirmed dead
"""

EXIT_OK = 0
EXIT_CONFIG = 64
EXIT_UNREACHABLE = 69
EXIT_INTERNAL = 70
EXIT_SYNC = 73
EXIT_INDETERMINATE = 74
EXIT_LOCK_HELD = 75
EXIT_INTERRUPTED = 130

# The band reserved for a remote command's own failure status (I6).
REMOTE_PASSTHROUGH_RANGE = range(1, 64)


class PerchError(Exception):
    """Base for every tool-level failure.

    Subclasses set `exit_code`. The base class itself maps to "internal tool
    error" so that an unclassified failure is never mistaken for a remote
    command's own exit status.
    """

    exit_code = EXIT_INTERNAL


class ConfigError(PerchError):
    """Config missing, malformed, or naming a target that cannot be resolved."""

    exit_code = EXIT_CONFIG


class TargetUnreachable(PerchError):
    """The target did not answer within the connect timeout."""

    exit_code = EXIT_UNREACHABLE


class InternalError(PerchError):
    """A bug in this tool, or an assumption that did not hold."""

    exit_code = EXIT_INTERNAL


class SyncError(PerchError):
    """The mirror failed or completed only partially.

    Raised before anything is executed against the tree (I4).
    """

    exit_code = EXIT_SYNC


class IndeterminateRun(PerchError):
    """The channel closed without delivering an exit status.

    Not success, not failure - the target most likely rebooted mid-run.
    """

    exit_code = EXIT_INDETERMINATE


class RunLockHeld(PerchError):
    """Another invocation holds this project's run lock."""

    exit_code = EXIT_LOCK_HELD


class Interrupted(PerchError):
    """Local SIGINT, and the remote process group was confirmed dead.

    Never raise this without that confirmation - doing so is a lie about I7.
    """

    exit_code = EXIT_INTERRUPTED


def exit_code_for_remote(code: int) -> int:
    """Translate a remote command's exit status into this tool's exit status.

    I6 is unconditional: the target's exit code is the tool's exit code, so
    this is the identity function and deliberately stays that way.

    Note the known collision: the tool-error codes above (64, 69, 70, 73, 74,
    75, 130) are outside the 1-63 band §7 reserves for remote failures, but a
    remote command is not obliged to stay in that band - a shell reports 127
    for "command not found" and 130 for its own SIGINT. When that happens the
    caller cannot distinguish the two sources from the exit status alone. I6
    says the remote status wins, so it does. The fix is out-of-band signalling
    (the --json `exit` event, P3-3), not clamping the number here.
    """
    if not isinstance(code, int):
        raise InternalError(f"remote exit status was not an integer: {code!r}")
    return code


def exit_code_for(exc: BaseException) -> int:
    """Map any exception to its documented exit code."""
    if isinstance(exc, PerchError):
        return exc.exit_code
    return EXIT_INTERNAL


def exit_code_for_run(exit_code: int, *, interrupted: bool = False, indeterminate: bool = False) -> int:
    """Map a C4 RunResult's fields to the tool's exit status (P2).

    Indeterminate wins over interrupted, which wins over ordinary passthrough
    - see ARCHITECTURE.md §7. A confirmed-dead interrupt and an indeterminate
    run each have exactly one fixed code, regardless of whatever `exit_code`
    happened to be in flight when they were detected; `exit_code` is only
    consulted in the ordinary case.

    Note: a remote command that happens to exit 130 or 74 entirely on its own
    (not from anything perch did) still passes through as 130 or 74 here -
    the same I6-over-clarity tradeoff already recorded for the 127 collision
    in exit_code_for_remote's docstring, not a new one.
    """
    if indeterminate:
        return EXIT_INDETERMINATE
    if interrupted:
        return EXIT_INTERRUPTED
    return exit_code_for_remote(exit_code)
