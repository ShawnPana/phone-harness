"""Step/run_steps: the declarative check-ladder runner behind `--doctor`."""
import sys
import unittest
from pathlib import Path
from unittest import mock

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

# Same technique as tests/test_ios_session.py: on a machine without pyobjc
# (e.g. the Linux/Windows CI runners), stub the frameworks so _doctor_ios's
# checks can still be exercised via mock.patch("Quartz.X", ..., create=True)
# — that needs the module importable, stub or real.
try:
    import Quartz  # noqa: F401
except ImportError:
    import importlib.machinery
    import types

    class _LazyModule(types.ModuleType):
        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            obj = type(name, (), {})
            setattr(self, name, obj)
            return obj

    for _name in ("Quartz", "Vision", "Foundation", "AppKit",
                  "ApplicationServices", "objc", "CoreFoundation"):
        if _name in sys.modules:
            continue
        _mod = _LazyModule(_name)
        _mod.__spec__ = importlib.machinery.ModuleSpec(_name, loader=None)
        sys.modules[_name] = _mod

import phone_harness.admin as admin  # noqa: E402
from phone_harness.admin import Step, run_steps  # noqa: E402


class RunStepsTagging(unittest.TestCase):
    def test_a_passing_step_prints_pass_and_reports_no_failure(self):
        out = []
        with mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            failures = run_steps([Step(lambda: ("thing ok", True, ""))])
        self.assertEqual(failures, [])
        self.assertIn("[PASS] thing ok", out[0])

    def test_a_fatal_failing_step_prints_fail_and_is_reported(self):
        out = []
        with mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            failures = run_steps([Step(lambda: ("thing bad", False, "do X"), fatal=True)])
        self.assertEqual(failures, ["thing bad"])
        self.assertIn("[FAIL] thing bad", out[0])
        self.assertIn("do X", out[0])

    def test_a_non_fatal_failing_step_prints_warn_not_fail_and_is_not_reported(self):
        """This is GitHub issue #4's second bug: a non-fatal check must not
        print [FAIL], and must not count toward the overall verdict."""
        out = []
        with mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            failures = run_steps([Step(lambda: ("thing bad", False, "ignore"), fatal=False)])
        self.assertEqual(failures, [])
        self.assertIn("[WARN] thing bad", out[0])
        self.assertNotIn("[FAIL]", out[0])


class RunStepsControlFlow(unittest.TestCase):
    def test_stop_if_failed_skips_the_remaining_steps(self):
        calls = []

        def second():
            calls.append("second")
            return ("second", True, "")

        with mock.patch("builtins.print"):
            run_steps([
                Step(lambda: ("first", False, ""), stop_if_failed=True),
                Step(second),
            ])
        self.assertEqual(calls, [])

    def test_a_step_returning_none_prints_nothing_and_is_skipped(self):
        out = []
        with mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            failures = run_steps([Step(lambda: None), Step(lambda: ("after", True, ""))])
        self.assertEqual(len(out), 1)
        self.assertIn("after", out[0])
        self.assertEqual(failures, [])

    def test_a_four_tuple_result_overrides_the_steps_own_fatal_flag(self):
        """Android's accessibility-tree check needs this: the exception
        branch is non-fatal even though the step's default is fatal=True."""
        out = []
        with mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            failures = run_steps([Step(lambda: ("tree", False, "known limitation", False),
                                        fatal=True)])
        self.assertEqual(failures, [])
        self.assertIn("[WARN] tree", out[0])

    def test_a_raising_step_is_reported_as_a_failure_instead_of_crashing(self):
        """This is GitHub issue #4's first bug, generalized: any step whose
        body raises (not just the ApplicationServices import) must become
        a clean FAIL line instead of an uncaught traceback."""
        def boom():
            raise ModuleNotFoundError("No module named 'ApplicationServices'")

        out = []
        with mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            failures = run_steps([Step(boom, fatal=True)])
        self.assertEqual(failures, ["boom"])
        self.assertIn("[FAIL] boom", out[0])
        self.assertIn("ApplicationServices", out[0])


class DoctorIosHostOsGuard(unittest.TestCase):
    """_doctor_ios must refuse cleanly off macOS instead of letting the
    pyobjc import fail with a pip-install hint that can't work there
    (pyobjc only ships macOS wheels)."""

    def test_off_macos_stops_before_the_pyobjc_check_with_a_clear_message(self):
        out = []
        with mock.patch("sys.platform", "linux"), \
             mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            failures = admin._doctor_ios()
        self.assertEqual(failures, ["running on macOS"])
        self.assertEqual(len(out), 1)
        self.assertIn("[FAIL] running on macOS", out[0])
        self.assertIn("needs macOS", out[0])

    def test_on_macos_the_ladder_continues_past_this_check(self):
        out = []
        with mock.patch("sys.platform", "darwin"), \
             mock.patch("ApplicationServices.AXIsProcessTrusted", return_value=True, create=True), \
             mock.patch("Quartz.CGPreflightScreenCaptureAccess", return_value=True, create=True), \
             mock.patch("phone_harness.mirror.APP_PATH", "/Applications/Mirroring.app"), \
             mock.patch("phone_harness.mirror.APP_NAME", "Mirroring"), \
             mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("phone_harness.mirror.running_app", return_value=object()), \
             mock.patch("phone_harness.mirror.find_window", return_value=None), \
             mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            admin._doctor_ios()
        self.assertTrue(any("[PASS] running on macOS" in l for l in out))


class DoctorIosPyobjcGuard(unittest.TestCase):
    """GitHub issue #4, bug #1: a missing ApplicationServices import must
    report a FAIL, not crash _doctor_ios before the PASS line even makes
    sense."""

    def test_a_missing_pyobjc_module_is_a_single_fail_not_a_crash(self):
        real_import = __import__

        def fake_import(name, *a, **kw):
            if name == "ApplicationServices":
                raise ModuleNotFoundError("No module named 'ApplicationServices'")
            return real_import(name, *a, **kw)

        out = []
        with mock.patch("sys.platform", "darwin"), \
             mock.patch("builtins.__import__", side_effect=fake_import), \
             mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            failures = admin._doctor_ios()
        self.assertEqual(len(failures), 1)
        self.assertIn("pyobjc frameworks", failures[0])
        self.assertTrue(any("[FAIL] pyobjc frameworks" in l for l in out))
        # Host-OS PASS, then pyobjc FAIL — nothing past that ran:
        self.assertEqual(len(out), 2)


class DoctorIosNonFatalTag(unittest.TestCase):
    """GitHub issue #4, bug #2: a non-fatal check (Mirroring not running
    yet) must print WARN, never FAIL, and must not block "all clear"."""

    def test_mirroring_not_running_prints_warn_and_is_non_fatal(self):
        out = []
        with mock.patch("sys.platform", "darwin"), \
             mock.patch("Quartz.CGPreflightScreenCaptureAccess", return_value=True, create=True), \
             mock.patch("ApplicationServices.AXIsProcessTrusted", return_value=True, create=True), \
             mock.patch("phone_harness.mirror.APP_PATH", "/Applications/Mirroring.app"), \
             mock.patch("phone_harness.mirror.APP_NAME", "Mirroring"), \
             mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("phone_harness.mirror.running_app", return_value=None), \
             mock.patch("phone_harness.mirror.find_window", return_value=None), \
             mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            admin._doctor_ios()
        self.assertTrue(any("[WARN]" in l and "running" in l for l in out))
        self.assertFalse(any("[FAIL]" in l and "running" in l for l in out))


class DoctorIosLadder(unittest.TestCase):
    """The capture->OCR->session-state tail, exercised with every real
    side effect mocked so it runs the same on any OS."""

    def test_a_good_capture_runs_ocr_then_checks_session_state(self):
        win = {"id": 42}
        out = []
        with mock.patch("sys.platform", "darwin"), \
             mock.patch("ApplicationServices.AXIsProcessTrusted", return_value=True, create=True), \
             mock.patch("Quartz.CGPreflightScreenCaptureAccess", return_value=True, create=True), \
             mock.patch("phone_harness.mirror.APP_PATH", "/Applications/Mirroring.app"), \
             mock.patch("phone_harness.mirror.APP_NAME", "Mirroring"), \
             mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("phone_harness.mirror.running_app", return_value=object()), \
             mock.patch("phone_harness.mirror.find_window", return_value=win), \
             mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)), \
             mock.patch("os.path.getsize", return_value=50_000), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch("os.unlink"), \
             mock.patch("phone_harness.ocr.recognize", return_value=[1, 2, 3]), \
             mock.patch("phone_harness.ios.IPhone") as iphone_cls, \
             mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            iphone_cls.return_value.send.return_value = "ready"
            failures = admin._doctor_ios()
        self.assertEqual(failures, [])
        self.assertTrue(any("Vision OCR works (3 text boxes)" in l for l in out))
        self.assertTrue(any("session state: ready" in l for l in out))

    def test_a_bad_capture_skips_ocr_but_still_checks_session_state(self):
        win = {"id": 42}
        out = []
        with mock.patch("sys.platform", "darwin"), \
             mock.patch("ApplicationServices.AXIsProcessTrusted", return_value=True, create=True), \
             mock.patch("Quartz.CGPreflightScreenCaptureAccess", return_value=True, create=True), \
             mock.patch("phone_harness.mirror.APP_PATH", "/Applications/Mirroring.app"), \
             mock.patch("phone_harness.mirror.APP_NAME", "Mirroring"), \
             mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("phone_harness.mirror.running_app", return_value=object()), \
             mock.patch("phone_harness.mirror.find_window", return_value=win), \
             mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)), \
             mock.patch("os.path.getsize", return_value=10), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch("os.unlink"), \
             mock.patch("phone_harness.ocr.recognize") as ocr_recognize, \
             mock.patch("phone_harness.ios.IPhone") as iphone_cls, \
             mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            iphone_cls.return_value.send.return_value = "ready"
            admin._doctor_ios()
        ocr_recognize.assert_not_called()
        self.assertFalse(any("Vision OCR" in l for l in out))
        self.assertTrue(any("session state: ready" in l for l in out))

    def test_no_window_stops_before_capture_and_session_state(self):
        out = []
        with mock.patch("sys.platform", "darwin"), \
             mock.patch("ApplicationServices.AXIsProcessTrusted", return_value=True, create=True), \
             mock.patch("Quartz.CGPreflightScreenCaptureAccess", return_value=True, create=True), \
             mock.patch("phone_harness.mirror.APP_PATH", "/Applications/Mirroring.app"), \
             mock.patch("phone_harness.mirror.APP_NAME", "Mirroring"), \
             mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("phone_harness.mirror.running_app", return_value=object()), \
             mock.patch("phone_harness.mirror.find_window", return_value=None), \
             mock.patch("subprocess.run") as run, \
             mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            failures = admin._doctor_ios()
        run.assert_not_called()
        self.assertIn("mirroring window found", failures)
        self.assertFalse(any("session state" in l for l in out))


class DoctorAndroidLadder(unittest.TestCase):
    def test_adb_missing_stops_before_anything_else_runs(self):
        out = []
        with mock.patch("phone_harness.config.get", return_value="adb"), \
             mock.patch("shutil.which", return_value=None), \
             mock.patch("phone_harness.android.Android") as android_cls, \
             mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            failures = admin._doctor_android()
        android_cls.assert_not_called()
        self.assertEqual(failures, ["adb found (adb)"])
        self.assertEqual(len(out), 1)

    def test_a_ready_phone_runs_the_full_ladder(self):
        out = []
        phone = mock.Mock()
        phone.send.side_effect = lambda op: {
            "session.state": "ready",
            "screen.bounds": {"id": "dev1", "w": 1080, "h": 2400},
            "tree": [1, 2, 3],
        }[op]
        with mock.patch("phone_harness.config.get", return_value="adb"), \
             mock.patch("shutil.which", side_effect=lambda b: "/usr/bin/adb" if b == "adb" else None), \
             mock.patch("phone_harness.android.Android", return_value=phone), \
             mock.patch("phone_harness.android._phone_label", return_value="Pixel"), \
             mock.patch("phone_harness.config.devices_of",
                        return_value={"phones": ["Pixel"], "primary": "Pixel"}), \
             mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            failures = admin._doctor_android()
        self.assertEqual(failures, [])
        self.assertTrue(any("talking to Pixel (dev1)" in l for l in out))
        self.assertTrue(any("accessibility tree readable (3 nodes)" in l for l in out))
        self.assertTrue(any("remembered phones: Pixel; primary: Pixel" in l for l in out))

    def test_a_locked_phone_warns_and_skips_bounds_and_tree(self):
        out = []
        phone = mock.Mock()
        phone.send.return_value = "locked"
        with mock.patch("phone_harness.config.get", return_value="adb"), \
             mock.patch("shutil.which", side_effect=lambda b: "/usr/bin/adb" if b == "adb" else None), \
             mock.patch("phone_harness.android.Android", return_value=phone), \
             mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            failures = admin._doctor_android()
        self.assertTrue(any("[WARN] phone unlocked" in l for l in out))
        self.assertFalse(any("talking to" in l for l in out))
        self.assertEqual(failures, [])  # "locked" counts as reachable; WARN isn't fatal

    def test_a_tree_read_error_is_reported_as_warn_not_fail(self):
        out = []
        phone = mock.Mock()

        def send(op):
            if op == "session.state":
                return "ready"
            if op == "screen.bounds":
                return {"id": "dev1", "w": 1080, "h": 2400}
            raise RuntimeError("uiautomator refused: screen never settled")

        phone.send.side_effect = send
        with mock.patch("phone_harness.config.get", return_value="adb"), \
             mock.patch("shutil.which", side_effect=lambda b: "/usr/bin/adb" if b == "adb" else None), \
             mock.patch("phone_harness.android.Android", return_value=phone), \
             mock.patch("phone_harness.android._phone_label", return_value="Pixel"), \
             mock.patch("phone_harness.config.devices_of",
                        return_value={"phones": ["Pixel"], "primary": "Pixel"}), \
             mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            failures = admin._doctor_android()
        self.assertTrue(any("[WARN] accessibility tree readable" in l for l in out))
        self.assertNotIn("accessibility tree readable", failures)


if __name__ == "__main__":
    unittest.main()
