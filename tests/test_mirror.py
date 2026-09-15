"""C3 workspace mirror. Offline - no target is reachable during these tests.

Everything here asserts on the command that WOULD be run, never on a transfer.
The transfer itself is proved by P0-3's/P1-1's done-tests against the real
target. session.rsh_command()/session.run() are faked throughout - a real
Session would try to reach the network, and this suite must not.
"""

import json
import os
import shlex
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

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
    stdout: str = ""


class FakeSession:
    """Stands in for session.Session without ever touching the network.

    manifest_result drives run_capturing() - the P4-4 fast path's one
    remote check. It defaults to a nonzero exit (as if remote_root did not
    exist yet), the same "cannot confirm, so don't skip" default push()
    itself falls back to - a test that wants a skip has to opt in by
    setting manifest_result to a matching listing, same as a real `find`
    would produce for a tree identical to the local one.
    """

    def __init__(self, *, mkdir_result=None, raise_on_run=None, manifest_result=None):
        self.mkdir_result = mkdir_result or FakeCompleted(returncode=0)
        self.raise_on_run = raise_on_run
        self.manifest_result = manifest_result or FakeCompleted(returncode=1)
        self.run_calls: list[str] = []
        self.run_capturing_calls: list[str] = []

    def rsh_command(self) -> str:
        return "ssh -o ControlMaster=auto -o ControlPath=/tmp/perch-test.sock"

    def run(self, remote_command: str):
        self.run_calls.append(remote_command)
        if self.raise_on_run is not None:
            raise self.raise_on_run
        return self.mkdir_result

    def run_capturing(self, remote_command: str, *, input=None):
        self.run_capturing_calls.append(remote_command)
        return self.manifest_result


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


class TestContentHash(unittest.TestCase):
    """P4-4. Offline: no target, no rsync - a pure function of the local tree."""

    def _tree(self, tmp: Path, **files: str) -> Path:
        for relpath, content in files.items():
            path = tmp / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return tmp

    def test_stable_across_runs_with_no_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(Path(tmp), **{"main.c": "int main() {}"})
            cfg = make_config(local_root=root)
            self.assertEqual(mirror.content_hash(cfg), mirror.content_hash(cfg))

    def test_one_changed_byte_changes_the_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(Path(tmp), **{"main.c": "int main() {}"})
            cfg = make_config(local_root=root)
            before = mirror.content_hash(cfg)
            (root / "main.c").write_text("int main() {1}")
            self.assertNotEqual(before, mirror.content_hash(cfg))

    def test_same_content_different_mtime_is_the_same_hash(self):
        # I3's concern from the other direction: a hash keyed on CONTENT,
        # not on stat() metadata a clock can lie about.
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(Path(tmp), **{"main.c": "int main() {}"})
            cfg = make_config(local_root=root)
            before = mirror.content_hash(cfg)
            path = root / "main.c"
            os.utime(path, (0, 0))
            self.assertEqual(before, mirror.content_hash(cfg))

    def test_a_new_file_changes_the_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(Path(tmp), **{"main.c": "int main() {}"})
            cfg = make_config(local_root=root)
            before = mirror.content_hash(cfg)
            (root / "new.c").write_text("void f() {}")
            self.assertNotEqual(before, mirror.content_hash(cfg))

    def test_builtin_excluded_dir_is_not_hashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(Path(tmp), **{"main.c": "int main() {}"})
            cfg = make_config(local_root=root)
            before = mirror.content_hash(cfg)
            (root / ".git").mkdir()
            (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
            self.assertEqual(before, mirror.content_hash(cfg))

    def test_builtin_excluded_extension_is_not_hashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(Path(tmp), **{"main.c": "int main() {}"})
            cfg = make_config(local_root=root)
            before = mirror.content_hash(cfg)
            (root / "main.o").write_text("garbage")
            self.assertEqual(before, mirror.content_hash(cfg))

    def test_project_exclude_is_not_hashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(Path(tmp), **{"main.c": "int main() {}"})
            cfg = make_config(local_root=root, exclude=("*.bin",))
            before = mirror.content_hash(cfg)
            (root / "out.bin").write_bytes(b"\x00\x01")
            self.assertEqual(before, mirror.content_hash(cfg))

    def test_the_sync_cache_file_itself_is_never_hashed(self):
        # .perch/ is a BUILTIN_EXCLUDE - the cache this module writes into
        # it must not feed back into the hash it was computed from.
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(Path(tmp), **{"main.c": "int main() {}"})
            cfg = make_config(local_root=root)
            before = mirror.content_hash(cfg)
            mirror._write_cache(cfg, before)
            self.assertEqual(before, mirror.content_hash(cfg))


class TestSyncCache(unittest.TestCase):
    def test_no_cache_file_is_a_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(local_root=Path(tmp))
            self.assertFalse(mirror._cache_hit(cfg, "anyhash"))

    def test_matching_cache_is_a_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(local_root=Path(tmp))
            mirror._write_cache(cfg, "abc123")
            self.assertTrue(mirror._cache_hit(cfg, "abc123"))

    def test_mismatched_hash_is_a_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(local_root=Path(tmp))
            mirror._write_cache(cfg, "abc123")
            self.assertFalse(mirror._cache_hit(cfg, "different"))

    def test_cache_for_a_different_remote_root_is_a_miss(self):
        # A config change (e.g. remote_root edited in .perch.toml) must not
        # let a stale cache from a differently-targeted sync be trusted.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(local_root=Path(tmp), remote_root="projects/blinky")
            mirror._write_cache(cfg, "abc123")
            other = make_config(local_root=Path(tmp), remote_root="projects/other")
            self.assertFalse(mirror._cache_hit(other, "abc123"))

    def test_cache_for_a_different_host_is_a_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(local_root=Path(tmp), host="pi")
            mirror._write_cache(cfg, "abc123")
            other = make_config(local_root=Path(tmp), host="other-pi")
            self.assertFalse(mirror._cache_hit(other, "abc123"))

    def test_corrupted_cache_json_is_a_miss_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(local_root=Path(tmp))
            path = mirror._cache_path(cfg)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not valid json")
            self.assertFalse(mirror._cache_hit(cfg, "anyhash"))

    def test_cache_missing_a_required_key_is_a_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(local_root=Path(tmp))
            path = mirror._cache_path(cfg)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"host": cfg.host}))
            self.assertFalse(mirror._cache_hit(cfg, "anyhash"))


class TestPushFastPath(unittest.TestCase):
    """P4-4's done-when, offline: rsync itself is faked (subprocess.run
    patched, as in test_session.py) so these prove the SKIP DECISION, not a
    real transfer - that half is proved live against the target."""

    def _project(self, tmp: Path) -> Config:
        root = Path(tmp) / "proj"
        root.mkdir()
        (root / "main.c").write_text("int main() {}")
        return make_config(local_root=root)

    def test_first_sync_has_no_cache_and_runs_rsync(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._project(tmp)
            session = FakeSession()
            with patch.object(mirror.subprocess, "run", return_value=FakeCompleted(0)) as run:
                mirror.push(cfg, session)
            run.assert_called_once()
            self.assertEqual(session.run_calls, [f"mkdir -p {cfg.remote_root}"])

    def test_a_successful_sync_writes_the_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._project(tmp)
            session = FakeSession()
            with patch.object(mirror.subprocess, "run", return_value=FakeCompleted(0)):
                mirror.push(cfg, session)
            self.assertTrue(mirror._cache_hit(cfg, mirror.content_hash(cfg)))

    def test_a_failed_sync_does_not_write_the_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._project(tmp)
            session = FakeSession()
            with patch.object(mirror.subprocess, "run", return_value=FakeCompleted(23)):
                with self.assertRaises(SyncError):
                    mirror.push(cfg, session)
            self.assertIsNone(mirror._load_cache(cfg))

    def _matching_manifest(self, cfg: Config) -> FakeCompleted:
        """What a real `find` on the target would print for a tree
        identical to the current local one - the only way a test's
        FakeSession agrees to a skip (see FakeSession's docstring)."""
        return FakeCompleted(returncode=0, stdout=mirror._local_size_manifest(cfg) + "\n")

    def test_second_unchanged_sync_skips_rsync(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._project(tmp)
            session = FakeSession()
            with patch.object(mirror.subprocess, "run", return_value=FakeCompleted(0)) as run:
                mirror.push(cfg, session)  # primes the cache
                run.reset_mock()
                session.run_calls.clear()
                session.manifest_result = self._matching_manifest(cfg)
                mirror.push(cfg, session)  # should skip
            run.assert_not_called()
            self.assertEqual(session.run_calls, [])  # no mkdir - no rsync attempted at all

    def test_a_skip_still_costs_exactly_one_cheap_remote_check(self):
        # Not "no network at all" - the whole point of the second signal is
        # one lightweight round trip, never a full rsync.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._project(tmp)
            session = FakeSession()
            with patch.object(mirror.subprocess, "run", return_value=FakeCompleted(0)):
                mirror.push(cfg, session)
                session.manifest_result = self._matching_manifest(cfg)
                session.run_capturing_calls.clear()
                mirror.push(cfg, session)
            self.assertEqual(len(session.run_capturing_calls), 1)

    def test_one_changed_byte_un_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._project(tmp)
            session = FakeSession()
            with patch.object(mirror.subprocess, "run", return_value=FakeCompleted(0)) as run:
                mirror.push(cfg, session)
                run.reset_mock()
                session.manifest_result = self._matching_manifest(cfg)
                (cfg.local_root / "main.c").write_text("int main() {1}")
                mirror.push(cfg, session)
            run.assert_called_once()

    def test_force_sync_bypasses_a_matching_cache_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._project(tmp)
            session = FakeSession()
            with patch.object(mirror.subprocess, "run", return_value=FakeCompleted(0)) as run:
                mirror.push(cfg, session)
                run.reset_mock()
                session.manifest_result = self._matching_manifest(cfg)
                mirror.push(cfg, session, force_sync=True)
            run.assert_called_once()

    def test_no_valid_cache_never_skips_even_with_a_stale_corrupted_file(self):
        # Whatever produced this state, an unparsable or absent cache must
        # fall through to a real sync rather than risk a false skip - and
        # never even reach the remote manifest check to decide that.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._project(tmp)
            path = mirror._cache_path(cfg)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("not json at all")
            session = FakeSession()
            session.manifest_result = self._matching_manifest(cfg)
            with patch.object(mirror.subprocess, "run", return_value=FakeCompleted(0)) as run:
                mirror.push(cfg, session)
            run.assert_called_once()
            self.assertEqual(session.run_capturing_calls, [])  # short-circuited before it

    def test_target_edited_directly_does_not_produce_a_false_skip(self):
        # P4-4's own done-when: the local tree never changed (a matching
        # cache), but the target's manifest no longer agrees - exactly what
        # an out-of-band edit made straight on the target looks like from
        # here. Must fall through to a real sync, not skip.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._project(tmp)
            session = FakeSession()
            with patch.object(mirror.subprocess, "run", return_value=FakeCompleted(0)) as run:
                mirror.push(cfg, session)  # primes the cache
                run.reset_mock()
                session.manifest_result = FakeCompleted(
                    returncode=0,
                    stdout="999\tmain.c\n",  # target's size no longer matches local
                )
                mirror.push(cfg, session)
            run.assert_called_once()

    def test_a_nonzero_remote_manifest_exit_never_skips(self):
        # e.g. remote_root was removed entirely since the last sync.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._project(tmp)
            session = FakeSession()
            with patch.object(mirror.subprocess, "run", return_value=FakeCompleted(0)) as run:
                mirror.push(cfg, session)
                run.reset_mock()
                session.manifest_result = FakeCompleted(returncode=1)
                mirror.push(cfg, session)
            run.assert_called_once()


class TestRemoteManifest(unittest.TestCase):
    def test_shape(self):
        cfg = make_config()
        cmd = mirror.remote_manifest_command(cfg)
        self.assertTrue(cmd.startswith(f"cd {cfg.remote_root} && find . "))
        self.assertIn("-type f -printf", cmd)

    def test_prune_clause_names_every_builtin_exclude_directory(self):
        cfg = make_config()
        cmd = mirror.remote_manifest_command(cfg)
        for pattern in BUILTIN_EXCLUDES:
            if pattern.endswith("/"):
                self.assertIn(shlex.quote(pattern.rstrip("/")), cmd)

    def test_remote_root_is_shell_quoted(self):
        cfg = make_config(remote_root="proj; rm -rf ~")
        cmd = mirror.remote_manifest_command(cfg)
        self.assertIn(shlex.quote("proj; rm -rf ~"), cmd)

    def test_parse_matches_local_manifest_for_an_identical_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.c").write_text("int main() {}")
            (root / "sub").mkdir()
            (root / "sub" / "util.c").write_text("void f() {}")
            cfg = make_config(local_root=root)
            remote_text = "\n".join(
                f"{p.stat().st_size}\t{p.relative_to(root).as_posix()}"
                for p in (root / "main.c", root / "sub" / "util.c")
            )
            self.assertEqual(
                mirror._parse_remote_manifest(cfg, remote_text),
                mirror._local_size_manifest(cfg),
            )

    def test_parse_drops_excluded_files(self):
        cfg = make_config(exclude=("*.bin",))
        remote_text = "10\tmain.c\n5\tout.bin\n"
        parsed = mirror._parse_remote_manifest(cfg, remote_text)
        self.assertIn("main.c", parsed)
        self.assertNotIn("out.bin", parsed)

    def test_parse_ignores_lines_with_no_tab(self):
        cfg = make_config()
        self.assertEqual(mirror._parse_remote_manifest(cfg, "garbage no tab here"), "")


if __name__ == "__main__":
    unittest.main()
