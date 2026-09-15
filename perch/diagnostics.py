"""C5 - diagnostic mapper: parse the live stream, rewrite paths, and pass
everything else through byte-identical (I8).

P3-1 (parsing) recognizes gcc/clang, linker and Python traceback lines and
extracts their fields. P3-2 (this module too - C5 is one module, per
ARCHITECTURE.md §4) adds path rewriting: turning the FILE a diagnostic names
into a local, `ls`-able path, using the (local_root, remote_root) pair C1
derives - this module never derives that pair itself, only consumes it. The
structured --json event stream is P3-3.

The fixture corpus this module is written against (P3-4,
tests/fixtures/*.stderr.txt, captured for real from the target - see
tests/fixtures/CAPTURE.md) came first, on purpose, so every pattern below
matches real gcc 14.2.0 / Python 3.13.5 output rather than a remembered guess.

PASS-THROUGH FIDELITY IS THE FRAGILE PROPERTY, not recognition. A line that
is not a diagnostic must emerge byte-identical (after only the ANSI strip
every line gets, diagnostic or not - see strip_ansi's docstring). Every
pattern below is deliberately narrow - see the noise fixture and the comment
above each pattern for the false-positive it was checked against.
"""

import re
from dataclasses import dataclass
from pathlib import Path

# Matches the SGR/CSI escape sequences gcc emits under
# -fdiagnostics-color=always (confirmed by raw byte capture -
# single_error_color.stderr.txt - real escapes sit directly before the
# filename, defeating an unstripped match). Applied to EVERY line, diagnostic
# or not, before anything else happens to it - colour codes are pointless
# noise once captured into a non-terminal pipe, and leaving them in would
# mean a JSON event's "message" field (P3-3) carries raw control bytes.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    """Strip ANSI SGR/CSI escape sequences. Applied to every line before
    matching AND before the line reaches the user - see module docstring for
    why stripped, not original, text is what's shown."""
    return _ANSI_RE.sub("", text)


@dataclass
class Diagnostic:
    """One recognized, file-referencing line. `severity` is None for a line
    that names a file but carries no error/warning/note of its own (the
    `In file included from` chain line) - the caller decides what counts as
    an emittable diagnostic (P3-3): only "error" and "warning" do. A "note"
    is recognized (so its path can still be rewritten for a human to click)
    but, per DEVELOPMENT-PLAN.md P3-1, is not a NEW diagnostic - it belongs
    to the error that preceded it, and gets no event of its own.
    """

    file: str
    line: int | None
    col: int | None
    severity: str | None  # "error" | "warning" | "note" | None
    message: str | None
    file_span: tuple[int, int]  # (start, end) of `file` within the CLEANED line -
    # P3-2 rewrites by slicing the original line at this exact span and
    # splicing in the resolved local path, rather than reconstructing the
    # whole line from its parsed pieces. A rebuild-from-parts risks silently
    # getting some OTHER piece of the line wrong (spacing, punctuation) even
    # when the file/line/col/severity/message themselves are all correct;
    # a span-based splice touches nothing but the file text itself.


# gcc/clang primary form: path:line:col: severity: message
#
# Requires THREE colon-delimited numeric/keyword fields before any text is
# accepted as a message - checked against noise.stdout.txt's
# "/home/towhid/.../data.bin: 42:17 not a diagnostic, just numbers" (a SPACE,
# not a colon, follows the first colon - already fails before the pattern
# even reaches the severity keyword) and
# "config.yaml:not-a-number:also-not-a-number: weird but not gcc shaped"
# (neither field is digits). Both correctly do not match.
_GCC_RE = re.compile(
    r"^(?P<file>[^:\s][^:]*):(?P<line>\d+):(?P<col>\d+):\s*"
    r"(?P<severity>error|warning|note):\s*(?P<message>.*)$"
)

# Linker form, a completely different shape from the above - no line, no
# col, a section+offset instead: path:(.text+0xOFFSET): message. Checked
# against the REST of the same linker_error.stderr.txt capture:
# "/usr/bin/ld: /tmp/ccIlWxtk.o: in function `main':" and
# "collect2: error: ld returned 1 exit status" do NOT reference a workspace
# file at all (a system binary path and a temp object file; "collect2" is a
# program name, not a file) - deliberately left unrecognized. Matching them
# would mean inventing a workspace path for a file that was never part of
# the workspace, which is worse than not recognizing the line at all.
_LINKER_RE = re.compile(r"^(?P<file>[^:\s][^:]*):\(\.[\w.]+\+0x[0-9a-fA-F]+\):\s*(?P<message>.*)$")

# Python traceback: File "path", line N[, in func]. Absolute paths in
# practice (see traceback.stderr.txt) but this pattern does not care either
# way - P3-2 resolves relative-vs-absolute, not this one.
_PYTHON_RE = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>.+))?$')

# gcc's "In file included from X:Y:" chain line, BEFORE the real error -
# names a DIFFERENT file than the one the actual diagnostic turns out to be
# in (confirmed: include_chain.stderr.txt's chain names include_main.c:2,
# but the real error is in include_util.h). No severity, no message - just a
# file reference worth rewriting for a human to follow the chain.
_INCLUDE_RE = re.compile(r"^In file included from (?P<file>.+?):(?P<line>\d+)(?::(?P<col>\d+))?:?\s*$")


def parse_line(line: str) -> Diagnostic | None:
    """Recognize one line. None means pass it through unchanged - the
    overwhelmingly common case, and the one this module must never get wrong.
    """
    clean = strip_ansi(line)

    m = _GCC_RE.match(clean)
    if m:
        return Diagnostic(
            file=m.group("file"),
            line=int(m.group("line")),
            col=int(m.group("col")),
            severity=m.group("severity"),
            message=m.group("message"),
            file_span=m.span("file"),
        )

    m = _LINKER_RE.match(clean)
    if m:
        return Diagnostic(
            file=m.group("file"), line=None, col=None, severity="error", message=m.group("message"),
            file_span=m.span("file"),
        )

    m = _PYTHON_RE.match(clean)
    if m:
        func = m.group("func")
        return Diagnostic(
            file=m.group("file"),
            line=int(m.group("line")),
            col=None,
            severity="error",
            message=(f"in {func}" if func else ""),
            file_span=m.span("file"),
        )

    m = _INCLUDE_RE.match(clean)
    if m:
        col = m.group("col")
        return Diagnostic(
            file=m.group("file"),
            line=int(m.group("line")),
            col=(int(col) if col else None),
            severity=None,
            message=None,
            file_span=m.span("file"),
        )

    return None


def is_event(diagnostic: Diagnostic) -> bool:
    """True for the diagnostics that get their own event (P3-3): a primary
    error or warning. A note is recognized (P3-1) so its path can still be
    rewritten (P3-2), but per DEVELOPMENT-PLAN.md it is not a NEW diagnostic;
    an include-chain line has no severity at all - it is pure context.
    """
    return diagnostic.severity in ("error", "warning")


# --------------------------------------------------------------------------
# P3-2: path rewriting.
# --------------------------------------------------------------------------

# GNU Make 4.4.1's own directory announcements under -C, confirmed to appear
# by default (no flag needed) on STDOUT - tests/fixtures/make_subdir.stdout.txt.
# "make[N]:" (a bracketed sub-make level) is handled defensively even though
# this target's single-level `-C` capture never produced one - a well-known,
# standard GNU Make behavior for RECURSIVE sub-makes, cheap to accept and
# safe: it only ever narrows which lines match, never widens it, since it is
# still anchored to the exact "Entering/Leaving directory '...'" text.
_ENTERING_RE = re.compile(r"^make(?:\[\d+\])?: Entering directory '(?P<dir>.+)'$")
_LEAVING_RE = re.compile(r"^make(?:\[\d+\])?: Leaving directory '(?P<dir>.+)'$")


class PathResolver:
    """Tracks the compiler's actual remote working directory across a run
    (P3-2's `make -C subdir` trap) and turns a diagnostic's raw file field
    into a local, `ls`-able path.

    Owns none of (local_root, remote_root) itself - both come from C1's
    Config, passed in once at construction. Never derives the pair; only
    consumes it (ARCHITECTURE.md: C1 is the only place it is derived).
    """

    def __init__(self, local_root: Path, remote_root: str):
        self.local_root = local_root
        self.remote_root = remote_root.rstrip("/")
        # A stack of directories relative to remote_root ("" == remote_root
        # itself), because Entering/Leaving is inherently LIFO - nested
        # `make -C` must pop back to the INTERMEDIATE level, not straight to
        # remote_root, even though only one level has been seen for real.
        self._cwd_stack: list[str] = [""]

    def observe(self, clean_line: str) -> None:
        """Watch one already-ANSI-stripped line for a directory change.
        Never raises, never rewrites the line - purely internal bookkeeping.
        """
        m = _ENTERING_RE.match(clean_line)
        if m:
            rel = self._workspace_relative(m.group("dir"))
            self._cwd_stack.append(rel if rel is not None else self._cwd_stack[-1])
            return
        m = _LEAVING_RE.match(clean_line)
        if m and len(self._cwd_stack) > 1:
            self._cwd_stack.pop()

    def _workspace_relative(self, path: str) -> str | None:
        """`path` (absolute or relative) as a path relative to remote_root,
        or None if it is not recognizably inside the workspace at all - a
        system path, a scratch file in /tmp, anything C3 never mirrored.
        """
        if path.startswith("/"):
            # remote_root may itself be absolute (an explicit config choice,
            # ARCHITECTURE.md/C1) or relative-to-$HOME (the common case) -
            # normalize to a single leading slash either way, or this
            # doubles up ("//srv/blinky/") and never matches anything.
            root_component = self.remote_root if self.remote_root.startswith("/") else f"/{self.remote_root}"
            needle = f"{root_component}/"
            idx = path.find(needle)
            if idx == -1:
                if path.rstrip("/") == root_component:
                    return ""
                return None
            return path[idx + len(needle):]
        # Relative: resolve against the compiler's CURRENT remote cwd, not
        # remote_root directly - this is the make -C subdir trap. gcc inside
        # `make -C subdir` names only "sub_error.c", relative to subdir.
        cwd = self._cwd_stack[-1]
        return f"{cwd}/{path}" if cwd else path

    def resolve(self, file: str) -> str | None:
        """The local, absolute, `ls`-able path for a diagnostic's raw file
        field, or None if it is not recognizably a workspace file (a system
        header, a /tmp object file) - callers must leave those untouched.
        """
        relative = self._workspace_relative(file)
        if relative is None:
            return None
        return str(self.local_root / relative)


def rewrite_line(line: str, diagnostic: Diagnostic, resolver: PathResolver) -> str:
    """Splice the resolved local path into `line` at the diagnostic's own
    file_span - see Diagnostic.file_span for why a splice, not a rebuild.
    Returns `line` completely unchanged if the file cannot be resolved
    (outside the workspace) - never fabricates a path.
    """
    local = resolver.resolve(diagnostic.file)
    if local is None:
        return line
    start, end = diagnostic.file_span
    return line[:start] + local + line[end:]


# --------------------------------------------------------------------------
# P3-3: the structured event stream. ARCHITECTURE.md §5/C5 gives the schema
# for stdout/stderr/diag events verbatim - these are exactly that shape.
# `exit` gains an `indeterminate` field beyond what ARCHITECTURE.md shows:
# that document predates P2-6, which is where "indeterminate" as a distinct
# outcome (not success, not failure, not merely "interrupted") was designed.
# Without this field a --json consumer would have to know that exit code 74
# specifically means indeterminate - exactly the kind of implicit knowledge
# structured events exist to avoid. Flagged as a deliberate extension, not a
# silent deviation.
# --------------------------------------------------------------------------


def stdout_event(line: str) -> dict:
    return {"t": "stdout", "line": line}


def stderr_event(line: str) -> dict:
    return {"t": "stderr", "line": line}


def event_for_stream(stream: str, line: str) -> dict:
    return stdout_event(line) if stream == "stdout" else stderr_event(line)


def diag_event(diagnostic: Diagnostic, resolved_file: str) -> dict:
    return {
        "t": "diag",
        "file": resolved_file,
        "line": diagnostic.line,
        "col": diagnostic.col,
        "severity": diagnostic.severity,
        "message": diagnostic.message,
    }


def exit_event(code: int, *, interrupted: bool, indeterminate: bool) -> dict:
    return {"t": "exit", "code": code, "interrupted": interrupted, "indeterminate": indeterminate}
