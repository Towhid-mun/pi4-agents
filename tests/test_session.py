"""C2 session manager. Offline throughout - nothing here reaches the network.

The multiplexing itself (a live control master, a warm second invocation) is
verified by hand against the real Pi and recorded in
docs/PHASE-1-BUILD-AND-RUN.md; that is the actual P1-1 done-test. This suite
covers the pure, offline-testable pieces: the ControlPath derivation and the
ssh option set.
"""

import unittest
from pathlib import Path

from perch.session import Session, control_path_for


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
        # not depend on the user's own ControlMaster lines. Verified by hand
        # against the real target with those lines commented out; see
        # docs/PHASE-1-BUILD-AND-RUN.md.
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


if __name__ == "__main__":
    unittest.main()
