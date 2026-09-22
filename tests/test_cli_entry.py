"""How the CLI is invoked: the script on stdin, or as one quoted argument."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")


class _Cli(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.env = {**os.environ, "PYTHONPATH": SRC, "PHONE_HARNESS_HOME": self.home.name,
                    "PHONE_HARNESS_TELEMETRY": "0", "PHONE_HARNESS_PLATFORM": "android"}

    def run_cli(self, *args, stdin=""):
        return subprocess.run([sys.executable, "-m", "phone_harness.run", *args], input=stdin,
                              capture_output=True, text=True, env=self.env)



class Invocation(_Cli):
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


class ConsoleEncoding(_Cli):
    """A Chinese or Korean Windows console encodes its pipes as cp936/cp949. Seen in
    the field: a tap fired, then the run died printing OCR text with an emoji in it.
    PYTHONIOENCODING=gbk gives that console on any OS."""

    def run_gbk(self, script, *args):
        env = {**self.env, "PYTHONIOENCODING": "gbk"}
        r = subprocess.run([sys.executable, "-m", "phone_harness.run", *args],
                           input=script.encode("utf-8"), capture_output=True, env=env)
        return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")

    def test_output_with_an_emoji_does_not_crash_a_gbk_pipe(self):
        code, out, err = self.run_gbk('rows = [{"text": "Close \u2705"}]\nprint("tapped", rows[0]["text"])')
        self.assertEqual(code, 0, err)
        self.assertNotIn("UnicodeEncodeError", err)
        self.assertEqual(out.strip(), "tapped Close \u2705")     # pipes are UTF-8, so the glyph survives

    def test_a_script_with_non_ascii_text_is_read_as_utf8(self):
        # The agent writes UTF-8. Decoded as gbk, an emoji or a CJK string in the script
        # itself raised UnicodeDecodeError before a single line ran.
        code, out, err = self.run_gbk('print("tap \u2705 done")\nprint("\u5fae\u4fe1 \ud55c\uae00")')
        self.assertEqual(code, 0, err)
        self.assertEqual(out.split("\n")[:2], ["tap \u2705 done", "\u5fae\u4fe1 \ud55c\uae00"])

    def test_pipes_are_utf8_with_replacement(self):
        code, out, err = self.run_gbk("import sys; print(sys.stdout.encoding.lower(), sys.stdout.errors, "
                                      "sys.stderr.encoding.lower(), sys.stderr.errors)")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.split(), ["utf-8", "replace", "utf-8", "replace"])


if __name__ == "__main__":
    unittest.main()
