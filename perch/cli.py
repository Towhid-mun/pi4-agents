"""C7 (draft) - command surface: argument parsing, verb dispatch, exit codes.

cli.py orchestrates the sequence in ARCHITECTURE.md §6 and owns no mechanism of
its own. Every mechanism lives in a component module.

Phase 0 implemented steps 1, 3, 5 and 7. P1-1 adds step 2 (Attach - C2,
session.py) underneath every other step: a single Session per invocation,
reused for the mirror's rsync and the command's ssh alike. Step 4 (Claim) is
P4-1, step 6 (Stream through the diagnostic mapper) is P2-1/P3-1. Artifact
auto-pull in step 7 is P4-2.
"""

import argparse
import sys

from perch import __version__, config, errors, executor, mirror
from perch.config import COMMAND_VERBS
from perch.session import Session

EPILOG = """\
The target's copy of the workspace is derived and disposable: the mirror
deletes, so a file removed locally is removed on the target. Anything generated
on the target inside the project directory is lost on the next sync unless it
is excluded in .perch.toml.

Configuration lives in .perch.toml, discovered by walking up from the working
directory. Connection detail - user, address, port, key - belongs in
~/.ssh/config; perch only ever knows the alias.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="perch",
        description="Build and run this project on a remote target over SSH.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"perch {__version__}")

    verbs = parser.add_subparsers(dest="verb", metavar="<verb>", required=True)

    verbs.add_parser("sync", help="mirror the workspace to the target and stop")

    execute = verbs.add_parser(
        "exec", help="sync, then run an arbitrary command in the remote root"
    )
    execute.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        metavar="<cmd>...",
        help="command and arguments to run on the target",
    )

    for verb in COMMAND_VERBS:
        sub = verbs.add_parser(
            verb, help=f"sync, then run commands.{verb} from .perch.toml"
        )
        sub.add_argument(
            "args",
            nargs=argparse.REMAINDER,
            metavar="[args...]",
            help=f"extra arguments appended to commands.{verb}",
        )

    return parser


def _parse(parser: argparse.ArgumentParser, argv: list[str] | None) -> argparse.Namespace:
    """Parse, and keep argparse from choosing an exit code of its own.

    argparse exits 2 on a usage error, which sits inside the 1-63 band §7
    reserves for a remote command's own failure status - a caller could not
    tell a malformed command line from a build that failed. --help and
    --version still exit 0 through the normal path.
    """
    try:
        return parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code in (0, None):
            raise
        raise errors.ConfigError(
            "invalid command line (see perch --help)"
        ) from None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = _parse(parser, argv)
        return _dispatch(args)
    except errors.PerchError as exc:
        print(f"perch: {exc}", file=sys.stderr)
        return errors.exit_code_for(exc)
    except KeyboardInterrupt:
        # Phase 0/1 are signal-naive by design (P2-3 owns this). We did not
        # forward the interrupt and we have not confirmed that anything on the
        # target is dead, so exiting 130 would be a lie about I7. The state of
        # the run is genuinely unknown, which is what 74 means.
        print(
            "perch: interrupted. Whether the remote command is still running on "
            "the target is unknown - signal forwarding arrives in P2-3.",
            file=sys.stderr,
        )
        return errors.exit_code_for(errors.IndeterminateRun())


def _dispatch(args: argparse.Namespace) -> int:
    # 1 Resolve. Fail on an unresolvable target before doing anything else.
    cfg = config.load()

    # 2 Attach. One Session per invocation, reused for every step below -
    # this is what makes the mirror's rsync and the command's ssh share one
    # multiplexed connection instead of each paying their own handshake.
    session = Session(cfg.host)

    # 3 Mirror. Raises SyncError, which aborts before step 5 (I4).
    mirror.push(cfg, session)

    if args.verb == "sync":
        return errors.EXIT_OK

    command = _command_for(cfg, args)

    # 5 Execute.
    result = executor.run(cfg, command, session)

    # 7 Settle. Phase 0/1 settle by propagating the status and nothing else.
    return errors.exit_code_for_remote(result.exit_code)


def _command_for(cfg: config.Config, args: argparse.Namespace) -> str:
    if args.verb == "exec":
        if not args.command:
            raise errors.ConfigError("exec needs a command to run")
        return executor.join(args.command)

    command = cfg.command_for(args.verb)
    if args.args:
        command = f"{command} {executor.join(args.args)}"
    return command


if __name__ == "__main__":
    sys.exit(main())
