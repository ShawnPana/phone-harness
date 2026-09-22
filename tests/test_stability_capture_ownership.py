"""Stability polls own their temporary capture destination, not backend caches."""
import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from phone_harness import android, transport


class StabilityCaptureOwnership(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.object(transport, "connect", return_value=Mock()):
            self.helpers = importlib.import_module("phone_harness.helpers")
        self.clock = 0
        self.paths = []
        self.phone = android.Android()
        self.phone._bounds = {"x": 0, "y": 0, "w": 1, "h": 1, "id": "fake"}
        for target, kwargs in (
            ("phone_harness.helpers.time.time", {"side_effect": lambda: self.clock}),
            ("phone_harness.helpers.time.sleep", {"side_effect": self.sleep}),
            ("phone_harness.helpers.send", {"side_effect": self.capture}),
            ("phone_harness.android.subprocess.run", {"side_effect": AssertionError("unexpected ADB")}),
        ):
            mocked = patch(target, **kwargs)
            mocked.start()
            self.addCleanup(mocked.stop)

    def sleep(self, seconds):
        self.clock += seconds

    def capture(self, op, **kwargs):
        self.assertEqual(op, "screen.capture")
        with patch.object(self.phone, "_adb", return_value=b"same synthetic frame"):
            result = self.phone.send(op, **kwargs)
        self.paths.append(Path(result[0]))
        self.addCleanup(Path(result[0]).unlink, missing_ok=True)
        return result

    def test_success_cleans_actual_android_capture_files(self):
        self.assertTrue(self.helpers.wait_stable())
        self.assertEqual(len(self.paths), 2)
        self.assertFalse(any(p.exists() for p in self.paths))

    def test_timeout_cleans_actual_android_capture_files(self):
        self.assertFalse(self.helpers.wait_stable(timeout=0.2))
        self.assertFalse(any(p.exists() for p in self.paths))

    def test_capture_failure_cleans_partially_written_owned_destination(self):
        paths = []
        def fail(op, path=None):
            self.assertIsNotNone(path, "poll must supply a private destination")
            paths.append(Path(path))
            Path(path).write_bytes(b"partial")
            raise RuntimeError("capture failed")
        with patch.object(self.helpers, "send", side_effect=fail), self.assertRaises(RuntimeError):
            self.helpers.wait_stable()
        self.assertFalse(any(p.exists() for p in paths))

    def test_backend_returning_shared_cache_does_not_lose_it(self):
        shared = Path(self.temp.name) / "shared.png"
        shared.write_bytes(b"retained caller screenshot")
        with patch.object(self.helpers, "send", return_value=(str(shared), {})):
            self.assertTrue(self.helpers.wait_stable())
        self.assertEqual(shared.read_bytes(), b"retained caller screenshot")

    def test_public_explicit_screenshot_survives_polling(self):
        public = Path(self.temp.name) / "public.png"
        self.helpers.screenshot(str(public))
        self.paths.clear()
        self.helpers.wait_stable()
        self.assertTrue(public.exists())
        self.assertFalse(any(p.exists() for p in self.paths))


if __name__ == "__main__":
    unittest.main()
