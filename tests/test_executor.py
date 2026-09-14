"""C4 (draft) command construction. Offline.

Only the pure parts are tested here. Actual execution needs a target and is
integration work; P2 replaces this module anyway. session.run() is faked - a
real Session would touch the network.
"""

import unittest
from dataclasses import dataclass
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


@dataclass
class FakeCompleted:
    returncode: int


class FakeSession:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode
        self.run_calls: list[str] = []

    def run(self, remote_command: str):
        self.run_calls.append(remote_command)
        return FakeCompleted(returncode=self.returncode)


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


class TestRun(unittest.TestCase):
    def test_runs_through_the_session_cding_into_the_remote_root(self):
        session = FakeSession(returncode=0)
        result = executor.run(make_config(), "make -j4", session)
        self.assertEqual(session.run_calls, ["cd projects/blinky && make -j4"])
        self.assertEqual(result.exit_code, 0)

    def test_exit_code_passes_through_unchanged(self):
        # I6, including a code that collides with ssh's own 255.
        for code in (0, 1, 63, 127, 255):
            with self.subTest(code=code):
                result = executor.run(make_config(), "true", FakeSession(returncode=code))
                self.assertEqual(result.exit_code, code)


class TestRunResult(unittest.TestCase):
    def test_defaults_are_not_interrupted_and_not_indeterminate(self):
        result = executor.RunResult(exit_code=0)
        self.assertFalse(result.interrupted)
        self.assertFalse(result.indeterminate)


class TestPhaseHonesty(unittest.TestCase):
    def test_module_is_marked_as_replaced_in_p2(self):
        source = Path(executor.__file__).read_text()
        self.assertIn("# REPLACED IN P2", source)


if __name__ == "__main__":
    unittest.main()
