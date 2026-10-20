"""Agent workspace seeding: first run copies defaults, an existing one is left alone.

    python -m unittest discover tests
"""
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DEFAULT_HELPERS = ROOT / "agent-workspace" / "agent_helpers.py"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from phone_harness.workspace import (  # noqa: E402
    ensure_agent_workspace,
    resolved_agent_workspace,
    seed_missing_files,
)


def _inside(child, parent):
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
    except ValueError:
        return False
    return True


def _run(code, workspace, home):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    env["PH_AGENT_WORKSPACE"] = str(workspace)
    env["PHONE_HARNESS_HOME"] = str(home)
    env["PHONE_HARNESS_PLATFORM"] = "android"
    env["PHONE_HARNESS_TELEMETRY"] = "0"
    env.pop("PHONE_HARNESS_SESSION", None)
    return subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, env=env)


class AgentWorkspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.ws = Path(self.tmp.name) / "workspace"

    def test_a_missing_workspace_is_seeded_from_the_defaults(self):
        r = _run("import phone_harness.config", self.ws, self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        seeded = self.ws / "agent_helpers.py"
        self.assertTrue(seeded.is_file(), r.stderr)
        self.assertEqual(seeded.read_bytes(), DEFAULT_HELPERS.read_bytes())

        r = _run(
            "import phone_harness.helpers as h\n"
            "assert callable(h.tap_icon)\n",
            self.ws, self.home)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_an_existing_workspace_is_left_untouched(self):
        self.ws.mkdir()
        custom = self.ws / "agent_helpers.py"
        custom.write_text("# agent edited this\nVALUE = 'custom'\n")
        extra = self.ws / "my_helpers.py"
        extra.write_text("mine = True\n")
        before = {p.name: p.read_bytes() for p in self.ws.iterdir()}

        r = _run("import phone_harness.config", self.ws, self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        after = {p.name: p.read_bytes() for p in self.ws.iterdir()}
        self.assertEqual(after, before)
        self.assertNotIn(b"def tap_icon", custom.read_bytes())

    def test_seed_missing_files_fills_gaps_and_keeps_existing_files(self):
        missing = Path(self.tmp.name) / "fresh"
        self.assertEqual(seed_missing_files(missing), missing)
        self.assertEqual((missing / "agent_helpers.py").read_bytes(),
                         DEFAULT_HELPERS.read_bytes())

        self.ws.mkdir()
        self.assertTrue((seed_missing_files(self.ws) / "agent_helpers.py").is_file())
        custom = self.ws / "agent_helpers.py"
        custom.write_text("# agent edited this\nVALUE = 'custom'\n")
        (self.ws / "notes.txt").write_text("keep\n")
        seed_missing_files(self.ws)
        self.assertEqual(custom.read_text(), "# agent edited this\nVALUE = 'custom'\n")
        self.assertEqual((self.ws / "notes.txt").read_text(), "keep\n")

    def test_a_partial_workspace_is_not_filled_in(self):
        self.ws.mkdir()
        (self.ws / "my_helpers.py").write_text("mine = True\n")
        self.assertEqual(ensure_agent_workspace(self.ws), self.ws)
        self.assertFalse((self.ws / "agent_helpers.py").exists())
        self.assertEqual((self.ws / "my_helpers.py").read_text(), "mine = True\n")

    def test_a_checkout_workspace_is_not_rewritten(self):
        before = DEFAULT_HELPERS.read_bytes()
        names = sorted(p.name for p in DEFAULT_HELPERS.parent.iterdir())
        env = os.environ.copy()
        env.pop("PH_AGENT_WORKSPACE", None)
        env["PYTHONPATH"] = str(SRC)
        env["PHONE_HARNESS_HOME"] = str(self.home)
        env["PHONE_HARNESS_PLATFORM"] = "android"
        env["PHONE_HARNESS_TELEMETRY"] = "0"
        r = subprocess.run(
            [sys.executable, "-c", "import phone_harness.config"],
            capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(DEFAULT_HELPERS.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in DEFAULT_HELPERS.parent.iterdir()), names)

    def test_a_git_checkout_uses_its_own_agent_workspace(self):
        self.assertTrue((ROOT / ".git").exists())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PH_AGENT_WORKSPACE", None)
            dest = resolved_agent_workspace()
        self.assertEqual(dest, ROOT / "agent-workspace")

    def test_a_prefix_without_git_resolves_outside_it(self):
        prefix = Path(self.tmp.name) / "tool" / "lib" / "python3.12"
        prefix.mkdir(parents=True)
        self.home.mkdir()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PH_AGENT_WORKSPACE", None)
            os.environ.pop("XDG_DATA_HOME", None)
            with mock.patch("phone_harness.workspace.repo_root", return_value=prefix), \
                 mock.patch("phone_harness.workspace.Path.home", return_value=self.home):
                dest = resolved_agent_workspace()
        self.assertEqual(dest, self.home / ".phone-harness" / "agent-workspace")
        self.assertFalse(_inside(dest, prefix))

        (self.home / ".phone-harness" / ".git").mkdir(parents=True)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PH_AGENT_WORKSPACE", None)
            os.environ.pop("XDG_DATA_HOME", None)
            with mock.patch("phone_harness.workspace.repo_root", return_value=prefix), \
                 mock.patch("phone_harness.workspace.Path.home", return_value=self.home):
                dest = resolved_agent_workspace()
        data = self.home / ".local" / "share" / "phone-harness" / "agent-workspace"
        self.assertEqual(dest, data)
        self.assertFalse(_inside(dest, prefix))
        self.assertFalse(_inside(dest, self.home / ".phone-harness"))

        xdg = Path(self.tmp.name) / "xdg"
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(xdg)}, clear=False):
            os.environ.pop("PH_AGENT_WORKSPACE", None)
            with mock.patch("phone_harness.workspace.repo_root", return_value=prefix), \
                 mock.patch("phone_harness.workspace.Path.home", return_value=self.home):
                dest = resolved_agent_workspace()
        self.assertEqual(dest, xdg / "phone-harness" / "agent-workspace")
        self.assertFalse(_inside(dest, prefix))

    def test_seed_skips_a_dotenv_beside_the_defaults(self):
        source = Path(self.tmp.name) / "defaults"
        source.mkdir()
        (source / "agent_helpers.py").write_text("default helpers\n")
        (source / ".env").write_text("SECRET=1\n")
        with mock.patch("phone_harness.workspace.packaged_defaults", return_value=source):
            ensure_agent_workspace(self.ws)
        self.assertEqual((self.ws / "agent_helpers.py").read_text(), "default helpers\n")
        self.assertFalse((self.ws / ".env").exists())


def _build_wheel(directory):
    """Build a wheel of this tree. Prefer the installed hatchling; CI may not
    have it, and then pip's isolated build downloads the backend."""
    cmd = [sys.executable, "-m", "pip", "wheel", "--no-deps",
           "--wheel-dir", str(directory), str(ROOT)]
    r = subprocess.run([*cmd, "--no-build-isolation"], capture_output=True, text=True)
    if r.returncode != 0:
        r = subprocess.run(cmd, capture_output=True, text=True)
    return r


class PackagedWorkspace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # uv venvs often ship without pip. `python -m pip` then errors with
        # "No module named pip" and fails the class; skip instead.
        probe = subprocess.run(
            [sys.executable, "-c", "import pip"],
            capture_output=True)
        if probe.returncode != 0:
            raise unittest.SkipTest(
                f"pip is not importable in sys.executable ({sys.executable})")
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls._tmp.name)
        # Build with a secret-shaped .env beside the defaults, the way a
        # developer's checkout looks. The wheel must not pack it.
        cls._stray_env = ROOT / "agent-workspace" / ".env"
        cls._wrote_env = not cls._stray_env.exists()
        if cls._wrote_env:
            cls._stray_env.write_text("PHONE_HARNESS_TOKEN=do-not-ship\n")
        r = _build_wheel(cls.out)
        if r.returncode != 0:
            cls._tmp.cleanup()
            raise AssertionError(r.stderr or r.stdout)
        wheels = list(cls.out.glob("*.whl"))
        if len(wheels) != 1:
            cls._tmp.cleanup()
            raise AssertionError(f"expected one wheel, got {wheels}")
        cls.wheel = wheels[0]

    @classmethod
    def tearDownClass(cls):
        if cls._wrote_env:
            cls._stray_env.unlink(missing_ok=True)
        cls._tmp.cleanup()

    def test_the_wheel_ships_agent_workspace(self):
        with zipfile.ZipFile(self.wheel) as zf:
            names = zf.namelist()
            packaged = "phone_harness/agent_workspace/agent_helpers.py"
            self.assertIn(packaged, names)
            self.assertEqual(zf.read(packaged), DEFAULT_HELPERS.read_bytes())
            self.assertTrue(any(name.endswith("phone_harness/SKILL.md") or
                                name == "phone_harness/SKILL.md" for name in names))

    def test_the_wheel_packs_only_the_default_helper(self):
        """A .env sat beside the defaults while this wheel was built."""
        with zipfile.ZipFile(self.wheel) as zf:
            under = [n for n in zf.namelist()
                     if n.startswith("phone_harness/agent_workspace/")]
        self.assertEqual(under, ["phone_harness/agent_workspace/agent_helpers.py"])

    def test_an_installed_wheel_seeds_without_clobbering_edits(self):
        """Install the wheel with pip --target (no venv; CI images vary) and
        import it with only that tree on PYTHONPATH, so the checkout cannot
        supply agent-workspace/."""
        target = self.out / "install"
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-deps",
             "--target", str(target), str(self.wheel)],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        self.assertTrue((target / "phone_harness" / "agent_workspace" /
                         "agent_helpers.py").is_file())

        def installed(workspace, code):
            env = os.environ.copy()
            env["PYTHONPATH"] = str(target)
            env["PH_AGENT_WORKSPACE"] = str(workspace)
            env["PHONE_HARNESS_HOME"] = str(self.out / "home")
            env["PHONE_HARNESS_PLATFORM"] = "android"
            env["PHONE_HARNESS_TELEMETRY"] = "0"
            env.pop("PHONE_HARNESS_SESSION", None)
            return subprocess.run(
                [sys.executable, "-c", code], capture_output=True, text=True,
                env=env, cwd=self.out)

        ws = self.out / "seeded"
        r = installed(ws,
                      "import phone_harness.helpers as h\n"
                      "assert callable(h.tap_icon)\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((ws / "agent_helpers.py").read_bytes(),
                         DEFAULT_HELPERS.read_bytes())

        (ws / "agent_helpers.py").write_text("VALUE = 'custom'\n")
        (ws / "my_helpers.py").write_text("mine = True\n")
        r = installed(ws,
                      "import phone_harness.helpers as h\n"
                      "assert h.VALUE == 'custom'\n"
                      "assert not hasattr(h, 'tap_icon')\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((ws / "agent_helpers.py").read_text(), "VALUE = 'custom'\n")
        self.assertEqual((ws / "my_helpers.py").read_text(), "mine = True\n")

    def test_an_install_resolves_outside_the_prefix(self):
        """A wheel install must not park helpers under lib/pythonX.Y.

        ``repo_root()`` there is inside the prefix and is replaced on
        upgrade. ``HOME`` is a temp directory so this does not touch the
        real ``~/.phone-harness``.
        """
        target = self.out / "prefix"
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-deps",
             "--target", str(target), str(self.wheel)],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        home_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(home_tmp.cleanup)
        home = Path(home_tmp.name)

        def installed():
            env = os.environ.copy()
            env["PYTHONPATH"] = str(target)
            env["HOME"] = str(home)
            env.pop("PH_AGENT_WORKSPACE", None)
            env.pop("XDG_DATA_HOME", None)
            env["PHONE_HARNESS_HOME"] = str(home / "state")
            env["PHONE_HARNESS_PLATFORM"] = "android"
            env["PHONE_HARNESS_TELEMETRY"] = "0"
            env.pop("PHONE_HARNESS_SESSION", None)
            return subprocess.run(
                [sys.executable, "-c",
                 "from phone_harness.workspace import agent_workspace, repo_root\n"
                 "print(repo_root())\n"
                 "print(agent_workspace())\n"],
                capture_output=True, text=True, env=env, cwd=home)

        r = installed()
        self.assertEqual(r.returncode, 0, r.stderr)
        prefix, ws = [Path(line) for line in r.stdout.splitlines()]
        self.assertEqual(ws, home / ".phone-harness" / "agent-workspace")
        self.assertFalse(_inside(ws, target))
        self.assertFalse(_inside(ws, prefix))
        self.assertEqual((ws / "agent_helpers.py").read_bytes(),
                         DEFAULT_HELPERS.read_bytes())
        self.assertFalse((prefix / "agent-workspace").exists())

        (ws / "agent_helpers.py").write_text("VALUE = 'kept'\n")
        (home / ".phone-harness" / ".git").mkdir()
        r = installed()
        self.assertEqual(r.returncode, 0, r.stderr)
        prefix, ws2 = [Path(line) for line in r.stdout.splitlines()]
        data = home / ".local" / "share" / "phone-harness" / "agent-workspace"
        self.assertEqual(ws2, data)
        self.assertFalse(_inside(ws2, target))
        self.assertFalse(_inside(ws2, prefix))
        self.assertFalse(_inside(ws2, home / ".phone-harness"))
        self.assertEqual((ws / "agent_helpers.py").read_text(), "VALUE = 'kept'\n")
        self.assertEqual((ws2 / "agent_helpers.py").read_bytes(),
                         DEFAULT_HELPERS.read_bytes())
