"""CLI help, bad flags, column layout, and optional color. No phone, no network."""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from phone_harness import android, cloud, term  # noqa: E402
from phone_harness.run import USAGE, _run as run_main  # noqa: E402
from phone_harness.update import installed_version  # noqa: E402


def _run(*args, stdin=None, env=None):
    merged = os.environ.copy()
    merged["PYTHONPATH"] = SRC + os.pathsep + merged.get("PYTHONPATH", "")
    merged["PHONE_HARNESS_TELEMETRY"] = "0"
    merged.pop("NO_COLOR", None)
    if env:
        merged.update(env)
    return subprocess.run([sys.executable, "-m", "phone_harness.run", *args],
                          input=stdin, capture_output=True, text=True, env=merged)


class Tty(io.StringIO):
    def isatty(self):
        return True


class HelpAndFlags(unittest.TestCase):
    def test_group_help_matches_the_usage_text_and_exits_cleanly(self):
        top = _run("--help")
        self.assertEqual(top.returncode, 0, top.stderr)
        self.assertIn("Commands:", top.stdout)
        self.assertIn("phone-harness cloud", top.stdout)

        for args, needle in ((["cloud", "--help"], "cloud ls"),
                             (["cloud", "-h"], "cloud start"),
                             (["android", "--help"], "android pair"),
                             (["android", "-h"], "android awake")):
            got = _run(*args)
            self.assertEqual(got.returncode, 0, got.stderr)
            self.assertIn(needle, got.stdout)
            got.stdout.encode("ascii")

        unknown = _run("cloud", "nope")
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("cloud ls", unknown.stdout)
        self.assertEqual(unknown.stderr, "")

        bogus = _run("--bogus")
        self.assertEqual(bogus.returncode, 2)
        self.assertIn("unrecognized arguments: --bogus", bogus.stderr)
        self.assertNotIn("Commands:", bogus.stdout)

    def test_every_subcommand_has_help_and_rejects_a_bad_flag(self):
        cloud_cmds = {
            "login": "--no-browser",
            "logout": None,
            "whoami": "--json",
            "start": "--for",
            "stop": "--all",
            "ls": "--json",
            "show": "--json",
            "use": "SID",
            "watch": "--print",
            "open": "--print",
            "phone": "--yes",
            "keys": "--json",
            "history": "--limit",
        }
        for name, flag in cloud_cmds.items():
            help_out = _run("cloud", name, "--help")
            self.assertEqual(help_out.returncode, 0, help_out.stderr)
            self.assertIn(f"phone-harness cloud {name}", help_out.stdout)
            self.assertIn("--session SID", help_out.stdout)
            help_out.stdout.encode("ascii")
            if flag and flag.startswith("-"):
                self.assertIn(flag, help_out.stdout, name)
            bad = _run("cloud", name, "--no-such-flag")
            self.assertEqual(bad.returncode, 2, name)
            self.assertIn("unrecognized arguments: --no-such-flag", bad.stderr)
            self.assertIn(f"phone-harness cloud {name}", bad.stderr)

        for name, flag in (("awake", "--bg"), ("rest", None), ("pair", None),
                           ("connect", None), ("use", None), ("forget", None)):
            help_out = _run("android", name, "--help")
            self.assertEqual(help_out.returncode, 0, help_out.stderr)
            self.assertIn(f"phone-harness android {name}", help_out.stdout)
            help_out.stdout.encode("ascii")
            if flag:
                self.assertIn(flag, help_out.stdout)
            bad = _run("android", name, "--no-such-flag")
            self.assertEqual(bad.returncode, 2, name)
            self.assertIn("unrecognized arguments: --no-such-flag", bad.stderr)

        awake_help = _run("android", "awake", "--help")
        self.assertIn("--no-mirror", awake_help.stdout)
        self.assertNotIn("--fg", awake_help.stdout)
        parsed, leftover = android._android_parsers()["awake"].parse_known_args(["--fg", "--bg"])
        self.assertEqual(leftover, [])
        self.assertTrue(parsed.fg and parsed.bg)

    def test_removed_flags_aliases_and_session_keep_their_messages(self):
        removed = _run("cloud", "start", "--iphone")
        self.assertEqual(removed.returncode, 1)
        self.assertIn("--iphone was removed", removed.stderr)
        self.assertNotIn("unrecognized", removed.stderr)
        self.assertNotIn("\x1b", removed.stderr)

        both = _run("cloud", "start", "--minutes", "5", "--for", "90s")
        self.assertEqual(both.returncode, 1)
        self.assertIn("not both", both.stderr)

        use = _run("cloud", "use")
        self.assertEqual(use.returncode, 1)
        self.assertIn("Usage: phone-harness cloud use SID", use.stderr)

        missing = _run("--session")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("--session needs a SID", missing.stderr)

        os.environ.pop("PHONE_HARNESS_SESSION", None)
        err = io.StringIO()
        try:
            with _stderr(err):
                code = cloud.cli(["--session"])
            self.assertEqual(code, 1)
            self.assertIn("--session needs a value", err.getvalue())

            err = io.StringIO()
            with _stderr(err):
                code = cloud.cli(["--session", "abc123", "use"])
            self.assertEqual(code, 1)
            self.assertEqual(os.environ.get("PHONE_HARNESS_SESSION"), "abc123")
            self.assertIn("Usage: phone-harness cloud use SID", err.getvalue())
        finally:
            os.environ.pop("PHONE_HARNESS_SESSION", None)

        pinned_help = _run("--session", "abc123", "cloud", "ls", "--help")
        self.assertEqual(pinned_help.returncode, 0, pinned_help.stderr)
        self.assertIn("phone-harness cloud ls", pinned_help.stdout)

        android_help = _run("android", "awake", "--session", "abc123", "--help")
        self.assertEqual(android_help.returncode, 0, android_help.stderr)
        self.assertIn("--bg", android_help.stdout)

        no_primary = _run("android", "connect")
        self.assertEqual(no_primary.returncode, 1)
        self.assertIn("no primary phone yet", no_primary.stdout)
        self.assertNotIn("\x1b", no_primary.stdout)

        start_help = _run("cloud", "start", "--help")
        self.assertIn("-m", start_help.stdout)
        self.assertIn("--minutes", start_help.stdout)
        self.assertNotIn("--iphone", start_help.stdout)
        history_help = _run("cloud", "history", "--help")
        self.assertIn("-n", history_help.stdout)

    def test_history_limit_flags_still_reach_the_command(self):
        with mock.patch.object(cloud, "_api", return_value={"items": []}) as api, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cloud.cli(["history", "-n", "3"]), 0)
            self.assertEqual(cloud.cli(["history", "--limit", "4"]), 0)
        self.assertIn("limit=3", api.call_args_list[0].args[1])
        self.assertIn("limit=4", api.call_args_list[1].args[1])

    def test_a_script_on_stdin_is_still_a_script(self):
        home = Path(self.id().replace(".", "_"))
        # Keep the script off the checkout's workspace and the real home.
        env = {"PHONE_HARNESS_HOME": str(Path("/tmp") / home),
               "PH_AGENT_WORKSPACE": str(Path("/tmp") / home / "ws"),
               "PHONE_HARNESS_PLATFORM": "android"}
        got = _run(stdin="print(6 * 7)\n", env=env)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("42", got.stdout)
        self.assertNotIn("unrecognized arguments", got.stderr)

    def test_hermes_scripts_do_not_receive_host_modules_or_path(self):
        home = Path("/tmp") / "ph-hermes-globals"
        script = (
            "names = set(globals())\n"
            "for key in ('Path', 'importlib', 'transport', 'hashlib', 'time',\n"
            "            'AGENT_WORKSPACE', 'CORE_DIR', 'screen', 'send', 'Unsupported'):\n"
            "    print(key, key in names)\n"
        )
        common = {
            "PHONE_HARNESS_HOME": str(home),
            "PH_AGENT_WORKSPACE": str(home / "ws"),
            "PHONE_HARNESS_PLATFORM": "android",
        }
        hermes = _run(stdin=script, env={**common, "PH_CLIENT": "hermes"})
        self.assertEqual(hermes.returncode, 0, hermes.stderr)
        got = dict(line.split() for line in hermes.stdout.splitlines() if line.strip())
        for key in ("Path", "importlib", "transport", "hashlib", "time", "AGENT_WORKSPACE", "CORE_DIR"):
            self.assertEqual(got[key], "False", key)
        for key in ("screen", "send", "Unsupported"):
            self.assertEqual(got[key], "True", key)

        plain = _run(stdin=script, env={**common, "PH_CLIENT": ""})
        self.assertEqual(plain.returncode, 0, plain.stderr)
        plain_got = dict(line.split() for line in plain.stdout.splitlines() if line.strip())
        for key in ("Path", "importlib", "transport", "AGENT_WORKSPACE"):
            self.assertEqual(plain_got[key], "True", key)


class _stderr:
    """Patch sys.stderr for one cloud.cli call."""

    def __init__(self, buf):
        self.buf = buf
        self.old = None

    def __enter__(self):
        self.old = sys.stderr
        sys.stderr = self.buf
        return self.buf

    def __exit__(self, *exc):
        sys.stderr = self.old


class Columns(unittest.TestCase):
    def test_columns_grow_to_fit_and_stay_aligned(self):
        long_id = "prof-948b7471ed7575fc17236906"
        long_sid = "sess-948b7471ed7575fc17236906"
        rows = [
            {"targeted": True, "label": "Saved phone", "platform": "android",
             "id": long_id, "state": "stored", "temporary": False,
             "session": {"id": long_sid, "left": "14 min"}},
            {"targeted": False, "label": "Temporary", "platform": "android",
             "id": None, "state": "-", "temporary": True, "session": {}},
        ]
        header, saved, temporary = cloud._format_ls(rows).splitlines()
        for line in (header, saved, temporary):
            self.assertEqual(line, line.rstrip())
        id_at = header.index("ID")
        state_at = header.index("STATE")
        note_at = header.index("NOTE")
        session_at = header.index("SESSION")
        left_at = header.index("LEFT")
        self.assertEqual(saved.index(long_id), id_at)
        self.assertEqual(state_at, id_at + len(long_id) + 1)
        self.assertTrue(saved[state_at:].startswith("stored"))
        self.assertEqual(saved.index(long_sid), session_at)
        self.assertEqual(left_at, session_at + len(long_sid) + 1)
        self.assertTrue(saved[left_at:].startswith("14 min"))
        self.assertTrue(saved.startswith("*"))
        self.assertTrue(temporary.startswith(" "))
        self.assertEqual(temporary.index("android"), header.index("PLATFORM"))
        self.assertEqual(temporary.index("temporary"), note_at)
        # A short id still sits under ID, padded out to the header when nothing is longer.
        short = cloud._format_ls([{
            "label": "A", "platform": "ios", "id": "iph", "state": "up",
            "temporary": False, "session": {"id": "s", "left": "1s"},
        }]).splitlines()
        self.assertEqual(short[1].index("iph"), short[0].index("ID"))
        self.assertEqual(short[0].index("STATE"), short[0].index("ID") + max(len("ID"), len("iph")) + 1)


class Color(unittest.TestCase):
    def setUp(self):
        self._no_color = os.environ.pop("NO_COLOR", None)

    def tearDown(self):
        if self._no_color is not None:
            os.environ["NO_COLOR"] = self._no_color
        else:
            os.environ.pop("NO_COLOR", None)

    def test_color_follows_the_stream_and_no_color(self):
        tty = Tty()
        plain = io.StringIO()
        self.assertEqual(term.paint("✓", "ok", tty), "\033[32m✓\033[0m")
        self.assertEqual(term.paint("nope", "err", tty), "\033[31mnope\033[0m")
        self.assertEqual(term.paint("aside", "note", tty), "\033[2maside\033[0m")
        self.assertEqual(term.paint("✓", "ok", plain), "✓")
        self.assertFalse(plain.isatty() if hasattr(plain, "isatty") else False)

        os.environ["NO_COLOR"] = ""
        self.assertEqual(term.paint("✓", "ok", tty), "✓")
        self.assertEqual(term.err("boom", tty), "boom")
        os.environ["NO_COLOR"] = "1"
        self.assertEqual(term.note("aside", tty), "aside")

    def test_a_terminal_colors_the_check_and_the_error(self):
        os.environ.pop("NO_COLOR", None)
        out, err = Tty(), Tty()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        home = tempfile.mkdtemp()
        try:
            with mock.patch.dict(os.environ, {"PHONE_HARNESS_HOME": home}, clear=False):
                os.environ.pop("PHONE_HARNESS_API_KEY", None)
                code = cloud.cli([])
                record = {"sid": "sid001", "label": None, "profile": None,
                          "link": {"kind": "adb", "host": "h", "port": 1, "code": "c"}}
                session = {"id": "sid001", "profile": None, "platform": "android",
                           "expires_at": time.time() + 900, "watch_url": None,
                           "startup": {"startup": "exact"}}
                with mock.patch.object(cloud, "_attach", return_value=record):
                    cloud._report(session, watch=False)
                removed = cloud.cli(["start", "--device"])
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        self.assertEqual(code, 1)
        self.assertEqual(removed, 1)
        text, errors = out.getvalue(), err.getvalue()
        self.assertIn("\033[31mNot signed in", text)
        self.assertIn("\033[0m", text)
        self.assertIn("\033[2m", text)
        self.assertIn("\033[32m✓\033[0m ready.", text)
        self.assertIn("\033[2m Resumed exactly where you left it.\033[0m", text)
        self.assertIn("\033[31m", errors)
        self.assertIn("--device was removed", errors)

        os.environ["NO_COLOR"] = "1"
        out, err = Tty(), Tty()
        sys.stdout, sys.stderr = out, err
        try:
            code = cloud.cli(["start", "--temp"])
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        self.assertEqual(code, 1)
        self.assertNotIn("\x1b", err.getvalue())
        self.assertIn("--temp was removed", err.getvalue())


class TopLevel(unittest.TestCase):
    def test_version_prints_the_installed_package_version(self):
        expected = f"phone-harness {installed_version()}\n"
        for flag in ("--version", "-V"):
            got = _run(flag)
            self.assertEqual(got.returncode, 0, got.stderr)
            self.assertEqual(got.stdout, expected)
            self.assertEqual(got.stderr, "")
            got.stdout.encode("ascii")
        for args in (["cloud", "--version"], ["android", "-V"], ["config", "--version"],
                     ["update", "-V"], ["skill", "--version"], ["doctor", "-V"]):
            got = _run(*args)
            self.assertEqual(got.returncode, 0, (args, got.stderr, got.stdout))
            self.assertEqual(got.stdout, expected, args)

    def test_unknown_command_and_flag_exit_2_and_name_themselves(self):
        command = _run("no-such-command")
        self.assertEqual(command.returncode, 2)
        self.assertIn("no-such-command", command.stderr)
        self.assertEqual(command.stdout, "")
        command.stderr.encode("ascii")

        flag = _run("--no-such-flag")
        self.assertEqual(flag.returncode, 2)
        self.assertIn("unrecognized arguments: --no-such-flag", flag.stderr)
        self.assertEqual(flag.stdout, "")
        flag.stderr.encode("ascii")

        nested = _run("doctor", "--no-such-flag")
        self.assertEqual(nested.returncode, 2)
        self.assertIn("--no-such-flag", nested.stderr)

    def test_every_top_level_command_has_help(self):
        cases = (
            (["doctor", "--help"], "ios|android"),
            (["doctor", "-h"], "ios or android"),
            (["skill", "--help"], "phone-harness skill"),
            (["skill", "-h"], "show this help message"),
            (["config", "--help"], "config get"),
            (["config", "-h"], "config set"),
            (["config", "get", "--help"], "KEY"),
            (["config", "set", "-h"], "VALUE"),
            (["config", "unset", "--help"], "KEY"),
            (["config", "path", "--help"], "config, state, and cache"),
            (["update", "--help"], "uv tool upgrade"),
            (["update", "-h"], "pip command"),
            (["cloud", "--help"], "cloud ls"),
            (["android", "--help"], "android pair"),
        )
        for args, needle in cases:
            got = _run(*args)
            self.assertEqual(got.returncode, 0, (args, got.stderr, got.stdout))
            self.assertIn(needle, got.stdout, args)
            self.assertNotIn("\x1b", got.stdout)
            got.stdout.encode("ascii")
        skill_help = _run("skill", "--help")
        self.assertNotIn("Which phone", skill_help.stdout)
        skill = _run("skill")
        self.assertEqual(skill.returncode, 0, skill.stderr)
        self.assertIn("Which phone", skill.stdout)
        options = _run("--help").stdout.split("options:", 1)[-1]
        self.assertNotIn("--doctor", options)

    def test_session_before_the_subcommand_still_pins_the_phone(self):
        home = Path("/tmp") / "ph-session-place"
        env = {"PHONE_HARNESS_HOME": str(home),
               "PH_AGENT_WORKSPACE": str(home / "ws"),
               "PHONE_HARNESS_PLATFORM": "android"}
        pinned = _run("--session", "sid-placed",
                      stdin="import os\nprint(os.environ['PHONE_HARNESS_SESSION'])\n",
                      env=env)
        self.assertEqual(pinned.returncode, 0, pinned.stderr)
        self.assertIn("sid-placed", pinned.stdout)

        middle = _run("cloud", "--session", "abc123", "ls", "--help")
        self.assertEqual(middle.returncode, 0, middle.stderr)
        self.assertIn("phone-harness cloud ls", middle.stdout)

        version = _run("--session", "abc123", "--version")
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertTrue(version.stdout.startswith("phone-harness "))

    def test_no_args_on_a_tty_prints_usage_to_stderr_and_exits_2(self):
        err = io.StringIO()
        with mock.patch("sys.stdin", Tty()), mock.patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as caught:
                run_main([])
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(err.getvalue(), USAGE)
        err.getvalue().encode("ascii")

    def test_doctor_alias_still_dispatches_and_help_does_not(self):
        with mock.patch("phone_harness.admin.run_doctor", return_value=0) as doctor:
            for argv, platform in ((["--doctor"], None),
                                   (["--doctor", "ios"], "ios"),
                                   (["doctor", "android"], "android"),
                                   (["doctor"], None)):
                with self.assertRaises(SystemExit) as caught:
                    run_main(argv)
                self.assertEqual(caught.exception.code, 0, argv)
                self.assertEqual(doctor.call_args.args[0], platform)
        help_out = _run("doctor", "--help")
        self.assertEqual(help_out.returncode, 0, help_out.stderr)
        self.assertNotIn("all clear", help_out.stdout)

    def test_config_set_output_is_unchanged(self):
        home = Path("/tmp") / "ph-config-cli"
        env = {"PHONE_HARNESS_HOME": str(home)}
        bad = _run("config", "set", "platform", "nope", env=env)
        self.assertEqual(bad.returncode, 2)
        self.assertIn("platform must be ios or android", bad.stdout)
        good = _run("config", "set", "platform", "android", env=env)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertIn('platform = "android"', good.stdout)
        got = _run("config", "get", "platform", env=env)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(got.stdout.strip(), '"android"')

    def test_docs_mention_version(self):
        root = Path(__file__).resolve().parents[1]
        # The README is a landing page; SKILL.md is where the agent learns the flag.
        skill = (root / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("phone-harness --version", skill)
        skill.encode("ascii")


class TelemetryDefault(unittest.TestCase):
    def test_cli_telemetry_stays_on_when_the_setting_was_never_set(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        from phone_harness.config import cli, get, lookup
        from phone_harness.telemetry import is_enabled

        with mock.patch.dict(os.environ, {"PHONE_HARNESS_HOME": home.name}, clear=False):
            os.environ.pop("PHONE_HARNESS_TELEMETRY", None)
            # GitHub Actions sets CI=true, and this repo's workflow sets
            # PHONE_HARNESS_TELEMETRY=0. This test is the ordinary default.
            os.environ.pop("CI", None)
            self.assertEqual(lookup("telemetry"), (True, "default"))
            self.assertTrue(get("telemetry"))
            self.assertTrue(is_enabled())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli([]), 0)
            line = next(item for item in out.getvalue().splitlines() if item.startswith("telemetry"))
            self.assertIn("true", line)
            self.assertIn("(default)", line)


class SessionAndStdinGuard(unittest.TestCase):
    """The top-level script path and --session live in run.py; argparse must not take them over."""

    def test_usage_text_stays_the_top_level_help(self):
        self.assertIn("<<'PY'", USAGE)
        self.assertNotIn("argparse", USAGE)


if __name__ == "__main__":
    unittest.main()


class DoctorSuggestsCloud(unittest.TestCase):
    def test_failed_local_doctor_points_at_cloud(self):
        import contextlib, io
        from unittest import mock
        from phone_harness import admin
        out = io.StringIO()
        with mock.patch.object(admin, "_doctor_android",
                               lambda: admin._check("adb found", False)), \
                contextlib.redirect_stdout(out):
            code = admin.run_doctor("android")
        self.assertEqual(code, 1)
        self.assertIn("phone-harness cloud login", out.getvalue())
        self.assertIn("setup/api.md", out.getvalue())

    def test_passing_doctor_does_not_mention_cloud(self):
        import contextlib, io
        from unittest import mock
        from phone_harness import admin
        out = io.StringIO()
        with mock.patch.object(admin, "_doctor_android",
                               lambda: admin._check("adb found", True)), \
                contextlib.redirect_stdout(out):
            self.assertEqual(admin.run_doctor("android"), 0)
        self.assertNotIn("cloud login", out.getvalue())


class DoctorNamesTheHostApp(unittest.TestCase):
    def test_outermost_app_is_named(self):
        from phone_harness import admin
        chain = {50: "40 /bin/zsh", 40: "30 /Applications/Helper.app/Contents/MacOS/Helper",
                 30: "1 /Applications/Agent.app/Contents/MacOS/Agent"}

        def fake(cmd, **kw):
            return mock.Mock(stdout=chain[int(cmd[-1])])
        with mock.patch.object(admin.os, "getppid", return_value=50), \
                mock.patch.object(admin.subprocess, "run", fake):
            self.assertEqual(admin._host_app(), '"Agent"')

    def test_no_app_falls_back(self):
        from phone_harness import admin
        with mock.patch.object(admin.os, "getppid", return_value=50), \
                mock.patch.object(admin.subprocess, "run",
                                  lambda cmd, **kw: mock.Mock(stdout="1 /bin/zsh")):
            self.assertIn("terminal or agent app", admin._host_app())
