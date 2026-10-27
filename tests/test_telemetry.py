"""PostHog identify for a cloud account. No network: the sender is mocked."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from phone_harness import config, telemetry  # noqa: E402
from phone_harness.cloud import _install_id  # noqa: E402


class IdentifyAccount(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.patched = mock.patch.dict(os.environ, {"PHONE_HARNESS_HOME": self.home.name}, clear=False)
        self.patched.start()
        self.addCleanup(self.patched.stop)
        os.environ.pop("PHONE_HARNESS_TELEMETRY", None)

    def telemetry_path(self):
        return Path(self.home.name) / "state" / "telemetry.json"

    def test_off_sends_nothing_and_does_not_remember_the_account(self):
        os.environ["PHONE_HARNESS_TELEMETRY"] = "0"
        with mock.patch("phone_harness.telemetry._send_detached") as send:
            telemetry.identify_account("u1")
            telemetry.identify_account("")
            telemetry.identify_account(None)
            send.assert_not_called()
        self.assertFalse(self.telemetry_path().exists())

    def test_identify_sets_the_account_on_the_install_and_skips_a_repeat(self):
        os.environ["PHONE_HARNESS_TELEMETRY"] = "1"
        sent = []
        with mock.patch("phone_harness.telemetry._send_detached", side_effect=sent.append):
            telemetry.identify_account("  u1  ")
            telemetry.identify_account("u1")
            telemetry.identify_account("u2")
        self.assertEqual(len(sent), 2)
        install = config.install_id()
        first, second = sent
        self.assertEqual(first["event"], "$identify")
        self.assertEqual(first["distinct_id"], install)
        self.assertEqual(first["api_key"], telemetry.POSTHOG_KEY)
        self.assertEqual(first["properties"]["$set"], {"account_id": "u1"})
        self.assertTrue(first["properties"]["$process_person_profile"])
        self.assertNotIn("$anon_distinct_id", first["properties"])
        self.assertNotIn("email", json.dumps(first))
        self.assertEqual(second["properties"]["$set"], {"account_id": "u2"})
        self.assertEqual(config.install_account(), "u2")
        stored = json.loads(self.telemetry_path().read_text())
        self.assertEqual(stored["install_id"], install)
        self.assertEqual(stored["account_id"], "u2")

    def test_a_missing_install_id_omits_the_cloud_header(self):
        with mock.patch("phone_harness.cloud.config.install_id", side_effect=OSError("disk")):
            self.assertIsNone(_install_id())
