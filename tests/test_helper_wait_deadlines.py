"""Polling owns elapsed sleep budgets; active backend calls have their own bound."""
import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from phone_harness import transport


class WaitDeadlines(unittest.TestCase):
    def setUp(self):
        with patch.object(transport, "connect", return_value=Mock()):
            self.helpers = importlib.import_module("phone_harness.helpers")
        self.now = 0
        self.sleeps = []
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.frame = Path(self.temp.name) / "frame.png"
        self.frame.write_bytes(b"frame")
        for name, effect in (("monotonic", lambda: self.now), ("time", lambda: self.now), ("sleep", self.sleep)):
            mocked = patch.object(self.helpers.time, name, side_effect=effect)
            mocked.start()
            self.addCleanup(mocked.stop)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def test_text_wait_clamps_oversized_interval_without_late_poll(self):
        with patch.object(self.helpers, "find_text", return_value=[]) as poll:
            self.assertIsNone(self.helpers.wait_for_text("absent", timeout=1, interval=60))
        self.assertEqual(self.sleeps, [1])
        self.assertEqual(poll.call_count, 1)

    def test_app_wait_clamps_oversized_interval_without_late_poll(self):
        with patch.object(self.helpers, "send", return_value="other") as poll:
            self.assertFalse(self.helpers.wait_for_app("wanted", timeout=1, interval=60))
        self.assertEqual(self.sleeps, [1])
        self.assertEqual(poll.call_count, 1)

    def test_initial_success_does_not_sleep(self):
        box = {"text": "wanted"}
        with patch.object(self.helpers, "find_text", return_value=[box]), \
                patch.object(self.helpers, "send", return_value="wanted"):
            self.assertIs(self.helpers.wait_for_text("wanted"), box)
            self.assertTrue(self.helpers.wait_for_app("wanted"))
        self.assertEqual(self.sleeps, [])

    def test_zero_budget_keeps_single_text_and_app_observation(self):
        with patch.object(self.helpers, "find_text", return_value=[]) as text_poll, \
                patch.object(self.helpers, "send", return_value="other") as app_poll:
            self.assertIsNone(self.helpers.wait_for_text("absent", timeout=0))
            self.assertFalse(self.helpers.wait_for_app("absent", timeout=0))
        self.assertEqual((text_poll.call_count, app_poll.call_count), (1, 1))
        self.assertEqual(self.sleeps, [])

    def test_stability_wait_clamps_interval_and_zero_budget_captures_nothing(self):
        with patch.object(self.helpers, "send", return_value=(str(self.frame), {})) as capture:
            self.assertFalse(self.helpers.wait_stable(timeout=1, interval=60))
            self.assertEqual(capture.call_count, 1)
            capture.reset_mock()
            self.assertFalse(self.helpers.wait_stable(timeout=0))
            capture.assert_not_called()
        self.assertEqual(self.sleeps, [1])

    def test_stability_success_keeps_consecutive_frame_semantics(self):
        with patch.object(self.helpers, "send", return_value=(str(self.frame), {})) as capture:
            self.assertTrue(self.helpers.wait_stable(timeout=1, interval=0.25))
        self.assertEqual(capture.call_count, 2)
        self.assertEqual(self.sleeps, [0.25])

    def test_elapsed_waits_do_not_depend_on_adjustable_wall_clock(self):
        with patch.object(self.helpers.time, "time", side_effect=AssertionError("wall clock used")), \
                patch.object(self.helpers, "find_text", return_value=[]), \
                patch.object(self.helpers, "send", return_value="other"):
            self.assertIsNone(self.helpers.wait_for_text("absent", timeout=1, interval=60))
            self.assertFalse(self.helpers.wait_for_app("absent", timeout=1, interval=60))

    def test_scroll_settle_clamps_only_its_polling_budget(self):
        with patch.object(self.helpers, "_win", return_value={"x": 0, "y": 0, "w": 100, "h": 100}), \
                patch.object(self.helpers, "_content_texts", side_effect=[[{"text": "before"}], [{"text": "after"}], [{"text": "late"}]]), \
                patch.object(self.helpers, "send"), \
                patch.object(self.helpers.time, "time", side_effect=AssertionError("wall clock used")):
            result = self.helpers.scroll_screen(settle=0.1)
        self.assertAlmostEqual(self.now, 0.5)  # existing gesture grace + caller's settle budget
        self.assertEqual(result["after"], frozenset(["after"]))


if __name__ == "__main__":
    unittest.main()
