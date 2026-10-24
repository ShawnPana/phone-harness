"""Local sign-out survives malformed best-effort revocation responses."""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from phone_harness import cloud


class Response:
    status = 200

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class LogoutParseFailure(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        env = patch.dict(os.environ, {"PHONE_HARNESS_HOME": home.name,
                                     "PHONE_HARNESS_TELEMETRY": "0"})
        env.start()
        self.addCleanup(env.stop)
        cloud._save_auth({"access_token": "synthetic-access", "refresh_token": "synthetic-refresh"})
        attached = patch.object(cloud, "attached", return_value=None)
        attached.start()
        self.addCleanup(attached.stop)

    def logout(self, responses):
        output = io.StringIO()
        with patch("urllib.request.urlopen", side_effect=responses) as request, redirect_stdout(output):
            result = cloud._logout([])
        self.assertEqual(result, 0)
        self.assertFalse(cloud._auth_path().exists())
        self.assertIn("Signed out", output.getvalue())
        return request.call_count

    def test_malformed_first_revocation_still_clears_auth(self):
        self.assertEqual(self.logout([Response(b"<html>unavailable</html>"), Response(b"{}")]), 2)

    def test_malformed_second_revocation_still_clears_auth(self):
        self.assertEqual(self.logout([Response(b"{}"), Response(b"not json")]), 2)

    def test_network_failure_still_clears_auth(self):
        self.assertEqual(self.logout([OSError("offline"), OSError("offline")]), 2)

    def test_invalid_utf8_reply_still_clears_auth(self):
        self.assertEqual(self.logout([Response(b"\xff"), Response(b"{}")]), 2)

    def test_valid_and_empty_success_replies_clear_auth(self):
        self.assertEqual(self.logout([Response(b"{}"), Response(b"")]), 2)

    def test_no_signin_does_not_call_revocation(self):
        cloud._auth_path().unlink()
        with patch("urllib.request.urlopen", side_effect=AssertionError("unexpected HTTP")) as request, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cloud._logout([]), 0)
        request.assert_not_called()

    def test_unrelated_programming_error_remains_visible(self):
        with patch.object(cloud, "_form_post", side_effect=RuntimeError("programming error")), \
                self.assertRaisesRegex(RuntimeError, "programming error"):
            cloud._logout([])


if __name__ == "__main__":
    unittest.main()
