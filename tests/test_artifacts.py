"""C6 artifact retrieval. Offline - no target is reachable during these tests.

Everything here asserts on the command that WOULD be run, or on the pure
list_command/pull_argv/default_dest logic - the actual transfer is proved by
P4-2's done-tests against the real target.
"""

import shlex
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from perch import artifacts
from perch.config import BUILTIN_EXCLUDES, Config
from perch.errors import PullError


def make_config(**overrides) -> Config:
    defaults = dict(
        host="pi",
        remote_root="projects/blinky",
        local_root=Path("/Users/towhid/work/blinky"),
        commands={"build": "make"},
        exclude=(),
        artifacts=(),
    )
    defaults.update(overrides)
    return Config(**defaults)


@dataclass
class FakeCompleted:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeSession:
    """Stands in for session.Session - no network."""

    def __init__(self, *, list_result=None):
        self.list_result = list_result if list_result is not None else FakeCompleted()
        self.calls: list[str] = []

    def rsh_command(self) -> str:
        return "ssh -o ControlMaster=auto -o ControlPath=/tmp/perch-test.sock"

    def run_capturing(self, remote_command: str, *, input=None):
        self.calls.append(remote_command)
        return self.list_result


class TestDefaultDest(unittest.TestCase):
    def test_rooted_under_dot_perch(self):
        cfg = make_config(local_root=Path("/work/blinky"))
        self.assertEqual(artifacts.default_dest(cfg), Path("/work/blinky/.perch/artifacts"))

    def test_dot_perch_is_already_a_builtin_exclude(self):
        # The whole P4-2.3 round-trip answer depends on this being true -
        # see the module docstring. If this ever fails, the default
        # destination is no longer round-trip-safe - the reasoning behind
        # it needs revisiting, not just this test.
        self.assertIn(".perch/", BUILTIN_EXCLUDES)


class TestListCommand(unittest.TestCase):
    def test_cds_into_remote_root(self):
        cmd = artifacts.list_command("projects/blinky", "*.bin")
        self.assertTrue(cmd.startswith("cd projects/blinky && "))

    def test_pattern_is_a_positional_argument_not_concatenated_text(self):
        # Trap 1 done safely: the pattern must arrive as $1 to the inner
        # bash -c, never spliced into the script text itself.
        cmd = artifacts.list_command("root", "*.bin")
        words = shlex.split(cmd)
        self.assertEqual(words[-1], "*.bin")

    def test_a_hostile_glob_cannot_break_out(self):
        hostile = "; rm -rf ~"
        cmd = artifacts.list_command("root", hostile)
        words = shlex.split(cmd)
        # The hostile text must appear as the trailing DATA argument, not
        # as a second top-level shell statement.
        self.assertEqual(words[-1], hostile)
        self.assertNotIn("rm -rf ~", cmd.replace(shlex.quote(hostile), ""))

    def test_uses_nullglob_so_no_match_means_no_output(self):
        cmd = artifacts.list_command("root", "*.bin")
        self.assertIn("nullglob", cmd)

    def test_references_the_pattern_as_a_bare_positional_so_it_can_glob(self):
        # $1 must be UNQUOTED in the for-loop, or wildcard expansion never
        # happens at all - this is the one place quoting would be wrong.
        cmd = artifacts.list_command("root", "*.bin")
        self.assertIn("for f in $1", cmd)


class TestPullArgv(unittest.TestCase):
    def test_uses_relative_to_preserve_subdirectory_structure(self):
        cfg = make_config(remote_root="projects/blinky")
        session = FakeSession()
        argv = artifacts.pull_argv(cfg, session, ["build/out.bin"], Path("/dest"))
        self.assertIn("-R", argv)
        self.assertIn("pi:projects/blinky/./build/out.bin", argv)

    def test_no_delete(self):
        cfg = make_config()
        argv = artifacts.pull_argv(cfg, FakeSession(), ["a.bin"], Path("/dest"))
        self.assertNotIn("--delete", argv)

    def test_no_checksum_a_pull_is_not_the_sync_i3_governs(self):
        cfg = make_config()
        argv = artifacts.pull_argv(cfg, FakeSession(), ["a.bin"], Path("/dest"))
        self.assertNotIn("--checksum", argv)

    def test_rsh_comes_from_the_session(self):
        cfg = make_config()
        session = FakeSession()
        argv = artifacts.pull_argv(cfg, session, ["a.bin"], Path("/dest"))
        self.assertIn(f"--rsh={session.rsh_command()}", argv)

    def test_multiple_matches_become_multiple_sources(self):
        cfg = make_config(remote_root="root")
        argv = artifacts.pull_argv(cfg, FakeSession(), ["a.bin", "sub/b.bin"], Path("/dest"))
        self.assertIn("pi:root/./a.bin", argv)
        self.assertIn("pi:root/./sub/b.bin", argv)

    def test_destination_has_a_trailing_separator(self):
        cfg = make_config()
        argv = artifacts.pull_argv(cfg, FakeSession(), ["a.bin"], Path("/dest"))
        self.assertEqual(argv[-1], "/dest/")


class TestPull(unittest.TestCase):
    def test_no_match_is_a_no_op_not_an_error(self):
        cfg = make_config()
        session = FakeSession(list_result=FakeCompleted(returncode=0, stdout=""))
        result = artifacts.pull(cfg, session, "*.bin", dest=Path("/tmp/nonexistent-perch-test"))
        self.assertEqual(result, [])

    def test_no_match_never_creates_the_dest_directory(self):
        # A no-op should stay a no-op end to end, not leave a side effect.
        cfg = make_config()
        session = FakeSession(list_result=FakeCompleted(returncode=0, stdout=""))
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "artifacts"
            artifacts.pull(cfg, session, "*.bin", dest=dest)
            self.assertFalse(dest.exists())

    def test_listing_failure_raises_pull_error(self):
        cfg = make_config()
        session = FakeSession(list_result=FakeCompleted(returncode=1, stderr="no such directory"))
        with self.assertRaises(PullError):
            artifacts.pull(cfg, session, "*.bin")

    def test_default_dest_used_when_none_given(self):
        cfg = make_config(local_root=Path("/tmp/does-not-exist-perch-test"))
        session = FakeSession(list_result=FakeCompleted(returncode=0, stdout=""))
        # No match, so this never actually tries to mkdir/rsync - just
        # confirms the no-dest path does not blow up before reaching there.
        result = artifacts.pull(cfg, session, "*.bin")
        self.assertEqual(result, [])

    def test_a_match_creates_dest_and_returns_the_matched_paths(self):
        cfg = make_config()
        session = FakeSession(list_result=FakeCompleted(returncode=0, stdout="build/out.bin\n"))
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "artifacts"
            with patch.object(artifacts, "_run", return_value=FakeCompleted(returncode=0)) as run:
                result = artifacts.pull(cfg, session, "build/*.bin", dest=dest)
            self.assertEqual(result, ["build/out.bin"])
            self.assertTrue(dest.is_dir())
            run.assert_called_once()

    def test_rsync_failure_raises_pull_error(self):
        cfg = make_config()
        session = FakeSession(list_result=FakeCompleted(returncode=0, stdout="a.bin\n"))
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "artifacts"
            with patch.object(artifacts, "_run", return_value=FakeCompleted(returncode=23)):
                with self.assertRaises(PullError):
                    artifacts.pull(cfg, session, "*.bin", dest=dest)

    def test_multiple_matched_lines_all_come_back(self):
        cfg = make_config()
        session = FakeSession(
            list_result=FakeCompleted(returncode=0, stdout="a.bin\nsub/b.bin\n")
        )
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "artifacts"
            with patch.object(artifacts, "_run", return_value=FakeCompleted(returncode=0)):
                result = artifacts.pull(cfg, session, "*", dest=dest)
            self.assertEqual(result, ["a.bin", "sub/b.bin"])


if __name__ == "__main__":
    unittest.main()
