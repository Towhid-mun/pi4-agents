"""C5 diagnostic parsing (P3-1). Entirely offline - fixtures are plain text
files captured from the real target (tests/fixtures/, see CAPTURE.md), never
a live connection. This is the fast feedback loop; keep it that way forever.
"""

import unittest
from pathlib import Path

from perch import diagnostics
from perch.diagnostics import Diagnostic, is_event, parse_line, strip_ansi

FIXTURES = Path(__file__).parent / "fixtures"


def lines_of(name: str) -> list[str]:
    text = (FIXTURES / name).read_text()
    return text.split("\n")[:-1] if text.endswith("\n") else text.split("\n")


class TestNoiseIsByteIdentical(unittest.TestCase):
    """The single most important property in this module (I8). A careless
    pattern corrupts real program output silently - assert byte-equality,
    not just "looks unchanged"."""

    def test_every_noise_line_is_unrecognized(self):
        for line in lines_of("noise.stdout.txt"):
            with self.subTest(line=line):
                self.assertIsNone(parse_line(line))

    def test_stripped_noise_is_byte_identical_to_the_original(self):
        # Noise has no ANSI in it, so strip_ansi must be a true no-op here -
        # this is the property that matters for I8, checked byte for byte.
        original = (FIXTURES / "noise.stdout.txt").read_bytes()
        stripped = strip_ansi(original.decode("utf-8")).encode("utf-8")
        self.assertEqual(stripped, original)

    def test_specific_adversarial_noise_lines_individually(self):
        # These were deliberately shaped to look diagnostic-ish - each one
        # is checked on its own so a future regression names the exact line
        # that broke, not just "some noise line failed".
        adversarial = [
            "12:34:56 INFO starting up",
            "/usr/bin/gcc: this looks like a compiler but is not one: 404",
            "src/main.c: this has a colon but no line:col shape at all",
            "error: standalone word 'error' with no file prefix",
            "/home/towhid/perch-diag-capture/data.bin: 42:17 not a diagnostic, just numbers",
            "warning level: 3, retry count: 12",
            "config.yaml:not-a-number:also-not-a-number: weird but not gcc shaped",
            "no newline at all, just a bare colon: and done",
        ]
        for line in adversarial:
            with self.subTest(line=line):
                self.assertIsNone(parse_line(line))

    def test_the_no_trailing_newline_line_is_still_present(self):
        # noise.sh's last line has no trailing \n - confirm the fixture
        # itself still has it (a capture or editor could silently add one).
        raw = (FIXTURES / "noise.stdout.txt").read_bytes()
        self.assertFalse(raw.endswith(b"\n"))


class TestCleanBuildHasNothingToParse(unittest.TestCase):
    def test_both_streams_are_empty(self):
        self.assertEqual((FIXTURES / "clean.stdout.txt").read_bytes(), b"")
        self.assertEqual((FIXTURES / "clean.stderr.txt").read_bytes(), b"")


class TestGccSingleError(unittest.TestCase):
    def test_the_primary_line_parses(self):
        diag = parse_line("single_error.c:5:22: error: expected ‘;’ before ‘return’")
        self.assertEqual(
            diag,
            Diagnostic(
                file="single_error.c", line=5, col=22, severity="error",
                message="expected ‘;’ before ‘return’",
            ),
        )
        self.assertTrue(is_event(diag))

    def test_the_no_line_col_context_line_is_not_recognized(self):
        # "file: In function 'X':" has the same "file: text" shape as a real
        # noise line (src/main.c: this has a colon...) - deliberately left
        # unrecognized rather than risk that collision. It carries no new
        # information the primary line right after it doesn't already have.
        self.assertIsNone(parse_line("single_error.c: In function ‘main’:"))

    def test_source_echo_and_caret_lines_are_not_recognized(self):
        for line in ("    5 |     printf(\"%d\\n\", x)", "      |                      ^"):
            with self.subTest(line=line):
                self.assertIsNone(parse_line(line))

    def test_full_fixture_produces_exactly_one_event(self):
        diags = [d for d in (parse_line(l) for l in lines_of("single_error.stderr.txt")) if d]
        events = [d for d in diags if is_event(d)]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].file, "single_error.c")


class TestGccMultiError(unittest.TestCase):
    def test_three_errors_and_one_note(self):
        diags = [d for d in (parse_line(l) for l in lines_of("multi_error.stderr.txt")) if d]
        severities = [d.severity for d in diags]
        self.assertEqual(severities, ["error", "error", "note", "error"])

    def test_only_the_errors_are_events_the_note_is_not(self):
        diags = [d for d in (parse_line(l) for l in lines_of("multi_error.stderr.txt")) if d]
        self.assertEqual(sum(1 for d in diags if is_event(d)), 3)

    def test_the_note_carries_its_own_location_not_the_prior_errors(self):
        diags = [d for d in (parse_line(l) for l in lines_of("multi_error.stderr.txt")) if d]
        note = diags[2]
        self.assertEqual(note.severity, "note")
        self.assertEqual((note.file, note.line, note.col), ("multi_error.c", 6, 26))


class TestGccWarningsOnly(unittest.TestCase):
    def test_two_warnings_both_events(self):
        diags = [d for d in (parse_line(l) for l in lines_of("warnings_only.stderr.txt")) if d]
        self.assertEqual([d.severity for d in diags], ["warning", "warning"])
        self.assertTrue(all(is_event(d) for d in diags))

    def test_bracketed_warning_flag_stays_part_of_the_message(self):
        diags = [d for d in (parse_line(l) for l in lines_of("warnings_only.stderr.txt")) if d]
        self.assertIn("-Wparentheses", diags[0].message)


class TestLinkerError(unittest.TestCase):
    def test_the_section_offset_line_is_recognized(self):
        diags = [d for d in (parse_line(l) for l in lines_of("linker_error.stderr.txt")) if d]
        self.assertEqual(len(diags), 1)
        diag = diags[0]
        self.assertEqual(diag.file, "linker_main.c")
        self.assertIsNone(diag.line)
        self.assertIsNone(diag.col)
        self.assertEqual(diag.severity, "error")
        self.assertIn("undefined reference", diag.message)
        self.assertTrue(is_event(diag))

    def test_the_ld_and_collect2_lines_are_not_recognized(self):
        # Neither references a workspace file - /usr/bin/ld is a system
        # binary, /tmp/ccXXXXXX.o is a scratch object file, and "collect2"
        # is a program name, not a file. Inventing a workspace path for any
        # of these would be worse than not recognizing the line.
        for line in lines_of("linker_error.stderr.txt"):
            if "undefined reference" in line:
                continue
            with self.subTest(line=line):
                self.assertIsNone(parse_line(line))


class TestIncludeChain(unittest.TestCase):
    def test_chain_line_has_no_severity_and_is_not_an_event(self):
        diag = parse_line("In file included from include_main.c:2:")
        self.assertEqual(diag.file, "include_main.c")
        self.assertEqual(diag.line, 2)
        self.assertIsNone(diag.col)
        self.assertIsNone(diag.severity)
        self.assertFalse(is_event(diag))

    def test_the_chain_names_a_different_file_than_the_real_error(self):
        diags = [d for d in (parse_line(l) for l in lines_of("include_chain.stderr.txt")) if d]
        chain = diags[0]
        real_error = next(d for d in diags if is_event(d))
        self.assertEqual(chain.file, "include_main.c")
        self.assertEqual(real_error.file, "include_util.h")
        self.assertNotEqual(chain.file, real_error.file)

    def test_full_fixture_has_two_events_one_note_one_chain_line(self):
        # 1 chain line (no severity) + 1 error + 1 note (not an event) + 1
        # trailing warning ("control reaches end of non-void function") = 4
        # recognized lines, 2 of them events (the error and the warning).
        diags = [d for d in (parse_line(l) for l in lines_of("include_chain.stderr.txt")) if d]
        self.assertEqual(len(diags), 4)
        self.assertEqual(sum(1 for d in diags if is_event(d)), 2)


class TestPythonTraceback(unittest.TestCase):
    def test_all_three_frames_recognized(self):
        diags = [d for d in (parse_line(l) for l in lines_of("traceback.stderr.txt")) if d]
        self.assertEqual(len(diags), 3)
        self.assertTrue(all(d.severity == "error" for d in diags))
        self.assertTrue(all(is_event(d) for d in diags))

    def test_deepest_frame_is_the_last_one_parsed(self):
        diags = [d for d in (parse_line(l) for l in lines_of("traceback.stderr.txt")) if d]
        self.assertEqual(diags[-1].file, "/home/towhid/perch-diag-capture/traceback.py")
        self.assertEqual(diags[-1].line, 2)
        self.assertIn("inner", diags[-1].message)

    def test_the_final_exception_line_is_not_recognized(self):
        # "ZeroDivisionError: division by zero" has no File prefix and no
        # file:line:col shape - correctly falls through as plain text.
        self.assertIsNone(parse_line("ZeroDivisionError: division by zero"))

    def test_the_traceback_header_is_not_recognized(self):
        self.assertIsNone(parse_line("Traceback (most recent call last):"))


class TestMakeSubdir(unittest.TestCase):
    def test_the_gcc_error_is_recognized_relative_to_subdir_not_remote_root(self):
        # P3-1 does not resolve this relative path against anything (P3-2's
        # job) - it just extracts exactly what gcc printed: the bare
        # filename, relative to wherever make -C actually ran it.
        diags = [d for d in (parse_line(l) for l in lines_of("make_subdir.stderr.txt")) if d]
        events = [d for d in diags if is_event(d)]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].file, "sub_error.c")

    def test_entering_and_leaving_directory_lines_are_not_diagnostics(self):
        # Real, needed for P3-2's cwd tracking - but that is a SEPARATE
        # mechanism, not parse_line's job. To this module they are just text.
        for line in lines_of("make_subdir.stdout.txt"):
            with self.subTest(line=line):
                self.assertIsNone(parse_line(line))

    def test_the_bracketed_make_error_summary_is_deliberately_not_recognized(self):
        # "make: *** [Makefile:2: sub_error] Error 1" technically contains a
        # file:line pair, but embedded inside "make: *** [...] Error N", not
        # at the start of the line - forcing a match here for marginal value
        # (the compiler's own, more precise error already covers this) risks
        # a pattern general enough to catch real noise. Left unrecognized.
        self.assertIsNone(parse_line("make: *** [Makefile:2: sub_error] Error 1"))


class TestAnsiColour(unittest.TestCase):
    def test_coloured_output_still_parses(self):
        # Confirmed by raw byte capture that real escape sequences sit
        # directly before the filename in this fixture - if strip_ansi did
        # not run before matching, this would not parse at all.
        diags = [d for d in (parse_line(l) for l in lines_of("single_error_color.stderr.txt")) if d]
        events = [d for d in diags if is_event(d)]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].file, "single_error.c")
        self.assertEqual((events[0].line, events[0].col), (5, 22))

    def test_stripped_message_contains_no_escape_bytes(self):
        diags = [d for d in (parse_line(l) for l in lines_of("single_error_color.stderr.txt")) if d]
        events = [d for d in diags if is_event(d)]
        self.assertNotIn("\x1b", events[0].message)

    def test_strip_ansi_removes_real_captured_sequences(self):
        raw = (FIXTURES / "single_error_color.stderr.txt").read_text()
        self.assertIn("\x1b[", raw)
        self.assertNotIn("\x1b[", strip_ansi(raw))

    def test_strip_ansi_is_a_no_op_on_plain_text(self):
        plain = "single_error.c:5:22: error: expected ';' before 'return'"
        self.assertEqual(strip_ansi(plain), plain)


class TestEncodingSafety(unittest.TestCase):
    """A decode error must never kill the stream - decoding itself happens
    in executor.py (errors="replace"), but this confirms parse_line() never
    chokes on the U+FFFD replacement characters that decode leaves behind."""

    def test_a_replacement_charactered_line_does_not_raise(self):
        raw = (FIXTURES / "invalid_utf8.stderr.txt").read_bytes()
        self.assertIn(b"\xff\xfe", raw)  # confirm the fixture really is invalid UTF-8
        decoded = raw.decode("utf-8", errors="replace")
        self.assertIn("�", decoded)
        diag = parse_line(decoded.rstrip("\n"))  # must not raise
        self.assertIsNotNone(diag)
        self.assertEqual(diag.file, "single_error.c")
        self.assertIn("�", diag.message)


class TestDiagnosticIsFrozenEnoughToCompare(unittest.TestCase):
    def test_equal_fields_compare_equal(self):
        a = diagnostics.Diagnostic(file="x.c", line=1, col=2, severity="error", message="m")
        b = diagnostics.Diagnostic(file="x.c", line=1, col=2, severity="error", message="m")
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
