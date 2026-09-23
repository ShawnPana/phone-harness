"""Malformed optional attachment state cannot break local backend selection."""
import math
import unittest
from unittest.mock import patch

from phone_harness import cloud


class AttachmentValidation(unittest.TestCase):
    valid = {"sid": "synthetic", "host": "phone.invalid", "port": 5555,
             "code": "synthetic", "expires_at": 2000, "unknown": "preserve"}

    def attached(self, session):
        with patch.object(cloud, "_load_state", return_value={"session": session}), \
                patch.object(cloud.time, "time", return_value=1000), \
                patch.object(cloud, "_api", side_effect=AssertionError("unexpected API call")):
            return cloud.attached()

    def test_complete_attachment_is_returned_without_rewriting(self):
        self.assertIs(self.attached(self.valid), self.valid)

    def test_expired_attachment_is_absent(self):
        self.assertIsNone(self.attached({**self.valid, "expires_at": 999}))

    def test_null_or_absent_expiry_remains_supported(self):
        for session in ({**self.valid, "expires_at": None}, {k: v for k, v in self.valid.items() if k != "expires_at"}):
            with self.subTest(session=session):
                self.assertIs(self.attached(session), session)

    def test_invalid_expiry_types_and_nonfinite_values_are_absent(self):
        for value in ("future", [], {}, True, math.inf, -math.inf, math.nan):
            with self.subTest(value=value):
                self.assertIsNone(self.attached({**self.valid, "expires_at": value}))

    def test_incomplete_or_invalid_endpoint_fields_are_absent(self):
        for key in ("sid", "host", "code", "port"):
            for value in (None, "", [], {}, True):
                with self.subTest(key=key, value=value):
                    self.assertIsNone(self.attached({**self.valid, key: value}))
            with self.subTest(missing=key):
                self.assertIsNone(self.attached({k: v for k, v in self.valid.items() if k != key}))

    def test_port_accepts_integer_and_numeric_string_within_tcp_range(self):
        for port in (1, 65535, "5555"):
            with self.subTest(port=port):
                session = {**self.valid, "port": port}
                self.assertIs(self.attached(session), session)
        for port in (0, 65536, -1, "abc", 1.5):
            with self.subTest(port=port):
                self.assertIsNone(self.attached({**self.valid, "port": port}))

    def test_nonobject_state_is_absent(self):
        for session in (None, [], "bad", 1):
            self.assertIsNone(self.attached(session))


if __name__ == "__main__":
    unittest.main()
