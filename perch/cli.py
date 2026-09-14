"""C7 - command surface: argument parsing, verb dispatch, exit codes.

cli.py orchestrates and owns no mechanism of its own (ARCHITECTURE.md §4).
Every mechanism lives in a component module; the sequence lives here.
"""

import argparse
import sys

from perch import __version__
from perch import errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="perch",
        description="Build and run this project on a remote target over SSH.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"perch {__version__}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        parser.parse_args(argv)
    except errors.PerchError as exc:
        print(f"perch: {exc}", file=sys.stderr)
        return errors.exit_code_for(exc)

    # Verbs arrive in P0-5.
    parser.print_help(sys.stderr)
    return errors.EXIT_CONFIG


if __name__ == "__main__":
    sys.exit(main())
