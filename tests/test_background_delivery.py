import unittest
from unittest import mock

from phone_harness import background


class RecordingSkyLight:
    def __init__(self, status=0):
        self.status = status
        self.posts = 0

    def SLPSPostEventRecordTo(self, psn, record):
        self.posts += 1
        return self.status


class FailingApplicationServices:
    def GetProcessForPID(self, pid, psn):
        return -600


class BackgroundDeliveryTests(unittest.TestCase):
    def test_mouse_record_is_posted_without_fronting_the_process(self):
        sky = RecordingSkyLight()

        with mock.patch.object(background, "_sky", sky), \
                mock.patch.object(
                    background, "_process_serial_number",
                    return_value=background._PSN()):
            background._post(123, 456, 1, 10, 20, 5, 6)

        self.assertEqual(1, sky.posts)

    def test_make_key_posts_both_edges_without_fronting_the_process(self):
        sky = RecordingSkyLight()

        with mock.patch.object(background, "_sky", sky), \
                mock.patch.object(
                    background, "_process_serial_number",
                    return_value=background._PSN()):
            background._make_key(123, 456)

        self.assertEqual(2, sky.posts)

    def test_event_delivery_error_is_visible(self):
        sky = RecordingSkyLight(status=1000)
        record = (background.ctypes.c_uint8 * 0xf8)()

        with mock.patch.object(background, "_sky", sky):
            with self.assertRaisesRegex(RuntimeError, "status 1000"):
                background._post_record(background._PSN(), record)

    def test_process_lookup_error_is_visible(self):
        with mock.patch.object(
                background, "_appserv", FailingApplicationServices()):
            with self.assertRaisesRegex(RuntimeError, "status -600"):
                background._process_serial_number(123)


if __name__ == "__main__":
    unittest.main()
