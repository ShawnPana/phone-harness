import os
import unittest
from unittest.mock import patch

with patch.dict(os.environ, {"PHONE_HARNESS_PLATFORM": "android"}):
    from phone_harness import helpers
from phone_harness.android import Android


class AndroidScrollTests(unittest.TestCase):
    def test_public_scroll_sends_a_drag_in_each_direction(self):
        phone = Android(serial="test-device")
        bounds = {"x": 0, "y": 0, "w": 1000, "h": 2000, "id": "test-device"}
        expected = {
            "down": "input swipe 500 1000 500 500 300",
            "up": "input swipe 500 1000 500 1500 300",
            "right": "input swipe 500 1000 250 1000 300",
            "left": "input swipe 500 1000 750 1000 300",
        }
        with patch.object(helpers, "phone", phone), \
             patch.object(phone, "_screen_require", return_value=bounds), \
             patch.object(phone, "_gate"), patch.object(phone, "_sh") as shell:
            for direction, command in expected.items():
                with self.subTest(direction=direction):
                    shell.reset_mock()
                    helpers.scroll(direction, amount=0.25)
                    shell.assert_called_once_with(command)

    def test_raw_vertical_scroll_keeps_existing_positional_arguments(self):
        phone = Android(serial="test-device")
        with patch.object(phone, "_gate"), patch.object(phone, "_sh") as shell:
            phone._input_scroll(100, 200, 30, 2)
            shell.assert_called_once_with("input swipe 100 200 100 230 150")


if __name__ == "__main__":
    unittest.main()
