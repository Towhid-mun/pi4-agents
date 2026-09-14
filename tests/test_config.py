"""C1 config resolution. Offline - never touches the network."""

import tempfile
import unittest
from pathlib import Path

from perch import config
from perch.errors import ConfigError

VALID = """\
host = "pi"
remote_root = "projects/blinky"
exclude = ["build/", "*.bin"]
artifacts = ["build/blinky"]

[commands]
build = "make -j4"
test = "make check"
run = "./build/blinky"
"""


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)

    def write(self, text, *, where=None):
        directory = self.root if where is None else self.root / where
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / config.CONFIG_FILENAME
        path.write_text(text)
        return path


class TestValidConfig(ConfigTestCase):
    def test_round_trips_to_the_expected_config(self):
        path = self.write(VALID)
        cfg = config.load_file(path)

        self.assertEqual(cfg.host, "pi")
        self.assertEqual(cfg.remote_root, "projects/blinky")
        self.assertEqual(cfg.local_root, self.root)
        self.assertEqual(
            cfg.commands,
            {"build": "make -j4", "test": "make check", "run": "./build/blinky"},
        )
        self.assertEqual(cfg.exclude, ("build/", "*.bin"))
        self.assertEqual(cfg.artifacts, ("build/blinky",))

    def test_is_frozen(self):
        cfg = config.load_file(self.write(VALID))
        with self.assertRaises(Exception):
            cfg.host = "other"  # type: ignore[misc]

    def test_roots_pair(self):
        cfg = config.load_file(self.write(VALID))
        self.assertEqual(cfg.roots, (self.root, "projects/blinky"))

    def test_local_root_is_the_config_directory_absolute(self):
        cfg = config.load_file(self.write(VALID))
        self.assertTrue(cfg.local_root.is_absolute())

    def test_defaults_applied(self):
        cfg = config.load_file(self.write('host = "pi"\n'))
        self.assertEqual(cfg.remote_root, f"perch/{self.root.name}")
        self.assertEqual(cfg.commands, {})
        self.assertEqual(cfg.exclude, ())
        self.assertEqual(cfg.artifacts, ())

    def test_trailing_slash_stripped_from_remote_root(self):
        cfg = config.load_file(self.write('host = "pi"\nremote_root = "a/b/"\n'))
        self.assertEqual(cfg.remote_root, "a/b")

    def test_absolute_remote_root_is_kept(self):
        cfg = config.load_file(self.write('host = "pi"\nremote_root = "/srv/x"\n'))
        self.assertEqual(cfg.remote_root, "/srv/x")

    def test_builtin_excludes_come_first_and_include_the_config_file(self):
        cfg = config.load_file(self.write(VALID))
        self.assertEqual(cfg.all_excludes[: len(config.BUILTIN_EXCLUDES)],
                         config.BUILTIN_EXCLUDES)
        self.assertIn(".git/", cfg.all_excludes)
        self.assertIn(config.CONFIG_FILENAME, cfg.all_excludes)
        self.assertEqual(cfg.all_excludes[-2:], ("build/", "*.bin"))

    def test_command_for(self):
        cfg = config.load_file(self.write(VALID))
        self.assertEqual(cfg.command_for("build"), "make -j4")

    def test_command_for_missing_verb_is_a_config_error_naming_the_verb(self):
        cfg = config.load_file(self.write('host = "pi"\n'))
        with self.assertRaises(ConfigError) as caught:
            cfg.command_for("build")
        message = str(caught.exception)
        self.assertIn("build", message)
        self.assertIn(str(cfg.source), message)


class TestDiscovery(ConfigTestCase):
    def test_walks_up_from_a_subdirectory(self):
        path = self.write(VALID)
        deep = self.root / "src" / "drivers"
        deep.mkdir(parents=True)
        self.assertEqual(config.find_config_file(deep), path)
        self.assertEqual(config.load(deep).local_root, self.root)

    def test_nearest_config_wins(self):
        self.write(VALID)
        inner = self.write('host = "pi"\nremote_root = "inner"\n', where="sub")
        self.assertEqual(config.find_config_file(self.root / "sub"), inner)

    def test_missing_config_names_the_expected_path(self):
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(ConfigError) as caught:
            # Search upward from a directory with no config anywhere above it
            # is impossible inside a tmpdir, so assert on the message shape.
            config.find_config_file(Path("/"))
        message = str(caught.exception)
        self.assertIn(config.CONFIG_FILENAME, message)
        self.assertIn("/", message)
        del empty


class TestRejection(ConfigTestCase):
    def assertConfigError(self, text, *needles):
        path = self.write(text)
        with self.assertRaises(ConfigError) as caught:
            config.load_file(path)
        message = str(caught.exception)
        # Every config error names the file it came from.
        self.assertIn(str(path), message)
        for needle in needles:
            self.assertIn(needle, message)
        return message

    def test_unknown_top_level_key_is_named(self):
        self.assertConfigError('host = "pi"\nremote_rot = "x"\n', "remote_rot")

    def test_unknown_key_suggests_the_near_miss(self):
        message = self.assertConfigError('host = "pi"\nartifact = ["x"]\n', "artifact")
        self.assertIn("artifacts", message)

    def test_unknown_command_verb_is_named(self):
        self.assertConfigError(
            'host = "pi"\n[commands]\nbuidl = "make"\n', "buidl", "commands"
        )

    def test_missing_host(self):
        self.assertConfigError("remote_root = \"x\"\n", "host")

    def test_host_must_be_an_alias_not_a_connection_string(self):
        self.assertConfigError('host = "towhid@10.0.0.131"\n', "host", "ssh/config")

    def test_host_wrong_type(self):
        self.assertConfigError("host = 42\n", "host", "int")

    def test_remote_root_wrong_type(self):
        self.assertConfigError('host = "pi"\nremote_root = 3\n', "remote_root", "int")

    def test_remote_root_rejects_dotdot(self):
        self.assertConfigError('host = "pi"\nremote_root = "a/../b"\n', "remote_root")

    def test_remote_root_rejects_bare_slash(self):
        self.assertConfigError('host = "pi"\nremote_root = "/"\n', "remote_root")

    def test_exclude_wrong_type(self):
        self.assertConfigError('host = "pi"\nexclude = "build"\n', "exclude", "str")

    def test_exclude_element_wrong_type(self):
        self.assertConfigError('host = "pi"\nexclude = ["a", 2]\n', "exclude[1]")

    def test_commands_wrong_type(self):
        self.assertConfigError('host = "pi"\ncommands = "make"\n', "commands")

    def test_command_value_wrong_type(self):
        self.assertConfigError(
            'host = "pi"\n[commands]\nbuild = 7\n', "commands.build", "int"
        )

    def test_empty_command_value(self):
        self.assertConfigError('host = "pi"\n[commands]\nbuild = "  "\n', "commands.build")

    def test_malformed_toml(self):
        self.assertConfigError('host = "pi\n', "malformed TOML")


class TestExitCode(ConfigTestCase):
    def test_every_config_failure_maps_to_64(self):
        from perch import errors

        self.assertEqual(errors.exit_code_for(ConfigError("x")), 64)


if __name__ == "__main__":
    unittest.main()
