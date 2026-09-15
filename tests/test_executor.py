"""C4 remote execution. Offline for the pure pieces.

Per DEVELOPMENT-PLAN.md's test strategy table, C4 execution itself is
integration-level (needs a reachable target) - the actual streaming behavior
is verified live against the real Pi, with a timestamped transcript in
docs/PHASE-2-BUILD-AND-RUN.md. What's testable with no network is the pure
command-string building.
"""

import shlex
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from perch import executor
from perch.config import Config
from perch.errors import InternalError, RunLockHeld, TargetUnreachable


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

    def test_records_pgid_into_the_run_lock(self):
        # P4-1: the same $PGID the marker protocol already computes is also
        # written into the lock file, for free - no second ssh round trip.
        cmd = executor.build_wrapped_command("myproj", "make", "tok")
        self.assertIn(executor.lock_pgid_path("myproj"), cmd)
        self.assertIn('printf "%s" "$PGID"', cmd)

    def test_lock_write_happens_before_the_pgid_marker_is_printed(self):
        cmd = executor.build_wrapped_command("myproj", "make", "tok")
        lock_write_idx = cmd.index(executor.lock_pgid_path("myproj"))
        marker_idx = cmd.index(executor.pgid_marker("tok"))
        self.assertLess(lock_write_idx, marker_idx)


class TestPtyCommand(unittest.TestCase):
    def test_records_pgid_into_the_run_lock(self):
        cfg = make_config(remote_root="myproj")
        cmd = executor._pty_command(cfg, "./blinky")
        self.assertIn(executor.lock_pgid_path("myproj"), cmd)

    def test_execs_into_the_real_command_rather_than_staying_a_parent(self):
        # exec replaces the shell in place (same pid/pgid) so the interactive
        # program itself, not a wrapper shell, ends up as the pty's foreground
        # process - required for native Ctrl-C delivery (ADR-2).
        cfg = make_config(remote_root="myproj")
        cmd = executor._pty_command(cfg, "./blinky")
        self.assertIn("exec sh -c", cmd)

    def test_no_setsid_or_marker_machinery(self):
        # ADR-2: pty mode deliberately does not carry pipe mode's wrapper.
        cfg = make_config(remote_root="myproj")
        cmd = executor._pty_command(cfg, "./blinky")
        self.assertNotIn("setsid", cmd)
        self.assertNotIn("__PERCH_", cmd)


class TestPgidRecord(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(executor, "STATE_DIR", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_record_reads_as_none(self):
        cfg = make_config()
        self.assertIsNone(executor._read_pgid_record(cfg))

    def test_write_then_read_round_trips(self):
        cfg = make_config()
        executor._write_pgid_record(cfg, 4242)
        self.assertEqual(executor._read_pgid_record(cfg), 4242)

    def test_clear_removes_it(self):
        cfg = make_config()
        executor._write_pgid_record(cfg, 4242)
        executor._clear_pgid_record(cfg)
        self.assertIsNone(executor._read_pgid_record(cfg))

    def test_clearing_a_record_that_does_not_exist_does_not_raise(self):
        executor._clear_pgid_record(make_config())  # must not raise

    def test_different_projects_get_different_records(self):
        a = make_config(remote_root="proj-a")
        b = make_config(remote_root="proj-b")
        executor._write_pgid_record(a, 111)
        executor._write_pgid_record(b, 222)
        self.assertEqual(executor._read_pgid_record(a), 111)
        self.assertEqual(executor._read_pgid_record(b), 222)

    def test_a_garbled_record_reads_as_none_rather_than_raising(self):
        cfg = make_config()
        path = executor.pgid_record_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not-a-number")
        self.assertIsNone(executor._read_pgid_record(cfg))




@dataclass
class FakeCompleted:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class FakeSession:
    """A session double for the kill/confirm/reap helpers - no network."""

    def __init__(self, alive_sequence=(), raise_unreachable=False):
        self.alive_sequence = list(alive_sequence)
        self.raise_unreachable = raise_unreachable
        self.calls: list[str] = []

    def run_capturing(self, remote_command: str, *, input=None):
        self.calls.append(remote_command)
        if self.raise_unreachable:
            raise TargetUnreachable("pi: unreachable")
        if remote_command.startswith("pgrep"):
            alive = self.alive_sequence.pop(0) if self.alive_sequence else False
            return FakeCompleted(returncode=0 if alive else 1)
        return FakeCompleted(returncode=0)


class TestGroupAlive(unittest.TestCase):
    def test_pgrep_zero_is_alive(self):
        self.assertTrue(executor._group_alive(FakeSession(alive_sequence=[True]), 123))

    def test_pgrep_nonzero_is_dead(self):
        self.assertFalse(executor._group_alive(FakeSession(alive_sequence=[False]), 123))

    def test_unreachable_propagates_rather_than_guessing(self):
        with self.assertRaises(TargetUnreachable):
            executor._group_alive(FakeSession(raise_unreachable=True), 123)


class TestTerminateGroup(unittest.TestCase):
    def test_dead_after_term_alone(self):
        session = FakeSession(alive_sequence=[False])
        self.assertTrue(executor._terminate_group(session, 123))
        self.assertIn("kill -TERM -- -123", session.calls)
        self.assertNotIn("kill -KILL -- -123", session.calls)

    def test_escalates_to_kill_when_term_does_not_work(self):
        # Still alive right after TERM (grace=0 -> exactly one poll, then the
        # deadline is already past), dead only after KILL. grace=0 removes
        # any dependence on real elapsed time, so this is deterministic.
        session = FakeSession(alive_sequence=[True, False])
        with patch.object(executor, "TERM_GRACE_SECONDS", 0), \
             patch.object(executor, "KILL_GRACE_SECONDS", 5.0):
            confirmed = executor._terminate_group(session, 123)
        self.assertTrue(confirmed)
        self.assertEqual(
            session.calls,
            ["kill -TERM -- -123", "pgrep -g 123", "kill -KILL -- -123", "pgrep -g 123"],
        )

    def test_escalate_immediately_skips_term(self):
        session = FakeSession(alive_sequence=[False])
        executor._terminate_group(session, 123, escalate_immediately=True)
        self.assertNotIn("kill -TERM -- -123", session.calls)
        self.assertIn("kill -KILL -- -123", session.calls)

    def test_survives_kill_reports_not_confirmed(self):
        session = FakeSession(alive_sequence=[True, True])
        with patch.object(executor, "TERM_GRACE_SECONDS", 0), \
             patch.object(executor, "KILL_GRACE_SECONDS", 0):
            confirmed = executor._terminate_group(session, 123)
        self.assertFalse(confirmed)




class TestReapStaleGroup(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(executor, "STATE_DIR", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_record_is_a_silent_no_op(self):
        executor.reap_stale_group(make_config(), FakeSession())  # must not raise

    def test_dead_record_is_just_cleared(self):
        cfg = make_config()
        executor._write_pgid_record(cfg, 999)
        executor.reap_stale_group(cfg, FakeSession(alive_sequence=[False]))
        self.assertIsNone(executor._read_pgid_record(cfg))

    def test_live_orphan_is_killed_and_record_cleared(self):
        cfg = make_config()
        executor._write_pgid_record(cfg, 999)
        session = FakeSession(alive_sequence=[True, False])
        executor.reap_stale_group(cfg, session)
        self.assertIn("kill -TERM -- -999", session.calls)
        self.assertIsNone(executor._read_pgid_record(cfg))

    def test_unreapable_orphan_raises_and_keeps_the_record(self):
        cfg = make_config()
        executor._write_pgid_record(cfg, 999)
        # alive_sequence[0] is the initial reap_stale_group() liveness check;
        # [1] and [2] are the TERM- and KILL-phase polls inside _terminate_group.
        session = FakeSession(alive_sequence=[True, True, True])
        with patch.object(executor, "TERM_GRACE_SECONDS", 0), \
             patch.object(executor, "KILL_GRACE_SECONDS", 0):
            with self.assertRaises(InternalError):
                executor.reap_stale_group(cfg, session)
        self.assertEqual(executor._read_pgid_record(cfg), 999)

    def test_unreachable_target_leaves_the_record_for_next_time(self):
        cfg = make_config()
        executor._write_pgid_record(cfg, 999)
        executor.reap_stale_group(cfg, FakeSession(raise_unreachable=True))
        self.assertEqual(executor._read_pgid_record(cfg), 999)




class TestRunResult(unittest.TestCase):
    def test_defaults_are_not_interrupted_and_not_indeterminate(self):
        result = executor.RunResult(exit_code=0)
        self.assertFalse(result.interrupted)
        self.assertFalse(result.indeterminate)


# --------------------------------------------------------------------------
# P4-1: the run lock.
# --------------------------------------------------------------------------

class TestLockPaths(unittest.TestCase):
    def test_lock_dir_lives_under_dot_perch(self):
        self.assertEqual(executor.lock_dir("myproj"), "myproj/.perch/run.lock")

    def test_pgid_path_is_inside_the_lock_dir(self):
        self.assertEqual(executor.lock_pgid_path("myproj"), "myproj/.perch/run.lock/pgid")

    def test_trailing_slash_on_remote_root_does_not_double_up(self):
        self.assertEqual(executor.lock_dir("myproj/"), "myproj/.perch/run.lock")


class TestClaimCommand(unittest.TestCase):
    def test_the_exclusion_boundary_is_a_bare_mkdir_not_dash_p(self):
        # Trap 1: mkdir -p never fails on "already exists", so it cannot be
        # the atomic gate - only a bare mkdir on the lock dir itself can be.
        cmd = executor._claim_command("myproj")
        self.assertIn("mkdir -p myproj/.perch", cmd)
        self.assertIn("mkdir myproj/.perch/run.lock", cmd)
        self.assertNotIn("mkdir -p myproj/.perch/run.lock", cmd)

    def test_reports_claimed_on_success(self):
        self.assertIn("echo CLAIMED", executor._claim_command("myproj"))

    def test_reports_the_holder_pgid_on_failure(self):
        cmd = executor._claim_command("myproj")
        self.assertIn(f"cat {shlex.quote(executor.lock_pgid_path('myproj'))}", cmd)


class TestReleaseCommand(unittest.TestCase):
    def test_removes_the_whole_lock_directory(self):
        self.assertEqual(
            executor._release_command("myproj"),
            f"rm -rf {shlex.quote(executor.lock_dir('myproj'))}",
        )


class FakeLockSession:
    """A session double for acquire_lock/release_lock (P4-1). Routes on the
    command's own shape, since one acquire_lock call can issue several
    different remote commands (claim, read holder pgid, pgrep, kill, rm)."""

    def __init__(self, claim_outputs=(), pgid_outputs=(), alive_sequence=(), raise_unreachable=False):
        self.claim_outputs = list(claim_outputs)
        self.pgid_outputs = list(pgid_outputs)
        self.alive_sequence = list(alive_sequence)
        self.raise_unreachable = raise_unreachable
        self.calls: list[str] = []

    def run_capturing(self, remote_command: str, *, input=None):
        self.calls.append(remote_command)
        if self.raise_unreachable:
            raise TargetUnreachable("pi: unreachable")
        if remote_command.startswith("mkdir -p"):
            out = self.claim_outputs.pop(0) if self.claim_outputs else "CLAIMED"
            return FakeCompleted(returncode=0, stdout=out)
        if remote_command.startswith("cat "):
            out = self.pgid_outputs.pop(0) if self.pgid_outputs else ""
            return FakeCompleted(returncode=0, stdout=out)
        if remote_command.startswith("pgrep"):
            alive = self.alive_sequence.pop(0) if self.alive_sequence else False
            return FakeCompleted(returncode=0 if alive else 1)
        return FakeCompleted(returncode=0)  # rm -rf, kill -*


class TestAcquireLock(unittest.TestCase):
    def test_claims_immediately_when_free(self):
        cfg = make_config(remote_root="myproj")
        session = FakeLockSession(claim_outputs=["CLAIMED"])
        executor.acquire_lock(cfg, session)  # must not raise
        self.assertEqual(len(session.calls), 1)

    def test_raises_75_naming_the_holder_when_alive(self):
        cfg = make_config(remote_root="myproj")
        session = FakeLockSession(claim_outputs=["4242"], alive_sequence=[True])
        with self.assertRaises(RunLockHeld) as ctx:
            executor.acquire_lock(cfg, session)
        self.assertIn("4242", str(ctx.exception))
        self.assertEqual(ctx.exception.exit_code, 75)

    def test_reclaims_automatically_when_holder_is_dead_no_replace_needed(self):
        cfg = make_config(remote_root="myproj")
        session = FakeLockSession(claim_outputs=["4242", "CLAIMED"], alive_sequence=[False])
        executor.acquire_lock(cfg, session)  # must not raise
        self.assertTrue(any(c.startswith("rm -rf") for c in session.calls))

    def test_never_removes_a_lock_without_confirming_the_holder_dead_first(self):
        cfg = make_config(remote_root="myproj")
        session = FakeLockSession(claim_outputs=["4242"], alive_sequence=[True])
        with self.assertRaises(RunLockHeld):
            executor.acquire_lock(cfg, session)
        self.assertFalse(any(c.startswith("rm -rf") for c in session.calls))

    def test_replace_kills_the_live_holder_then_takes_over(self):
        cfg = make_config(remote_root="myproj")
        session = FakeLockSession(claim_outputs=["4242", "CLAIMED"], alive_sequence=[True, False])
        with patch.object(executor, "TERM_GRACE_SECONDS", 0):
            executor.acquire_lock(cfg, session, replace=True)  # must not raise
        self.assertIn("kill -TERM -- -4242", session.calls)
        self.assertTrue(any(c.startswith("rm -rf") for c in session.calls))

    def test_replace_raises_internal_error_if_the_holder_will_not_die(self):
        cfg = make_config(remote_root="myproj")
        # 3 liveness checks: acquire_lock's own initial one, then
        # _terminate_group's TERM-phase poll and KILL-phase poll.
        session = FakeLockSession(claim_outputs=["4242"], alive_sequence=[True, True, True])
        with patch.object(executor, "TERM_GRACE_SECONDS", 0), \
             patch.object(executor, "KILL_GRACE_SECONDS", 0):
            with self.assertRaises(InternalError):
                executor.acquire_lock(cfg, session, replace=True)
        # Never removed a lock it could not confirm dead.
        self.assertFalse(any(c.startswith("rm -rf") for c in session.calls))

    def test_without_replace_a_live_holder_is_never_killed(self):
        cfg = make_config(remote_root="myproj")
        session = FakeLockSession(claim_outputs=["4242"], alive_sequence=[True])
        with self.assertRaises(RunLockHeld):
            executor.acquire_lock(cfg, session, replace=False)
        self.assertFalse(any(c.startswith("kill") for c in session.calls))

    def test_a_lock_just_claimed_by_a_competitor_with_no_pgid_yet_is_treated_as_held(self):
        # The narrow race: another invocation's mkdir landed but its own
        # pgid write has not - conservatively refuse rather than guess.
        cfg = make_config(remote_root="myproj")
        session = FakeLockSession(claim_outputs=[""], pgid_outputs=[""])
        with patch.object(executor, "LOCK_STALE_RETRY_DELAY", 0):
            with self.assertRaises(RunLockHeld) as ctx:
                executor.acquire_lock(cfg, session)
        self.assertIn("not been recorded yet", str(ctx.exception))

    def test_a_lock_whose_pgid_appears_after_the_short_wait_is_handled_normally(self):
        cfg = make_config(remote_root="myproj")
        session = FakeLockSession(
            claim_outputs=["", "CLAIMED"], pgid_outputs=["777"], alive_sequence=[False]
        )
        with patch.object(executor, "LOCK_STALE_RETRY_DELAY", 0):
            executor.acquire_lock(cfg, session)  # must not raise
        self.assertTrue(any(c.startswith("rm -rf") for c in session.calls))

    def test_persistent_dead_relock_contention_exhausts_retries(self):
        cfg = make_config(remote_root="myproj")
        session = FakeLockSession(
            claim_outputs=["4242", "111", "222", "333"],
            alive_sequence=[False, False, False, False],
        )
        with self.assertRaises(InternalError):
            executor.acquire_lock(cfg, session)

    def test_a_live_relock_race_raises_lock_held_not_internal_error(self):
        # After we've reclaimed a confirmed-dead lock, a genuinely different,
        # live invocation beat us to the re-claim - that is ordinary
        # contention (75), not a tool bug (70).
        cfg = make_config(remote_root="myproj")
        session = FakeLockSession(claim_outputs=["4242", "999"], alive_sequence=[False, True])
        with self.assertRaises(RunLockHeld) as ctx:
            executor.acquire_lock(cfg, session)
        self.assertIn("999", str(ctx.exception))


class TestReleaseLock(unittest.TestCase):
    def test_removes_the_lock_directory(self):
        cfg = make_config(remote_root="myproj")
        session = FakeLockSession()
        executor.release_lock(cfg, session)
        self.assertEqual(session.calls, [executor._release_command("myproj")])

    def test_unreachable_target_is_swallowed_not_raised(self):
        cfg = make_config(remote_root="myproj")
        executor.release_lock(cfg, FakeLockSession(raise_unreachable=True))  # must not raise


if __name__ == "__main__":
    unittest.main()
