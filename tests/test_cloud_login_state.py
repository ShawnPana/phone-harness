"""Fresh grants and refresh grants have distinct local state ownership."""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from phone_harness import cloud, config


class LoginState(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        env = patch.dict(os.environ, {"PHONE_HARNESS_HOME": home.name,
                                     "PHONE_HARNESS_TELEMETRY": "0"})
        env.start()
        self.addCleanup(env.stop)
        cloud._save_auth({"access_token": "old-access", "refresh_token": "old-refresh",
                          "email": "old@example.invalid"})
        self.session = {"sid": "old-session", "host": "phone.invalid", "port": 5555,
                        "code": "synthetic", "profile": "old-profile"}
        cloud._save_state({"session": self.session, "profile_id": "old-profile"})
        self.start = {"device_code": "synthetic", "user_code": "LOCAL", "interval": 0,
                      "verification_uri": "https://example.invalid/activate"}
        for target in ("urllib.request.urlopen", "subprocess.run", "webbrowser.open"):
            guard = patch(target, side_effect=AssertionError("unexpected external call"))
            guard.start()
            self.addCleanup(guard.stop)

    def login(self, profile="new-profile", tokens=None, error=None):
        tokens = tokens or {"access_token": "new-access"}
        me = {"email": "new@example.invalid", "profile": {"id": profile} if profile else None}
        with patch.object(cloud, "_form_post", side_effect=[(200, self.start), (200, tokens)]), \
                patch.object(cloud, "_api", side_effect=error, return_value=me), \
                redirect_stdout(io.StringIO()):
            return cloud._login(["--no-browser"])

    def test_fresh_grant_does_not_inherit_previous_refresh_token(self):
        self.assertEqual(self.login(), 0)
        auth = config._read(cloud._auth_path(), {})
        self.assertEqual(auth["access_token"], "new-access")
        self.assertIsNone(auth["refresh_token"])

    def test_new_profile_clears_old_attachment_without_releasing_remote_session(self):
        self.login()
        self.assertIsNone(cloud.attached())
        self.assertEqual(cloud._load_state()["profile_id"], "new-profile")

    def test_same_verified_profile_keeps_attachment(self):
        self.login(profile="old-profile")
        self.assertEqual(cloud.attached(), self.session)

    def test_missing_profile_does_not_prove_attachment_ownership(self):
        self.login(profile=None)
        self.assertIsNone(cloud.attached())

    def test_temporary_attachment_is_not_assumed_to_belong_to_fresh_login(self):
        self.session["profile"] = None
        cloud._save_state({"session": self.session, "profile_id": "old-profile"})
        self.login(profile="old-profile")
        self.assertIsNone(cloud.attached())

    def test_failed_signin_keeps_existing_credentials_and_attachment(self):
        auth, state = cloud._auth_path().read_bytes(), cloud._state_path().read_bytes()
        with self.assertRaises(SystemExit):
            self.login(error=cloud.CloudError(403, {"error": "refused"}))
        self.assertEqual(cloud._auth_path().read_bytes(), auth)
        self.assertEqual(cloud._state_path().read_bytes(), state)

    def test_nonrotating_refresh_preserves_refresh_token_and_email(self):
        with patch.object(cloud, "_form_post", return_value=(200, {"access_token": "refreshed"})):
            self.assertEqual(cloud._refresh(config._read(cloud._auth_path(), {})), "refreshed")
        auth = config._read(cloud._auth_path(), {})
        self.assertEqual(auth["refresh_token"], "old-refresh")
        self.assertEqual(auth["email"], "old@example.invalid")
        self.assertEqual(cloud.attached(), self.session)

    def test_rotating_refresh_replaces_refresh_token(self):
        with patch.object(cloud, "_form_post", return_value=(200, {"access_token": "refreshed", "refresh_token": "rotated"})):
            cloud._refresh(config._read(cloud._auth_path(), {}))
        self.assertEqual(config._read(cloud._auth_path(), {})["refresh_token"], "rotated")


if __name__ == "__main__":
    unittest.main()
