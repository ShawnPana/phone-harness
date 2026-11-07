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


class AndroidScreenCaptureTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        self.phone = Android(serial="test-device")

    def test_lit_display_args_single_display(self):
        dump = (
            'Display Devices: size=1\n'
            '  DisplayDeviceInfo{"Built-in Screen": uniqueId="local:0", 1080 x 2400, state ON}\n'
        )
        with patch.object(self.phone, "_adb", return_value=dump):
            self.assertEqual(self.phone._lit_display_args(), [])

    def test_lit_display_args_foldable_one_on(self):
        dump = (
            'Display Devices: size=2\n'
            '  DisplayDeviceInfo{"Inner Display": uniqueId="local:4619827259835644672", 2076 x 2152, state OFF}\n'
            '  DisplayDeviceInfo{"Outer Display": uniqueId="local:4619827259835644673", 1080 x 2342, state ON}\n'
        )
        with patch.object(self.phone, "_adb", return_value=dump):
            self.assertEqual(self.phone._lit_display_args(), ["-d", "4619827259835644673"])

    def test_lit_display_args_foldable_both_on_or_off(self):
        dump = (
            'Display Devices: size=2\n'
            '  DisplayDeviceInfo{"Inner Display": uniqueId="local:4619827259835644672", 2076 x 2152, state ON}\n'
            '  DisplayDeviceInfo{"Outer Display": uniqueId="local:4619827259835644673", 1080 x 2342, state ON}\n'
        )
        with patch.object(self.phone, "_adb", return_value=dump):
            self.assertEqual(self.phone._lit_display_args(), [])

    def test_lit_display_args_adb_failure(self):
        with patch.object(self.phone, "_adb", side_effect=RuntimeError("dumpsys failed")):
            self.assertEqual(self.phone._lit_display_args(), [])

    def test_screen_capture_strips_warning_prefix(self):
        warning = (
            b"[Warning] Multiple displays were found, but no display id was specified!\n"
            b"Defaulting to the first display found.\n"
        )
        valid_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        raw_output = warning + valid_png

        def adb_call(*args, **kwargs):
            if "dumpsys" in args:
                return ""
            if "screencap" in args:
                return raw_output
            raise AssertionError(args)

        with patch.object(self.phone, "_screen_require", return_value={"w": 1080, "h": 2342}), \
             patch.object(self.phone, "_adb", side_effect=adb_call):
            path, bounds = self.phone._screen_capture()
            try:
                with open(path, "rb") as f:
                    data = f.read()
                self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
                self.assertNotIn(b"[Warning]", data)
                self.assertEqual(data, valid_png)
            finally:
                if os.path.exists(path):
                    os.unlink(path)

    def test_screen_capture_passes_display_id_when_available(self):
        screencap_args = []
        valid_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"

        def adb_call(*args, **kwargs):
            if "screencap" in args:
                screencap_args.extend(args)
                return valid_png
            raise AssertionError(args)

        with patch.object(self.phone, "_screen_require", return_value={"w": 1080, "h": 2342}), \
             patch.object(self.phone, "_lit_display_args", return_value=["-d", "4619827259835644673"]), \
             patch.object(self.phone, "_adb", side_effect=adb_call):
            path, bounds = self.phone._screen_capture()
            try:
                self.assertIn("-d", screencap_args)
                self.assertIn("4619827259835644673", screencap_args)
            finally:
                if os.path.exists(path):
                    os.unlink(path)


if __name__ == "__main__":
    unittest.main()
