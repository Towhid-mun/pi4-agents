"""C4 remote execution. Offline for the pure pieces.

Per DEVELOPMENT-PLAN.md's test strategy table, C4 execution itself is
integration-level (needs a reachable target) - the actual streaming behavior
is verified live against the real Pi, with a timestamped transcript in
docs/PHASE-2-BUILD-AND-RUN.md. What's testable with no network is the pure
command-string building.
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
        self.assertEqual(
            executor.remote_command("p", "make -j4 && ./run"),
            "cd p && make -j4 && ./run",
        )


class TestJoin(unittest.TestCase):
    def test_argv_from_the_command_line_is_quoted(self):
        self.assertEqual(executor.join(["echo", "two words"]), "echo 'two words'")

    def test_argv_cannot_inject_a_second_command(self):
        self.assertEqual(executor.join(["echo", "; rm -rf ~"]), "echo '; rm -rf ~'")


class TestRunResult(unittest.TestCase):
    def test_defaults_are_not_interrupted_and_not_indeterminate(self):
        result = executor.RunResult(exit_code=0)
        self.assertFalse(result.interrupted)
        self.assertFalse(result.indeterminate)


if __name__ == "__main__":
    unittest.main()
