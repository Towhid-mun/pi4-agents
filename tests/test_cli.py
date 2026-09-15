"""C7 (draft) argument surface and the §6 sequence. Offline.

Nothing here reaches a target. The sequence is asserted by substituting the
component entry points cli.py calls. doctor's Session methods are patched at
the class level, since cli.py constructs its own Session internally.
"""

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path

from perch import cli, config, errors, executor, mirror
from perch.session import Session


def make_config(**overrides) -> config.Config:
    defaults = dict(
        host="pi",
        remote_root="projects/blinky",
        local_root=Path("/Users/towhid/work/blinky"),
        commands={"build": "make -j4", "test": "make check", "run": "./blinky"},
        exclude=(),
        artifacts=(),
        source=Path("/Users/towhid/work/blinky/.perch.toml"),
    )
    defaults.update(overrides)
    return config.Config(**defaults)


class SequenceTestCase(unittest.TestCase):
    """Replace C1, C3 and C4 with recorders and watch the order."""

    def setUp(self):
        self.calls = []
        self.cfg = make_config()
        self.sync_error = None
        self.exit_code = 0

        def load(start=None):
            self.calls.append(("resolve", None))
            return self.cfg

        def push(cfg, session):
            self.calls.append(("mirror", None))
            if self.sync_error is not None:
                raise self.sync_error

        def run(cfg, command, session, *, tty=False):
            self.calls.append(("execute", command))
            self.last_tty = tty
            return executor.RunResult(exit_code=self.exit_code)

        for module, name, replacement in (
            (config, "load", load),
            (mirror, "push", push),
            (executor, "run", run),
        ):
            original = getattr(module, name)
            setattr(module, name, replacement)
            self.addCleanup(setattr, module, name, original)
        # cli imported these by module, so patching the module attribute is
        # what cli.py actually looks up at call time.

    def invoke(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()


class TestSequence(SequenceTestCase):
    def test_resolve_then_mirror_then_execute(self):
        code, _, _ = self.invoke(["build"])
        self.assertEqual(
            [step for step, _ in self.calls], ["resolve", "mirror", "execute"]
        )
        self.assertEqual(code, 0)

    def test_sync_stops_after_the_mirror(self):
        code, _, _ = self.invoke(["sync"])
        self.assertEqual([step for step, _ in self.calls], ["resolve", "mirror"])
        self.assertEqual(code, 0)

    def test_a_failed_mirror_aborts_before_execute(self):
        # I4: never execute against a partially synced tree.
        self.sync_error = mirror.SyncError("rsync exited 23")
        code, _, err = self.invoke(["build"])
        self.assertNotIn("execute", [step for step, _ in self.calls])
        self.assertEqual(code, 73)
        self.assertIn("rsync exited 23", err)

    def test_a_config_failure_aborts_before_the_mirror(self):
        def boom(start=None):
            self.calls.append(("resolve", None))
            raise errors.ConfigError("/x/.perch.toml: missing required key 'host'")

        config.load = boom
        code, _, err = self.invoke(["build"])
        self.assertEqual([step for step, _ in self.calls], ["resolve"])
        self.assertEqual(code, 64)
        self.assertIn("host", err)


class TestCommandConstruction(SequenceTestCase):
    def command(self, argv):
        self.invoke(argv)
        return dict(self.calls)["execute"]

    def test_build_uses_the_configured_command(self):
        self.assertEqual(self.command(["build"]), "make -j4")

    def test_test_and_run_use_theirs(self):
        self.assertEqual(self.command(["test"]), "make check")
        self.calls.clear()
        self.assertEqual(self.command(["run"]), "./blinky")

    def test_extra_args_are_appended_and_quoted(self):
        self.assertEqual(self.command(["build", "V=1", "two words"]), "make -j4 V=1 'two words'")

    def test_exec_joins_its_argv_safely(self):
        self.assertEqual(self.command(["exec", "uname", "-m"]), "uname -m")

    def test_exec_cannot_inject_a_second_command(self):
        self.assertEqual(self.command(["exec", "echo", "; rm -rf ~"]), "echo '; rm -rf ~'")

    def test_a_verb_with_no_configured_command_exits_64(self):
        self.cfg = make_config(commands={})
        code, _, err = self.invoke(["build"])
        self.assertEqual(code, 64)
        self.assertIn("build", err)

    def test_exec_with_no_command_exits_64(self):
        code, _, err = self.invoke(["exec"])
        self.assertEqual(code, 64)


class TestExitStatus(SequenceTestCase):
    def test_remote_status_is_the_tool_status(self):
        # I6, including the codes that collide with the tool's own range.
        for code in (0, 1, 2, 63, 127):
            with self.subTest(code=code):
                self.calls.clear()
                self.exit_code = code
                observed, _, _ = self.invoke(["build"])
                self.assertEqual(observed, code)


class TestTtyAndJson(SequenceTestCase):
    def test_tty_flag_reaches_the_executor(self):
        self.invoke(["build", "--tty"])
        self.assertTrue(self.last_tty)

    def test_tty_defaults_to_false(self):
        self.invoke(["build"])
        self.assertFalse(self.last_tty)

    def test_tty_and_json_together_is_refused(self):
        # P2-5: a pty's streams are merged and carry control characters -
        # cannot be classified into structured events (Phase 3).
        code, _, err = self.invoke(["build", "--tty", "--json"])
        self.assertEqual(code, 64)
        self.assertIn("--tty", err)
        self.assertIn("--json", err)
        self.assertNotIn("execute", [step for step, _ in self.calls])

    def test_json_alone_is_accepted_for_now(self):
        # --json is recognized now (P2-5's combination check needs it to
        # exist) but does nothing on its own until P3-3.
        code, _, _ = self.invoke(["build", "--json"])
        self.assertEqual(code, 0)

    def test_tty_works_on_exec_too(self):
        self.invoke(["exec", "--tty", "true"])
        self.assertTrue(self.last_tty)


class TestSettleOutcomes(SequenceTestCase):
    """P2-6: the interrupted/indeterminate outcomes C4 can report must say
    the word, not just choose the number - see errors.exit_code_for_run for
    the number half of this contract."""

    def run_with(self, **result_kwargs):
        def run(cfg, command, session, *, tty=False):
            self.calls.append(("execute", command))
            return executor.RunResult(exit_code=result_kwargs.pop("exit_code", 1), **result_kwargs)

        executor.run = run
        return self.invoke(["build"])

    def test_indeterminate_exits_74_and_says_the_word(self):
        code, _, err = self.run_with(indeterminate=True)
        self.assertEqual(code, 74)
        self.assertIn("indeterminate", err)

    def test_interrupted_confirmed_exits_130(self):
        code, _, err = self.run_with(interrupted=True)
        self.assertEqual(code, 130)
        self.assertIn("interrupted", err)

    def test_indeterminate_wins_over_interrupted(self):
        # Mirrors errors.exit_code_for_run's own precedence.
        code, _, err = self.run_with(interrupted=True, indeterminate=True)
        self.assertEqual(code, 74)
        self.assertIn("indeterminate", err)

    def test_ordinary_completion_prints_neither_word(self):
        code, _, err = self.run_with(exit_code=0)
        self.assertEqual(code, 0)
        self.assertNotIn("indeterminate", err)
        self.assertNotIn("interrupted", err)


class TestArgumentSurface(unittest.TestCase):
    def test_usage_error_does_not_land_in_the_remote_failure_band(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = cli.main(["nosuchverb"])
        self.assertEqual(code, 64)
        self.assertNotIn(code, errors.REMOTE_PASSTHROUGH_RANGE)

    def test_help_exits_zero(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            cli.main(["--help"])
        self.assertEqual(caught.exception.code, 0)

    def test_every_phase_one_verb_is_present(self):
        parser = cli.build_parser()
        actions = [a for a in parser._actions if a.dest == "verb"]
        self.assertEqual(
            sorted(actions[0].choices),
            ["build", "doctor", "exec", "run", "sync", "test"],
        )

    def test_no_verb_from_a_later_phase_has_leaked_in(self):
        parser = cli.build_parser()
        actions = [a for a in parser._actions if a.dest == "verb"]
        for later in ("pull", "logs", "shell"):
            self.assertNotIn(later, actions[0].choices)

    def test_help_states_that_the_mirror_deletes(self):
        self.assertIn("deletes", cli.EPILOG)

    def test_help_states_doctor_is_read_only(self):
        self.assertIn("doctor", cli.EPILOG)


@dataclass
class FakeCompleted:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class TestDoctor(unittest.TestCase):
    """Patch Session's methods at the class level - cli.py builds its own."""

    def setUp(self):
        self.cfg = make_config()
        self.probe_stdout = ""
        self.alive_before = False
        self.probe_returncode = 0
        self.captured_input = None

        def load(start=None):
            return self.cfg

        def alive(self_session):
            return self.alive_before

        def run_capturing(self_session, remote_command, *, input=None):
            self.captured_input = input
            self.last_remote_command = remote_command
            return FakeCompleted(returncode=self.probe_returncode, stdout=self.probe_stdout)

        for target, name, replacement in (
            (config, "load", load),
            (Session, "alive", alive),
            (Session, "run_capturing", run_capturing),
        ):
            original = getattr(target, name)
            setattr(target, name, replacement)
            self.addCleanup(setattr, target, name, original)

    def invoke(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["doctor"])
        return code, out.getvalue(), err.getvalue()

    def test_probe_sent_over_stdin_not_the_command_line(self):
        self.probe_stdout = "PERCH:os=Debian\n"
        self.invoke()
        self.assertEqual(self.last_remote_command, "sh -s")
        self.assertIn("PERCH:", self.captured_input)

    def test_report_includes_parsed_fields(self):
        self.probe_stdout = "\n".join(
            [
                "PERCH:os=Debian GNU/Linux 13 (trixie)",
                "PERCH:kernel=6.6.51+rpt-rpi-v8",
                "PERCH:arch=aarch64",
                "PERCH:tool_gcc=__MISSING__",
            ]
        )
        code, out, _ = self.invoke()
        self.assertEqual(code, 0)
        self.assertIn("Debian GNU/Linux 13 (trixie)", out)
        self.assertIn("aarch64", out)

    def test_missing_tool_reports_as_missing_not_a_failure(self):
        self.probe_stdout = "PERCH:tool_gcc=__MISSING__\n"
        code, out, _ = self.invoke()
        self.assertEqual(code, 0)
        self.assertIn("MISSING", out)

    def test_does_not_sync(self):
        # doctor is read-only - mirror.push must never be called.
        calls = []
        original = mirror.push
        mirror.push = lambda *a, **k: calls.append("mirror")
        self.addCleanup(setattr, mirror, "push", original)
        self.invoke()
        self.assertEqual(calls, [])

    def test_nonzero_probe_is_an_internal_error_not_a_traceback(self):
        self.probe_returncode = 1
        code, _, err = self.invoke()
        self.assertEqual(code, 70)
        self.assertIn("perch:", err)


class TestParseProbeOutput(unittest.TestCase):
    def test_parses_tagged_lines(self):
        text = "PERCH:os=Debian\nPERCH:arch=aarch64\n"
        self.assertEqual(cli.parse_probe_output(text), {"os": "Debian", "arch": "aarch64"})

    def test_ignores_untagged_lines(self):
        text = "some banner\nPERCH:os=Debian\nWarning: something\n"
        self.assertEqual(cli.parse_probe_output(text), {"os": "Debian"})

    def test_value_may_contain_an_equals_sign(self):
        text = "PERCH:tool_make=GNU Make 4.3=extra\n"
        self.assertEqual(cli.parse_probe_output(text)["tool_make"], "GNU Make 4.3=extra")


if __name__ == "__main__":
    unittest.main()
