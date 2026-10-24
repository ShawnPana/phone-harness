"""The once-a-day update notice, and `phone-harness update`. No network."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from phone_harness import update  # noqa: E402
from phone_harness.run import USAGE, _skill_text  # noqa: E402


class _Body:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class UpdateCheck(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self._old_home = os.environ.get("PHONE_HARNESS_HOME")
        os.environ["PHONE_HARNESS_HOME"] = self.home.name

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("PHONE_HARNESS_HOME", None)
        else:
            os.environ["PHONE_HARNESS_HOME"] = self._old_home

    def _opener(self, version, calls):
        def opener(req, timeout):
            calls.append((req.full_url, timeout))
            return _Body({"info": {"version": version}})
        return opener

    def test_versions_compare_numerically(self):
        self.assertTrue(update.is_newer("0.4.0", "0.3.0"))
        self.assertTrue(update.is_newer("0.3.10", "0.3.9"))
        self.assertTrue(update.is_newer("1.0", "0.9.9"))
        self.assertFalse(update.is_newer("0.3.0", "0.3.0"))
        self.assertFalse(update.is_newer("0.3.0", "0.4.0"))

    def test_a_newer_release_prints_one_line_and_is_cached_for_a_day(self):
        calls = []
        err = io.StringIO()
        update.announce(opener=self._opener("9.9.0", calls), version="0.3.0",
                        now=1_000_000, stderr=err)
        self.assertEqual(
            err.getvalue().strip(),
            "phone-harness 9.9.0 is available (you have 0.3.0): run phone-harness update")
        self.assertEqual(calls, [("https://pypi.org/pypi/phone-harness/json", 2)])
        again = io.StringIO()

        def boom(*args, **kwargs):
            raise AssertionError("cache should have covered a day")

        update.announce(opener=boom, version="0.3.0", now=1_000_000 + 10, stderr=again)
        self.assertEqual(again.getvalue().strip(), err.getvalue().strip())
        cached = json.loads((Path(self.home.name) / "cache" / "update.json").read_text())
        self.assertEqual(cached["latest"], "9.9.0")
        self.assertEqual(cached["checked_at"], 1_000_000)

    def test_the_same_version_is_quiet(self):
        err = io.StringIO()
        update.announce(opener=self._opener("0.3.0", []), version="0.3.0",
                        now=1_000_000, stderr=err)
        self.assertEqual(err.getvalue(), "")

    def test_a_network_error_does_not_raise_or_retry_until_tomorrow(self):
        calls = []

        def opener(req, timeout):
            calls.append(1)
            raise TimeoutError("timed out")

        err = io.StringIO()
        update.announce(opener=opener, version="0.3.0", now=1_000_000, stderr=err)
        update.announce(opener=opener, version="0.3.0", now=1_000_000 + 60, stderr=err)
        self.assertEqual(calls, [1])
        self.assertEqual(err.getvalue(), "")

    def test_notice_is_skipped_without_a_tty_when_opted_out_and_in_tests(self):
        self.assertTrue(update.suppressed(
            environ={"PHONE_HARNESS_NO_UPDATE_CHECK": "1"}, isatty=True, testing=False))
        self.assertTrue(update.suppressed(environ={}, isatty=False, testing=False))
        self.assertTrue(update.suppressed(environ={}, isatty=True, testing=True))
        self.assertFalse(update.suppressed(environ={}, isatty=True, testing=False))
        self.assertTrue(update.under_test())
        with patch.object(update, "announce", side_effect=AssertionError("should not run")):
            update.maybe_notify()

    def test_maybe_notify_swallows_a_broken_check(self):
        with patch.object(update, "suppressed", return_value=False), \
             patch.object(update, "announce", side_effect=TimeoutError("nope")):
            update.maybe_notify()


class UpdateCommand(unittest.TestCase):
    def test_uv_tool_and_git_checkout_are_detected_from_the_path(self):
        self.assertTrue(update.is_uv_tool_prefix(
            Path("/home/me/.local/share/uv/tools/phone-harness")))
        self.assertFalse(update.is_uv_tool_prefix(Path("/usr/lib")))
        self.assertIsNone(update.git_checkout_root(
            Path("/tmp/lib/site-packages/phone_harness/update.py")))
        root = update.git_checkout_root(Path(__file__).resolve())
        self.assertEqual(root, Path(__file__).resolve().parents[1])

    def test_uv_tool_runs_uv_tool_upgrade(self):
        with patch.object(update, "install_mode", return_value="uv"), \
             patch.object(update.subprocess, "call", return_value=0) as call:
            out = io.StringIO()
            with redirect_stdout(out):
                code = update.cmd_update([])
        self.assertEqual(code, 0)
        call.assert_called_once_with(["uv", "tool", "upgrade", "phone-harness"])
        self.assertIn("uv", out.getvalue())

    def test_a_missing_uv_prints_the_command_instead_of_raising(self):
        with patch.object(update, "install_mode", return_value="uv"), \
             patch.object(update.subprocess, "call", side_effect=FileNotFoundError):
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = update.cmd_update([])
        self.assertEqual(code, 1)
        self.assertIn("uv tool upgrade phone-harness", err.getvalue())

    def test_a_git_checkout_pulls_ff_only(self):
        root = update.git_checkout_root(Path(update.__file__).resolve())
        self.assertIsNotNone(root)
        with patch.object(update, "install_mode", return_value="git"), \
             patch.object(update.subprocess, "call", return_value=0) as call:
            out = io.StringIO()
            with redirect_stdout(out):
                code = update.cmd_update([])
        self.assertEqual(code, 0)
        call.assert_called_once_with(["git", "-C", str(root), "pull", "--ff-only"])
        self.assertIn(str(root), out.getvalue())

    def test_anything_else_prints_the_pip_command_and_does_not_run_it(self):
        with patch.object(update, "install_mode", return_value="pip"), \
             patch.object(update.subprocess, "call") as call:
            out = io.StringIO()
            with redirect_stdout(out):
                code = update.cmd_update([])
        self.assertEqual(code, 0)
        call.assert_not_called()
        self.assertEqual(out.getvalue().strip(), "pip install -U phone-harness")

    def test_extra_arguments_are_refused(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = update.cmd_update(["--now"])
        self.assertEqual(code, 2)
        self.assertIn("Usage: phone-harness update", err.getvalue())

    def test_help_and_the_skill_mention_update(self):
        self.assertIn("phone-harness update", USAGE)
        USAGE.encode("ascii")
        skill = _skill_text()
        self.assertIn("phone-harness update", skill)
        self.assertIn("PHONE_HARNESS_NO_UPDATE_CHECK=1", skill)
        skill.encode("ascii")
        install = Path(__file__).resolve().parents[1].joinpath("install.md").read_text(encoding="utf-8")
        self.assertIn("phone-harness update", install)
        self.assertIn("pip install -U phone-harness", install)


if __name__ == "__main__":
    unittest.main()
