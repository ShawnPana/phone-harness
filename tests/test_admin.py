"""Diagnostics must report missing prerequisites without running a device."""
import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from phone_harness import admin


class DoctorReporting(unittest.TestCase):
    def setUp(self):
        admin._failures.clear()
        self.addCleanup(admin._failures.clear)

    def test_missing_accessibility_framework_is_a_failure_not_a_traceback(self):
        modules = {name: ModuleType(name) for name in ("Quartz", "Vision", "AppKit")}
        modules["ApplicationServices"] = None
        output = io.StringIO()
        with patch.dict(sys.modules, modules), \
                patch.dict(os.environ, {"PHONE_HARNESS_PLATFORM": "ios"}), \
                contextlib.redirect_stdout(output):
            result = admin.run_doctor("ios")
        self.assertEqual(result, 1)
        self.assertIn("[FAIL] pyobjc frameworks", output.getvalue())
        self.assertIn("pyobjc-framework-ApplicationServices", output.getvalue())
        self.assertNotIn("[PASS] pyobjc frameworks", output.getvalue())
        self.assertNotIn("all clear", output.getvalue())

    def test_optional_failure_is_a_warning(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertFalse(admin._check("optional mirror", False, "not required", fatal=False))
        self.assertIn("[WARN] optional mirror", output.getvalue())
        self.assertNotIn("[FAIL]", output.getvalue())
        self.assertEqual(admin._failures, [])

    def test_fatal_failure_still_fails_and_success_still_passes(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertFalse(admin._check("required", False))
            self.assertTrue(admin._check("available", True))
        self.assertIn("[FAIL] required", output.getvalue())
        self.assertIn("[PASS] available", output.getvalue())
        self.assertEqual(admin._failures, ["required"])


if __name__ == "__main__":
    unittest.main()
