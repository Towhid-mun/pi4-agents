"""C5 diagnostic parsing (P3-1). Entirely offline - fixtures are plain text
files captured from the real target (tests/fixtures/, see CAPTURE.md), never
a live connection. This is the fast feedback loop; keep it that way forever.
"""

import unittest
from pathlib import Path

from perch import diagnostics
from perch.diagnostics import Diagnostic, PathResolver, is_event, parse_line, rewrite_line, strip_ansi

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
        line = "single_error.c:5:22: error: expected ‘;’ before ‘return’"
        diag = parse_line(line)
        self.assertEqual(
            diag,
            Diagnostic(
                file="single_error.c", line=5, col=22, severity="error",
                message="expected ‘;’ before ‘return’",
                file_span=(0, len("single_error.c")),
            ),
        )
        self.assertEqual(line[diag.file_span[0]:diag.file_span[1]], "single_error.c")
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
        a = diagnostics.Diagnostic(
            file="x.c", line=1, col=2, severity="error", message="m", file_span=(0, 3)
        )
        b = diagnostics.Diagnostic(
            file="x.c", line=1, col=2, severity="error", message="m", file_span=(0, 3)
        )
        self.assertEqual(a, b)


class TestPathResolver(unittest.TestCase):
    def setUp(self):
        self.local_root = Path("/Users/towhid/work/blinky")
        self.resolver = PathResolver(self.local_root, "perch-diag-capture")

    def test_relative_path_resolves_against_remote_root(self):
        self.assertEqual(
            self.resolver.resolve("single_error.c"),
            str(self.local_root / "single_error.c"),
        )

    def test_absolute_path_under_remote_root_resolves(self):
        # Real shape from traceback.stderr.txt.
        self.assertEqual(
            self.resolver.resolve("/home/towhid/perch-diag-capture/traceback.py"),
            str(self.local_root / "traceback.py"),
        )

    def test_absolute_path_outside_the_workspace_is_left_unresolved(self):
        # Real shapes from linker_error.stderr.txt - a scratch object file
        # and (hypothetically) a system header. Neither was ever mirrored;
        # fabricating a local path for either would be worse than leaving
        # the line untouched.
        self.assertIsNone(self.resolver.resolve("/tmp/ccIlWxtk.o"))
        self.assertIsNone(self.resolver.resolve("/usr/include/stdio.h"))

    def test_make_dash_c_subdir_shifts_relative_resolution(self):
        # The exact trap DEVELOPMENT-PLAN.md names, and the exact line GNU
        # Make 4.4.1 really printed (make_subdir.stdout.txt).
        self.resolver.observe("make: Entering directory '/home/towhid/perch-diag-capture/subdir'")
        self.assertEqual(
            self.resolver.resolve("sub_error.c"),
            str(self.local_root / "subdir" / "sub_error.c"),
        )

    def test_leaving_directory_restores_the_previous_level(self):
        self.resolver.observe("make: Entering directory '/home/towhid/perch-diag-capture/subdir'")
        self.resolver.observe("make: Leaving directory '/home/towhid/perch-diag-capture/subdir'")
        self.assertEqual(self.resolver.resolve("single_error.c"), str(self.local_root / "single_error.c"))

    def test_nested_entering_pops_in_lifo_order(self):
        self.resolver.observe("make: Entering directory '/home/towhid/perch-diag-capture/a'")
        self.resolver.observe("make: Entering directory '/home/towhid/perch-diag-capture/a/b'")
        self.assertEqual(self.resolver.resolve("x.c"), str(self.local_root / "a" / "b" / "x.c"))
        self.resolver.observe("make: Leaving directory '/home/towhid/perch-diag-capture/a/b'")
        self.assertEqual(self.resolver.resolve("x.c"), str(self.local_root / "a" / "x.c"))
        self.resolver.observe("make: Leaving directory '/home/towhid/perch-diag-capture/a'")
        self.assertEqual(self.resolver.resolve("x.c"), str(self.local_root / "x.c"))

    def test_an_unbalanced_leaving_does_not_underflow(self):
        self.resolver.observe("make: Leaving directory '/home/towhid/perch-diag-capture'")  # no matching Entering
        self.assertEqual(self.resolver.resolve("x.c"), str(self.local_root / "x.c"))  # must not raise

    def test_bracketed_sub_make_level_is_still_recognized(self):
        # Not observed on this target (single-level -C only) but standard,
        # well-documented GNU Make behavior for recursive sub-makes - cheap
        # and safe to accept since it only narrows the match, never widens it.
        self.resolver.observe("make[1]: Entering directory '/home/towhid/perch-diag-capture/subdir'")
        self.assertEqual(self.resolver.resolve("x.c"), str(self.local_root / "subdir" / "x.c"))

    def test_entering_a_path_outside_the_workspace_does_not_crash_later_resolution(self):
        self.resolver.observe("make: Entering directory '/some/other/place'")
        # Falls back to whatever the cwd already was (remote_root itself),
        # rather than adopting a bogus, unresolvable relative offset.
        self.assertEqual(self.resolver.resolve("x.c"), str(self.local_root / "x.c"))

    def test_absolute_remote_root_works_too(self):
        resolver = PathResolver(self.local_root, "/srv/blinky")
        self.assertEqual(resolver.resolve("/srv/blinky/src/main.c"), str(self.local_root / "src/main.c"))
        self.assertIsNone(resolver.resolve("/tmp/x.o"))


class TestRewriteLine(unittest.TestCase):
    def setUp(self):
        self.local_root = Path("/Users/towhid/work/blinky")
        self.resolver = PathResolver(self.local_root, "perch-diag-capture")

    def test_splices_only_the_file_span_leaving_everything_else_untouched(self):
        line = "single_error.c:5:22: error: expected ‘;’ before ‘return’"
        diag = parse_line(line)
        rewritten = rewrite_line(line, diag, self.resolver)
        self.assertEqual(
            rewritten,
            f"{self.local_root / 'single_error.c'}:5:22: error: expected ‘;’ before ‘return’",
        )

    def test_unresolvable_file_leaves_the_line_completely_unchanged(self):
        # A system header - never part of the mirrored workspace.
        line = "/usr/include/stdio.h:100:1: error: fake"
        diag = parse_line(line)
        self.assertEqual(rewrite_line(line, diag, self.resolver), line)

    def test_real_make_subdir_fixture_end_to_end(self):
        # Replays the ACTUAL captured lines in their real temporal order, not
        # stream-by-stream: make prints "Entering directory" (stdout), THEN
        # runs the recipe, whose failure is what produces the gcc error
        # (stderr) - "Leaving directory" (stdout) is announced only AFTER
        # the recipe finishes, so at the moment the error is resolved, the
        # resolver must still believe it is inside subdir. This is the trap
        # DEVELOPMENT-PLAN.md names, replayed against the real capture.
        resolver = PathResolver(self.local_root, "perch-diag-capture")
        entering = next(l for l in lines_of("make_subdir.stdout.txt") if "Entering directory" in l)
        resolver.observe(strip_ansi(entering))
        error_line = next(
            l for l in lines_of("make_subdir.stderr.txt")
            if ": error:" in l
        )
        diag = parse_line(error_line)
        rewritten = rewrite_line(error_line, diag, resolver)
        self.assertEqual(
            rewritten,
            f"{self.local_root / 'subdir' / 'sub_error.c'}:4:20: error: "
            "‘missing_symbol’ undeclared (first use in this function)",
        )
        # ls-able: the rewritten path is absolute and points at a real
        # location under local_root (gate item 1's actual check, done for
        # real against the live workspace in the live verification pass).
        self.assertTrue(Path(rewritten.split(":")[0]).is_absolute())


class TestJsonEvents(unittest.TestCase):
    """P3-3's event schema (ARCHITECTURE.md §5/C5). Every event must survive
    a real json.dumps/json.loads round trip - that's the actual requirement
    ("every line parses as JSON"), not just "looks like the right dict"."""

    def test_stdout_and_stderr_events_match_the_schema_exactly(self):
        import json

        self.assertEqual(json.loads(json.dumps(diagnostics.stdout_event("hello"))),
                          {"t": "stdout", "line": "hello"})
        self.assertEqual(json.loads(json.dumps(diagnostics.stderr_event("oops"))),
                          {"t": "stderr", "line": "oops"})

    def test_event_for_stream_dispatches_correctly(self):
        self.assertEqual(diagnostics.event_for_stream("stdout", "x")["t"], "stdout")
        self.assertEqual(diagnostics.event_for_stream("stderr", "x")["t"], "stderr")

    def test_diag_event_matches_the_schema_exactly(self):
        import json

        diag = parse_line("single_error.c:5:22: error: bad thing")
        event = diagnostics.diag_event(diag, "/local/single_error.c")
        self.assertEqual(
            json.loads(json.dumps(event)),
            {
                "t": "diag", "file": "/local/single_error.c", "line": 5, "col": 22,
                "severity": "error", "message": "bad thing",
            },
        )

    def test_exit_event_matches_architecture_mds_example_shape(self):
        # ARCHITECTURE.md §5/C5: {"t": "exit", "code": 1, "interrupted": false}
        # - "indeterminate" is an addition this codebase makes (P2-6 postdates
        # that schema); confirmed present alongside, not instead of, the
        # documented fields.
        import json

        event = diagnostics.exit_event(1, interrupted=False, indeterminate=False)
        parsed = json.loads(json.dumps(event))
        self.assertEqual(parsed["t"], "exit")
        self.assertEqual(parsed["code"], 1)
        self.assertEqual(parsed["interrupted"], False)
        self.assertIn("indeterminate", parsed)

    def test_every_recognized_fixture_line_produces_a_json_serializable_event(self):
        # Every diag this module can produce, from every real fixture, must
        # survive json.dumps - including gcc's curly quotes and any other
        # non-ASCII text in a real message.
        import json

        for fixture in (
            "single_error.stderr.txt", "multi_error.stderr.txt", "warnings_only.stderr.txt",
            "linker_error.stderr.txt", "include_chain.stderr.txt", "traceback.stderr.txt",
            "make_subdir.stderr.txt",
        ):
            for line in lines_of(fixture):
                diag = parse_line(line)
                if diag is not None:
                    json.dumps(diagnostics.diag_event(diag, diag.file))  # must not raise


if __name__ == "__main__":
    unittest.main()
