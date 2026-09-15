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
import platform
import shutil
import subprocess
import sys

from pathlib import Path

from perch import __version__, artifacts, config, errors, executor, mirror
from perch.config import COMMAND_VERBS
from perch.session import Session

EPILOG = """\
The target's copy of the workspace is derived and disposable: the mirror
deletes, so a file removed locally is removed on the target. Anything generated
on the target inside the project directory is lost on the next sync unless it
is excluded in .perch.toml, or retrieved first with `pull` (or auto-pulled via
[artifacts] after a successful run).

`pull`'s default destination (.perch/artifacts/) is excluded from the mirror,
so a pulled file is never pushed back to the target. An explicit destination
you name yourself is NOT protected: if it lands inside the workspace and is
not excluded, the next sync pushes it back, which can overwrite a fresher
target-side build with your now-stale local copy.

Configuration lives in .perch.toml, discovered by walking up from the working
directory. Connection detail - user, address, port, key - belongs in
~/.ssh/config; perch only ever knows the alias.

`doctor` and `pull` are read-only: neither syncs, and `pull` does not need
[commands].

A sync is skipped entirely when the local content hash matches the one
recorded after the last real sync (.perch/sync-cache.json) - this can never
see a change made directly on the target (e.g. over your own ssh session),
only a change on the host. Pass --force-sync to always run rsync.
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

    sync = verbs.add_parser("sync", help="mirror the workspace to the target and stop")
    sync.add_argument(
        "--force-sync",
        action="store_true",
        help="run rsync even if the fast path (P4-4) would otherwise skip "
        "it because the local content hash matches the last known-synced "
        "state - use this if the target may have diverged out of band "
        "(e.g. an edit made directly on it over ssh)",
    )

    verbs.add_parser(
        "doctor",
        help="probe the target: identity, arch, memory, disk, toolchain, device files",
    )

    init = verbs.add_parser(
        "init",
        help="scaffold .perch.toml, .vscode/tasks.json and a CLAUDE.md into "
        "the current directory, then run doctor",
    )
    init.add_argument(
        "alias",
        nargs="?",
        default=None,
        help="ssh alias from ~/.ssh/config (omit to choose interactively "
        "from the aliases found there)",
    )
    init.add_argument(
        "--force",
        action="store_true",
        help="overwrite any of .perch.toml / .vscode/tasks.json / CLAUDE.md "
        "that already exist (default: leave them alone and say so)",
    )

    pull = verbs.add_parser(
        "pull",
        help="retrieve files matching a glob from the target (glob expands "
        "on the TARGET's shell, not locally)",
    )
    pull.add_argument("glob", help="pattern, expanded on the target, relative to remote_root")
    pull.add_argument(
        "dest",
        nargs="?",
        default=None,
        help="local destination directory (default: .perch/artifacts/, which "
        "is excluded from the mirror so it is never pushed back - see --help)",
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
            help="structured event stream on stdout",
        )
        sub.add_argument(
            "--replace",
            action="store_true",
            help="take the run lock from a live holder: kill that process "
            "group first (confirmed dead), then take over. Without this, a "
            "held lock fails fast (exit 75) naming the holder.",
        )
        sub.add_argument(
            "--force-sync",
            action="store_true",
            help="run rsync even if the fast path (P4-4) would otherwise "
            "skip it because the local content hash matches the last "
            "known-synced state - use this if the target may have diverged "
            "out of band (e.g. an edit made directly on it over ssh)",
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


def _refuse_if_running_on_the_target() -> None:
    """P5-1. ARCHITECTURE.md §1 fixes Host as macOS - perch itself is meant
    to run there and ssh OUT to the target, never the reverse. The
    realistic way this gets violated by accident is a VS Code window
    connected to the Pi over Remote-SSH: a task's shell then runs ON the
    Pi, and `perch` (which itself shells out to `ssh <alias>`) would try
    to ssh from the Pi to itself - or, since the Pi has its own separate
    `~/.ssh/config`, more likely just fail to resolve the alias at all.
    Either way that is a confusing, working-looking failure days away
    from this one obvious cause, not a clean error - so it is checked and
    refused here, before config resolution or any network activity,
    rather than left to surface however the connection attempt happens to
    fail.
    """
    if platform.system() != "Darwin":
        raise errors.ConfigError(
            f"this looks like {platform.system()} ({platform.machine()}), "
            "not macOS - perch must run on the HOST, never the target. If "
            "this is a VS Code window connected to the Pi over Remote-SSH, "
            "close it and reopen the project as a plain LOCAL window."
        )


def _dispatch(args: argparse.Namespace) -> int:
    _refuse_if_running_on_the_target()

    if args.verb == "init":
        # Scaffolds the very config step 1 (Resolve) is about to require -
        # runs before config.load(), the only verb that must.
        return _run_init(args.alias, force=args.force)

    # 1 Resolve. Fail on an unresolvable target before doing anything else.
    cfg = config.load()

    # 2 Attach. One Session per invocation, reused for every step below -
    # this is what makes the mirror's rsync and the command's ssh share one
    # multiplexed connection instead of each paying their own handshake.
    session = Session(cfg.host)

    if args.verb == "doctor":
        return _run_doctor(cfg, session)

    if args.verb == "pull":
        # Read-only, like doctor: no sync (there is nothing here that needs
        # the workspace to match first), no run lock (not an execution).
        return _run_pull(cfg, session, args.glob, args.dest)

    tty = getattr(args, "tty", False)
    json_sink = getattr(args, "json", False)
    replace = getattr(args, "replace", False)
    force_sync = getattr(args, "force_sync", False)
    if tty and json_sink:
        # P2-5: a pty's streams are merged and carry control characters -
        # C5 (Phase 3) cannot classify that into structured events. Refused
        # here, before touching the network at all, so this is a config
        # error (64) rather than something that fails midway through a run.
        raise errors.ConfigError(
            "--tty and --json cannot be combined: a pty merges stdout and "
            "stderr into one stream, which cannot be classified into "
            "structured events"
        )

    # 3 Mirror. Raises SyncError (or a classified connection error) which
    # aborts before step 5 (I4). quiet=json_sink: P3-3 requires stdout carry
    # only JSON in --json mode, and rsync's own itemized change list is tool
    # progress, not part of the remote command's diagnostic stream.
    mirror.push(cfg, session, quiet=json_sink, force_sync=force_sync)

    if args.verb == "sync":
        return errors.EXIT_OK

    command = _command_for(cfg, args)

    # 4 Claim, 5 Execute. run() takes the lock (exit 75 if held and not
    # --replace) before either mode ever runs the command, and releases it
    # on every path out (P4-1).
    result = executor.run(cfg, command, session, tty=tty, json_mode=json_sink, replace=replace)

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
    elif result.exit_code == 0 and cfg.artifacts:
        # Auto-pull (P4-2/C6): ONLY on the remote command's own exit 0 -
        # ARCHITECTURE.md is explicit about that condition, not "the tool
        # didn't error." Always stderr, json_sink or not - Settle-step
        # housekeeping already goes there regardless of mode (P2-4's
        # reaping notice, P2-6's indeterminate/interrupted wording), and
        # --json's stdout must carry nothing but the event stream, which
        # already ended with its own exit event before this runs.
        _auto_pull(cfg, session)
    return errors.exit_code_for_run(
        result.exit_code, interrupted=result.interrupted, indeterminate=result.indeterminate
    )


def _auto_pull(cfg: config.Config, session: Session) -> None:
    """Pull every configured artifact glob, in order, stopping at the first
    failure (PullError propagates - a secondary failure after an otherwise
    successful run is still a real failure the caller should see, not one
    to swallow silently)."""
    for glob in cfg.artifacts:
        matched = artifacts.pull(cfg, session, glob)
        if matched:
            print(
                f"perch: pulled {len(matched)} file(s) matching {glob!r} into "
                f"{artifacts.default_dest(cfg)}: {', '.join(matched)}",
                file=sys.stderr,
            )
        else:
            print(f"perch: no files matched {glob!r} - nothing to pull", file=sys.stderr)


def _run_pull(cfg: config.Config, session: Session, glob: str, dest: str | None) -> int:
    dest_path = Path(dest).resolve() if dest is not None else artifacts.default_dest(cfg)
    matched = artifacts.pull(cfg, session, glob, dest_path)
    if matched:
        print(f"pulled {len(matched)} file(s) into {dest_path}:")
        for path in matched:
            print(f"  {path}")
    else:
        print(f"no files matched {glob!r} on {cfg.host}:{cfg.remote_root}")
    return errors.EXIT_OK


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
# init (P5-3, C8) - onboarding. Thin, like the rest of Phase 5: it writes
# the same three artifacts a person would otherwise copy by hand
# (.perch.toml, .vscode/tasks.json, a CLAUDE.md) and then runs the
# already-existing `doctor` verb, so the very first thing this command
# does after scaffolding is prove (or disprove) that what it wrote works -
# "it works" or a precise statement of what's missing, never silence.
#
# The one deliberate exception to I9 ("every command works
# non-interactively") in this whole tool: with no alias argument, this
# prompts on stdin. That is what the ticket asks for ("with no argument,
# list the aliases... and ask") and it is inherent to a one-time,
# by-a-human onboarding command - not something an agent's build loop ever
# calls this way. An agent (or a script) should always pass the alias
# explicitly (`perch init pi`), which never prompts. Called with no
# argument and no terminal to ask on, it fails fast rather than hanging.
# --------------------------------------------------------------------------

_TASKS_JSON_TEMPLATE = """\
// perch's editor integration. Thin: these tasks call the perch CLI and
// nothing else. The problem matcher below was derived and verified
// against real output.
//
// *** DO NOT open this project with VS Code Remote-SSH connected to the target. ***
// These tasks assume they are running on the HOST. If the window is
// attached to the target over Remote-SSH, the task's shell runs ON THE
// TARGET, so `perch` (which itself shells out to `ssh <alias>`) would try
// to SSH from the target to itself. It will not fail cleanly. Open this
// project in a plain LOCAL window (the remote indicator, if your editor
// has one, should read nothing - never a Remote-SSH host).
{
  "version": "2.0.0",
  "tasks": [
    {
      "label": "perch: build",
      "type": "shell",
      "command": "perch",
      "args": ["build"],
      "group": { "kind": "build", "isDefault": true },
      "presentation": { "reveal": "always", "panel": "shared", "clear": true },
      "problemMatcher": {
        // C5 (perch/diagnostics.py) rewrites a diagnostic's file field to
        // an ABSOLUTE local path before this ever reaches the terminal, so
        // fileLocation is "absolute" - NOT ["relative", "${workspaceFolder}"].
        "owner": "perch",
        "applyTo": "allDocuments",
        "fileLocation": "absolute",
        "pattern": {
          "regexp": "^(.*?):(\\\\d+):(\\\\d+):\\\\s+(error|warning):\\\\s+(.*)$",
          "file": 1,
          "line": 2,
          "column": 3,
          "severity": 4,
          "message": 5
        }
      }
    },
    {
      "label": "perch: test",
      "type": "shell",
      "command": "perch",
      "args": ["test"],
      "group": { "kind": "test", "isDefault": true },
      "presentation": { "reveal": "always", "panel": "shared", "clear": true },
      "problemMatcher": {
        "owner": "perch",
        "applyTo": "allDocuments",
        "fileLocation": "absolute",
        "pattern": {
          "regexp": "^(.*?):(\\\\d+):(\\\\d+):\\\\s+(error|warning):\\\\s+(.*)$",
          "file": 1,
          "line": 2,
          "column": 3,
          "severity": 4,
          "message": 5
        }
      }
    },
    {
      "label": "perch: run",
      "type": "shell",
      "command": "perch",
      "args": ["run"],
      "group": "none",
      "presentation": { "reveal": "always", "panel": "shared", "clear": true },
      "problemMatcher": {
        "owner": "perch",
        "applyTo": "allDocuments",
        "fileLocation": "absolute",
        "pattern": {
          "regexp": "^(.*?):(\\\\d+):(\\\\d+):\\\\s+(error|warning):\\\\s+(.*)$",
          "file": 1,
          "line": 2,
          "column": 3,
          "severity": 4,
          "message": 5
        }
      }
    },
    {
      "label": "perch: doctor",
      "type": "shell",
      "command": "perch",
      "args": ["doctor"],
      "group": "none",
      "presentation": { "reveal": "always", "panel": "shared", "clear": true },
      "problemMatcher": []
    }
  ]
}
"""

_CLAUDE_MD_TEMPLATE = """\
# Agent instructions

This project builds, tests and runs on a remote target through `perch`
(`perch --help`), never locally.

- **Never build or run this project's code locally.** Only the target can
  compile and run it - use `perch build` / `perch test` / `perch run` /
  `perch exec <cmd>`.
- **This workspace is the source of truth.** The target's copy is derived
  and disposable - the mirror deletes, so a file removed here is removed
  there too on the next sync.
- **Target-side output that matters comes back with `perch pull`.**
  Anything generated on the target and not pulled (or listed under
  `[artifacts]` in `.perch.toml`) is lost on the next sync.
"""


def _list_ssh_aliases() -> list[str]:
    """Host patterns from ~/.ssh/config, in file order, skipping any
    pattern containing a wildcard/negation character (`*`, `?`, `!`) -
    `Host *` and similar catch-alls are ssh_config plumbing, not a target
    a project would ever name. A line may list several patterns
    space-separated (real ssh_config syntax); each is considered on its
    own, in order, so a wildcard sitting next to a real alias on the same
    line does not hide the real one.
    """
    ssh_config = Path.home() / ".ssh" / "config"
    if not ssh_config.is_file():
        return []
    aliases = []
    for line in ssh_config.read_text().splitlines():
        parts = line.strip().split()
        if len(parts) < 2 or parts[0].lower() != "host":
            continue
        for pattern in parts[1:]:
            if any(c in pattern for c in "*?!"):
                continue
            aliases.append(pattern)
    return aliases


def _choose_ssh_alias() -> str:
    aliases = _list_ssh_aliases()
    if not aliases:
        raise errors.ConfigError(
            "no ssh alias given, and none found in ~/.ssh/config to choose "
            "from. Add a Host entry there, or run `perch init <alias>` "
            "directly."
        )
    if not sys.stdin.isatty():
        raise errors.ConfigError(
            "no ssh alias given, and stdin is not a terminal to ask "
            "interactively. Run `perch init <alias>` with an explicit alias."
        )
    print("ssh aliases found in ~/.ssh/config:", file=sys.stderr)
    for index, alias in enumerate(aliases, start=1):
        print(f"  {index}. {alias}", file=sys.stderr)
    while True:
        try:
            choice = input(f"Choose a target [1-{len(aliases)}]: ").strip()
        except EOFError:
            raise errors.ConfigError("no choice made - run `perch init <alias>` instead") from None
        if choice.isdigit() and 1 <= int(choice) <= len(aliases):
            return aliases[int(choice) - 1]
        print(f"not a number from 1 to {len(aliases)}", file=sys.stderr)


def _guess_build_command(project_dir: Path) -> str | None:
    """The one command P5-3 actually guesses - `test`/`run` are too
    project-specific to guess safely and are left as commented examples
    instead (see _render_perch_toml)."""
    if (project_dir / "Makefile").is_file() or (project_dir / "makefile").is_file():
        return "make"
    if (project_dir / "pyproject.toml").is_file():
        return "python3 -m build"
    return None


def _render_perch_toml(alias: str, project_dir: Path) -> str:
    """COMMENTS explaining each field - for most users this generated file
    IS the documentation, not something they read ARCHITECTURE.md to
    understand first."""
    remote_root = f"perch/{project_dir.name}"
    guessed_build = _guess_build_command(project_dir)
    build_line = f'build = "{guessed_build}"' if guessed_build else '# build = "make"'
    return f"""\
# perch project configuration, written by `perch init`.
# For the full command surface: `perch --help`.

# The ssh alias to build/run on. Resolved entirely by ssh via your own
# ~/.ssh/config - perch never stores a hostname, port, user or key.
host = "{alias}"

# Where the project lives on the target, relative to $HOME there (or an
# absolute path). Guessed from this directory's own name.
remote_root = "{remote_root}"

# Extra glob patterns to exclude from the mirror, one per line, beyond the
# built-in list (.git/, .venv/, __pycache__/, node_modules/, *.o, *.pyc,
# .DS_Store, .perch.toml, .perch/).
# exclude = ["build/", "*.bin"]

# Globs auto-pulled from the target into .perch/artifacts/ after every
# successful run. Anything generated on the target and not listed here
# (or retrieved with `perch pull`) is lost on the next sync.
# artifacts = ["main"]

[commands]
# Shell commands run on the target, inside remote_root, after every sync.
# Extra arguments given to `perch build`/`test`/`run` are appended to these.
{build_line}
# test = "make test"
# `run` gets its OWN sync first, same as every verb - a binary built by a
# separate, earlier `perch build` exists only on the target, so that sync
# deletes it (I2) before this command ever runs. Rebuild in the same
# command, don't rely on a previous build's artifact surviving:
# run = "make && ./main"
"""


def _write_scaffold_file(path: Path, content: str, *, force: bool) -> bool:
    """Writes `content` to `path` unless it already exists and `force` is
    False. Returns whether it was (over)written, so the caller can report
    exactly what happened - P5-3's own done-when includes "must say which
    files it wrote"."""
    if path.exists() and not force:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return True


def _run_init(alias: str | None, *, force: bool) -> int:
    if alias is None:
        alias = _choose_ssh_alias()

    project_dir = Path.cwd()
    scaffold = (
        (project_dir / config.CONFIG_FILENAME, _render_perch_toml(alias, project_dir)),
        (project_dir / ".vscode" / "tasks.json", _TASKS_JSON_TEMPLATE),
        (project_dir / "CLAUDE.md", _CLAUDE_MD_TEMPLATE),
    )
    for path, content in scaffold:
        relative = path.relative_to(project_dir)
        if _write_scaffold_file(path, content, force=force):
            print(f"perch: wrote {relative}", file=sys.stderr)
        else:
            print(
                f"perch: {relative} already exists - left it alone (--force to overwrite)",
                file=sys.stderr,
            )

    perch_toml = project_dir / config.CONFIG_FILENAME
    print(f"perch: running `perch doctor` against {alias!r}...", file=sys.stderr)
    cfg = config.load_file(perch_toml)
    session = Session(cfg.host)
    return _run_doctor(cfg, session)


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
