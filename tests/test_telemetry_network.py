"""Opted out means nothing leaves the machine.

The real CLI, in a process tree watched by tests/support/network_watch: every network attempt
and every process start is logged, and anything off this machine is refused, so a stray send
fails a test here and never reaches PostHog. A fake PostHog takes what is allowed through.

    python -m unittest tests.test_telemetry_network
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from phone_harness.telemetry import POSTHOG_HOST  # noqa: E402

WATCH = str(ROOT / "tests" / "support" / "network_watch")
HERMES_PLUGIN = ROOT / "integrations" / "hermes" / "__init__.py"
OPENCLAW_PHONE_EXEC = ROOT / "integrations" / "openclaw" / "src" / "phone-exec.ts"

# One of each way a run can end. Each sends one cli_event when telemetry is on.
RUNS = (
    ([], "print('hello')\n"),
    ([], "raise RuntimeError('boom')\n"),
    ([], "import sys; sys.exit(3)\n"),
    (["--version"], None),
    (["config"], None),
    (["no-such-command"], None),
)
OFF_ENV = (
    {"PHONE_HARNESS_TELEMETRY": "0"},
    {"PHONE_HARNESS_TELEMETRY": "false"},
    {"PHONE_HARNESS_TELEMETRY": "no"},
    {"PHONE_HARNESS_TELEMETRY": "off"},
    {"PHONE_HARNESS_TELEMETRY": ""},
    {"PHONE_HARNESS_TELEMETRY": "junk"},
    {"DO_NOT_TRACK": "1"},
    {"DO_NOT_TRACK": "true"},
    {"DO_NOT_TRACK": "yes"},
    {"DO_NOT_TRACK": "1", "PHONE_HARNESS_TELEMETRY": "1"},
)
HERMES_MARKS = (
    {"HERMES_SESSION_ID": "s1"},
    {"HERMES_SESSION_ID": ""},
    {"HERMES_REAL_HOME": "/home/me"},
    {"HERMES_AGENT": "true"},                                     # all a Docker or SSH backend carries
    {"PH_CLIENT": "hermes"},
    {"HERMES_REAL_HOME": "/home/me", "CLAUDECODE": "1"},          # another agent's marker comes first
)


def node_major():
    node = shutil.which("node")
    if not node:
        return None
    version = subprocess.run([node, "--version"], capture_output=True, text=True, check=False).stdout
    return int(version.strip().lstrip("v").split(".")[0] or 0)


class FakePostHog(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.events.append(json.loads(body or b"{}"))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST                                  # where a feature flag call would land


class Watched(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakePostHog)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.fresh_machine()

    def fresh_machine(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.log = self.home / "network-watch.jsonl"
        self.server.events = []

    def env(self, **extra):
        return {"PATH": os.environ.get("PATH", ""), "HOME": str(self.home), "LANG": "C.UTF-8",
                "PYTHONPATH": WATCH + os.pathsep + SRC, "NETWORK_WATCH_LOG": str(self.log),
                "PH_POSTHOG_HOST": f"http://127.0.0.1:{self.server.server_port}",
                "PHONE_HARNESS_PLATFORM": "android", "PHONE_HARNESS_ADB": "/nonexistent/adb", **extra}

    def run_cli(self, *args, stdin=None, **extra):
        return subprocess.run([sys.executable, "-m", "phone_harness.run", *args], input=stdin,
                              capture_output=True, text=True, timeout=60, env=self.env(**extra))

    def run_each_way(self, **extra):
        for args, stdin in RUNS:
            self.run_cli(*args, stdin=stdin, **extra)

    def stored(self, body):
        path = self.home / ".config" / "phone-harness" / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    def choose(self, word):
        """What the user typed, with its own run left out of what a test watches."""
        result = self.run_cli("config", "set", "telemetry", word, PHONE_HARNESS_TELEMETRY="0",
                              NETWORK_WATCH_LOG=os.devnull)
        self.assertEqual(result.returncode, 0, result.stderr)

    def watched(self, *kinds):
        lines = self.log.read_text().splitlines() if self.log.exists() else []
        return [w for w in map(json.loads, lines) if w["kind"] in kinds]

    def sent(self, count):
        """The events that reached PostHog, once `count` have or the deadline passes."""
        deadline = time.monotonic() + 15
        while len(self.server.events) < count and time.monotonic() < deadline:
            time.sleep(0.05)
        return self.server.events

    def assert_nothing_left(self):
        """A sender is started before the CLI exits, so this needs no wait for a detached one."""
        self.assertTrue(self.watched("start"), "the watch did not load")
        self.assertEqual([w for w in self.watched("spawn") if w["sender"]], [])
        self.assertEqual(self.watched("connect"), [])
        self.assertEqual(self.server.events, [])
        self.assertFalse((self.home / ".local" / "state" / "phone-harness" / "telemetry.json").exists(),
                         "an install id was minted")


class Cli(Watched):
    def test_the_default_sends_which_is_what_gives_every_zero_here_its_meaning(self):
        self.run_each_way()
        events = self.sent(len(RUNS))
        self.assertEqual([e["event"] for e in events], ["cli_event"] * len(RUNS))
        self.assertEqual(sum(w["sender"] for w in self.watched("spawn")), len(RUNS))
        self.assertTrue(self.watched("connect"))

    def test_the_watch_sees_the_real_host_and_stops_it(self):
        env = self.env()
        env.pop("PH_POSTHOG_HOST")
        subprocess.run([sys.executable, "-m", "phone_harness.run", "--version"], env=env, capture_output=True)
        host = POSTHOG_HOST.split("://")[1]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not any(w["host"] == host for w in self.watched("connect")):
            time.sleep(0.05)
        self.assertIn(host, [w["host"] for w in self.watched("connect")])

    def test_each_opt_out_in_the_environment_sends_nothing(self):
        for off in OFF_ENV:
            with self.subTest(off):
                self.fresh_machine()
                self.run_each_way(**off)
                self.assert_nothing_left()

    def test_each_way_of_saying_no_to_config_set_sends_nothing(self):
        for word in ("false", "False", "off", "no", "0", "disabled"):
            with self.subTest(word):
                self.fresh_machine()
                self.choose(word)
                self.run_each_way()
                self.assert_nothing_left()

    def test_the_opt_out_command_sends_nothing_itself(self):
        self.assertEqual(self.run_cli("config", "set", "telemetry", "false").returncode, 0)
        self.assert_nothing_left()

    def test_a_no_already_stored_as_text_sends_nothing(self):
        for body in ('{"telemetry": "false"}', '{"telemetry": "off"}'):
            with self.subTest(body):
                self.fresh_machine()
                self.stored(body)
                self.run_each_way()
                self.assert_nothing_left()

    def test_a_config_file_that_cannot_be_read_sends_nothing(self):
        for body in ('{"telemetry": false,}', "[1]", "not json"):
            with self.subTest(body):
                self.fresh_machine()
                self.stored(body)
                self.run_each_way()
                self.assert_nothing_left()

    def test_do_not_track_beats_a_stored_yes(self):
        self.choose("true")
        self.run_each_way(DO_NOT_TRACK="1")
        self.assert_nothing_left()

    def test_do_not_track_in_the_workspace_env_file_counts(self):
        workspace = self.home / "workspace"
        workspace.mkdir()
        (workspace / ".env").write_text("DO_NOT_TRACK=1\n")
        self.run_each_way(PH_AGENT_WORKSPACE=str(workspace))
        self.assert_nothing_left()

    def test_a_do_not_track_of_zero_or_nothing_is_no_opt_out(self):
        for value in ("0", ""):
            with self.subTest(value):
                self.fresh_machine()
                self.run_cli("--version", DO_NOT_TRACK=value)
                self.assertEqual(len(self.sent(1)), 1)

    def test_a_child_a_script_starts_inherits_the_opt_out(self):
        script = ("import subprocess, sys\n"
                  "subprocess.run([sys.executable, '-m', 'phone_harness.run', '--version'], check=True)\n"
                  "subprocess.run([sys.executable, '-m', 'phone_harness.run', 'config'], check=True)\n"
                  "print('children done')\n")
        for off in ({"DO_NOT_TRACK": "1"}, {"PHONE_HARNESS_TELEMETRY": "0"}):
            with self.subTest(off):
                self.fresh_machine()
                result = self.run_cli(stdin=script, **off)
                self.assertIn("children done", result.stdout, result.stderr)
                self.assert_nothing_left()

    def test_importing_the_cli_starts_nothing(self):
        probe = ("import atexit, sys, threading\n"
                 "hooks = sys.excepthook, threading.excepthook\n"
                 "exits = atexit._ncallbacks()\n"
                 "import phone_harness.run, phone_harness.cloud, phone_harness.remote, phone_harness.helpers\n"
                 "assert atexit._ncallbacks() == exits, 'an atexit flush'\n"
                 "assert (sys.excepthook, threading.excepthook) == hooks, 'exception autocapture'\n"
                 "assert threading.active_count() == 1, 'a background thread'\n"
                 "assert not [m for m in sys.modules if 'posthog' in m.lower()], 'an analytics client'\n")
        result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=self.env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.watched("connect", "spawn"), [])


class Hermes(Watched):
    """Opt-in, however the CLI was reached."""

    def phone_exec(self, code, child_home=None, **extra):
        driver = ("import importlib.util, os, sys, types\n"
                  f"child_home = {child_home!r}\n"
                  "if child_home:\n"
                  "    local = types.ModuleType('tools.environments.local')\n"
                  "    local.hermes_subprocess_env = lambda: {**os.environ, 'HOME': child_home}\n"
                  "    sys.modules.update({'tools': types.ModuleType('tools'),\n"
                  "                        'tools.environments': types.ModuleType('tools.environments'),\n"
                  "                        'tools.environments.local': local})\n"
                  f"spec = importlib.util.spec_from_file_location('ph_hermes', {str(HERMES_PLUGIN)!r})\n"
                  "plugin = importlib.util.module_from_spec(spec)\n"
                  "sys.modules['ph_hermes'] = plugin\n"
                  "spec.loader.exec_module(plugin)\n"
                  f"print(plugin.phone_exec({{'code': {code!r}}}))\n")
        result = subprocess.run([sys.executable, "-c", driver], capture_output=True, text=True, timeout=60,
                                env=self.env(**extra))
        self.assertIn("ran", result.stdout, result.stderr)

    def test_a_shell_call_sends_nothing_until_the_user_opts_in(self):
        for marks in HERMES_MARKS:
            with self.subTest(marks):
                self.fresh_machine()
                self.run_each_way(**marks)
                self.assert_nothing_left()

    def test_a_shell_call_sends_once_the_user_opted_in(self):
        for marks in ({"HERMES_SESSION_ID": "s1"}, {"HERMES_REAL_HOME": "/home/me"}, {"HERMES_AGENT": "true"}):
            with self.subTest(marks):
                self.fresh_machine()
                self.choose("true")
                self.run_cli("--version", **marks)
                [event] = self.sent(1)
                self.assertEqual(event["properties"]["client"], "hermes")

    def test_a_stored_no_and_do_not_track_hold_after_marks_too(self):
        self.choose("false")
        self.run_each_way(HERMES_REAL_HOME="/home/me")
        self.fresh_machine()
        self.choose("true")
        self.run_each_way(HERMES_REAL_HOME="/home/me", DO_NOT_TRACK="1")
        self.assert_nothing_left()

    def test_phone_exec_sends_nothing_by_default(self):
        self.phone_exec("print('ran')\n")
        self.assert_nothing_left()

    def test_phone_exec_sends_after_an_opt_in(self):
        self.choose("true")
        self.phone_exec("print('ran')\n")
        [event] = self.sent(1)
        self.assertEqual(event["properties"]["client"], "hermes")
        self.assertEqual(event["properties"]["task"], "print('ran')\n")

    def test_phone_exec_keeps_the_opt_in_when_hermes_moves_the_childs_home(self):
        moved = self.home / "profile-home"
        moved.mkdir()
        self.choose("true")
        self.phone_exec("print('ran')\n", child_home=str(moved))
        self.assertEqual(len(self.sent(1)), 1)

    def test_do_not_track_beats_an_opt_in_in_phone_exec(self):
        self.choose("true")
        self.phone_exec("print('ran')\n", DO_NOT_TRACK="1")
        self.assert_nothing_left()

    def test_a_child_of_a_phone_exec_script_inherits_the_no(self):
        script = ("import subprocess, sys\n"
                  "subprocess.run([sys.executable, '-m', 'phone_harness.run', '--version'], check=True)\n"
                  "print('ran')\n")
        self.phone_exec(script)
        self.assert_nothing_left()


class OnByDefault(Watched):
    """The Claude and Codex skills, and OpenClaw, are opt-out like the plain CLI."""

    def test_the_skills_and_openclaw_send_by_default_and_honour_an_opt_out(self):
        for client, marks in (("claude-code", {"CLAUDECODE": "1"}), ("codex", {"CODEX_THREAD_ID": "t"}),
                              ("openclaw", {"PH_CLIENT": "openclaw"}), ("openclaw", {"OPENCLAW_CLI": "1"})):
            with self.subTest(client=client, marks=marks):
                self.fresh_machine()
                self.run_cli("--version", **marks)
                [event] = self.sent(1)
                self.assertEqual(event["properties"]["client"], client)
                self.fresh_machine()
                self.choose("false")
                self.run_each_way(**marks)
                self.fresh_machine()
                self.run_each_way(DO_NOT_TRACK="1", **marks)
                self.assert_nothing_left()

    @unittest.skipUnless((node_major() or 0) >= 24, "needs Node 24 or newer")
    def test_the_openclaw_plugin_sends_by_default_and_honours_an_opt_out(self):
        driver = (f"import {{ phoneExec }} from {json.dumps(str(OPENCLAW_PHONE_EXEC))};\n"
                  "const command = { file: process.env.PY, args: ['-m', 'phone_harness.run'] };\n"
                  "const result = await phoneExec({ code: \"print('ran')\\n\" },\n"
                  "  { env: { ...process.env }, workspace: process.env.HOME + '/ws', command });\n"
                  "console.log(JSON.stringify(result.content));\n")

        def phone_exec(**extra):
            result = subprocess.run(["node", "--no-warnings", "--input-type=module", "-e", driver],
                                    capture_output=True, text=True, timeout=60,
                                    env=self.env(PY=sys.executable, **extra))
            self.assertIn("ran", result.stdout, result.stderr)

        phone_exec()
        [event] = self.sent(1)
        self.assertEqual((event["properties"]["client"], event["properties"]["task"]), ("openclaw", "print('ran')\n"))
        self.fresh_machine()
        phone_exec(DO_NOT_TRACK="1")
        self.assert_nothing_left()
        self.fresh_machine()
        self.choose("false")
        phone_exec()
        self.assert_nothing_left()
        self.fresh_machine()
        self.choose("true")
        phone_exec(DO_NOT_TRACK="1")
        self.assert_nothing_left()


if __name__ == "__main__":
    unittest.main()
