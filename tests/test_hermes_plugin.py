"""The Hermes plugin in integrations/hermes, tested without Hermes or a phone.

It loads integrations/hermes/__init__.py by path, points its data folder at a temp
dir and swaps the phone-harness command for a plain Python that runs the script it
is given, so what is checked is the plumbing around a script: the approval gate,
the environment the script runs in, one script on the phone at a time, timeouts,
and what register() hands Hermes.

    python -m unittest discover tests
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

PLUGIN_INIT = Path(__file__).resolve().parents[1] / "integrations" / "hermes" / "__init__.py"


def load_plugin() -> Any:
    spec = importlib.util.spec_from_file_location("phone_harness_hermes_plugin", PLUGIN_INIT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeCtx:
    """The slice of Hermes's PluginContext the plugin uses."""

    def __init__(self, settings=None):
        self.settings = dict(settings or {})
        self.tools, self.hooks, self.skills = {}, {}, {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = dict(toolset=toolset, schema=schema, handler=handler, **kw)

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_skill(self, name, path):
        self.skills[name] = path


class Base(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        self.plugin._data_dir = lambda: self.data
        self.plugin._workspace = lambda: self.data / "agent-workspace"
        # "phone-harness" here is a Python that runs whatever script arrives on stdin.
        self.plugin._command = lambda: ([sys.executable, "-c", "import sys; exec(sys.stdin.read())"], None)
        self.plugin._skill_path = lambda: None
        self.ctx = FakeCtx({"platform": "ios"})
        self.plugin.register(self.ctx)

    def run_tool(self, code, **args):
        return json.loads(self.plugin.phone_exec({"code": code, **args}))


class Registration(Base):
    def test_one_tool_and_the_approval_hook(self):
        tool = self.ctx.tools["phone_exec"]
        self.assertEqual(tool["toolset"], "phone-harness")
        self.assertEqual(tool["schema"]["name"], "phone_exec")
        self.assertTrue(tool["check_fn"]())
        self.assertIn("pre_tool_call", self.ctx.hooks)

    def test_skill_is_registered_when_found(self):
        skill = self.data / "SKILL.md"
        skill.write_text("---\nname: phone-harness\n---\n")
        self.plugin._skill_path = lambda: skill
        ctx = FakeCtx()
        self.plugin.register(ctx)
        self.assertEqual(ctx.skills, {"phone-harness": skill})


class ApprovalGate(Base):
    PHONE_ONLY = [
        'ensure_device()\nprint([o["text"] for o in ocr()][:10])',
        'import json, re, time\nprint(json.dumps(screen_info()))',
        'from phone_harness import helpers\nhelpers.tap_text("Weather")',
        'from collections import Counter\nprint(Counter(o["text"] for o in ocr()))',
        'def (',  # a SyntaxError cannot run, so there is nothing to approve
    ]
    PAST_THE_PHONE = [
        "import os", "import subprocess", "import os.path", "from os import path", "import Quartz",
        "from . import agent_helpers", 'open("notes.txt", "w").write("x")', '__import__("os")',
        'eval("1")', "print.__self__", 'getattr(print, "__self__")', 'shell("pm list packages")',
    ]

    def hook(self, code, tool="phone_exec"):
        return self.ctx.hooks["pre_tool_call"](tool_name=tool, args={"code": code}, task_id="t")

    def test_phone_scripts_run_without_asking(self):
        for code in self.PHONE_ONLY:
            with self.subTest(code=code):
                self.assertIsNone(self.plugin.approval_reason(code))
                self.assertIsNone(self.hook(code))

    def test_scripts_that_reach_past_the_phone_ask_first(self):
        for code in self.PAST_THE_PHONE:
            with self.subTest(code=code):
                self.assertIsNotNone(self.plugin.approval_reason(code))
                directive = self.hook(code)
                self.assertEqual(directive["action"], "approve")
                self.assertIn("phone_exec", directive["message"])

    def test_other_tools_are_left_alone(self):
        self.assertIsNone(self.hook("import os", tool="terminal"))


class Environment(Base):
    def env(self, settings, inherited=None):
        self.ctx.settings = settings
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        for key in ("PHONE_HARNESS_PLATFORM", "PHONE_HARNESS_TELEMETRY", "PYTHONPATH"):
            os.environ.pop(key, None)
        os.environ.update(inherited or {})
        return self.plugin._env(None, self.data / "ws")

    def test_platform_setting_and_telemetry_off_by_default(self):
        env = self.env({"platform": "ios"}, {"PYTHONHOME": "/somewhere", "PYTHONPATH": "/hermes"})
        self.assertEqual(env["PHONE_HARNESS_PLATFORM"], "ios")
        self.assertEqual(env["PHONE_HARNESS_TELEMETRY"], "0")
        self.assertEqual(env["PH_AGENT_WORKSPACE"], str(self.data / "ws"))
        self.assertEqual(env["PH_CLIENT"], "hermes")
        self.assertNotIn("PYTHONHOME", env)
        self.assertNotIn("PYTHONPATH", env)

    def test_auto_platform_and_opted_in_telemetry_leave_phone_harness_in_charge(self):
        env = self.env({"platform": "auto", "telemetry": True})
        self.assertNotIn("PHONE_HARNESS_PLATFORM", env)
        self.assertNotIn("PHONE_HARNESS_TELEMETRY", env)

    def test_bundled_package_gets_its_own_site_dir(self):
        self.ctx.settings = {}
        self.assertEqual(self.plugin._env("/site", self.data)["PYTHONPATH"], "/site")


class Running(Base):
    def test_prints_come_back_as_output(self):
        result = self.run_tool("print(6 * 7)")
        self.assertTrue(result["success"])
        self.assertEqual(result["output"], "42\n")
        self.assertEqual(result["workspace"], str(self.data / "agent-workspace"))

    def test_the_script_runs_in_the_workspace_with_the_plugin_environment(self):
        result = self.run_tool("import os\nprint(os.getcwd())\nprint(os.environ['PHONE_HARNESS_PLATFORM'])")
        cwd, platform = result["output"].split()
        self.assertEqual(Path(cwd).resolve(), (self.data / "agent-workspace").resolve())
        self.assertEqual(platform, "ios")

    def test_a_failing_script_reports_exit_code_and_stderr(self):
        result = self.run_tool('raise SystemExit("iPhone in Use")')
        self.assertFalse(result["success"])
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("iPhone in Use", result["stderr"])

    def test_screenshot_paths_in_the_output_are_listed(self):
        shot = self.data / "shot.png"
        shot.write_bytes(b"\x89PNG")
        result = self.run_tool(f"print('saved {shot}')")
        self.assertEqual(result["screenshots"], [str(shot)])

    def test_empty_code_and_a_missing_cli_are_errors_not_crashes(self):
        self.assertFalse(self.run_tool("   ")["success"])
        self.plugin._command = lambda: None
        self.assertIn("not installed", self.run_tool("print(1)")["error"])

    def test_a_script_past_its_timeout_is_killed(self):
        self.plugin.MIN_TIMEOUT_S = 1
        started = time.monotonic()
        result = self.run_tool("import time\ntime.sleep(30)", timeout_s=1)
        self.assertFalse(result["success"])
        self.assertIn("timed out after 1s", result["error"])
        self.assertLess(time.monotonic() - started, 15)

    @unittest.skipIf(os.name == "nt", "flock is POSIX; Windows takes the msvcrt branch")
    def test_one_script_on_the_phone_at_a_time(self):
        import fcntl
        self.plugin.LOCK_WAIT_S = 0.5
        with open(self.data / "phone.lock", "a+b") as held:   # another Hermes process driving the phone
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            busy = self.run_tool("print('should not run')")
        self.assertFalse(busy["success"])
        self.assertIn("busy", busy["error"])
        self.assertTrue(self.run_tool("print('after')")["success"])   # the lock was released


if __name__ == "__main__":
    unittest.main()
