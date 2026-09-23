"""Input only follows a confirmed awake/unlocked observation."""
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from phone_harness import admin, config
from phone_harness.android import Android


class WakeGate(unittest.TestCase):
    def setUp(self):
        self.phone = Android()
        for target, kwargs in (("phone_harness.android.time.time", {"return_value": 1000}),
                               ("phone_harness.android.time.sleep", {"return_value": None})):
            mocked = patch(target, **kwargs)
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_failed_wake_rejects_input_without_caching_ready(self):
        with patch.object(self.phone, "_screen_status", return_value=(False, False)) as status, \
                patch.object(self.phone, "_sh") as shell:
            with self.assertRaisesRegex(RuntimeError, "asleep"):
                self.phone._input_tap(100, 200)
        self.assertEqual(status.call_count, 2)
        shell.assert_called_once_with("input keyevent KEYCODE_WAKEUP")
        self.assertEqual(self.phone._gate_at, 0)

    def test_failed_wake_can_be_retried_after_device_recovers(self):
        with patch.object(self.phone, "_screen_status", side_effect=[(False, False), (False, False), (True, False)]) as status, \
                patch.object(self.phone, "_sh") as shell:
            with self.assertRaises(RuntimeError):
                self.phone._input_tap(100, 200)
            self.phone._input_tap(100, 200)
        self.assertEqual(status.call_count, 3)
        self.assertEqual(shell.call_args_list[-1].args, ("input tap 100 200",))

    def test_successful_wake_allows_input(self):
        with patch.object(self.phone, "_screen_status", side_effect=[(False, False), (True, False)]), \
                patch.object(self.phone, "_sh") as shell:
            self.phone._input_tap(100, 200)
        self.assertEqual(shell.call_count, 2)
        self.assertEqual(self.phone._gate_at, 1000)

    def test_locked_phone_keeps_unlock_guidance(self):
        with patch.object(self.phone, "_screen_status", return_value=(True, True)), \
                patch.object(self.phone, "_sh") as shell:
            with self.assertRaisesRegex(RuntimeError, "Unlock it"):
                self.phone._input_tap(100, 200)
        shell.assert_not_called()

    def test_already_awake_burst_keeps_fast_path(self):
        with patch.object(self.phone, "_screen_status", return_value=(True, False)) as status, \
                patch.object(self.phone, "_sh") as shell:
            self.phone._input_tap(100, 200)
            self.phone._input_tap(200, 300)
        self.assertEqual(status.call_count, 1)
        self.assertEqual(shell.call_count, 2)

    def test_session_state_distinguishes_asleep_from_missing_adb(self):
        with patch.object(self.phone, "_resolve", return_value="fake"), \
                patch.object(self.phone, "_screen_status", return_value=(False, False)), \
                patch.object(self.phone, "_sh"):
            self.assertEqual(self.phone.send("session.state"), "asleep")

    def test_session_require_reports_failed_wake_instead_of_installing_adb(self):
        self.phone._bounds = {"x": 0, "y": 0, "w": 720, "h": 1280, "id": "fake"}
        with patch.object(self.phone, "_resolve", return_value="fake"), \
                patch.object(self.phone, "_screen_status", return_value=(False, False)), \
                patch.object(self.phone, "_sh"):
            with self.assertRaisesRegex(RuntimeError, "asleep"):
                self.phone.send("session.require")

    def test_doctor_gives_wake_guidance_for_asleep_state(self):
        output = io.StringIO()
        with patch.object(config, "get", side_effect=lambda key: "android" if key == "platform" else "fake-adb"), \
                patch.object(admin.shutil, "which", return_value="fake-tool"), \
                patch("phone_harness.android.Android", return_value=self.phone), \
                patch.object(self.phone, "send", return_value="asleep") as send, \
                redirect_stdout(output):
            self.assertEqual(admin.run_doctor("android"), 1)
        self.assertIn("wake it on the device", output.getvalue())
        send.assert_called_once_with("session.state")


if __name__ == "__main__":
    unittest.main()
