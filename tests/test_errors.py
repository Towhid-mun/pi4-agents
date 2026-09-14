"""Contract test for the exit-code map (ARCHITECTURE.md §7).

Offline. No target required, ever.
"""

import unittest

from perch import errors


class TestExitCodeMap(unittest.TestCase):
    """Every documented row of §7 maps to its documented code."""

    def test_documented_codes(self):
        cases = [
            (errors.ConfigError, 64),
            (errors.TargetUnreachable, 69),
            (errors.InternalError, 70),
            (errors.SyncError, 73),
            (errors.IndeterminateRun, 74),
            (errors.RunLockHeld, 75),
            (errors.Interrupted, 130),
        ]
        for cls, code in cases:
            with self.subTest(cls=cls.__name__):
                self.assertEqual(cls.exit_code, code)
                self.assertEqual(errors.exit_code_for(cls("boom")), code)

    def test_base_class_is_internal_error(self):
        # An unclassified failure must never look like a remote command's
        # own exit status.
        self.assertEqual(errors.PerchError.exit_code, errors.EXIT_INTERNAL)
        self.assertEqual(errors.exit_code_for(errors.PerchError("boom")), 70)

    def test_every_subclass_has_a_documented_code(self):
        documented = {64, 69, 70, 73, 74, 75, 130}
        seen = set()

        def walk(cls):
            for sub in cls.__subclasses__():
                seen.add(sub.exit_code)
                walk(sub)

        walk(errors.PerchError)
        self.assertTrue(seen)
        self.assertEqual(seen - documented, set())

    def test_tool_codes_never_collide_with_the_passthrough_band(self):
        # §7 reserves 1-63 for the remote command's own failure status.
        for code in (64, 69, 70, 73, 74, 75, 130):
            self.assertNotIn(code, errors.REMOTE_PASSTHROUGH_RANGE)

    def test_unknown_exception_maps_to_internal_error(self):
        self.assertEqual(errors.exit_code_for(ValueError("not ours")), 70)

    def test_remote_exit_code_passes_through_unchanged(self):
        # I6: the target's exit code is the tool's exit code.
        for code in (0, 1, 2, 63, 127):
            self.assertEqual(errors.exit_code_for_remote(code), code)

    def test_remote_exit_code_rejects_a_non_integer(self):
        with self.assertRaises(errors.InternalError):
            errors.exit_code_for_remote(None)


class TestExitCodeForRun(unittest.TestCase):
    def test_ordinary_completion_passes_through(self):
        self.assertEqual(errors.exit_code_for_run(0), 0)
        self.assertEqual(errors.exit_code_for_run(1), 1)

    def test_interrupted_is_130_regardless_of_exit_code(self):
        self.assertEqual(errors.exit_code_for_run(17, interrupted=True), 130)

    def test_indeterminate_is_74_regardless_of_exit_code(self):
        self.assertEqual(errors.exit_code_for_run(17, indeterminate=True), 74)

    def test_indeterminate_wins_over_interrupted(self):
        self.assertEqual(
            errors.exit_code_for_run(0, interrupted=True, indeterminate=True), 74
        )


class TestVersion(unittest.TestCase):
    def test_version_is_importable(self):
        from perch import __version__

        self.assertRegex(__version__, r"^\d+\.\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
