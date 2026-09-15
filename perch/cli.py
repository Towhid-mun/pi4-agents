"""C7 (draft) - command surface: argument parsing, verb dispatch, exit codes.

cli.py orchestrates the sequence in ARCHITECTURE.md §6 and owns no mechanism of
its own. Every mechanism lives in a component module.

Phase 0 implemented steps 1, 3, 5 and 7. Phase 1 adds step 2 (Attach - C2,
session.py) underneath every other step, and one read-only verb (`doctor`)
that intentionally skips steps 3 and 5 - it probes the target directly rather
than syncing and running a configured command. Step 4 (Claim) is P4-1, step 6
(Stream through the diagnostic mapper) is P2-1/P3-1. Artifact auto-pull in
step 7 is P4-2.
"""

import argparse
import shutil
import subprocess
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

`doctor` is read-only: it does not sync and does not need [commands].
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

    verbs.add_parser(
        "doctor",
        help="probe the target: identity, arch, memory, disk, toolchain, device files",
    )

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

    # --tty and --json apply to every verb that actually runs a command.
    # exec's REMAINDER would swallow them if placed after the command, so
    # they only work before the command/args - documented in --help.
    for sub in (execute, *[verbs.choices[v] for v in COMMAND_VERBS]):
        sub.add_argument(
            "--tty",
            action="store_true",
            help="allocate a pty for an interactive remote program (ADR-2); "
            "never combine with --json",
        )
        sub.add_argument(
            "--json",
            action="store_true",
            help="structured event stream on stdout (Phase 3 - not yet "
            "implemented; recognized now only so --tty --json can be refused)",
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
        # Every connection failure (P1-2) and every config failure is already
        # a classified PerchError with a one-line message by the time it gets
        # here - this is the one place a traceback would otherwise leak, and
        # it never does.
        print(f"perch: {exc}", file=sys.stderr)
        return errors.exit_code_for(exc)
    except KeyboardInterrupt:
        # A bare KeyboardInterrupt only reaches here from OUTSIDE a tracked
        # remote command - during config resolution or the mirror step (3),
        # before step 5 has captured any process group. executor.run()'s own
        # signal handling (P2-3) covers the window where one exists; there is
        # nothing here to confirm dead, so exiting 130 would be a lie about
        # I7. The state is genuinely unknown, which is what 74 means.
        print(
            "perch: interrupted before a remote process group existed to "
            "signal - state on the target is unknown.",
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

    if args.verb == "doctor":
        return _run_doctor(cfg, session)

    # 3 Mirror. Raises SyncError (or a classified connection error) which
    # aborts before step 5 (I4).
    mirror.push(cfg, session)

    if args.verb == "sync":
        return errors.EXIT_OK

    command = _command_for(cfg, args)

    tty = getattr(args, "tty", False)
    json_sink = getattr(args, "json", False)
    if tty and json_sink:
        # P2-5: a pty's streams are merged and carry control characters -
        # C5 (Phase 3) cannot classify that into structured events. Refused
        # here, not deeper in, so this is a config error (64) rather than
        # something that fails midway through a run.
        raise errors.ConfigError(
            "--tty and --json cannot be combined: a pty merges stdout and "
            "stderr into one stream, which cannot be classified into "
            "structured events"
        )

    # 5 Execute.
    result = executor.run(cfg, command, session, tty=tty)

    # 7 Settle. Phase 0/1 settled by propagating the status alone; P2 adds
    # the interrupted/indeterminate outcomes C4 can now report. P2-6 requires
    # the word itself, not just the number - a caller reading only an exit
    # code cannot tell 74-for-a-reboot apart from any other tool failure.
    if result.indeterminate:
        print(
            "perch: indeterminate - the channel closed without an exit status "
            "(the target likely rebooted or the network dropped mid-run). "
            "Not success, not failure. The next invocation reconnects unaided.",
            file=sys.stderr,
        )
    elif result.interrupted:
        print(
            "perch: interrupted - the remote process group was signalled and "
            "confirmed dead.",
            file=sys.stderr,
        )
    return errors.exit_code_for_run(
        result.exit_code, interrupted=result.interrupted, indeterminate=result.indeterminate
    )


def _command_for(cfg: config.Config, args: argparse.Namespace) -> str:
    if args.verb == "exec":
        if not args.command:
            raise errors.ConfigError("exec needs a command to run")
        return executor.join(args.command)

    command = cfg.command_for(args.verb)
    if args.args:
        command = f"{command} {executor.join(args.args)}"
    return command


# --------------------------------------------------------------------------
# doctor (P1-3)
#
# Read-only, so it does not go through mirror.push - a project need not even
# have valid [commands] for doctor to run. The probe is one POSIX sh script
# sent over ssh's stdin (session.run_capturing), never interpolated into the
# command line: a script embedded in the command line has to survive quoting
# through two shells (ours building the ssh argv, then the remote shell
# parsing it), and that is exactly the kind of thing a stray quote or space
# defeats. Each line of output is tagged so it can't be confused with
# anything the shell itself might print.
# --------------------------------------------------------------------------

_PROBE_TAG = "PERCH:"

_PROBE_SCRIPT = r"""
field() { printf '%s%s=%s\n' "$1" "$2" "$3"; }
have() { command -v "$1" >/dev/null 2>&1; }
TAG="__TAG__"

if [ -r /etc/os-release ]; then
  os_val="$(. /etc/os-release 2>/dev/null; printf '%s' "${PRETTY_NAME:-unknown}")"
else
  os_val="unknown"
fi
field "$TAG" os "$os_val"

field "$TAG" kernel "$(uname -r 2>/dev/null || echo unknown)"
field "$TAG" arch "$(uname -m 2>/dev/null || echo unknown)"

if have free; then
  field "$TAG" mem "$(free -h | awk 'NR==2{print $2" total, "$7" available"}')"
else
  field "$TAG" mem unknown
fi

if have df; then
  field "$TAG" disk "$(df -h / | awk 'NR==2{print $4" free of "$2}')"
else
  field "$TAG" disk unknown
fi

for tool in gcc make python3 rsync; do
  if have "$tool"; then
    field "$TAG" "tool_$tool" "$("$tool" --version 2>&1 | head -1)"
  else
    field "$TAG" "tool_$tool" __MISSING__
  fi
done

if have gpiodetect || dpkg -s libgpiod2 >/dev/null 2>&1 || dpkg -s gpiod >/dev/null 2>&1; then
  field "$TAG" lib_libgpiod present
else
  field "$TAG" lib_libgpiod __MISSING__
fi

i2c="$(ls /dev/i2c-* 2>/dev/null | tr '\n' ' ')"
if [ -n "$i2c" ]; then field "$TAG" dev_i2c "$i2c"; else field "$TAG" dev_i2c __MISSING__; fi

gpiochip="$(ls /dev/gpiochip* 2>/dev/null | tr '\n' ' ')"
if [ -n "$gpiochip" ]; then field "$TAG" dev_gpiochip "$gpiochip"; else field "$TAG" dev_gpiochip __MISSING__; fi
""".replace("__TAG__", _PROBE_TAG)


def parse_probe_output(text: str) -> dict[str, str]:
    """Pull PERCH:key=value lines out of the probe's stdout.

    Anything else on stdout is ignored rather than mis-parsed - defensive
    against a login banner or a shell warning landing on the same stream.
    """
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith(_PROBE_TAG):
            continue
        key_value = line[len(_PROBE_TAG):]
        key, sep, value = key_value.partition("=")
        if sep:
            fields[key] = value
    return fields


def _present(fields: dict[str, str], key: str, default: str = "unknown") -> str:
    value = fields.get(key, default)
    return "MISSING" if value == "__MISSING__" else value


def _host_rsync_version() -> str:
    rsync = shutil.which("rsync")
    if rsync is None:
        return "MISSING"
    completed = subprocess.run([rsync, "--version"], capture_output=True, text=True)
    first_line = completed.stdout.splitlines()[0] if completed.stdout else "unknown"
    return first_line


def format_doctor_report(host: str, session: Session, was_alive: bool, fields: dict[str, str]) -> str:
    socket_state = "already live" if was_alive else "established this run"
    rows = [
        ("target", host),
        ("reachable", "yes"),
        ("control socket", f"{session.control_path} ({socket_state})"),
        ("", ""),
        ("os", _present(fields, "os")),
        ("kernel", _present(fields, "kernel")),
        ("architecture", _present(fields, "arch")),
        ("memory", _present(fields, "mem")),
        ("disk (/)", _present(fields, "disk")),
        ("", ""),
        ("gcc", _present(fields, "tool_gcc")),
        ("make", _present(fields, "tool_make")),
        ("python3", _present(fields, "tool_python3")),
        ("rsync", _present(fields, "tool_rsync")),
        ("libgpiod", _present(fields, "lib_libgpiod")),
        ("/dev/i2c-*", _present(fields, "dev_i2c")),
        ("/dev/gpiochip*", _present(fields, "dev_gpiochip")),
        ("", ""),
        ("host rsync", f"{_host_rsync_version()}   (this Mac, from S0-1)"),
    ]
    label_width = max(len(label) for label, _ in rows if label)
    lines = []
    for label, value in rows:
        if not label:
            lines.append("")
        else:
            lines.append(f"{label.ljust(label_width)}   {value}")
    return "\n".join(lines)


def _run_doctor(cfg: config.Config, session: Session) -> int:
    was_alive = session.alive()
    completed = session.run_capturing("sh -s", input=_PROBE_SCRIPT)
    if completed.returncode != 0:
        raise errors.InternalError(
            f"probe script exited {completed.returncode} on {cfg.host}: "
            f"{completed.stderr.strip()}"
        )
    fields = parse_probe_output(completed.stdout)
    print(format_doctor_report(cfg.host, session, was_alive, fields))
    return errors.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
