"""Readiness waits only release sessions allocated by this invocation."""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from phone_harness import cloud


class WaitOwnership(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        env = patch.dict(os.environ, {"PHONE_HARNESS_HOME": home.name,
                                     "PHONE_HARNESS_TELEMETRY": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.calls = []
        self.state = "provisioning"
        self.clock_reads = 0
        for target, kwargs in (
            ("phone_harness.cloud._api", {"side_effect": self.api}),
            ("phone_harness.cloud.time.monotonic", {"side_effect": self.clock}),
            ("phone_harness.cloud.time.sleep", {"return_value": None}),
            ("phone_harness.cloud.shutil.which", {"return_value": "fake-adb"}),
            ("phone_harness.cloud.ensure_connected", {"return_value": "phone.invalid:5555"}),
            ("phone_harness.android._run", {"side_effect": AssertionError("unexpected ADB call")}),
            ("urllib.request.urlopen", {"side_effect": AssertionError("unexpected HTTP call")}),
        ):
            mocked = patch(target, **kwargs)
            mocked.start()
            self.addCleanup(mocked.stop)

    def clock(self):
        self.clock_reads += 1
        return (self.clock_reads - 1) * (cloud.READY_WAIT + 1)

    def api(self, method, path, body=None, **kwargs):
        self.calls.append((method, path))
        if (method, path) == ("GET", "/sessions"):
            return [{"id": "existing"}]
        if (method, path) == ("GET", "/me"):
            return {"profile": {"id": "saved", "state": "running", "session": "existing"}}
        if (method, path) == ("POST", "/sessions"):
            return {"id": "created"}
        if path in ("/sessions/existing", "/sessions/created"):
            if method == "DELETE":
                return {}
            return {"id": path.rsplit("/", 1)[1], "state": self.state,
                    "adb": {"host": "phone.invalid", "port": 5555, "code": "synthetic"}}
        raise AssertionError((method, path))

    def assert_borrowed_timeout(self, fn):
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as error:
            fn()
        self.assertIn("Gave up", str(error.exception))
        self.assertNotIn("asked the service", str(error.exception))
        self.assertFalse(any(method == "DELETE" for method, _ in self.calls))

    def test_use_timeout_does_not_release_existing_session(self):
        self.assert_borrowed_timeout(lambda: cloud._use(["existing"]))

    def test_attached_start_timeout_does_not_release_existing_session(self):
        cloud._save_state({"session": {"sid": "existing", "host": "phone.invalid",
                                      "port": 5555, "code": "synthetic"}})
        self.assert_borrowed_timeout(lambda: cloud._start(["--no-watch"]))
        self.assertEqual(cloud.attached()["sid"], "existing")

    def test_saved_profile_reuse_timeout_does_not_release_existing_session(self):
        self.assert_borrowed_timeout(lambda: cloud._start(["--no-watch"]))

    def test_new_allocation_timeout_still_requests_release(self):
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as error:
            cloud._start(["--temp", "--no-watch"])
        self.assertIn(("DELETE", "/sessions/created"), self.calls)
        self.assertIn("asked the service", str(error.exception))
        self.assertEqual(sum(method == "DELETE" for method, _ in self.calls), 1)

    def test_ready_existing_session_still_attaches(self):
        self.state = "ready"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cloud._use(["existing"]), 0)
        self.assertEqual(cloud.attached()["sid"], "existing")
        self.assertFalse(any(method == "DELETE" for method, _ in self.calls))

    def test_terminal_existing_states_stop_without_release(self):
        for state in ("error", "closing"):
            with self.subTest(state=state):
                self.state = state
                self.calls.clear()
                with self.assertRaises(SystemExit) as error:
                    cloud._wait_ready("existing")
                self.assertIn("did not start", str(error.exception))
                self.assertEqual(self.calls, [("GET", "/sessions/existing")])


if __name__ == "__main__":
    unittest.main()
