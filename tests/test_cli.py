"""C7 (draft) argument surface and the §6 sequence. Offline.

Nothing here reaches a target. The sequence is asserted by substituting the
component entry points cli.py calls.
"""

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from perch import cli, config, errors, executor, mirror


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

        def run(cfg, command, session):
            self.calls.append(("execute", command))
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

    def test_every_phase_zero_verb_is_present(self):
        parser = cli.build_parser()
        actions = [a for a in parser._actions if a.dest == "verb"]
        self.assertEqual(
            sorted(actions[0].choices), ["build", "exec", "run", "sync", "test"]
        )

    def test_no_verb_from_a_later_phase_has_leaked_in(self):
        parser = cli.build_parser()
        actions = [a for a in parser._actions if a.dest == "verb"]
        for later in ("doctor", "pull", "logs", "shell"):
            self.assertNotIn(later, actions[0].choices)

    def test_help_states_that_the_mirror_deletes(self):
        self.assertIn("deletes", cli.EPILOG)


if __name__ == "__main__":
    unittest.main()
