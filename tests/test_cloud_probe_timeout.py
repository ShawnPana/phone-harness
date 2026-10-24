"""Only the initial read-only health probe timeout enters connection recovery."""
import subprocess
import unittest
from unittest.mock import patch

from phone_harness import android, cloud


class ProbeTimeout(unittest.TestCase):
    session = {"sid": "synthetic", "host": "phone.invalid", "port": 5555, "code": "synthetic"}

    def test_initial_timeout_recovers_once_with_bounded_steps(self):
        responses = [subprocess.TimeoutExpired("fake-adb", 15), "", "connected", "unlocked", "ph-ok"]
        with patch.object(android, "_run", side_effect=responses) as run:
            self.assertEqual(cloud.ensure_connected(self.session, quiet=True), "phone.invalid:5555")
        self.assertEqual([c.kwargs["timeout"] for c in run.call_args_list], [15, 10, 20, 20, 15])
        self.assertEqual([c.args[0] for c in run.call_args_list], ["-s", "disconnect", "connect", "-s", "-s"])

    def test_healthy_probe_skips_reconnect(self):
        with patch.object(android, "_run", return_value="ph-ok") as run:
            cloud.ensure_connected(self.session, quiet=True)
        self.assertEqual(run.call_count, 1)

    def test_failed_connect_stops_without_unlock_or_reprobe(self):
        with patch.object(android, "_run", side_effect=[subprocess.TimeoutExpired("fake-adb", 15), "", "failed"]) as run:
            with self.assertRaisesRegex(RuntimeError, "could not reach"):
                cloud.ensure_connected(self.session, quiet=True)
        self.assertEqual(run.call_count, 3)

    def test_reprobe_timeout_is_not_retried(self):
        with patch.object(android, "_run", side_effect=["locked", "", "connected", "", subprocess.TimeoutExpired("fake-adb", 15)]) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                cloud.ensure_connected(self.session, quiet=True)
        self.assertEqual(run.call_count, 5)

    def test_locked_reprobe_is_not_retried(self):
        with patch.object(android, "_run", side_effect=["locked", "", "connected", "", "locked"]) as run:
            with self.assertRaisesRegex(RuntimeError, "stayed locked"):
                cloud.ensure_connected(self.session, quiet=True)
        self.assertEqual(run.call_count, 5)

    def test_non_timeout_probe_error_is_not_swallowed(self):
        with patch.object(android, "_run", side_effect=FileNotFoundError("fake-adb")) as run:
            with self.assertRaises(FileNotFoundError):
                cloud.ensure_connected(self.session, quiet=True)
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
