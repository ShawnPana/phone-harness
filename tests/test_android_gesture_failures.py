"""A transport/runtime failure is not an unsupported gesture capability."""
import subprocess
import unittest
from unittest.mock import patch

from phone_harness import android


class GestureFailures(unittest.TestCase):
    def setUp(self):
        self.phone = android.Android()
        self.phone._resolved = True
        self.phone._bounds = {"x": 0, "y": 0, "w": 720, "h": 1280, "id": "fake"}
        self.commands = []
        self.error = b"adb: error: device offline"
        for target, kwargs in (
            ("phone_harness.android._adb_bin", {"return_value": "fake-adb"}),
            ("phone_harness.android.subprocess.run", {"side_effect": self.run_adb}),
            ("phone_harness.cloud.attached", {"return_value": None}),
        ):
            mocked = patch(target, **kwargs)
            mocked.start()
            self.addCleanup(mocked.stop)
        gate = patch.object(self.phone, "_gate")
        gate.start()
        self.addCleanup(gate.stop)

    def run_adb(self, args, **kwargs):
        command = args[-1]
        self.commands.append(command)
        err = self.error if "motionevent" in command else None
        return subprocess.CompletedProcess(args, 1 if err else 0, b"", err or b"")

    def scroll(self):
        self.phone._input_scroll(360, 640, dy=-200)

    def test_transient_error_propagates_without_fallback_or_cached_capability(self):
        with self.assertRaisesRegex(RuntimeError, "device offline"):
            self.scroll()
        self.assertEqual(len(self.commands), 1)
        self.assertIsNone(self.phone._motionevents)

    def test_unknown_runtime_failure_is_not_assumed_unsupported(self):
        self.error = b"Permission denied"
        with self.assertRaisesRegex(RuntimeError, "Permission denied"):
            self.scroll()
        self.assertEqual(len(self.commands), 1)
        self.assertIsNone(self.phone._motionevents)

    def test_known_unknown_command_uses_and_caches_legacy_fallback(self):
        self.error = b"Error: Unknown command: motionevent"
        self.scroll()
        self.scroll()
        self.assertIs(self.phone._motionevents, False)
        self.assertEqual(sum("motionevent" in command for command in self.commands), 1)
        self.assertEqual(sum("input swipe" in command for command in self.commands), 2)

    def test_transient_failure_does_not_poison_later_supported_gesture(self):
        with self.assertRaises(RuntimeError):
            self.scroll()
        self.error = None
        self.scroll()
        self.assertIs(self.phone._motionevents, True)
        self.assertEqual(len(self.commands), 2)
        self.assertTrue(all("motionevent" in command for command in self.commands))

    def test_previously_supported_gesture_preserves_errors(self):
        self.error = None
        self.scroll()
        self.error = b"Error: Unknown command: motionevent"
        with self.assertRaises(RuntimeError):
            self.scroll()
        self.assertIs(self.phone._motionevents, True)
        self.assertEqual(len(self.commands), 2)


if __name__ == "__main__":
    unittest.main()
