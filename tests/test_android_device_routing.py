import os
import unittest
from unittest.mock import patch

from phone_harness.android import Android
from phone_harness.transport import connect


class AndroidDeviceRoutingTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("ANDROID_SERIAL", None)

    def test_interleaved_connections_route_to_their_own_device(self):
        with patch("phone_harness.android._run", return_value="ok") as run:
            first = connect("android", serial="first")
            first._sh("getprop ro.build.version.sdk")
            second = connect("android", serial="second")
            second._sh("getprop ro.build.version.sdk")
            first._sh("getprop ro.build.version.sdk")
            self.assertEqual([call.args[:2] for call in run.call_args_list],
                             [("-s", "first"), ("-s", "second"), ("-s", "first")])
        self.assertNotIn("ANDROID_SERIAL", os.environ)

    def test_explicit_serial_does_not_change_environment_default(self):
        os.environ["ANDROID_SERIAL"] = "default"
        first = Android(serial="explicit")
        second = Android()
        self.assertEqual(first._resolve(), "explicit")
        self.assertEqual(second._resolve(), "default")
        self.assertEqual(os.environ["ANDROID_SERIAL"], "default")

    def test_bounds_keep_the_connection_identity(self):
        first = Android(serial="first")
        second = Android(serial="second")
        with patch("phone_harness.android._run", return_value="Physical size: 1080x1920"):
            self.assertEqual(first.send("screen.bounds")["id"], "first")
            self.assertEqual(second.send("screen.bounds")["id"], "second")

    def test_auto_selection_is_cached_without_changing_process_environment(self):
        with patch("phone_harness.android._load", return_value={}), \
             patch("phone_harness.android._attached", return_value=[("usb", "device", "usb")]) as attached, \
             patch("phone_harness.android._prop", return_value="model"), \
             patch("phone_harness.android._remember"):
            phone = Android()
            self.assertEqual(phone._resolve(), "usb")
            self.assertEqual(phone._resolve(), "usb")
            attached.assert_called_once_with()
        self.assertNotIn("ANDROID_SERIAL", os.environ)


if __name__ == "__main__":
    unittest.main()
