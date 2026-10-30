"""What the CLI tells PostHog and the cloud: cli_event, $identify, the cloud headers.
No network: the detached sender is a list.

    python -m unittest tests.test_telemetry
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from phone_harness import config, run, telemetry  # noqa: E402
from phone_harness.remote import Remote  # noqa: E402

AGENT_MARKERS = [m.partition("=")[0] for m, _ in telemetry._AGENT_ENV_MARKERS]


class Telemetry(unittest.TestCase):
    """A fresh state dir, telemetry on, no agent in the environment, nothing sent."""

    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        env = mock.patch.dict(os.environ, {"PHONE_HARNESS_HOME": home.name,
                                           "PHONE_HARNESS_TELEMETRY": "1"})
        env.start()
        self.addCleanup(env.stop)
        for name in AGENT_MARKERS + ["PH_CLIENT", "PH_CLIENT_VERSION"]:
            os.environ.pop(name, None)
        self.sent = []
        sender = mock.patch.object(telemetry, "_send_detached", self.sent.append)
        sender.start()
        self.addCleanup(sender.stop)
        telemetry._session.clear()
        self.addCleanup(telemetry._session.clear)

    def event(self, **kw):
        telemetry.capture_cli_event(**{"action": "completed", "command": "script", **kw})
        return self.sent[-1]["properties"]

    def test_off_sends_nothing_links_nothing_and_adds_no_header(self):
        os.environ["PHONE_HARNESS_TELEMETRY"] = "0"
        os.environ["CLAUDECODE"] = "1"
        telemetry.link_user("u1")
        telemetry.capture_cli_event(action="completed", command="script")
        self.assertEqual(self.sent, [])
        self.assertEqual(telemetry.cloud_headers(), {})
        self.assertIsNone(config.telemetry_user())
        self.assertFalse(config.paths()["telemetry"].exists())

    def test_the_install_is_linked_once_per_sign_in(self):
        install = config.install_id()
        telemetry.link_user("u1")
        telemetry.link_user("u1")
        [identify] = self.sent
        self.assertEqual((identify["event"], identify["distinct_id"]), ("$identify", "u1"))
        self.assertEqual(identify["properties"]["$anon_distinct_id"], install)
        self.assertEqual(identify["properties"]["user_id"], "u1")
        telemetry.link_user("u2")                         # another account on this machine
        self.assertEqual([e["distinct_id"] for e in self.sent], ["u1", "u2"])
        telemetry.unlink_user()
        self.assertIsNone(config.telemetry_user())
        telemetry.link_user("u2")                         # signed in again
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(config.install_id(), install)
        for junk in (None, "", 42, {"uid": "u3"}):
            telemetry.link_user(junk)
        self.assertEqual(len(self.sent), 3)

    def test_a_failed_send_is_not_remembered_as_linked(self):
        with mock.patch.object(telemetry, "_send_detached", side_effect=OSError("no fork")):
            telemetry.link_user("u1")
        self.assertIsNone(config.telemetry_user())
        telemetry.link_user("u1")
        self.assertEqual(len(self.sent), 1)

    def test_a_cli_event_names_the_account_the_session_and_the_client(self):
        install = config.install_id()
        config.set_telemetry_user("u1")
        telemetry.note_session("sid1", "ios")
        os.environ["PH_CLIENT"] = "hermes"
        os.environ["CLAUDECODE"] = "1"
        props = self.event(phone="remote", subcommand=None, argv=None)
        self.assertEqual(self.sent[-1]["distinct_id"], install)
        self.assertEqual({k: props[k] for k in ("user_id", "session_id", "platform", "channel",
                                                "client", "agent_client", "phone")},
                         {"user_id": "u1", "session_id": "sid1", "platform": "ios", "channel": "cli",
                          "client": "hermes", "agent_client": "claude-code", "phone": "remote"})

        telemetry._session.clear()
        config.set_telemetry_user(None)
        os.environ.pop("PH_CLIENT")
        props = self.event(phone="iphone-mirroring")
        self.assertEqual((props["user_id"], props["session_id"], props["platform"], props["client"]),
                         (None, None, "ios", "claude-code"))
        self.assertEqual(self.event(phone="android")["platform"], "android")
        self.assertIsNone(self.event(command="cloud", subcommand="ls")["platform"])

    def test_task_and_output_are_sent_as_before(self):
        task = "# task: count\n" + "x" * 25_000
        props = self.event(task=task, task_intent="count", output="42\n", output_length=3)
        self.assertEqual(props["task"], task[:telemetry.MAX_TASK_LENGTH])
        self.assertEqual(props["task_length"], len(task))
        self.assertEqual((props["task_intent"], props["output"], props["output_length"]),
                         ("count", "42\n", 3))

    def test_a_session_note_keeps_what_it_learned_about_that_session(self):
        telemetry.note_session("s1", "android")
        telemetry.note_session("s1")
        self.assertEqual(telemetry._session, {"session_id": "s1", "platform": "android"})
        telemetry.note_session("s2")
        self.assertEqual(telemetry._session, {"session_id": "s2"})

    def test_the_cloud_headers_name_the_version_and_a_printable_client(self):
        version = telemetry._version() or "unknown"
        self.assertEqual(telemetry.cloud_headers(), {"X-Phone-Harness-Version": version})
        os.environ["CLAUDECODE"] = "1"
        self.assertEqual(telemetry.cloud_headers(),
                         {"X-Phone-Harness-Version": version, "X-Agent-Client": "claude-code"})
        os.environ["PH_CLIENT"] = "hérmès\r\nX-Injected: 1"
        client = telemetry.cloud_headers()["X-Agent-Client"]
        client.encode("ascii")
        self.assertNotIn("\r", client)
        self.assertNotIn("\n", client)
        os.environ["PH_CLIENT"] = "éè"                  # nothing printable left
        self.assertEqual(telemetry.cloud_headers(), {"X-Phone-Harness-Version": version})

    def test_the_linked_account_is_stored_beside_the_install_id(self):
        config.set_telemetry_user(None)                    # nothing to forget: no file written
        self.assertFalse(config.paths()["telemetry"].exists())
        install = config.install_id()
        config.set_telemetry_user("u1")
        self.assertEqual((config.telemetry_user(), config.install_id()), ("u1", install))
        config.set_telemetry_user(None)
        self.assertEqual((config.telemetry_user(), config.install_id()), (None, install))


class CommandLine(unittest.TestCase):
    def test_the_session_pin_is_left_out_and_a_pairing_code_is_masked(self):
        argv = run._telemetry_argv(["--session", "sid1", "cloud", "start", "ios", "--for", "5m"])
        self.assertEqual(argv, ["cloud", "start", "ios", "--for", "5m"])
        self.assertEqual(run._telemetry_command(argv), "cloud")
        self.assertEqual(run._telemetry_command(run._telemetry_argv(["--session", "sid1"])), "script")
        self.assertEqual(run._telemetry_argv(["android", "pair", "10.0.0.2:37000", "123456", "pixel"]),
                         ["android", "pair", "10.0.0.2:37000", "<code>", "pixel"])
        self.assertEqual(run._telemetry_argv(["android", "pair", "654321"]),
                         ["android", "pair", "<code>"])
        self.assertEqual(run._telemetry_argv(["android", "connect", "10.0.0.2:5555"]),
                         ["android", "connect", "10.0.0.2:5555"])


class RemoteDetail(unittest.TestCase):
    def test_the_detail_names_the_host_not_the_grant_in_the_control_url(self):
        detail = Remote("https://api.example/iphone/c/grant-token-0123456789", "bearer")._session_detail()
        self.assertIn("api.example", detail)
        self.assertNotIn("grant-token", detail)


if __name__ == "__main__":
    unittest.main()
