"""How the CLI is invoked: the script on stdin, or as one quoted argument."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")


class Invocation(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.env = {**os.environ, "PYTHONPATH": SRC, "PHONE_HARNESS_HOME": self.home.name,
                    "PHONE_HARNESS_TELEMETRY": "0", "PHONE_HARNESS_PLATFORM": "android"}

    def run_cli(self, *args, stdin=""):
        return subprocess.run([sys.executable, "-m", "phone_harness.run", *args], input=stdin,
                              capture_output=True, text=True, env=self.env)

    def test_the_script_on_stdin_runs(self):
        r = self.run_cli(stdin='print("via stdin")')
        self.assertEqual((r.returncode, r.stdout), (0, "via stdin\n"), r.stderr)

    def test_a_script_passed_as_the_one_argument_runs(self):
        # codex, opencode and hermes generate `phone-harness "<script>"`; this was the top
        # first-run failure: the usage text came back and the task never ran.
        r = self.run_cli('print("hello from phone")')
        self.assertEqual((r.returncode, r.stdout), (0, "hello from phone\n"), r.stderr)
        r = self.run_cli("from phone_harness.helpers import *\nprint(1 + 1)")
        self.assertEqual((r.returncode, r.stdout), (0, "2\n"), r.stderr)

    def test_a_lone_word_is_a_mistyped_command_not_a_script(self):
        r = self.run_cli("statu")
        self.assertEqual(r.returncode, 1)
        self.assertIn("'statu' is not a command", r.stderr)
        self.assertIn("A script goes on stdin", r.stderr)
        self.assertIn("phone-harness <<'PY'", r.stderr)          # the usage text follows
        self.assertNotIn("NameError", r.stderr)                    # it was never executed

    def test_several_arguments_are_not_a_script(self):
        r = self.run_cli("run", 'print("x")')
        self.assertEqual(r.returncode, 1)
        self.assertIn("is not a command", r.stderr)
        self.assertNotIn("x\n", r.stdout)

    def test_help_and_subcommands_still_dispatch(self):
        r = self.run_cli("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("Usage:", r.stdout)
        r = self.run_cli("skill")
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.startswith("---"), r.stdout[:40])


if __name__ == "__main__":
    unittest.main()
