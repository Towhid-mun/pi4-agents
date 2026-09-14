"""C2 session manager. Offline throughout - subprocess.run is monkeypatched
wherever a real ssh invocation would otherwise happen, so nothing here reaches
the network. The done-tests that actually exercise a live control master
against the real Pi are documented in docs/PHASE-1-BUILD-AND-RUN.md.
"""

import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from perch import session as session_mod
from perch.errors import TargetUnreachable
from perch.session import Session, classify_ssh_failure, control_path_for


@dataclass
class FakeCompleted:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class TestControlPath(unittest.TestCase):
    def test_stable_for_the_same_alias(self):
        self.assertEqual(control_path_for("pi"), control_path_for("pi"))

    def test_differs_for_a_different_alias(self):
        self.assertNotEqual(control_path_for("pi"), control_path_for("pi2"))

    def test_lives_under_the_given_socket_dir(self):
        path = control_path_for("pi", socket_dir=Path("/tmp/sockets"))
        self.assertEqual(path.parent, Path("/tmp/sockets"))

    def test_not_the_expanded_r_at_h_colon_p_form(self):
        # That form is user@host:port - exactly what can overflow the ~104
        # byte AF_UNIX cap combined with a long ~/.ssh/. A hash never grows
        # with the alias, username or home directory length.
        path = control_path_for("towhid@my-extremely-long-raspberry-pi-hostname.local:2222")
        self.assertNotIn("@", path.name)
        self.assertNotIn(":", path.name)

    def test_filename_length_does_not_grow_with_the_alias(self):
        # This is the specific overflow %r@%h:%p causes: a long alias, user or
        # hostname makes the FILE NAME balloon. A hash-based name is fixed
        # width regardless. (The home directory's own length is a separate,
        # unbounded concern this cannot and does not try to fix.)
        short = control_path_for("pi")
        long_alias = control_path_for("a" * 500)
        self.assertEqual(len(short.name), len(long_alias.name))

    def test_stays_well_under_the_macos_sun_path_limit_for_a_realistic_home(self):
        realistic_home = Path("/Users/towhid-with-a-longer-than-average-username")
        path = control_path_for("a-fairly-long-project-specific-ssh-alias-name", socket_dir=realistic_home / ".ssh")
        self.assertLess(len(str(path).encode()), 100)


class TestBaseSshOptions(unittest.TestCase):
    def test_control_master_auto(self):
        opts = Session("pi").base_ssh_options()
        self.assertIn("ControlMaster=auto", opts)

    def test_control_path_under_ssh_dir(self):
        session = Session("pi", control_socket_dir=Path("/tmp/sockets"))
        opts = session.base_ssh_options()
        self.assertIn(f"ControlPath={session.control_path}", opts)
        self.assertTrue(str(session.control_path).startswith("/tmp/sockets"))

    def test_control_persist_present(self):
        opts = Session("pi", control_persist="10m").base_ssh_options()
        self.assertIn("ControlPersist=10m", opts)

    def test_connect_timeout_present_and_explicit(self):
        opts = Session("pi", connect_timeout=5).base_ssh_options()
        self.assertIn("ConnectTimeout=5", opts)

    def test_batch_mode_yes_so_nothing_ever_prompts(self):
        # I9: no prompt, ever.
        opts = Session("pi").base_ssh_options()
        self.assertIn("BatchMode=yes", opts)

    def test_these_are_all_o_flags_not_config_file_reliant(self):
        # Every setting above is a -o on the command line, which overrides a
        # same-named directive in ~/.ssh/config - this is what makes the tool
        # not depend on the user's own ControlMaster lines.
        session = Session("pi")
        opts = session.base_ssh_options()
        self.assertEqual(opts.count("-o"), 5)


class TestSshArgv(unittest.TestCase):
    def test_shape(self):
        argv = Session("pi").ssh_argv("uname -m")
        self.assertEqual(argv[0], "ssh")
        self.assertEqual(argv[-2], "pi")
        self.assertEqual(argv[-1], "uname -m")

    def test_uses_the_alias_only(self):
        # I10: user, address and port live in ~/.ssh/config, never here.
        argv = Session("pi").ssh_argv("true")
        joined = " ".join(argv)
        self.assertNotIn("@", joined)
        self.assertNotIn("-p ", joined)
        self.assertNotIn("-i ", joined)


class TestRshCommand(unittest.TestCase):
    def test_is_a_single_shell_joined_string_starting_with_ssh(self):
        command = Session("pi").rsh_command()
        self.assertTrue(command.startswith("ssh "))
        self.assertIn("ControlMaster=auto", command)

    def test_matches_the_same_options_as_ssh_argv(self):
        session = Session("pi")
        self.assertIn(str(session.control_path), session.rsh_command())


class TestPtyArgv(unittest.TestCase):
    def test_forces_pty_allocation(self):
        argv = Session("pi").pty_argv("cd root && make")
        self.assertEqual(argv[0], "ssh")
        self.assertIn("-tt", argv)

    def test_carries_the_same_multiplexing_options(self):
        session = Session("pi")
        argv = session.pty_argv("true")
        self.assertIn(f"ControlPath={session.control_path}", argv)

    def test_shape(self):
        argv = Session("pi").pty_argv("cd root && make")
        self.assertEqual(argv[-2], "pi")
        self.assertEqual(argv[-1], "cd root && make")


class TestClassifySshFailure(unittest.TestCase):
    def test_connection_timed_out_is_unreachable_and_transient(self):
        result = classify_ssh_failure("pi", "ssh: connect to host 10.0.0.99 port 22: Operation timed out\n")
        self.assertIsNotNone(result)
        self.assertTrue(result.transient)
        self.assertIsInstance(result.error, TargetUnreachable)
        self.assertIn("pi", str(result.error))

    def test_connection_refused_is_unreachable(self):
        result = classify_ssh_failure("pi", "ssh: connect to host 10.0.0.99 port 22: Connection refused\n")
        self.assertIsNotNone(result)
        self.assertTrue(result.transient)

    def test_host_is_down_is_unreachable(self):
        # What macOS actually reports for an address with no host present on
        # the local subnet (distinct from a routed-but-silent address, which
        # times out instead). Found by running the documented
        # unreachable-target procedure against a real unused LAN address -
        # "Host is down" was missing from the pattern list and fell through
        # unclassified, leaking a raw ssh line and exiting 73 instead of 69.
        result = classify_ssh_failure("pi", "ssh: connect to host 10.0.0.250 port 22: Host is down\n")
        self.assertIsNotNone(result)
        self.assertTrue(result.transient)

    def test_network_is_unreachable(self):
        result = classify_ssh_failure("pi", "connect to host 10.0.0.99 port 22: Network is unreachable\n")
        self.assertIsNotNone(result)
        self.assertTrue(result.transient)

    def test_permission_denied_is_auth_failure_and_not_transient(self):
        result = classify_ssh_failure("pi", "towhid@10.0.0.131: Permission denied (publickey).\n")
        self.assertIsNotNone(result)
        self.assertFalse(result.transient)
        self.assertIn("key", str(result.error))

    def test_host_key_mismatch_is_not_transient(self):
        result = classify_ssh_failure(
            "pi", "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
            "REMOTE HOST IDENTIFICATION HAS CHANGED!\n"
        )
        self.assertIsNotNone(result)
        self.assertFalse(result.transient)
        self.assertIn("ssh-keygen", str(result.error))

    def test_an_unrecognized_255_is_not_classified(self):
        # A remote command that happens to exit 255 itself (I6) - must not be
        # misreported as a connection failure.
        result = classify_ssh_failure("pi", "my-script: custom failure, exiting 255\n")
        self.assertIsNone(result)

    def test_every_classification_names_the_host(self):
        for stderr in (
            "Connection timed out",
            "Permission denied",
            "Host key verification failed",
        ):
            with self.subTest(stderr=stderr):
                result = classify_ssh_failure("my-alias", stderr)
                self.assertIn("my-alias", str(result.error))


class TestEnsureReachable(unittest.TestCase):
    """subprocess.run is patched throughout - no network access."""

    def test_succeeds_silently_on_a_clean_connection(self):
        with patch.object(session_mod.subprocess, "run", return_value=FakeCompleted(0)) as run:
            Session("pi").ensure_reachable()
        self.assertEqual(run.call_count, 1)

    def test_raises_target_unreachable_when_every_attempt_times_out(self):
        completed = FakeCompleted(255, stderr="Connection timed out")
        with patch.object(session_mod.subprocess, "run", return_value=completed) as run, \
             patch.object(session_mod.time, "sleep") as sleep:
            with self.assertRaises(TargetUnreachable):
                Session("pi").ensure_reachable()
        self.assertEqual(run.call_count, session_mod.RECONNECT_ATTEMPTS)
        sleep.assert_called()

    def test_recovers_if_a_later_attempt_succeeds(self):
        # The retry exists for exactly this: the target mid-boot.
        responses = [FakeCompleted(255, stderr="Connection refused"), FakeCompleted(0)]
        with patch.object(session_mod.subprocess, "run", side_effect=responses) as run, \
             patch.object(session_mod.time, "sleep"):
            Session("pi").ensure_reachable()  # must not raise
        self.assertEqual(run.call_count, 2)

    def test_auth_failure_does_not_retry(self):
        completed = FakeCompleted(255, stderr="Permission denied (publickey).")
        with patch.object(session_mod.subprocess, "run", return_value=completed) as run, \
             patch.object(session_mod.time, "sleep") as sleep:
            with self.assertRaises(TargetUnreachable):
                Session("pi").ensure_reachable()
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()

    def test_an_unrecognized_255_is_treated_as_reachable(self):
        completed = FakeCompleted(255, stderr="unrelated remote script failure")
        with patch.object(session_mod.subprocess, "run", return_value=completed) as run:
            Session("pi").ensure_reachable()  # must not raise
        self.assertEqual(run.call_count, 1)

    def test_every_failure_maps_to_69(self):
        from perch import errors

        self.assertEqual(errors.exit_code_for(TargetUnreachable("x")), 69)


class TestRun(unittest.TestCase):
    def test_checks_reachability_before_the_real_command(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return FakeCompleted(0)

        with patch.object(session_mod.subprocess, "run", side_effect=fake_run):
            Session("pi").run("uname -m")
        # First call is the reachability check ('true'), second the real one.
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][0][-1] == "true")
        self.assertTrue(calls[1][0][-1] == "uname -m")

    def test_does_not_attempt_the_real_command_when_unreachable(self):
        completed = FakeCompleted(255, stderr="Connection timed out")
        with patch.object(session_mod.subprocess, "run", return_value=completed) as run, \
             patch.object(session_mod.time, "sleep"):
            with self.assertRaises(TargetUnreachable):
                Session("pi").run("rm -rf /")
        # Every call was the reachability probe ('true'), never the real one.
        for call in run.call_args_list:
            self.assertEqual(call.args[0][-1], "true")


class TestPopen(unittest.TestCase):
    def test_checks_reachability_before_launching(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(("run", argv))
            return FakeCompleted(0)

        class FakePopen:
            def __init__(self, argv, **kwargs):
                calls.append(("popen", argv))
                self.kwargs = kwargs

        with patch.object(session_mod.subprocess, "run", side_effect=fake_run), \
             patch.object(session_mod.subprocess, "Popen", FakePopen):
            Session("pi").popen("./ticker.sh")
        self.assertEqual([kind for kind, _ in calls], ["run", "popen"])
        self.assertEqual(calls[0][1][-1], "true")
        self.assertEqual(calls[1][1][-1], "./ticker.sh")

    def test_does_not_launch_when_unreachable(self):
        completed = FakeCompleted(255, stderr="Connection timed out")
        popen_calls = []
        with patch.object(session_mod.subprocess, "run", return_value=completed), \
             patch.object(session_mod.subprocess, "Popen", lambda *a, **k: popen_calls.append(1)), \
             patch.object(session_mod.time, "sleep"):
            with self.assertRaises(TargetUnreachable):
                Session("pi").popen("./ticker.sh")
        self.assertEqual(popen_calls, [])

    def test_stdin_closed_stdout_stderr_are_separate_pipes(self):
        captured = {}

        class FakePopen:
            def __init__(self, argv, **kwargs):
                captured.update(kwargs)

        with patch.object(session_mod.subprocess, "run", return_value=FakeCompleted(0)), \
             patch.object(session_mod.subprocess, "Popen", FakePopen):
            Session("pi").popen("./ticker.sh")
        self.assertEqual(captured["stdin"], session_mod.subprocess.DEVNULL)
        self.assertEqual(captured["stdout"], session_mod.subprocess.PIPE)
        self.assertEqual(captured["stderr"], session_mod.subprocess.PIPE)


if __name__ == "__main__":
    unittest.main()
