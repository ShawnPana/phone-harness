"""`--doctor` behaviour that needs no phone: what it prints when a step fails."""
import contextlib
import io
import subprocess
import sys
import types
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import phone_harness  # noqa: E402
from phone_harness import admin  # noqa: E402


class _FailingIPhone:
    """An iPhone whose session.state fails the way it does without Screen Recording."""

    def send(self, op, **kw):
        raise RuntimeError("background capture failed: CGWindowListCreateImage "
                           "returned nothing, screencapture failed")


def _fake_macos():
    """Stand-ins for pyobjc and the iPhone backend: permissions missing, window found."""
    quartz = types.SimpleNamespace(CGPreflightScreenCaptureAccess=lambda: False)
    mirror = types.SimpleNamespace(
        APP_NAME="iPhone Mirroring", APP_PATH="/", running_app=lambda: object(),
        find_window=lambda: {"id": 1, "x": 0, "y": 0, "w": 300, "h": 650})
    return {
        "Quartz": quartz, "Vision": types.ModuleType("Vision"),
        "AppKit": types.ModuleType("AppKit"),
        "ApplicationServices": types.SimpleNamespace(AXIsProcessTrusted=lambda: False),
        "phone_harness.mirror": mirror,
        "phone_harness.ios": types.SimpleNamespace(IPhone=_FailingIPhone),
    }


class DoctorIOS(unittest.TestCase):
    def test_a_failed_capture_reaches_the_verdict_instead_of_a_traceback(self):
        fakes = _fake_macos()
        blank = subprocess.CompletedProcess([], returncode=1)
        out = io.StringIO()
        with unittest.mock.patch.dict(sys.modules, fakes), \
                unittest.mock.patch.object(phone_harness, "mirror", fakes["phone_harness.mirror"], create=True), \
                unittest.mock.patch.object(phone_harness, "ios", fakes["phone_harness.ios"], create=True), \
                unittest.mock.patch.object(admin.subprocess, "run", return_value=blank), \
                contextlib.redirect_stdout(out):
            code = admin.run_doctor("ios")
        text = out.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] window capture works", text)
        self.assertIn("[FAIL] session state — background capture failed", text)
        self.assertTrue(text.rstrip().endswith("fix the FAILs above, then re-run"), text)


if __name__ == "__main__":
    unittest.main()
