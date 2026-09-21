import os
import unittest
from unittest.mock import patch

from phone_harness.android import Android


def display(width, height, display_id=0):
    return (f"  Display: mDisplayId={display_id} (organized)\n"
            f"    init=1080x2280 440dpi cur={width}x{height} app={width}x{height}\n")


class AndroidScreenBoundsTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        self.phone = Android(serial="test-device")

    def test_bounds_follow_rotation_and_resize_on_same_connection(self):
        current = [1080, 2280]
        def shell(command):
            if command == "dumpsys window displays":
                return display(*current)
            return "Physical size: 1080x2280"
        with patch.object(self.phone, "_sh", side_effect=shell):
            for size in [(1080, 2280), (2280, 1080), (720, 1280), (1080, 2280)]:
                with self.subTest(size=size):
                    current[:] = size
                    bounds = self.phone.send("screen.bounds")
                    self.assertEqual((bounds["w"], bounds["h"]), size)
                    self.assertEqual(bounds["id"], "test-device")

    def test_secondary_display_is_not_used(self):
        output = display(800, 600, 2) + display(2280, 1080)
        with patch.object(self.phone, "_sh", return_value=output):
            bounds = self.phone.send("screen.bounds")
        self.assertEqual((bounds["w"], bounds["h"]), (2280, 1080))

    def test_unknown_dump_falls_back_to_override_size(self):
        def shell(command):
            return ("unknown format" if command == "dumpsys window displays"
                    else "Physical size: 1080x2280\nOverride size: 720x1280")
        with patch.object(self.phone, "_sh", side_effect=shell):
            bounds = self.phone.send("screen.bounds")
        self.assertEqual((bounds["w"], bounds["h"]), (720, 1280))

    def test_unavailable_window_dump_falls_back_to_physical_size(self):
        def shell(command):
            if command == "dumpsys window displays":
                raise RuntimeError("not available")
            return "Physical size: 1080x2280"
        with patch.object(self.phone, "_sh", side_effect=shell):
            bounds = self.phone.send("screen.bounds")
        self.assertEqual((bounds["w"], bounds["h"]), (1080, 2280))

    def test_missing_dimensions_do_not_reuse_stale_bounds(self):
        with patch.object(self.phone, "_sh", return_value="Physical size: 1080x2280"):
            self.assertIsNotNone(self.phone.send("screen.bounds"))
        with patch.object(self.phone, "_sh", return_value="unavailable"):
            self.assertIsNone(self.phone.send("screen.bounds"))


if __name__ == "__main__":
    unittest.main()
