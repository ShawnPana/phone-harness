"""Only an authoritative missing-session response permits forgetting it."""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from phone_harness import cloud


class TransientSessionState(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        env = patch.dict(os.environ, {"PHONE_HARNESS_HOME": home.name,
                                     "PHONE_HARNESS_TELEMETRY": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.error_status = 503
        self.live_state = "ready"
        self.calls = []
        self.clock_reads = 0
        for target, kwargs in (
            ("phone_harness.cloud._api", {"side_effect": self.api}),
            ("phone_harness.cloud._bearer", {"return_value": "synthetic"}),
            ("phone_harness.cloud.shutil.which", {"return_value": "fake-adb"}),
            ("phone_harness.cloud.ensure_connected", {"return_value": "phone.invalid:5555"}),
            ("phone_harness.cloud.time.monotonic", {"side_effect": self.clock}),
            ("phone_harness.cloud.time.sleep", {"return_value": None}),
            ("phone_harness.android._run", {"return_value": ""}),
            ("urllib.request.urlopen", {"side_effect": AssertionError("unexpected HTTP call")}),
            ("webbrowser.open", {"side_effect": AssertionError("unexpected browser call")}),
        ):
            mocked = patch(target, **kwargs)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.save_attachment()

    def clock(self):
        self.clock_reads += 1
        return self.clock_reads * (cloud.SAVE_WAIT + 1)

    def save_attachment(self):
        cloud._save_state({"session": {"sid": "existing", "host": "phone.invalid",
                                      "port": 5555, "code": "synthetic"}})

    def api(self, method, path, body=None, **kwargs):
        self.calls.append((method, path))
        if (method, path) == ("GET", "/me"):
            return {"profile": {"id": "saved", "state": "running", "session": "existing"}}
        if (method, path) == ("GET", "/sessions/existing"):
            if self.error_status:
                raise cloud.CloudError(self.error_status, {"error": "synthetic session error"})
            return {"id": "existing", "state": self.live_state, "profile": "saved"}
        if (method, path) == ("POST", "/sessions"):
            return {"id": "created"}
        if (method, path) == ("GET", "/sessions/created"):
            return {"id": "created", "state": "ready",
                    "adb": {"host": "phone.invalid", "port": 5556, "code": "synthetic"}}
        raise AssertionError((method, path))

    def cli(self, args):
        self.output = io.StringIO()
        with redirect_stdout(self.output), redirect_stderr(io.StringIO()):
            return cloud.cli(args)

    def test_status_preserves_attachment_on_nonabsence_errors(self):
        for status in (401, 403, 429, 500, 503):
            with self.subTest(status=status):
                self.save_attachment()
                before = cloud._state_path().read_bytes()
                self.error_status = status
                self.assertEqual(self.cli([]), 1)
                self.assertEqual(cloud._state_path().read_bytes(), before)
                self.assertNotIn("is gone", self.output.getvalue())

    def test_start_does_not_provision_replacement_on_nonabsence_errors(self):
        for status in (401, 403, 429, 500, 503):
            with self.subTest(status=status):
                self.save_attachment()
                before = cloud._state_path().read_bytes()
                self.calls.clear()
                self.error_status = status
                self.assertEqual(self.cli(["start", "--temp", "--no-watch"]), 1)
                self.assertEqual(cloud._state_path().read_bytes(), before)
                self.assertFalse(any(method == "POST" for method, _ in self.calls))

    def test_profile_lookup_reports_nonabsence_errors_without_polling_or_creation(self):
        cloud._save_state({"profile_id": "saved"})
        before = cloud._state_path().read_bytes()
        for status in (401, 403, 429, 500, 503):
            with self.subTest(status=status):
                self.calls.clear()
                self.error_status = status
                self.assertEqual(self.cli(["start", "--no-watch"]), 1)
                self.assertEqual(cloud._state_path().read_bytes(), before)
                self.assertEqual(self.calls, [("GET", "/me"), ("GET", "/sessions/existing")])

    def test_status_404_still_detaches(self):
        self.error_status = 404
        self.assertEqual(self.cli([]), 0)
        self.assertIsNone(cloud.attached())
        self.assertIn("is gone; detaching", self.output.getvalue())

    def test_start_404_can_replace_confirmed_missing_session(self):
        self.error_status = 404
        self.assertEqual(self.cli(["start", "--temp", "--no-watch"]), 0)
        self.assertEqual(cloud.attached()["sid"], "created")
        self.assertEqual(sum(method == "POST" for method, _ in self.calls), 1)

    def test_healthy_ready_and_closing_status_remain_accurate(self):
        self.error_status = None
        for state in ("ready", "closing"):
            with self.subTest(state=state):
                self.live_state = state
                before = cloud._state_path().read_bytes()
                self.assertEqual(self.cli([]), 0)
                self.assertIn(state, self.output.getvalue())
                self.assertEqual(cloud._state_path().read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
