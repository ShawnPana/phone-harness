import os
import sys
import tempfile
import unittest
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from phone_harness import config  # noqa: E402


class ConfigUnsetTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self._old_home = os.environ.get("PHONE_HARNESS_HOME")
        os.environ["PHONE_HARNESS_HOME"] = self.home.name

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("PHONE_HARNESS_HOME", None)
        else:
            os.environ["PHONE_HARNESS_HOME"] = self._old_home

    def test_unset_nested_key_with_scalar_parent_does_not_crash(self):
        config.save({"android": "custom-string"})
        # Previously raised AttributeError: 'str' object has no attribute 'pop'
        config.unset("android.adb")
        self.assertEqual(config.load(), {"version": config.VERSION, "android": "custom-string"})

    def test_unset_nested_key_removes_value(self):
        config.set("android.adb", "/custom/adb")
        self.assertEqual(config.get("android.adb"), "/custom/adb")
        config.unset("android.adb")
        self.assertEqual(config.get("android.adb"), config.DEFAULTS["android"]["adb"])


if __name__ == "__main__":
    unittest.main()
