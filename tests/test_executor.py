"""C4 (draft) command construction. Offline.

Only the pure parts are tested here. Actual execution needs a target and is
integration work; P2 replaces this module anyway.
"""

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
        # A configured command is shell by definition; quoting it would make
        # "make && ./run" a single argv element named "make && ./run".
        self.assertEqual(
            executor.remote_command("p", "make -j4 && ./run"),
            "cd p && make -j4 && ./run",
        )


class TestJoin(unittest.TestCase):
    def test_argv_from_the_command_line_is_quoted(self):
        self.assertEqual(executor.join(["echo", "two words"]), "echo 'two words'")

    def test_argv_cannot_inject_a_second_command(self):
        joined = executor.join(["echo", "; rm -rf ~"])
        self.assertEqual(joined, "echo '; rm -rf ~'")


class TestSshArgv(unittest.TestCase):
    def test_shape(self):
        argv = executor.ssh_argv(make_config(), "uname -m")
        self.assertEqual(argv, ["ssh", "pi", "cd projects/blinky && uname -m"])

    def test_uses_the_alias_only(self):
        # I10: user, address and port live in ~/.ssh/config, never here.
        argv = executor.ssh_argv(make_config(), "true")
        joined = " ".join(argv)
        self.assertNotIn("@", joined)
        self.assertNotIn("-p ", joined)
        self.assertNotIn("-i ", joined)


class TestRunResult(unittest.TestCase):
    def test_defaults_are_not_interrupted_and_not_indeterminate(self):
        result = executor.RunResult(exit_code=0)
        self.assertFalse(result.interrupted)
        self.assertFalse(result.indeterminate)


class TestPhaseZeroHonesty(unittest.TestCase):
    def test_module_is_marked_as_replaced_in_p2(self):
        source = Path(executor.__file__).read_text()
        self.assertIn("# REPLACED IN P2", source)


if __name__ == "__main__":
    unittest.main()
