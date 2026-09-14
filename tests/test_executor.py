"""C4 remote execution. Offline for the pure pieces.

Per DEVELOPMENT-PLAN.md's test strategy table, C4 execution itself is
integration-level (needs a reachable target) - the actual streaming behavior
is verified live against the real Pi, with a timestamped transcript in
docs/PHASE-2-BUILD-AND-RUN.md. What's testable with no network is the pure
command-string building.
"""

import shlex
import unittest
from pathlib import Path

from perch import executor
from perch.config import Config


def make_config(**overrides) -> Config:
    defaults = dict(
        host="pi",
        remote_root="projects/blinky",
        local_root=Path("/Users/towhid/work/blinky"),
        commands={"build": "make -j4"},
        exclude=(),
        artifacts=(),
    )
    defaults.update(overrides)
    return Config(**defaults)


class TestRemoteCommand(unittest.TestCase):
    def test_cds_into_the_remote_root_first(self):
        self.assertEqual(
            executor.remote_command("projects/blinky", "make"),
            "cd projects/blinky && make",
        )

    def test_remote_root_is_quoted(self):
        self.assertEqual(
            executor.remote_command("proj/my stuff", "make"),
            "cd 'proj/my stuff' && make",
        )

    def test_a_hostile_remote_root_cannot_break_out(self):
        self.assertEqual(
            executor.remote_command("proj; rm -rf ~", "make"),
            "cd 'proj; rm -rf ~' && make",
        )

    def test_the_command_stays_shell_text(self):
        self.assertEqual(
            executor.remote_command("p", "make -j4 && ./run"),
            "cd p && make -j4 && ./run",
        )


class TestJoin(unittest.TestCase):
    def test_argv_from_the_command_line_is_quoted(self):
        self.assertEqual(executor.join(["echo", "two words"]), "echo 'two words'")

    def test_argv_cannot_inject_a_second_command(self):
        self.assertEqual(executor.join(["echo", "; rm -rf ~"]), "echo '; rm -rf ~'")


class TestMarkers(unittest.TestCase):
    def test_token_is_high_entropy_and_unique_per_call(self):
        a, b = executor.new_marker_token(), executor.new_marker_token()
        self.assertNotEqual(a, b)
        self.assertGreaterEqual(len(a), 32)  # secrets.token_hex(16) -> 32 hex chars

    def test_pgid_and_exit_markers_differ(self):
        token = "abc123"
        self.assertNotEqual(executor.pgid_marker(token), executor.exit_marker(token))

    def test_markers_are_scoped_to_their_own_token(self):
        # A marker-shaped line from a DIFFERENT run's token must not classify.
        self.assertIsNone(executor.classify_line(f"{executor.pgid_marker('other')}123", "mine"))


class TestClassifyLine(unittest.TestCase):
    def test_pgid_line(self):
        token = "deadbeef"
        line = f"{executor.pgid_marker(token)}4242"
        self.assertEqual(executor.classify_line(line, token), ("pgid", "4242", ""))

    def test_exit_line(self):
        token = "deadbeef"
        line = f"{executor.exit_marker(token)}255"
        self.assertEqual(executor.classify_line(line, token), ("exit", "255", ""))

    def test_ordinary_output_is_not_classified(self):
        self.assertIsNone(executor.classify_line("gcc: error: something", "deadbeef"))

    def test_marker_glued_to_a_final_unterminated_line_recovers_the_leading_text(self):
        # The marker is printed with a plain trailing newline and no
        # separator of its own - if the program's last line had none, the
        # marker can end up glued to the end of it.
        token = "deadbeef"
        line = f"partial output{executor.exit_marker(token)}0"
        self.assertEqual(executor.classify_line(line, token), ("exit", "0", "partial output"))

    def test_a_program_printing_something_marker_shaped_is_not_confused(self):
        # The whole point of a high-entropy per-run token (P2-2): a build
        # that happens to print something marker-shaped must not be mistaken
        # for the real marker of a DIFFERENT run.
        token = "realtoken"
        forged = "__PERCH_guessedtoken_EXIT_0"
        self.assertIsNone(executor.classify_line(forged, token))


class TestBuildWrappedCommand(unittest.TestCase):
    def test_shape(self):
        cmd = executor.build_wrapped_command("projects/blinky", "make", "tok")
        self.assertTrue(cmd.startswith("setsid --wait bash -c "))

    def test_traps_pipe_and_hup(self):
        # ADR-5 / S0-2: without this, SIGKILL-ing the local client kills the
        # remote group on its very next write (SIGPIPE), not from anything
        # ssh does deliberately.
        cmd = executor.build_wrapped_command("root", "make", "tok")
        self.assertIn('trap "" PIPE HUP', cmd)

    def test_does_not_trap_term(self):
        # P2-3 signals the group with TERM first; the wrapper must not
        # swallow that or graceful shutdown never has a chance to work.
        cmd = executor.build_wrapped_command("root", "make", "tok")
        self.assertNotIn("PIPE HUP TERM", cmd)
        self.assertNotIn("TERM PIPE HUP", cmd)

    def test_captures_pgid_via_ps_not_dollar_dollar(self):
        # setsid can fork when the caller is already a group leader, so a
        # bare $$ is not trustworthy - must be resolved via `ps`.
        cmd = executor.build_wrapped_command("root", "make", "tok")
        self.assertIn("ps -o pgid=", cmd)

    def test_uses_wait_not_bare_setsid(self):
        # Bare setsid can return before the (possibly forked) real command
        # has run at all - see ADR-5.
        cmd = executor.build_wrapped_command("root", "make", "tok")
        self.assertIn("setsid --wait", cmd)
        self.assertNotIn("setsid  bash", cmd)

    def test_prefers_stdbuf_when_available_falls_back_when_not(self):
        cmd = executor.build_wrapped_command("root", "make", "tok")
        self.assertIn("stdbuf -oL -eL", cmd)
        self.assertIn("command -v stdbuf", cmd)

    def test_pgid_and_exit_markers_present_for_this_token(self):
        cmd = executor.build_wrapped_command("root", "make", "tok")
        self.assertIn(executor.pgid_marker("tok"), cmd)
        self.assertIn(executor.exit_marker("tok"), cmd)

    def test_remote_root_and_command_survive_the_nested_quoting(self):
        # Round-trip through shlex (POSIX word splitting/quote removal - the
        # same rules dash/bash on the target apply) rather than substring
        # matching, since the nested single-quoting means the LITERAL text
        # "echo 'hi there'" never appears verbatim in the wrapper - it's
        # re-escaped as data one level in.
        cmd = executor.build_wrapped_command("proj/my stuff", "echo 'hi there'", "tok")
        outer_words = shlex.split(cmd)
        self.assertEqual(outer_words[:4], ["setsid", "--wait", "bash", "-c"])
        payload = outer_words[4]
        # The payload embeds one shlex.quote()'d copy of the inner command as
        # a literal argument to `sh -c` - locate and round-trip THAT.
        inner_expected = "cd 'proj/my stuff' && echo 'hi there'"
        self.assertIn(shlex.quote(inner_expected), payload)
        # And a real POSIX shell parsing that inner sh -c invocation recovers
        # the exact original string.
        recovered = shlex.split(f"sh -c {shlex.quote(inner_expected)}")
        self.assertEqual(recovered, ["sh", "-c", inner_expected])

    def test_a_hostile_command_cannot_break_out_of_the_wrapper(self):
        hostile = "make; rm -rf ~"
        cmd = executor.build_wrapped_command("root", hostile, "tok")
        payload = shlex.split(cmd)[4]
        # The hostile text must appear as DATA (quoted) inside the inner sh -c
        # argument, not as a second top-level statement of the payload.
        inner_expected = f"cd root && {hostile}"
        self.assertIn(shlex.quote(inner_expected), payload)


class TestRunResult(unittest.TestCase):
    def test_defaults_are_not_interrupted_and_not_indeterminate(self):
        result = executor.RunResult(exit_code=0)
        self.assertFalse(result.interrupted)
        self.assertFalse(result.indeterminate)


if __name__ == "__main__":
    unittest.main()
