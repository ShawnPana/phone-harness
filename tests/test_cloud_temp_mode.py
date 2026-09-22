"""Requested cloud mode with fake API/ADB and temporary local state."""
import io
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from phone_harness import cloud


class TemporaryMode(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        env = patch.dict(os.environ, {"PHONE_HARNESS_HOME": home.name,
                                     "PHONE_HARNESS_TELEMETRY": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.existing = self.session("existing", "saved-profile")
        self.created = self.session("temporary", None)
        self.calls = []
        self.creation_error = None
        for target, kwargs in (
            ("phone_harness.cloud.shutil.which", {"return_value": "fake-adb"}),
            ("phone_harness.cloud.ensure_connected", {"return_value": "phone.invalid:5555"}),
            ("phone_harness.cloud._api", {"side_effect": self.api}),
            ("phone_harness.android._run", {"side_effect": AssertionError("unexpected ADB call")}),
            ("urllib.request.urlopen", {"side_effect": AssertionError("unexpected HTTP call")}),
            ("webbrowser.open", {"side_effect": AssertionError("unexpected browser call")}),
        ):
            mocked = patch(target, **kwargs)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.attach_existing()

    @staticmethod
    def session(sid, profile):
        return {"id": sid, "state": "ready", "profile": profile,
                "expires_at": time.time() + 600,
                "adb": {"host": "phone.invalid", "port": 5555, "code": "synthetic"}}

    def attach_existing(self):
        cloud._attach(self.existing, "saved-profile")

    def api(self, method, path, body=None, **kwargs):
        self.calls.append((method, path, body))
        if (method, path) == ("GET", "/sessions/existing"):
            return self.existing
        if (method, path) == ("POST", "/sessions"):
            if self.creation_error:
                raise self.creation_error
            return self.created
        if (method, path) == ("GET", "/sessions/temporary"):
            return self.created
        raise AssertionError((method, path, body))

    def start(self, *args):
        with redirect_stdout(io.StringIO()):
            return cloud._start([*args, "--no-watch"])

    def test_explicit_temp_creates_throwaway_and_preserves_saved_session(self):
        self.assertEqual(self.start("--temp"), 0)
        self.assertEqual(cloud.attached()["sid"], "temporary")
        posts = [body for method, path, body in self.calls if method == "POST"]
        self.assertEqual(posts, [{"timeout_seconds": 900}])
        self.assertFalse(any(method == "DELETE" for method, _, _ in self.calls))

    def test_repeated_temporary_start_reuses_temporary_session(self):
        self.existing["profile"] = None
        self.attach_existing()
        self.assertEqual(self.start("--temp"), 0)
        self.assertEqual(cloud.attached()["sid"], "existing")
        self.assertFalse(any(method == "POST" for method, _, _ in self.calls))

    def test_ordinary_start_reuses_saved_session(self):
        self.assertEqual(self.start(), 0)
        self.assertEqual(cloud.attached()["sid"], "existing")
        self.assertFalse(any(method == "POST" for method, _, _ in self.calls))

    def test_creation_failure_leaves_saved_attachment_and_session_intact(self):
        before = cloud._state_path().read_bytes()
        self.creation_error = cloud.CloudError(503, {"error": "try later"})
        with self.assertRaises(cloud.CloudError):
            self.start("--temp")
        self.assertEqual(cloud._state_path().read_bytes(), before)
        self.assertFalse(any(method == "DELETE" for method, _, _ in self.calls))


if __name__ == "__main__":
    unittest.main()
