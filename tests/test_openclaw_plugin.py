"""The OpenClaw plugin in integrations/openclaw, checked without OpenClaw or a phone.

The plugin's behaviour is tested in TypeScript (src/*.test.ts, `npm test`). This file checks what
those tests cannot see from inside the plugin: the published metadata, the one-line pin, that the
classifier stays identical to the Hermes plugin's, and that the guide the skill is built from
still fits. It also runs `npm test` when this machine can.

    python -m unittest discover tests
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "integrations" / "openclaw"
HERMES = ROOT / "integrations" / "hermes" / "__init__.py"
PHONE_EXEC = PLUGIN / "src" / "phone-exec.ts"


def manifest():
    return json.loads((PLUGIN / "openclaw.plugin.json").read_text(encoding="utf-8"))


def package():
    return json.loads((PLUGIN / "package.json").read_text(encoding="utf-8"))


def node_major():
    node = shutil.which("node")
    if node is None:
        return None
    version = subprocess.run([node, "--version"], capture_output=True, text=True, check=False).stdout
    match = re.match(r"v(\d+)\.", version)
    return int(match.group(1)) if match else None


class Manifest(unittest.TestCase):
    def test_declares_the_one_tool_the_entry_registers(self):
        self.assertEqual(manifest()["contracts"]["tools"], ["phone_exec"])
        self.assertIn('TOOL_NAME = "phone_exec"', (PLUGIN / "src" / "approval.ts").read_text(encoding="utf-8"))

    def test_the_id_matches_the_entry(self):
        self.assertEqual(manifest()["id"], "phone-harness")
        self.assertIn('id: "phone-harness"', (PLUGIN / "src" / "index.ts").read_text(encoding="utf-8"))

    def test_clawhub_needs_a_name_and_takes_exactly_one_category(self):
        self.assertTrue(manifest()["name"])
        self.assertEqual(manifest()["categories"], ["computer-use"])

    def test_loads_at_startup_so_the_gate_is_live_with_the_tool(self):
        self.assertIs(manifest()["activation"]["onStartup"], True)

    def test_leaves_the_messaging_profile_out_and_marks_side_effects(self):
        metadata = manifest()["toolMetadata"]["phone_exec"]
        self.assertEqual(metadata["profiles"], ["coding"])
        self.assertIs(metadata["sideEffecting"], True)

    def test_the_tool_is_not_optional(self):
        self.assertNotIn("optional", manifest()["toolMetadata"]["phone_exec"])

    def test_doctor_flags_the_classifier_opt_in_and_it_defaults_off(self):
        schema = manifest()["configSchema"]
        self.assertIs(schema["additionalProperties"], False)
        self.assertIs(schema["properties"]["classifierAutoRun"]["default"], False)
        self.assertEqual(manifest()["configContracts"]["dangerousFlags"],
                         [{"path": "classifierAutoRun", "equals": True}])

    def test_carries_no_version_that_could_drift_from_the_package(self):
        self.assertNotIn("version", manifest())


class Package(unittest.TestCase):
    def test_points_at_built_javascript(self):
        self.assertEqual(package()["openclaw"]["extensions"], ["./dist/index.js"])

    def test_clawhub_compatibility_fields_agree(self):
        block = package()["openclaw"]
        built = block["build"]["openclawVersion"]
        self.assertEqual(block["build"]["pluginSdkVersion"], built)
        self.assertEqual(block["compat"]["pluginApi"], f">={built}")
        self.assertEqual(block["compat"]["minGatewayVersion"], built)
        self.assertEqual(block["install"]["minHostVersion"], f">={built}")
        self.assertEqual(package()["peerDependencies"]["openclaw"], f">={built}")
        self.assertEqual(package()["devDependencies"]["openclaw"], built)

    def test_is_publishable(self):
        self.assertNotIn("private", package())

    def test_ships_everything_the_manifest_and_runtime_read(self):
        files = package()["files"]
        for path in ("assets", "classifier.py", "dist", "openclaw.plugin.json", "README.md", "skills"):
            self.assertIn(path, files)

    def test_runtime_imports_are_real_dependencies(self):
        self.assertIn("typebox", package()["dependencies"])

    def test_the_artwork_exists_within_the_documented_limits(self):
        icon = PLUGIN / "assets" / "icon.png"
        self.assertLessEqual(icon.stat().st_size, 512 * 1024)
        self.assertEqual(icon.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertLessEqual((PLUGIN / "assets" / "activity.svg").stat().st_size, 32 * 1024)


class Pin(unittest.TestCase):
    def test_one_exact_release_on_one_line(self):
        found = re.findall(r'^export const PHONE_HARNESS_VERSION = "(\d+\.\d+\.\d+)";$',
                           PHONE_EXEC.read_text(encoding="utf-8"), re.MULTILINE)
        self.assertEqual(len(found), 1)

    def test_nothing_else_repeats_the_release(self):
        version = re.search(r'PHONE_HARNESS_VERSION = "([^"]+)"', PHONE_EXEC.read_text(encoding="utf-8")).group(1)
        pattern = re.compile(rf"(?<![\d.]){re.escape(version)}(?![\d.])")
        for path in [PLUGIN / "README.md", PLUGIN / "src" / "index.ts", PLUGIN / "src" / "approval.ts",
                     PLUGIN / "scripts" / "build-skill.mjs", PLUGIN / "package.json",
                     PLUGIN / "openclaw.plugin.json"]:
            self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")), path.name)


class Classifier(unittest.TestCase):
    """classifier.py is a copy of the Hermes logic. Hermes stays pinned to its own release, so the
    two cannot share a module yet; this keeps a fix to one from missing the other."""

    @staticmethod
    def logic(source):
        start = source.index("# The only import the classifier treats as phone-only.")
        end = source.index("def _script_summary") if "def _script_summary" in source else source.index('if __name__')
        lines = source[start:end].splitlines()
        hermes_only = ("_FM_END = ", "_SUMMARY_LIMIT = ", "_plugin_ctx = None")
        return "\n".join(line for line in lines if not line.startswith(hermes_only)).strip()

    def test_matches_the_hermes_plugin_exactly(self):
        hermes = self.logic(HERMES.read_text(encoding="utf-8"))
        ours = self.logic((PLUGIN / "classifier.py").read_text(encoding="utf-8"))
        self.assertEqual(re.sub(r"\n{2,}", "\n", ours), re.sub(r"\n{2,}", "\n", hermes))

    def reason(self, code):
        result = subprocess.run([sys.executable, "-I", str(PLUGIN / "classifier.py")],
                                input=code, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)["reason"]

    def test_passes_a_script_that_only_drives_the_phone(self):
        self.assertIsNone(self.reason("ensure_device()\nprint(screen())\ntap_text('Wi-Fi')"))
        self.assertIsNone(self.reason("from phone_harness.helpers import screen\nprint(screen())"))

    def test_stops_a_script_that_reaches_past_the_phone(self):
        for code, reason in [
            ("import os", "imports os"),
            ("open('/etc/passwd')", "calls open()"),
            ("f = open\nf('/etc/passwd')", "calls f(), an alias of a risky call"),
            ("shell('id')", "calls shell()"),
            ('send("raw", "x")', 'calls send("raw", ...)'),
            ("().__class__", "uses __class__"),
            ("from phone_harness.helpers import Path", "imports Path from phone_harness.helpers"),
        ]:
            self.assertEqual(self.reason(code), reason)

    def test_leaves_a_syntax_error_to_the_script_itself(self):
        self.assertIsNone(self.reason("def broken(:"))


class SkillGuide(unittest.TestCase):
    def test_the_guide_has_the_frontmatter_the_build_replaces(self):
        self.assertTrue((ROOT / "SKILL.md").read_text(encoding="utf-8").startswith("---\n"))

    @unittest.skipIf(shutil.which("node") is None, "node is not installed")
    def test_the_built_skill_is_the_guide_under_openclaw_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "integrations" / "openclaw"
            (target / "scripts").mkdir(parents=True)
            (target / "src").mkdir()
            shutil.copy(PLUGIN / "scripts" / "build-skill.mjs", target / "scripts")
            shutil.copy(PHONE_EXEC, target / "src")
            shutil.copy(ROOT / "SKILL.md", root / "SKILL.md")
            subprocess.run([shutil.which("node"), str(target / "scripts" / "build-skill.mjs")], check=True)
            built = (target / "skills" / "phone-harness" / "SKILL.md").read_text(encoding="utf-8")
        frontmatter = built.split("---\n")[1]
        self.assertIn("name: phone-harness\n", frontmatter)
        self.assertIn('"bins": ["uv"]', frontmatter)
        description = re.search(r'^description: "(.*)"$', frontmatter, re.MULTILINE).group(1)
        self.assertLess(len(description), 160)
        guide = (ROOT / "SKILL.md").read_text(encoding="utf-8").split("---\n", 2)[2].lstrip("\n")
        self.assertTrue(built.endswith(guide))
        preface = built.split(guide)[0]
        self.assertIn("phone_exec", preface)
        # ClawHub reviews what a listing runs: sign-in stays on the pinned release, and the one
        # credential a script can read is declared.
        version = re.search(r'PHONE_HARNESS_VERSION = "([^"]+)"', PHONE_EXEC.read_text(encoding="utf-8")).group(1)
        self.assertIn(f"uvx --from phone-harness=={version} phone-harness cloud login", preface)
        self.assertIn('"name": "PHONE_HARNESS_API_KEY"', frontmatter)
        self.assertIn('"required": false', frontmatter)


class Readme(unittest.TestCase):
    def test_says_every_run_asks_and_what_always_allow_means(self):
        text = (PLUGIN / "README.md").read_text(encoding="utf-8")
        self.assertIn("Every `phone_exec` call asks you before it runs", text)
        self.assertIn("It ends with the session", text)
        self.assertIn("If nobody can answer", text)

    def test_says_the_classifier_can_be_bypassed(self):
        text = (PLUGIN / "README.md").read_text(encoding="utf-8")
        self.assertIn("heuristic", text)
        self.assertIn("bypassed", text)

    def test_says_telemetry_is_opt_in(self):
        self.assertIn("phone-harness config set telemetry true", (PLUGIN / "README.md").read_text(encoding="utf-8"))


class NodeSuite(unittest.TestCase):
    @unittest.skipUnless((node_major() or 0) >= 24, "needs Node 24 or newer")
    @unittest.skipUnless((PLUGIN / "node_modules" / "typebox").is_dir(), "run npm install in integrations/openclaw")
    def test_the_plugin_tests_pass(self):
        result = subprocess.run(
            [shutil.which("node"), "--test", "src/approval.test.ts", "src/index.test.ts", "src/phone-exec.test.ts"],
            cwd=PLUGIN, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout[-4000:])


if __name__ == "__main__":
    unittest.main()
