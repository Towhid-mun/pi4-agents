"""C3 workspace mirror. Offline - no target is reachable during these tests.

Everything here asserts on the command that WOULD be run, never on a transfer.
The transfer itself is proved by P0-3's/P1-1's done-tests against the real
target. session.rsh_command()/session.run() are faked throughout - a real
Session would try to reach the network, and this suite must not.
"""

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from perch import mirror
from perch.config import BUILTIN_EXCLUDES, Config
from perch.errors import SyncError, TargetUnreachable


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


class FakeSession:
    """Stands in for session.Session without ever touching the network."""

    def __init__(self, *, mkdir_result=None, raise_on_run=None):
        self.mkdir_result = mkdir_result or FakeCompleted(returncode=0)
        self.raise_on_run = raise_on_run
        self.run_calls: list[str] = []

    def rsh_command(self) -> str:
        return "ssh -o ControlMaster=auto -o ControlPath=/tmp/perch-test.sock"

    def run(self, remote_command: str):
        self.run_calls.append(remote_command)
        if self.raise_on_run is not None:
            raise self.raise_on_run
        return self.mkdir_result


class TestFlags(unittest.TestCase):
    def test_checksum_is_present(self):
        # I3. If this test is ever deleted, read the comment in mirror.py.
        self.assertIn("--checksum", mirror.BASE_FLAGS)

    def test_delete_is_present(self):
        self.assertIn("--delete", mirror.BASE_FLAGS)

    def test_archive_and_compress(self):
        self.assertIn("-a", mirror.BASE_FLAGS)
        self.assertIn("-z", mirror.BASE_FLAGS)

    def test_no_flag_that_would_weaken_the_comparison(self):
        forbidden = {"--size-only", "--ignore-times", "--update", "-u"}
        self.assertEqual(forbidden & set(mirror.BASE_FLAGS), set())


class TestRsyncArgv(unittest.TestCase):
    def test_shape(self):
        argv = mirror.rsync_argv(make_config(), "/tmp/ex.txt", FakeSession())
        self.assertEqual(argv[0], "rsync")
        for flag in mirror.BASE_FLAGS:
            self.assertIn(flag, argv)
        self.assertIn("--exclude-from=/tmp/ex.txt", argv)

    def test_rsh_comes_from_the_session_not_a_hardcoded_ssh(self):
        # C2 owns every remote-shell invocation; mirror.py only asks for it.
        session = FakeSession()
        argv = mirror.rsync_argv(make_config(), "/tmp/ex.txt", session)
        self.assertIn(f"--rsh={session.rsh_command()}", argv)

    def test_source_has_a_trailing_separator(self):
        # Without it rsync copies the directory INTO the destination and the
        # target tree ends up one level too deep.
        argv = mirror.rsync_argv(make_config(), "/tmp/ex.txt", FakeSession())
        self.assertTrue(argv[-2].endswith("/"))
        self.assertTrue(argv[-2].startswith("/Users/towhid/work/blinky"))

    def test_destination_is_alias_colon_remote_root(self):
        argv = mirror.rsync_argv(make_config(), "/tmp/ex.txt", FakeSession())
        self.assertEqual(argv[-1], "pi:projects/blinky/")

    def test_destination_uses_the_alias_only(self):
        # I10: no user, no address, no port ever reaches the command line.
        argv = mirror.rsync_argv(make_config(), "/tmp/ex.txt", FakeSession())
        joined = " ".join(argv)
        self.assertNotIn("@", joined)
        self.assertNotIn("10.0.0", joined)

    def test_absolute_remote_root(self):
        argv = mirror.rsync_argv(
            make_config(remote_root="/srv/blinky"), "/tmp/ex.txt", FakeSession()
        )
        self.assertEqual(argv[-1], "pi:/srv/blinky/")


class TestMkdirCommand(unittest.TestCase):
    def test_creates_the_remote_root(self):
        self.assertEqual(mirror.mkdir_command("projects/blinky"), "mkdir -p projects/blinky")

    def test_remote_path_is_shell_quoted(self):
        self.assertEqual(
            mirror.mkdir_command("proj/my stuff"), "mkdir -p 'proj/my stuff'"
        )

    def test_a_hostile_remote_root_cannot_break_out_of_the_argument(self):
        self.assertEqual(
            mirror.mkdir_command("proj; rm -rf ~"), "mkdir -p 'proj; rm -rf ~'"
        )


class TestEnsureRemoteRoot(unittest.TestCase):
    def test_runs_mkdir_through_the_session(self):
        session = FakeSession()
        mirror._ensure_remote_root(make_config(), session)
        self.assertEqual(session.run_calls, ["mkdir -p projects/blinky"])

    def test_nonzero_mkdir_raises_sync_error(self):
        session = FakeSession(mkdir_result=FakeCompleted(returncode=1))
        with self.assertRaises(SyncError):
            mirror._ensure_remote_root(make_config(), session)

    def test_unreachable_target_is_not_rewrapped_as_a_sync_error(self):
        # A connection failure keeps its own exit code (69), it does not
        # become a generic sync failure (73) - mirror.py must not catch and
        # rewrap what session.run() already classified.
        session = FakeSession(raise_on_run=TargetUnreachable("pi: unreachable"))
        with self.assertRaises(TargetUnreachable):
            mirror._ensure_remote_root(make_config(), session)


class TestExcludes(unittest.TestCase):
    def test_builtins_are_all_present(self):
        text = mirror.exclude_file_contents(make_config())
        for pattern in BUILTIN_EXCLUDES:
            self.assertIn(f"{pattern}\n", text)

    def test_config_excludes_follow_the_builtins(self):
        text = mirror.exclude_file_contents(make_config(exclude=("build/", "*.bin")))
        lines = text.splitlines()
        self.assertEqual(lines[: len(BUILTIN_EXCLUDES)], list(BUILTIN_EXCLUDES))
        self.assertEqual(lines[-2:], ["build/", "*.bin"])

    def test_one_pattern_per_line_and_a_trailing_newline(self):
        text = mirror.exclude_file_contents(make_config(exclude=("build/",)))
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(len(text.splitlines()), len(BUILTIN_EXCLUDES) + 1)

    def test_the_tool_config_is_never_mirrored(self):
        self.assertIn(".perch.toml\n", mirror.exclude_file_contents(make_config()))


class TestFailure(unittest.TestCase):
    def test_missing_local_root_raises_before_any_subprocess(self):
        cfg = make_config(local_root=Path("/definitely/not/here"))
        session = FakeSession()
        with self.assertRaises(SyncError):
            mirror.push(cfg, session)
        self.assertEqual(session.run_calls, [])  # never even tried mkdir

    def test_sync_failure_maps_to_73(self):
        from perch import errors

        self.assertEqual(errors.exit_code_for(SyncError("x")), 73)

    def test_local_root_that_exists_but_is_a_file(self):
        with tempfile.NamedTemporaryFile() as handle:
            cfg = make_config(local_root=Path(handle.name))
            session = FakeSession()
            with self.assertRaises(SyncError):
                mirror.push(cfg, session)
            self.assertEqual(session.run_calls, [])


if __name__ == "__main__":
    unittest.main()
