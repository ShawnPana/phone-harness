"""Usage telemetry must never include the phone or script's contents."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from phone_harness import config, run, telemetry


class MetadataOnlyTelemetry(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        env = patch.dict(os.environ, {
            "PHONE_HARNESS_HOME": self.home.name,
            "PHONE_HARNESS_TELEMETRY": "true",
        })
        env.start()
        self.addCleanup(env.stop)
        sender = patch.object(telemetry, "_send_detached")
        self.sender = sender.start()
        self.addCleanup(sender.stop)

    def assert_no_content(self, sentinel):
        payload = self.sender.call_args.args[0]
        self.assertNotIn(sentinel, json.dumps(payload))
        properties = payload["properties"]
        for field in ("task", "task_intent", "step_intent", "output", "steps", "error_message"):
            self.assertNotIn(field, properties)
        return properties

    def test_default_event_excludes_every_content_field(self):
        sentinel = "synthetic-private-content"
        telemetry.capture_cli_event(
            action="completed", command="script", task=sentinel,
            task_intent=sentinel, step=sentinel, phone="android",
            output=sentinel, output_length=len(sentinel),
            steps=[{"helper": "type_text", "args": sentinel, "error": sentinel}],
            step_count=1, duration_seconds=0.5, exit_code=0, error_message=sentinel,
        )
        properties = self.assert_no_content(sentinel)
        self.assertEqual(properties["command"], "script")
        self.assertEqual(properties["phone"], "android")
        self.assertEqual(properties["step_count"], 1)
        self.assertEqual(properties["exit_code"], 0)
        self.assertEqual(properties["output_length"], len(sentinel))

    def test_cli_success_and_error_paths_keep_only_usage_metadata(self):
        sentinel = "synthetic-script-and-screen-content"
        for error in (None, SystemExit(sentinel), RuntimeError(sentinel)):
            with self.subTest(error=type(error).__name__):
                self.sender.reset_mock()
                def script(_args):
                    print(sentinel)
                    run._traced("type_text", lambda value: None)(sentinel)
                    if error is not None:
                        raise error
                with patch.object(sys, "argv", ["phone-harness"]), \
                        patch.object(sys, "stdin", io.StringIO(sentinel)), \
                        patch.object(run, "_run", side_effect=script), \
                        contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    if error is None:
                        run.main()
                    else:
                        with self.assertRaises(type(error)):
                            run.main()
                self.sender.assert_called_once()
                properties = self.assert_no_content(sentinel)
                self.assertEqual(properties["exit_code"], 0 if error is None else 1)
                self.assertEqual(properties["step_count"], 1)

    def test_cloud_errors_never_send_response_contents(self):
        sentinel = "synthetic-cloud-response-content"
        with patch.object(sys, "argv", ["phone-harness", "cloud", "status"]), \
                patch.object(run, "_run", side_effect=RuntimeError(sentinel)), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError):
                run.main()
        self.assert_no_content(sentinel)

    def test_opt_out_does_not_create_identity_or_send(self):
        with patch.dict(os.environ, {"PHONE_HARNESS_TELEMETRY": "false"}):
            telemetry.capture_cli_event(action="completed", command="skill")
        self.sender.assert_not_called()
        self.assertFalse(config.paths()["telemetry"].exists())


if __name__ == "__main__":
    unittest.main()
