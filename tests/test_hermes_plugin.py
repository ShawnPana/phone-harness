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
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

PLUGIN_INIT = Path(__file__).resolve().parents[1] / "integrations" / "hermes" / "__init__.py"
REPO_SKILL = Path(__file__).resolve().parents[1] / "SKILL.md"
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def load_plugin() -> Any:
    spec = importlib.util.spec_from_file_location("phone_harness_hermes_plugin", PLUGIN_INIT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeCtx:
    """The slice of Hermes's PluginContext the plugin uses."""

    def __init__(self):
        self.tools, self.hooks, self.skills = {}, {}, {}
        self.settings = {}

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = dict(toolset=toolset, schema=schema, handler=handler, **kw)

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_skill(self, name, path, description="", frontmatter=None):
        self.skills[name] = {"path": path, "description": description, "frontmatter": dict(frontmatter or {})}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)


class Base(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        self.plugin._data_dir = lambda: self.data
        self.plugin._workspace = lambda: self.data / "agent-workspace"
        # The installed module, here a Python that runs whatever script arrives on stdin.
        self.plugin._command = lambda: [sys.executable, "-c", "import sys; exec(sys.stdin.read())"]
        self.plugin._packaged_skill = lambda: None
        self.ctx = FakeCtx()
        self.plugin.register(self.ctx)
        # The suffix scrub, not a Hermes install that may or may not be on this machine.
        self._saved_modules = {name: sys.modules.get(name) for name in (
            "tools", "tools.environments", "tools.environments.local",
            "tools.vision_tools", "tools.vision_tools_history_budget",
        )}
        tools = types.ModuleType("tools")
        tools.__path__ = []
        environments = types.ModuleType("tools.environments")
        environments.__path__ = []
        local = types.ModuleType("tools.environments.local")
        tools.environments = environments
        environments.local = local
        sys.modules["tools"] = tools
        sys.modules["tools.environments"] = environments
        sys.modules["tools.environments.local"] = local
        sys.modules.pop("tools.vision_tools", None)
        sys.modules.pop("tools.vision_tools_history_budget", None)
        self.addCleanup(self._restore_modules)

    def _restore_modules(self):
        for name, module in self._saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def run_tool(self, code, **args):
        result = self.plugin.phone_exec({"code": code, **args})
        if isinstance(result, dict) and result.get("_multimodal"):
            result = result["text_summary"]
        return json.loads(result)

    def save_code(self, path: Path) -> str:
        return (
            "from pathlib import Path\n"
            f"p = Path({str(path)!r})\n"
            "p.write_bytes(b'img')\n"
            "print(p)\n"
        )

    def install_vision(self, *, native=True, resize=None, embed_bytes=256_000, max_dimension=1568):
        tools = sys.modules["tools"]
        vision = types.ModuleType("tools.vision_tools")
        budget = types.ModuleType("tools.vision_tools_history_budget")
        calls = []

        def default_resize(path, mime_type=None, max_base64_bytes=None, max_dimension=None, force_jpeg=False):
            calls.append({
                "path": Path(path),
                "mime_type": mime_type,
                "max_base64_bytes": max_base64_bytes,
                "max_dimension": max_dimension,
                "force_jpeg": force_jpeg,
            })
            return "data:image/jpeg;base64,qq"

        vision._EMBED_MAX_DIMENSION = max_dimension
        vision._should_use_native_vision_fast_path = lambda: native
        vision._resize_image_for_vision = resize or default_resize
        budget.resolve_embed_target_bytes = lambda: embed_bytes
        tools.vision_tools = vision
        tools.vision_tools_history_budget = budget
        sys.modules["tools.vision_tools"] = vision
        sys.modules["tools.vision_tools_history_budget"] = budget
        return calls


class Registration(Base):
    def test_one_tool_and_the_approval_hook(self):
        tool = self.ctx.tools["phone_exec"]
        self.assertEqual(tool["toolset"], "phone-harness")
        self.assertEqual(tool["schema"]["name"], "phone_exec")
        self.assertTrue(tool["check_fn"]())
        self.assertIn("pre_tool_call", self.ctx.hooks)

    def test_the_tool_description_points_at_the_skill(self):
        description = self.ctx.tools["phone_exec"]["schema"]["description"]
        self.assertIn("ensure_device()", description)
        self.assertIn("screen()", description)
        self.assertIn("screenshot()", description)
        self.assertIn("attached", description)
        self.assertIn("phone-harness:phone-harness", description)
        self.assertIn("passwords", description)
        self.assertNotIn("scroll_until", description)


class ApprovalGate(Base):
    PHONE_ONLY = [
        'ensure_device()\nprint([o["text"] for o in screen()][:10])',
        'from phone_harness.helpers import screen\nprint(screen())',
        'from phone_harness import helpers\nhelpers.tap_text("Weather")',
        'import phone_harness.helpers as helpers\nprint(helpers.screen_info())',
        'send("screen.text")',
        'send("input.tap", x=1, y=2)',
        'send("input.text", s="raw")',
        's = send\ns("screen.capture")',
        'def (',  # a SyntaxError cannot run, so the classifier has nothing to approve
    ]
    PAST_THE_PHONE = [
        "import os", "import json", "import subprocess", "import os.path", "from os import path",
        "from collections import Counter",
        "import Quartz", "import phone_harness", "import phone_harness.transport",
        "from phone_harness import transport", "from phone_harness.helpers import Path",
        "from phone_harness.helpers import importlib", "from . import agent_helpers",
        'open("notes.txt", "w").write("x")', '__import__("os")', 'eval("1")',
        'push_file("~/.ssh/id_rsa")', 'p = push_file\np("~/notes.txt")',
        'from phone_harness.helpers import push_file as p\np("~/notes.txt")',
        "print.__self__", 'getattr(print, "__self__")', 'shell("pm list packages")',
        'send("raw", cmd="id")', 'send(op="raw")', 'op = "raw"\nsend(op)',
        'f = open\nf("/etc/passwd")', 's = send\ns("raw", cmd="id")',
        'phone.send("raw", cmd="id")',
        'from phone_harness.helpers import shell as sh\nsh("id")',
        'a = open\nb = a\nb("x")',
        'import phone_harness.helpers as helpers\nprint(helpers.Path.home())',
    ]

    def setUp(self):
        super().setUp()
        self._saved_auto = os.environ.pop(self.plugin.CLASSIFIER_AUTO_RUN_ENV, None)
        self.addCleanup(self._restore_auto)

    def _restore_auto(self):
        if self._saved_auto is None:
            os.environ.pop(self.plugin.CLASSIFIER_AUTO_RUN_ENV, None)
        else:
            os.environ[self.plugin.CLASSIFIER_AUTO_RUN_ENV] = self._saved_auto

    def hook(self, code, tool="phone_exec"):
        return self.ctx.hooks["pre_tool_call"](tool_name=tool, args={"code": code}, task_id="t")

    def test_every_phone_exec_asks_by_default(self):
        for code in self.PHONE_ONLY + self.PAST_THE_PHONE:
            with self.subTest(code=code):
                directive = self.hook(code)
                self.assertEqual(directive["action"], "approve")
                self.assertEqual(directive["rule_key"], "phone_exec")
                self.assertIn("phone_exec", directive["message"])
                self.assertLessEqual(len(directive["message"]), 180)

    def test_the_default_message_describes_the_script(self):
        directive = self.hook('ensure_device()\nprint(screen())')
        self.assertIn("ensure_device()", directive["message"])
        self.assertIn("print(screen())", directive["message"])

    def test_phone_only_scripts_are_quiet_only_after_opt_in(self):
        os.environ[self.plugin.CLASSIFIER_AUTO_RUN_ENV] = "1"
        for code in self.PHONE_ONLY:
            with self.subTest(code=code):
                self.assertIsNone(self.plugin.approval_reason(code))
                self.assertIsNone(self.hook(code))

    def test_opt_in_still_asks_for_scripts_that_reach_past_the_phone(self):
        os.environ[self.plugin.CLASSIFIER_AUTO_RUN_ENV] = "1"
        for code in self.PAST_THE_PHONE:
            with self.subTest(code=code):
                self.assertIsNotNone(self.plugin.approval_reason(code), code)
                directive = self.hook(code)
                self.assertEqual(directive["action"], "approve")
                self.assertEqual(directive["rule_key"], "phone_exec")
                self.assertIn("phone_exec", directive["message"])
                self.assertIn("heuristic", directive["message"])

    def test_the_plugin_setting_opts_into_auto_run_when_the_env_var_is_unset(self):
        self.ctx.settings[self.plugin.CLASSIFIER_AUTO_RUN] = True
        self.assertIsNone(self.hook("print(screen())"))
        asked = self.hook('send("raw", cmd="id")')
        self.assertEqual(asked["rule_key"], "phone_exec")
        self.assertEqual(asked["action"], "approve")

    def test_an_explicit_env_var_off_beats_the_plugin_setting(self):
        self.ctx.settings[self.plugin.CLASSIFIER_AUTO_RUN] = True
        os.environ[self.plugin.CLASSIFIER_AUTO_RUN_ENV] = "0"
        directive = self.hook("print(screen())")
        self.assertEqual(directive["action"], "approve")
        self.assertEqual(directive["rule_key"], "phone_exec")

    def test_other_tools_are_left_alone(self):
        self.assertIsNone(self.hook("import os", tool="terminal"))


class Environment(Base):
    def test_inherited_env_passes_through_with_workspace_and_client(self):
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ["PYTHONHOME"] = "/somewhere"
        os.environ["PYTHONPATH"] = "/hermes"
        os.environ["PHONE_HARNESS_PLATFORM"] = "ios"
        os.environ["PHONE_HARNESS_TELEMETRY"] = "1"
        self.plugin._package_dir = lambda: None
        env = self.plugin._env(self.data / "ws")
        self.assertEqual(env["PH_AGENT_WORKSPACE"], str(self.data / "ws"))
        self.assertEqual(env["PH_CLIENT"], "hermes")
        self.assertEqual(env["PYTHONHOME"], "/somewhere")
        self.assertEqual(env["PYTHONPATH"], "/hermes")
        self.assertEqual(env["PHONE_HARNESS_PLATFORM"], "ios")
        self.assertEqual(env["PHONE_HARNESS_TELEMETRY"], "1")

    def test_the_script_finds_the_package_hermes_keeps_out_of_child_environments(self):
        # A standard Hermes runs on a bare Python, attaches its packages at boot and strips
        # them from PYTHONPATH, so a child of that Python sees none of them by itself.
        site = self.data / "site-packages"
        package = site / "phone_harness"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "run.py").write_text("print('ran from', __file__)\n")
        self.plugin._package_dir = lambda: package
        stripped = {**os.environ, "PYTHONPATH": "/your/libs"}
        sys.modules["tools.environments.local"].hermes_subprocess_env = lambda: dict(stripped)
        env = self.plugin._env(self.data / "ws")
        self.assertEqual(env["PYTHONPATH"], os.pathsep.join((str(site), "/your/libs")))
        ran = subprocess.run([sys.executable, "-m", "phone_harness.run"], env=env, cwd=self.data,
                             capture_output=True, text=True)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertIn(str(package / "run.py"), ran.stdout)

    def test_without_the_package_no_folder_is_added(self):
        without = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        sys.modules["tools.environments.local"].hermes_subprocess_env = lambda: dict(without)
        self.plugin._package_dir = lambda: None
        self.assertNotIn("PYTHONPATH", self.plugin._env(self.data / "ws"))

    def test_the_plugin_does_not_invent_a_platform(self):
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ.pop("PHONE_HARNESS_PLATFORM", None)
        os.environ.pop("PHONE_HARNESS_TELEMETRY", None)
        os.environ["PHONE_HARNESS_HOME"] = str(self.data)
        env = self.plugin._env(self.data / "ws")
        self.assertNotIn("PHONE_HARNESS_PLATFORM", env)
        self.assertEqual(env["PHONE_HARNESS_TELEMETRY"], "0")

    def test_secret_suffixes_are_removed_and_phone_harness_vars_stay(self):
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ["GH_TOKEN"] = "ghp-test"
        os.environ["DB_SECRET"] = "s3cret"
        os.environ["openai_api_key"] = "sk-lower"
        os.environ["PHONE_HARNESS_PLATFORM"] = "android"
        os.environ["PHONE_HARNESS_CUSTOM"] = "kept"
        os.environ["PHONE_HARNESS_API_KEY"] = "ph-key"
        os.environ["PATH"] = "/usr/bin:/bin"
        os.environ["HOME"] = "/home/me"
        os.environ["ANDROID_HOME"] = "/opt/android"
        os.environ["ANDROID_SERIAL"] = "emulator-5554"
        env = self.plugin._env(self.data / "ws")
        for gone in ("OPENAI_API_KEY", "GH_TOKEN", "DB_SECRET", "openai_api_key"):
            self.assertNotIn(gone, env)
        self.assertEqual(env["PHONE_HARNESS_PLATFORM"], "android")
        self.assertEqual(env["PHONE_HARNESS_CUSTOM"], "kept")
        self.assertEqual(env["PHONE_HARNESS_API_KEY"], "ph-key")
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertEqual(env["HOME"], "/home/me")
        self.assertEqual(env["ANDROID_HOME"], "/opt/android")
        self.assertEqual(env["ANDROID_SERIAL"], "emulator-5554")
        self.assertEqual(env["PH_CLIENT"], "hermes")
        self.assertEqual(env["PH_AGENT_WORKSPACE"], str(self.data / "ws"))

    def test_hermes_subprocess_env_is_used_when_it_imports(self):
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ["PHONE_HARNESS_PLATFORM"] = "ios"
        os.environ["PHONE_HARNESS_CUSTOM"] = "kept"
        os.environ["PHONE_HARNESS_API_KEY"] = "ph-key"
        os.environ.pop("PHONE_HARNESS_TELEMETRY", None)
        os.environ["PHONE_HARNESS_HOME"] = str(self.data)

        def hermes_subprocess_env():
            return {"PATH": "/from-hermes", "HOME": "/home/hermes", "ANDROID_SERIAL": "emulator-5554",
                    "LANG": "C"}

        sys.modules["tools.environments.local"].hermes_subprocess_env = hermes_subprocess_env
        env = self.plugin._env(self.data / "ws")
        self.assertEqual(env["PATH"], "/from-hermes")
        self.assertEqual(env["HOME"], "/home/hermes")
        self.assertEqual(env["ANDROID_SERIAL"], "emulator-5554")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(env["PHONE_HARNESS_PLATFORM"], "ios")
        self.assertEqual(env["PHONE_HARNESS_CUSTOM"], "kept")
        self.assertEqual(env["PHONE_HARNESS_API_KEY"], "ph-key")
        self.assertEqual(env["PH_CLIENT"], "hermes")
        self.assertEqual(env["PHONE_HARNESS_TELEMETRY"], "0")

    def test_a_bad_hermes_subprocess_env_fails_loudly(self):
        sys.modules["tools.environments.local"].hermes_subprocess_env = lambda: ["not", "a", "dict"]
        with self.assertRaises(TypeError):
            self.plugin._env(self.data / "ws")


class Workspace(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        self.plugin._data_dir = lambda: self.data
        self.saved = os.environ.get("PH_AGENT_WORKSPACE")
        os.environ.pop("PH_AGENT_WORKSPACE", None)
        self.addCleanup(self.restore)

    def restore(self):
        if self.saved is None:
            os.environ.pop("PH_AGENT_WORKSPACE", None)
        else:
            os.environ["PH_AGENT_WORKSPACE"] = self.saved

    def test_default_is_one_folder_in_the_plugin_data_dir(self):
        self.assertEqual(self.plugin._workspace(), self.data / "agent-workspace")

    def test_an_inherited_workspace_is_honored(self):
        os.environ["PH_AGENT_WORKSPACE"] = str(self.data / "custom")
        self.assertEqual(self.plugin._workspace(), self.data / "custom")

    def test_a_blank_inherited_workspace_is_ignored(self):
        os.environ["PH_AGENT_WORKSPACE"] = "   "
        self.assertEqual(self.plugin._workspace(), self.data / "agent-workspace")


class Command(unittest.TestCase):
    def test_runs_the_installed_module_on_hermes_interpreter(self):
        plugin = load_plugin()
        plugin._package_dir = lambda: Path("/opt/hermes/phone_harness")
        self.assertEqual(plugin._command(), [sys.executable, "-m", "phone_harness.run"])

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_a_linked_package_stays_beside_its_dependencies(self):
        plugin = load_plugin()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "store" / "phone_harness").mkdir(parents=True)
            (root / "site-packages").mkdir()
            (root / "site-packages" / "phone_harness").symlink_to(root / "store" / "phone_harness")
            origin = root / "site-packages" / "phone_harness" / "__init__.py"
            with mock.patch("importlib.util.find_spec", return_value=types.SimpleNamespace(origin=str(origin))):
                self.assertEqual(plugin._package_dir(), origin.parent)

    def test_a_missing_package_is_unavailable(self):
        plugin = load_plugin()
        plugin._package_dir = lambda: None
        self.assertIsNone(plugin._command())
        self.assertFalse(plugin._available())


class Skill(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_the_real_skill_frontmatter_has_a_description(self):
        frontmatter = self.plugin._parse_frontmatter(REPO_SKILL.read_text(encoding="utf-8"))
        self.assertEqual(frontmatter["name"], "phone-harness")
        self.assertTrue(frontmatter["description"].startswith("Control the user's phone"))

    def test_a_package_without_skill_md_registers_nothing(self):
        self.plugin._package_dir = lambda: self.root
        ctx = FakeCtx()
        self.plugin.register(ctx)
        self.assertEqual(ctx.skills, {})

    def test_the_packaged_file_is_registered_with_its_frontmatter(self):
        path = self.root / "SKILL.md"
        path.write_text(
            '---\nname: phone-harness\ndescription: "Drive the phone."\n---\n\n# phone-harness\n',
            encoding="utf-8",
        )
        self.plugin._package_dir = lambda: self.root
        self.plugin._command = lambda: [sys.executable, "-c", ""]
        ctx = FakeCtx()
        self.plugin.register(ctx)
        self.assertEqual(ctx.skills["phone-harness"]["path"], path)
        self.assertEqual(ctx.skills["phone-harness"]["description"], "Drive the phone.")
        self.assertEqual(ctx.skills["phone-harness"]["frontmatter"], {
            "name": "phone-harness",
            "description": "Drive the phone.",
        })

    def test_a_failing_skill_registration_is_logged(self):
        path = self.root / "SKILL.md"
        path.write_text(
            '---\nname: phone-harness\ndescription: "Drive the phone."\n---\n\n# phone-harness\n',
            encoding="utf-8",
        )
        self.plugin._package_dir = lambda: self.root
        self.plugin._command = lambda: [sys.executable, "-c", ""]

        class Boom(FakeCtx):
            def register_skill(self, name, path, description="", frontmatter=None):
                raise RuntimeError("skill store down")

        ctx = Boom()
        with self.assertLogs("phone_harness.hermes", level="WARNING") as caught:
            self.plugin.register(ctx)
        self.assertIn("phone_exec", ctx.tools)
        self.assertTrue(any("skill store down" in line for line in caught.output))


class WorkspaceSeed(unittest.TestCase):
    def test_an_empty_workspace_is_seeded_and_an_edit_is_kept(self):
        src = Path(__file__).resolve().parents[1] / "src"
        sys.path.insert(0, str(src))
        self.addCleanup(sys.path.remove, str(src))
        plugin = load_plugin()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = Path(tmp.name)
        plugin._data_dir = lambda: data
        plugin._workspace = lambda: data / "agent-workspace"
        plugin._command = lambda: [sys.executable, "-c", "import sys; exec(sys.stdin.read())"]
        plugin._packaged_skill = lambda: None

        first = json.loads(plugin.phone_exec({"code": "print('seeded')"}))
        self.assertTrue(first["success"], first)
        helper = data / "agent-workspace" / "agent_helpers.py"
        self.assertTrue(helper.is_file())
        helper.write_text("# keep\n")
        (data / "agent-workspace" / "notes.txt").write_text("mine\n")

        again = json.loads(plugin.phone_exec({"code": "print('again')"}))
        self.assertTrue(again["success"], again)
        self.assertEqual(helper.read_text(), "# keep\n")
        self.assertEqual((data / "agent-workspace" / "notes.txt").read_text(), "mine\n")


class TelemetryOptIn(Base):
    """Hermes runs are opt-in. The CLI default stays on when the setting was never stored."""

    def setUp(self):
        super().setUp()
        self.saved = dict(os.environ)
        os.environ["PHONE_HARNESS_HOME"] = str(self.data)
        for name in ("PHONE_HARNESS_TELEMETRY", "DO_NOT_TRACK", "PH_CLIENT",
                     "HERMES_SESSION_ID", "HERMES_REAL_HOME", "HERMES_AGENT"):
            os.environ.pop(name, None)
        # GitHub Actions sets CI=true, and this repo's workflow sets
        # PHONE_HARNESS_TELEMETRY=0. These checks are the ordinary opt-in rules,
        # including the child process.
        os.environ.pop("CI", None)
        self.addCleanup(self.restore_env)

    def restore_env(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def child_enabled(self) -> bool:
        env = self.plugin._env(self.data / "ws")
        env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-c",
             "from phone_harness.telemetry import is_enabled\n"
             "print('1' if is_enabled() else '0')"],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip() == "1"

    def test_unset_is_off_under_hermes_and_the_cli_default_stays_on(self):
        from phone_harness.config import get, lookup
        from phone_harness.telemetry import is_enabled

        self.assertEqual(lookup("telemetry"), (True, "default"))
        self.assertTrue(get("telemetry"))
        self.assertTrue(is_enabled())
        env = self.plugin._env(self.data / "ws")
        self.assertEqual(env["PHONE_HARNESS_TELEMETRY"], "0")
        self.assertNotIn("PHONE_HARNESS_TELEMETRY", os.environ)
        self.assertTrue(is_enabled())
        self.assertFalse(self.child_enabled())

    def test_an_explicit_stored_true_is_on(self):
        from phone_harness.config import set as set_config

        set_config("telemetry", "true")
        env = self.plugin._env(self.data / "ws")
        self.assertEqual(env["PHONE_HARNESS_TELEMETRY"], "1")
        self.assertTrue(self.child_enabled())

    def test_a_stored_true_reaches_a_child_with_another_home(self):
        from phone_harness.config import set as set_config

        set_config("telemetry", "true")
        env = self.plugin._env(self.data / "ws")
        env.pop("PHONE_HARNESS_HOME")
        env["HOME"] = str(self.data / "profile-home")
        env["PYTHONPATH"] = str(SRC)
        proc = subprocess.run(
            [sys.executable, "-c", "from phone_harness.telemetry import is_enabled; print(is_enabled())"],
            capture_output=True, text=True, env=env, check=True,
        )
        self.assertEqual(proc.stdout.strip(), "True")

    def test_do_not_track_reaches_the_child_and_beats_a_stored_true(self):
        from phone_harness.config import set as set_config

        set_config("telemetry", "true")
        os.environ["DO_NOT_TRACK"] = "1"
        self.assertEqual(self.plugin._env(self.data / "ws")["DO_NOT_TRACK"], "1")
        self.assertFalse(self.child_enabled())

    def test_do_not_track_survives_the_hermes_env_helper(self):
        sys.modules["tools.environments.local"].hermes_subprocess_env = lambda: {"PATH": "/from-hermes"}
        os.environ["DO_NOT_TRACK"] = "1"
        self.assertEqual(self.plugin._env(self.data / "ws")["DO_NOT_TRACK"], "1")

    def test_an_explicit_stored_false_is_off(self):
        from phone_harness.config import set as set_config
        from phone_harness.telemetry import is_enabled

        set_config("telemetry", "false")
        self.assertFalse(is_enabled())
        env = self.plugin._env(self.data / "ws")
        self.assertEqual(env["PHONE_HARNESS_TELEMETRY"], "0")
        self.assertFalse(self.child_enabled())

    def test_an_enabling_env_var_is_on(self):
        os.environ["PHONE_HARNESS_TELEMETRY"] = "true"
        env = self.plugin._env(self.data / "ws")
        self.assertEqual(env["PHONE_HARNESS_TELEMETRY"], "1")
        self.assertTrue(self.child_enabled())

    def test_a_disabling_env_var_is_off(self):
        os.environ["PHONE_HARNESS_TELEMETRY"] = "0"
        env = self.plugin._env(self.data / "ws")
        self.assertEqual(env["PHONE_HARNESS_TELEMETRY"], "0")
        self.assertFalse(self.child_enabled())

    def test_the_plugin_reads_the_same_config_file_as_the_cli(self):
        from phone_harness.config import paths

        self.assertEqual(self.plugin._config_json(), paths()["config"])
        os.environ.pop("PHONE_HARNESS_HOME", None)
        os.environ["XDG_CONFIG_HOME"] = str(self.data / "xdg")
        self.assertEqual(self.plugin._config_json(), paths()["config"])


class Running(Base):
    def test_prints_come_back_as_output(self):
        result = self.run_tool("print(6 * 7)")
        self.assertTrue(result["success"])
        self.assertEqual(result["output"], "42\n")
        self.assertEqual(result["workspace"], str(self.data / "agent-workspace"))

    def test_the_script_runs_in_the_workspace_with_the_plugin_environment(self):
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ["PHONE_HARNESS_PLATFORM"] = "ios"
        result = self.run_tool(
            "import os\nprint(os.getcwd())\nprint(os.environ['PH_CLIENT'])\n"
            "print(os.environ['PHONE_HARNESS_PLATFORM'])"
        )
        cwd, client, platform = result["output"].split()
        self.assertEqual(Path(cwd).resolve(), (self.data / "agent-workspace").resolve())
        self.assertEqual(client, "hermes")
        self.assertEqual(platform, "ios")

    def test_a_failing_script_reports_exit_code_and_stderr(self):
        result = self.run_tool('raise SystemExit("iPhone in Use")')
        self.assertFalse(result["success"])
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("iPhone in Use", result["stderr"])

    def test_a_screenshot_written_during_the_call_is_listed(self):
        shot = self.data / "shot.png"
        result = self.run_tool(self.save_code(shot))
        self.assertEqual(result["screenshots"], [str(shot)])
        self.assertTrue(shot.is_file())

    def test_an_image_path_on_stderr_is_not_attached(self):
        calls = self.install_vision()
        shot = self.data / "err.png"
        code = (
            "import sys\n"
            "from pathlib import Path\n"
            f"p = Path({str(shot)!r})\n"
            "p.write_bytes(b'img')\n"
            "sys.stderr.write(str(p) + '\\n')\n"
            "print('done')\n"
        )
        raw = self.plugin.phone_exec({"code": code})
        self.assertIsInstance(raw, str)
        self.assertNotIn("screenshots", json.loads(raw))
        self.assertEqual(calls, [])

    def test_an_older_image_path_is_not_this_call(self):
        shot = self.data / "old.png"
        shot.write_bytes(b"png")
        old = time.time() - 30
        os.utime(shot, (old, old))
        result = self.run_tool(f"print({str(shot)!r})")
        self.assertNotIn("screenshots", result)

    def test_only_an_absolute_image_from_this_call_counts(self):
        missing = "/no/such/phone-harness-shot.png"
        code = (
            "print('shot.png')\n"
            "print('./nested.jpg')\n"
            f"print({missing!r})\n"
            "print('https://cdn.example/a.png')\n"
            "print('/tmp/notes.png.bak')\n"
        )
        result = self.run_tool(code)
        self.assertNotIn("screenshots", result)
        found = self.plugin._IMAGE_PATH.findall(
            r"C:\shots\a.JPEG /tmp/b.webp shot.png ./c.jpg '/tmp/e.png' /tmp/a.png.bak"
        )
        self.assertIn(r"C:\shots\a.JPEG", found)
        self.assertIn("/tmp/b.webp", found)
        self.assertIn("/tmp/e.png", found)
        self.assertNotIn("shot.png", found)
        self.assertNotIn("./c.jpg", found)
        self.assertFalse(any(item.endswith("c.jpg") or item.endswith(".bak") for item in found))

    def test_the_last_fresh_image_is_the_one_kept(self):
        first = self.data / "first.png"
        second = self.data / "second.jpg"
        stale = self.data / "stale.webp"
        stale.write_bytes(b"old")
        os.utime(stale, (time.time() - 30, time.time() - 30))
        code = self.save_code(first) + self.save_code(second) + f"print({str(stale)!r})\n"
        result = self.run_tool(code)
        self.assertEqual(result["screenshots"], [str(second)])

    def test_a_screenshot_is_attached_when_the_model_can_view_images(self):
        cases = (
            ("live.png", "image/png"),
            ("live.jpg", "image/jpeg"),
            ("live.jpeg", "image/jpeg"),
            ("live.webp", "image/webp"),
            ("live.PNG", "image/png"),
        )
        for name, mime in cases:
            with self.subTest(name=name):
                calls = self.install_vision(embed_bytes=1234, max_dimension=1568)
                shot = self.data / name
                raw = self.plugin.phone_exec({"code": self.save_code(shot)})
                self.assertIsInstance(raw, dict)
                self.assertTrue(raw["_multimodal"])
                self.assertEqual(raw["meta"], {"screenshot_path": str(shot), "native_vision": True})
                self.assertEqual(raw["content"][0]["type"], "text")
                self.assertIn("inspect it with your native vision", raw["content"][0]["text"])
                self.assertEqual(raw["content"][1], {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64,qq"},
                })
                summary = json.loads(raw["text_summary"])
                self.assertEqual(summary["screenshots"], [str(shot)])
                self.assertTrue(summary["success"])
                self.assertEqual(calls, [{
                    "path": shot,
                    "mime_type": mime,
                    "max_base64_bytes": 1234,
                    "max_dimension": 1568,
                    "force_jpeg": True,
                }])

    def test_a_failed_script_still_attaches_the_screenshot_it_saved(self):
        calls = self.install_vision()
        shot = self.data / "then-fail.png"
        raw = self.plugin.phone_exec({"code": self.save_code(shot) + "raise SystemExit(2)\n"})
        self.assertIsInstance(raw, dict)
        summary = json.loads(raw["text_summary"])
        self.assertFalse(summary["success"])
        self.assertEqual(summary["exit_code"], 2)
        self.assertEqual(summary["screenshots"], [str(shot)])
        self.assertEqual(calls[0]["path"], shot)

    def test_attach_falls_back_to_text_when_the_model_cannot_view_images(self):
        calls = self.install_vision(native=False)
        shot = self.data / "text-only.png"
        raw = self.plugin.phone_exec({"code": self.save_code(shot)})
        self.assertIsInstance(raw, str)
        payload = json.loads(raw)
        self.assertEqual(payload["screenshots"], [str(shot)])
        self.assertNotIn("_multimodal", payload)
        self.assertEqual(calls, [])

    def test_attach_falls_back_to_text_when_resizing_fails(self):
        def boom(*args, **kwargs):
            raise RuntimeError("cannot read image")

        self.install_vision(resize=boom)
        shot = self.data / "broken.png"
        raw = self.plugin.phone_exec({"code": self.save_code(shot)})
        self.assertIsInstance(raw, str)
        payload = json.loads(raw)
        self.assertEqual(payload["screenshots"], [str(shot)])
        self.assertNotIn("data:image", raw)

    def test_attach_falls_back_to_text_when_the_image_data_is_unusable(self):
        self.install_vision(resize=lambda *args, **kwargs: "")
        shot = self.data / "empty.png"
        raw = self.plugin.phone_exec({"code": self.save_code(shot)})
        self.assertIsInstance(raw, str)
        self.assertEqual(json.loads(raw)["screenshots"], [str(shot)])

    def test_attach_falls_back_to_text_when_vision_support_is_missing(self):
        shot = self.data / "plain.png"
        raw = self.plugin.phone_exec({"code": self.save_code(shot)})
        self.assertIsInstance(raw, str)
        self.assertEqual(json.loads(raw)["screenshots"], [str(shot)])

    def test_a_file_modified_within_the_timestamp_slack_counts(self):
        shot = self.data / "edge.png"
        shot.write_bytes(b"x")
        # Whole-second filesystems can store the mtime a second early. A file
        # written just before the check stays inside that slack; one from
        # several seconds ago does not.
        self.assertEqual(self.plugin._find_screenshot(str(shot), time.time()), str(shot))
        old = time.time() - 10
        os.utime(shot, (old, old))
        self.assertIsNone(self.plugin._find_screenshot(str(shot), time.time()))

    def test_empty_code_and_a_missing_module_are_errors_not_crashes(self):
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

    def test_one_script_on_the_phone_at_a_time(self):
        self.plugin.LOCK_WAIT_S = 0.5
        self.assertTrue(self.plugin._THREAD_LOCK.acquire(timeout=1))
        try:
            busy = self.run_tool("print('should not run')")
        finally:
            self.plugin._THREAD_LOCK.release()
        self.assertFalse(busy["success"])
        self.assertIn("busy", busy["error"])
        self.assertTrue(self.run_tool("print('after')")["success"])   # the lock was released


class PluginManifest(unittest.TestCase):
    def test_the_package_pin_is_exact_and_the_plugin_version_stays(self):
        text = (PLUGIN_INIT.parent / "plugin.yaml").read_text(encoding="utf-8")
        self.assertIn("\nversion: 0.1.0\n", text)
        self.assertIn("phone-harness==0.3.3", text)
        self.assertNotIn("phone-harness>=", text)
        self.assertIn('requires_hermes: ">=0.21.6"', text)
        self.assertIn("classifier_auto_run:", text)
        self.assertIn("heuristic", text)
        self.assertIn("bypass", text)

    def test_the_readme_says_every_run_asks_and_describes_the_opt_in(self):
        text = (PLUGIN_INIT.parent / "README.md").read_text(encoding="utf-8")
        self.assertIn("Every `phone_exec` call asks", text)
        self.assertIn("classifier_auto_run", text)
        self.assertIn("PHONE_HARNESS_CLASSIFIER_AUTO_RUN", text)
        self.assertIn("heuristic", text)
        self.assertIn("bypassed", text)
        self.assertNotIn("never ask", text)
        self.assertIn("attached to the tool result", text)
        self.assertIn("screenshots", text)
        self.assertIn("`screenshot()` prints the path", text)
