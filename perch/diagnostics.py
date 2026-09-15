"""C5 - diagnostic mapper: parse the live stream, rewrite paths, and pass
everything else through byte-identical (I8).

This ticket (P3-1) is parsing only: recognize gcc/clang, linker and Python
traceback lines and extract their fields. Path rewriting (using the
(local_root, remote_root) pair C1 derives - this module does not derive it)
is P3-2; the structured --json event stream is P3-3.

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
        )

    m = _LINKER_RE.match(clean)
    if m:
        return Diagnostic(
            file=m.group("file"), line=None, col=None, severity="error", message=m.group("message")
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
        )

    return None


def is_event(diagnostic: Diagnostic) -> bool:
    """True for the diagnostics that get their own event (P3-3): a primary
    error or warning. A note is recognized (P3-1) so its path can still be
    rewritten (P3-2), but per DEVELOPMENT-PLAN.md it is not a NEW diagnostic;
    an include-chain line has no severity at all - it is pure context.
    """
    return diagnostic.severity in ("error", "warning")
