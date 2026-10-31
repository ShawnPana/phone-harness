"""macOS permission status and `doctor --fix`. The grants, System Settings,
the panel process and the clock are fakes: nothing here reads TCC, opens
System Settings, or shows a window."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from phone_harness import admin, permissions  # noqa: E402
from phone_harness.run import _run as run_main, _telemetry_command  # noqa: E402

TERMINAL = "/System/Applications/Utilities/Terminal.app"
TERMINAL_BIN = TERMINAL + "/Contents/MacOS/Terminal"
EDITOR = "/Applications/Editor.app"
HELPER = EDITOR + "/Contents/Helpers/Plugin Host.app/Contents/MacOS/plugin-host"
PYTHON = "/Users/me/.local/share/uv/tools/phone-harness/bin/python"
FRAMEWORK_PYTHON = ("/Library/Frameworks/Python.framework/Versions/3.12/Resources/"
                    "Python.app/Contents/MacOS/Python")
ANCHOR = "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?"
ACCESSIBILITY_URL = ANCHOR + "Privacy_Accessibility"
SCREEN_URL = ANCHOR + "Privacy_ScreenCapture"
SCREEN_LIST = "Screen & System Audio Recording"


def _host(kind="app", path=TERMINAL, executable=TERMINAL_BIN, cdhash="host1", name="Terminal"):
    return {"kind": kind, "name": name, "path": path, "executable": executable, "cdhash": cdhash}


def _python_host():
    return _host(kind="binary", path=PYTHON, executable=PYTHON, name="python", cdhash="bin1")


def _ssh_host():
    return dict(_python_host(), via="ssh")


def _binary(path=PYTHON, cdhash="bin1", adhoc=False, inside_host=False):
    return {"path": path, "cdhash": cdhash, "adhoc": adhoc, "inside_host": inside_host}


def _report(accessibility, screen, host=None, binary=None, version=(15, 0, 0), grants=None):
    return permissions.build_report(
        accessibility, screen, host or _host(), binary or _binary(), version, grants or {},
    )


def _env(**extra):
    env = os.environ.copy()
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["PHONE_HARNESS_TELEMETRY"] = "0"
    env.update(extra)
    return env


def _cli(*args):
    with tempfile.TemporaryDirectory() as home:
        return subprocess.run([sys.executable, "-m", "phone_harness.run", *args],
                              capture_output=True, text=True, timeout=60,
                              env=_env(PHONE_HARNESS_HOME=home))


class Tty(io.StringIO):
    def isatty(self):
        return True


class Calls:
    """Fakes that record what was called, in order."""

    def __init__(self):
        self.log = []

    def fake(self, name, result=None, error=None):
        def call(*args, **kwargs):
            self.log.append((name, args, kwargs))
            if error is not None:
                raise error
            return result
        return call

    def names(self):
        return [name for name, _args, _kwargs in self.log]

    def get(self, name):
        return next((args, kwargs) for called, args, kwargs in self.log if called == name)


class TempHome(unittest.TestCase):
    """A PHONE_HARNESS_HOME of its own, so the guide lock and the saved grants stay in it."""

    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.home = home.name
        patcher = mock.patch.dict(os.environ, {"PHONE_HARNESS_HOME": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)


class HostAndLinks(unittest.TestCase):
    def test_outer_bundle_is_the_one_settings_lists(self):
        self.assertEqual(permissions.bundle_root(HELPER), EDITOR)
        nested = ("/Users/me/Library/Application Support/Example/"
                  "Current/Example Host.app/Contents/MacOS/example-host")
        self.assertEqual(permissions.bundle_root(nested),
                         "/Users/me/Library/Application Support/Example/Current/Example Host.app")
        self.assertEqual(permissions.bundle_root(TERMINAL_BIN), TERMINAL)
        self.assertIsNone(permissions.bundle_root("/usr/local/bin/python3"))
        self.assertIsNone(permissions.bundle_root(""))

    def test_python_app_inside_a_framework_is_not_a_launching_app(self):
        for path in (
            FRAMEWORK_PYTHON,
            "/opt/homebrew/Cellar/python@3.12/3.12.4/Frameworks/Python.framework/Versions/3.12/"
            "Resources/Python.app/Contents/MacOS/Python",
            "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/"
            "Resources/Python.app/Contents/MacOS/Python",
        ):
            self.assertIsNone(permissions.bundle_root(path), path)
        # An app that ships its own framework Python is still that app.
        shipped = (EDITOR + "/Contents/Frameworks/Python.framework/Versions/3.12/Resources/"
                   "Python.app/Contents/MacOS/Python")
        self.assertEqual(permissions.bundle_root(shipped), EDITOR)

    def walk(self, exes, parents, environ=None, apps=None, self_exe=PYTHON):
        return permissions.host_target(
            pid=10, parent_of=parents.get, executable_of=exes.get, self_exe=self_exe,
            environ=environ or {}, app_for_bundle=(apps or {}).get,
        )

    def test_walk_stops_at_the_nearest_app_and_keeps_its_outer_bundle(self):
        host = self.walk({10: PYTHON, 20: HELPER, 30: TERMINAL_BIN}, {10: 20, 20: 30, 30: 1})
        self.assertEqual(host, {"kind": "app", "name": "Editor", "path": EDITOR,
                                "executable": HELPER, "via": "parent"})

    def test_walk_passes_a_root_login_and_lands_on_terminal(self):
        host = self.walk({10: PYTHON, 20: "/bin/zsh", 30: "/usr/bin/login", 40: TERMINAL_BIN},
                         {10: 20, 20: 30, 30: 40, 40: 1})
        self.assertEqual(host["path"], TERMINAL)
        self.assertEqual(host["name"], "Terminal")

    def test_walk_passes_a_framework_python_and_lands_on_terminal(self):
        host = self.walk({10: FRAMEWORK_PYTHON, 20: "/bin/zsh", 30: TERMINAL_BIN},
                         {10: 20, 20: 30, 30: 1}, self_exe=FRAMEWORK_PYTHON)
        self.assertEqual(host["kind"], "app")
        self.assertEqual(host["path"], TERMINAL)

    def test_no_app_means_the_python_binary_is_the_grant(self):
        host = self.walk({10: PYTHON, 20: "/bin/zsh"}, {10: 20, 20: 1})
        self.assertEqual(host, {
            "kind": "binary", "name": "python", "path": PYTHON, "executable": PYTHON, "via": "",
        })
        unknown = self.walk({10: PYTHON, 20: "/bin/zsh"}, {10: 20, 20: 1},
                            environ={"__CFBundleIdentifier": "com.example.gone"})
        self.assertEqual(unknown["kind"], "binary")

    def test_under_tmux_the_app_that_started_the_server_is_named(self):
        # The tmux server is a child of launchd, so no app is above the pane.
        tmux = ({10: PYTHON, 20: "/bin/zsh", 30: "/opt/homebrew/bin/tmux"}, {10: 20, 20: 30, 30: 1})
        host = self.walk(*tmux, environ={"__CFBundleIdentifier": "com.apple.Terminal"},
                         apps={"com.apple.Terminal": TERMINAL})
        self.assertEqual(host, {"kind": "app", "name": "Terminal", "path": TERMINAL,
                                "executable": "", "via": "session"})
        # An app above the process wins over the environment it inherited.
        above = self.walk({10: PYTHON, 20: HELPER}, {10: 20, 20: 1},
                          environ={"__CFBundleIdentifier": "com.apple.Terminal"},
                          apps={"com.apple.Terminal": TERMINAL})
        self.assertEqual((above["path"], above["via"]), (EDITOR, "parent"))

    def test_an_ssh_login_with_no_app_is_marked_as_one(self):
        ssh = {"SSH_CONNECTION": "10.0.0.2 50000 10.0.0.1 22"}
        login = self.walk({10: PYTHON, 20: "/bin/zsh", 30: "/usr/libexec/sshd-session"},
                          {10: 20, 20: 30, 30: 1}, environ=ssh)
        self.assertEqual((login["kind"], login["path"], login["via"]), ("binary", PYTHON, "ssh"))
        # A tmux server started at the Mac and attached over SSH still names its app.
        attached = self.walk({10: PYTHON, 20: "/bin/zsh", 30: "/opt/homebrew/bin/tmux"},
                             {10: 20, 20: 30, 30: 1},
                             environ=dict(ssh, __CFBundleIdentifier="com.apple.Terminal"),
                             apps={"com.apple.Terminal": TERMINAL})
        self.assertEqual((attached["path"], attached["via"]), (TERMINAL, "session"))

    def test_a_bundle_id_resolves_to_the_running_app_first(self):
        appkit = mock.Mock(spec=["NSRunningApplication", "NSWorkspace"])
        running = mock.Mock()
        running.bundleURL.return_value.path.return_value = TERMINAL
        appkit.NSRunningApplication.runningApplicationsWithBundleIdentifier_.return_value = [running]
        workspace = appkit.NSWorkspace.sharedWorkspace.return_value
        workspace.URLForApplicationWithBundleIdentifier_.return_value.path.return_value = EDITOR
        resolve = permissions.app_for_bundle_id
        with mock.patch.object(permissions.sys, "platform", "darwin"), \
                mock.patch.dict(sys.modules, {"AppKit": appkit}):
            self.assertEqual(resolve("com.apple.Terminal"), TERMINAL)
            appkit.NSRunningApplication.runningApplicationsWithBundleIdentifier_.return_value = []
            self.assertEqual(resolve("com.example.editor"), EDITOR)
            workspace.URLForApplicationWithBundleIdentifier_.return_value = None
            self.assertIsNone(resolve("com.example.gone"))
            self.assertIsNone(resolve(""))
            appkit.NSRunningApplication.runningApplicationsWithBundleIdentifier_.side_effect = \
                RuntimeError("no window server")
            self.assertIsNone(resolve("com.apple.Terminal"))
        with mock.patch.object(permissions.sys, "platform", "linux"), \
                mock.patch.dict(sys.modules, {"AppKit": None}):
            self.assertIsNone(resolve("com.apple.Terminal"))

    def test_deep_link_and_list_name_follow_the_os_version(self):
        self.assertEqual(permissions.settings_url("accessibility", (15, 0, 0)), ACCESSIBILITY_URL)
        self.assertEqual(permissions.settings_url("screen_recording", (13, 0)), SCREEN_URL)
        legacy = permissions.settings_url("accessibility", (12, 6, 0))
        self.assertEqual(legacy, "x-apple.systempreferences:com.apple.preference.security?"
                                 "Privacy_Accessibility")
        name = permissions.list_name
        self.assertEqual(name("accessibility", (26, 0)), "Accessibility")
        self.assertEqual(name("accessibility", (27, 0, 0)), "Device Control and Data Access")
        self.assertEqual(name("screen_recording", (14, 6)), "Screen Recording")
        self.assertEqual(name("screen_recording", (15, 0)), SCREEN_LIST)
        self.assertEqual(name("screen_recording", (27, 0)), SCREEN_LIST)

    def test_codesign_text_yields_cdhash_and_adhoc(self):
        signed = "CDHash=AbCdEf0123456789\nAuthority=Apple\n"
        self.assertEqual(permissions.parse_codesign(signed), {"cdhash": "abcdef0123456789", "adhoc": False})
        adhoc = "CDHash=abc\nSignature=adhoc\n"
        self.assertEqual(permissions.parse_codesign(adhoc)["adhoc"], True)
        self.assertEqual(permissions.parse_codesign("flags=0x2(adhoc)\n")["adhoc"], True)
        self.assertEqual(permissions.parse_codesign("")["cdhash"], "")


class Report(unittest.TestCase):
    def test_both_granted_is_ok_and_records_once(self):
        report = _report(True, True)
        self.assertTrue(report["ok"])
        self.assertEqual(report["accessibility"]["state"], "granted")
        self.assertEqual(report["screen_recording"]["state"], "granted")
        self.assertEqual(permissions.explain(report), "")
        saved = permissions.updated_grants({}, report)
        again = permissions.updated_grants(saved, report)
        self.assertEqual(saved, again)
        self.assertEqual(saved["accessibility"]["host_cdhash"], "host1")
        self.assertEqual(saved["accessibility"]["binary_cdhash"], "bin1")

    def test_denied_names_the_app_the_python_row_and_the_pane(self):
        report = _report(False, True)
        entry = report["accessibility"]
        self.assertEqual(entry["state"], "denied")
        self.assertIn('turn on "Terminal"', entry["summary"])
        self.assertIn("drag it in", entry["summary"])
        self.assertIn("switch is already on", entry["summary"])
        self.assertIn("Privacy_Accessibility", entry["suggestion"])
        text = permissions.explain(report)
        self.assertEqual(text.splitlines(), [
            f'Grant "Terminal" ({TERMINAL}).',
            f"If the list also shows the Python binary ({PYTHON}) on its own row, turn that on too.",
            "Accessibility: " + ACCESSIBILITY_URL,
            "Guided fix: phone-harness doctor ios --fix",
        ])
        self.assertNotIn("Guided fix", permissions.explain(report, hint=False))
        text.encode("ascii")

    def test_the_python_row_note_has_a_short_form_for_the_panel(self):
        report = _report(False, True)
        self.assertEqual(permissions.python_row_note(report),
                         "If the list also shows python on its own row, turn that on too.")

    def test_stale_when_the_app_signature_changes(self):
        grants = {"accessibility": {
            "host_path": TERMINAL, "host_cdhash": "old", "host_kind": "app",
            "binary_path": PYTHON, "binary_cdhash": "bin1",
        }}
        report = _report(False, True, host=_host(cdhash="new"), grants=grants)
        self.assertEqual(report["accessibility"]["state"], "stale")
        self.assertEqual(report["accessibility"]["reason"], "host_cdhash")
        self.assertIn("updated or re-signed", report["accessibility"]["summary"])
        self.assertEqual(report["screen_recording"]["state"], "granted")
        # A denial that keeps the same signature is a switch turned off, not a stale grant.
        same = _report(False, True, host=_host(cdhash="old"), grants=grants)
        self.assertEqual(same["accessibility"]["state"], "denied")
        self.assertEqual(same["accessibility"]["reason"], "")

    def test_stale_when_the_python_binary_moves_or_is_resigned(self):
        grants = {"screen_recording": {
            "host_path": TERMINAL, "host_cdhash": "host1", "host_kind": "app",
            "binary_path": "/old/python", "binary_cdhash": "bin1",
        }}
        moved = _report(True, False, binary=_binary(path="/new/python"), grants=grants)
        self.assertEqual(moved["screen_recording"]["state"], "stale")
        self.assertEqual(moved["screen_recording"]["reason"], "binary_moved")
        self.assertIn("/old/python", moved["screen_recording"]["summary"])
        self.assertIn("/new/python", moved["screen_recording"]["summary"])

        resigned = _report(
            True, False,
            binary=_binary(cdhash="bin2"),
            grants={"screen_recording": {
                "host_path": TERMINAL, "host_cdhash": "host1", "host_kind": "app",
                "binary_path": PYTHON, "binary_cdhash": "bin1",
            }},
        )
        self.assertEqual(resigned["screen_recording"]["reason"], "binary_cdhash")
        self.assertIn("re-signed", resigned["screen_recording"]["summary"])

    def test_a_different_launching_app_needs_its_own_grant(self):
        grants = {"accessibility": {
            "host_path": TERMINAL, "host_cdhash": "host1", "host_kind": "app",
            "binary_path": PYTHON, "binary_cdhash": "bin1",
        }}
        cursor = _host(path="/Applications/Cursor.app", executable="/Applications/Cursor.app/Contents/MacOS/Cursor",
                       name="Cursor", cdhash="cursor")
        report = _report(False, True, host=cursor, grants=grants)
        self.assertEqual(report["accessibility"]["state"], "denied")
        self.assertIn("Cursor", report["accessibility"]["summary"])
        self.assertIn(TERMINAL, report["note"])
        # A new app that also runs a different Python is still that app's
        # grant, not a stale binary left behind by an upgrade.
        moved = _report(False, True, host=cursor, binary=_binary(path="/other/python"), grants=grants)
        self.assertEqual(moved["accessibility"]["state"], "denied")
        self.assertIn("/other/python", moved["note"])

    def test_binary_inside_the_host_app_is_not_listed_twice(self):
        binary = _binary(path=EDITOR + "/Contents/MacOS/Editor")
        report = _report(False, False, host=_host(path=EDITOR, name="Editor",
                                                  executable=EDITOR + "/Contents/MacOS/Editor"),
                         binary=binary)
        self.assertTrue(report["binary"]["inside_host"])
        self.assertEqual(permissions.python_row_note(report), "")
        self.assertNotIn("on its own row", permissions.explain(report))

    def test_no_launching_app_tells_the_user_to_grant_the_binary(self):
        report = _report(False, False, host=_python_host())
        text = permissions.explain(report)
        self.assertIn("No launching app", text)
        self.assertIn(PYTHON, text)
        self.assertEqual(permissions.python_row_note(report), "")

    def test_an_app_found_from_the_session_says_why_it_was_named(self):
        report = _report(False, True, host=dict(_host(executable=""), via="session"))
        self.assertEqual(report["host"]["via"], "session")
        self.assertEqual(permissions.explain(report).splitlines()[:2], [
            f'Grant "Terminal" ({TERMINAL}).',
            "Nothing above this process is an app (tmux, screen, or a detached job), "
            "so this is the app its session was started from.",
        ])
        self.assertEqual(permissions.fix_mode(report, tty=True, gui=True), "panel")
        self.assertEqual(_report(False, True)["host"]["via"], "")
        self.assertNotIn("session was started", permissions.explain(_report(False, True)))

    def test_an_ssh_login_points_to_a_tmux_session_started_at_the_mac(self):
        # Grants Terminal and an older Python once had do not make an SSH login stale.
        grants = {kind: {"host_path": TERMINAL, "host_cdhash": "host1", "host_kind": "app",
                         "binary_path": "/old/python", "binary_cdhash": "bin0"} for kind in
                  ("accessibility", "screen_recording")}
        report = _report(False, False, host=_ssh_host(), binary=_binary(adhoc=True), grants=grants)
        for kind in ("accessibility", "screen_recording"):
            self.assertEqual((report[kind]["state"], report[kind]["reason"]), ("denied", ""))
            self.assertIn("SSH service", report[kind]["summary"])
            self.assertIn("tmux session started from a terminal app at the Mac",
                          report[kind]["summary"])
        self.assertTrue(report["screen_recording"]["summary"].endswith(
            f"turn that app on in {SCREEN_LIST}"))
        self.assertEqual(report["note"], "")
        text = permissions.explain(report)
        self.assertEqual(text.splitlines(), [
            "This is an SSH login, so a grant for Python or a terminal app does not apply to it.",
            "At the Mac, start tmux in a terminal app and run phone-harness doctor ios --fix in it. "
            "Then attach to that tmux session over SSH and run phone-harness there.",
            "Accessibility: " + ACCESSIBILITY_URL,
            f"{SCREEN_LIST}: " + SCREEN_URL,
        ])
        text.encode("ascii")

    def test_unknown_when_the_frameworks_are_missing(self):
        report = _report(None, None)
        self.assertEqual(report["accessibility"]["state"], "unknown")
        self.assertFalse(report["ok"])
        self.assertIn("pyobjc", report["accessibility"]["summary"])
        self.assertEqual(report["accessibility"]["url"], "")
        self.assertNotIn("Guided fix", permissions.explain(report))

    def test_macos_27_uses_the_renamed_accessibility_list(self):
        report = _report(False, True, version=(27, 0, 0))
        self.assertEqual(report["accessibility"]["list"], "Device Control and Data Access")
        self.assertIn("Device Control and Data Access", report["accessibility"]["summary"])

    def test_a_failed_check_keeps_the_recorded_grant(self):
        report = _report(True, True)
        saved = permissions.updated_grants({}, report)
        denied = _report(False, True, host=_host(cdhash="host1"), grants=saved)
        self.assertEqual(denied["accessibility"]["state"], "denied")
        self.assertEqual(permissions.updated_grants(saved, denied), saved)

    def test_doctor_lines_and_the_json_payload(self):
        report = _report(False, True)
        lines = permissions.doctor_lines(report)
        self.assertEqual([(label, ok) for label, ok, _hint in lines], [
            (permissions.ACCESSIBILITY_LABEL, False), (permissions.SCREEN_RECORDING_LABEL, True)])
        self.assertEqual(lines[0][2], report["accessibility"]["summary"])
        self.assertEqual(lines[1][2], "")
        payload = json.loads(json.dumps(permissions.report_payload(report)))
        self.assertEqual(set(payload), {"ok", "needed", "platform", "host", "binary",
                                        "accessibility", "screen_recording", "note"})
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["host"]["name"], "Terminal")
        self.assertEqual(payload["accessibility"]["state"], "denied")
        self.assertEqual(payload["accessibility"]["url"], ACCESSIBILITY_URL)
        self.assertEqual(payload["screen_recording"]["state"], "granted")

    def test_off_macos_nothing_is_needed(self):
        report = permissions.not_needed_report()
        self.assertTrue(report["ok"])
        self.assertFalse(report["needed"])
        self.assertEqual(report["accessibility"]["state"], "not_required")
        self.assertEqual(permissions.explain(report), "")
        self.assertEqual(permissions.fix_mode(report, tty=True, gui=True), "none")


class FreshRead(unittest.TestCase):
    def test_each_read_is_a_new_interpreter_running_the_probe_module(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return mock.Mock(stdout='noise\n{"accessibility": true, "screen_recording": false}\n')

        with mock.patch.object(permissions, "local_preflight",
                               side_effect=AssertionError("read in this process")):
            states = permissions.fresh_preflight(runner)
        self.assertEqual(states, {"accessibility": True, "screen_recording": False})
        argv, kwargs = calls[0]
        self.assertEqual(argv, [sys.executable, "-m", "phone_harness.permission_probe"])
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(kwargs["capture_output"])
        self.assertEqual(kwargs["timeout"], permissions._PROBE_TIMEOUT)
        self.assertNotIn("env", kwargs)

    def test_a_probe_that_cannot_run_or_answer_falls_back_to_this_process(self):
        local = {"accessibility": None, "screen_recording": True}

        def broken(*_args, **_kwargs):
            raise OSError("no python")

        def hung(*_args, **_kwargs):
            raise subprocess.TimeoutExpired("python", 8)

        def garbled(*_args, **_kwargs):
            return mock.Mock(stdout="Traceback (most recent call last):\n")

        with mock.patch.object(permissions, "local_preflight", return_value=local) as fallback:
            for runner in (broken, hung, garbled):
                self.assertEqual(permissions.fresh_preflight(runner), local, runner.__name__)
        self.assertEqual(fallback.call_count, 3)


class Collect(TempHome):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(permissions, "_python_binary", return_value=PYTHON)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_off_macos_collect_reads_nothing(self):
        with mock.patch.object(permissions.sys, "platform", "linux"), \
                mock.patch.object(permissions, "fresh_preflight", side_effect=AssertionError("read")):
            report = permissions.collect()
        self.assertFalse(report["needed"])
        self.assertTrue(report["ok"])

    def test_on_macos_collect_reads_through_a_new_process(self):
        with mock.patch.object(permissions.sys, "platform", "darwin"), \
                mock.patch.object(permissions, "fresh_preflight",
                                  return_value={"accessibility": True, "screen_recording": False}) as read:
            report = permissions.collect(host=_host(), version=(15, 0, 0),
                                         identify=lambda _path: {"cdhash": "x", "adhoc": False})
        read.assert_called_once_with()
        self.assertEqual(report["accessibility"]["state"], "granted")
        self.assertEqual(report["screen_recording"]["state"], "denied")
        self.assertEqual(report["screen_recording"]["list"], SCREEN_LIST)
        self.assertEqual(report["binary"], {"path": PYTHON, "cdhash": "x", "adhoc": False,
                                            "inside_host": False})

    def test_collect_remembers_a_grant_and_flags_the_next_signature_change(self):
        hashes = {"host": "aaa"}

        def identify(path):
            if path and path.endswith(".app"):
                return {"cdhash": hashes["host"], "adhoc": False}
            return {"cdhash": "bbb", "adhoc": False}

        with mock.patch.object(permissions.sys, "platform", "darwin"):
            first = permissions.collect(
                preflight={"accessibility": True, "screen_recording": True},
                host=_host(cdhash=""), identify=identify, version=(15, 0, 0),
            )
            self.assertTrue(first["ok"])
            saved = json.loads((Path(self.home) / "state" / "permissions.json").read_text())
            self.assertEqual(saved["grants"]["accessibility"]["host_path"], TERMINAL)
            hashes["host"] = "ccc"
            second = permissions.collect(
                preflight={"accessibility": False, "screen_recording": True},
                host=_host(cdhash=""), identify=identify, version=(15, 0, 0),
            )
        self.assertEqual(second["accessibility"]["state"], "stale")
        self.assertEqual(second["accessibility"]["reason"], "host_cdhash")


class Sessions(unittest.TestCase):
    def test_ssh_is_read_from_the_environment(self):
        self.assertFalse(permissions.over_ssh({}))
        self.assertFalse(permissions.over_ssh({"SSH_CONNECTION": ""}))
        for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
            self.assertTrue(permissions.over_ssh({name: "10.0.0.2 50000 10.0.0.1 22"}), name)

    def test_a_desktop_is_a_window_server_session_on_the_console(self):
        quartz = mock.Mock(spec=["CGSessionCopyCurrentDictionary", "kCGSessionOnConsoleKey"])
        # The constant's value is not its name. Reading the name would miss it.
        quartz.kCGSessionOnConsoleKey = "kCGSSessionOnConsoleKey"
        cases = (
            (None, False),
            ({}, False),
            ({"kCGSSessionOnConsoleKey": True}, True),
            ({"kCGSSessionOnConsoleKey": False}, False),
            ({"kCGSSessionUserNameKey": "me"}, True),
        )
        with mock.patch.object(permissions.sys, "platform", "darwin"), \
                mock.patch.dict(sys.modules, {"Quartz": quartz}):
            for info, expected in cases:
                quartz.CGSessionCopyCurrentDictionary.return_value = info
                self.assertIs(permissions.gui_session(), expected, info)
            quartz.CGSessionCopyCurrentDictionary.side_effect = RuntimeError("no session")
            self.assertFalse(permissions.gui_session())
        with mock.patch.object(permissions.sys, "platform", "linux"), \
                mock.patch.dict(sys.modules, {"Quartz": None}):
            self.assertFalse(permissions.gui_session())


class FixMode(unittest.TestCase):
    def test_each_session_gets_its_own_guide(self):
        mode = permissions.fix_mode
        missing = _report(False, True)
        self.assertEqual(mode(_report(True, True), tty=True, gui=True), "none")
        for tty in (True, False):
            for gui in (True, False):
                self.assertEqual(mode(missing, tty=tty, noninteractive=True, gui=gui), "skip")
                self.assertEqual(mode(_report(None, None), tty=tty, gui=gui), "manual")
                self.assertEqual(mode(_report(None, True), tty=tty, gui=gui), "manual")
        self.assertEqual(mode(missing, tty=True, gui=True), "panel")
        self.assertEqual(mode(_report(False, True, host=_python_host()), tty=True, gui=True),
                         "open_and_wait")
        self.assertEqual(mode(missing, tty=True, gui=True, ssh=True), "wait")
        self.assertEqual(mode(missing, tty=True, gui=False), "wait")
        self.assertEqual(mode(missing, tty=False, gui=True), "open")
        self.assertEqual(mode(missing, tty=False, gui=True, ssh=True), "manual")
        self.assertEqual(mode(missing, tty=False, gui=False), "manual")

    def test_an_ssh_login_gets_text_and_no_wait(self):
        mode = permissions.fix_mode
        login = _report(False, True, host=_ssh_host())
        for tty in (True, False):
            for gui in (True, False):
                self.assertEqual(mode(login, tty=tty, gui=gui, ssh=True), "manual")
                self.assertEqual(mode(login, tty=tty, noninteractive=True, gui=gui, ssh=True),
                                 "skip")
        # Under a tmux server started at the Mac, a grant made there lands, so it waits.
        attached = _report(False, True, host=dict(_host(executable=""), via="session"))
        self.assertEqual(mode(attached, tty=True, gui=True, ssh=True), "wait")


class Fix(unittest.TestCase):
    def setUp(self):
        self.calls = Calls()
        self.said = []

    def run_fix(self, report, **overrides):
        calls = self.calls
        options = dict(
            tty=True, gui=True, ssh=False, timeout=30, say=self.said.append,
            panel=calls.fake("panel", ("granted", "")),
            wait=calls.fake("wait", "granted"),
            opener=calls.fake("opener", True),
            recollect=calls.fake("recollect", _report(True, True)),
        )
        options.update(overrides)
        return permissions.fix(report, **options)

    def test_nothing_missing_does_nothing(self):
        result, report = self.run_fix(_report(True, True))
        self.assertEqual(result, {"mode": "none", "outcome": "none", "detail": "", "steps": []})
        self.assertTrue(report["ok"])
        self.assertEqual(self.calls.log, [])

    def test_yes_opens_nothing_and_waits_for_nothing(self):
        for tty in (True, False):
            result, _report_after = self.run_fix(_report(False, False), noninteractive=True, tty=tty)
            self.assertEqual(result, {"mode": "skip", "outcome": "skipped", "detail": "",
                                      "steps": ["accessibility", "screen_recording"]})
        self.assertEqual(self.calls.log, [])
        self.assertEqual(self.said, [])

    def test_a_pipe_at_the_mac_opens_the_first_missing_pane_and_returns(self):
        result, _report_after = self.run_fix(_report(True, False), tty=False)
        self.assertEqual((result["mode"], result["outcome"]), ("open", "opened"))
        self.assertEqual(self.calls.log, [("opener", (SCREEN_URL,), {})])
        failed, _report_after = self.run_fix(_report(False, True), tty=False,
                                             opener=self.calls.fake("opener", False))
        self.assertEqual((failed["outcome"], failed["detail"]), ("manual", "open_failed"))

    def test_text_only_without_a_desktop_or_a_readable_state(self):
        missing = _report(False, True)
        login = _report(False, True, host=_ssh_host())
        for report, options, detail in ((missing, {"tty": False, "gui": False}, "no_desktop"),
                                        (missing, {"tty": False, "ssh": True}, "no_desktop"),
                                        (_report(None, None), {}, "unknown"),
                                        (login, {"ssh": True}, "ssh"),
                                        (login, {"ssh": True, "tty": False}, "ssh")):
            result, _report_after = self.run_fix(report, **options)
            self.assertEqual((result["mode"], result["outcome"], result["detail"]),
                             ("manual", "manual", detail), options)
        self.assertEqual(self.calls.log, [])
        self.assertEqual(self.said, [])

    def test_a_terminal_at_the_mac_gets_the_panel_and_reads_again(self):
        before = _report(False, False)
        result, after = self.run_fix(before)
        self.assertEqual(result, {"mode": "panel", "outcome": "granted", "detail": "",
                                  "steps": ["accessibility", "screen_recording"]})
        self.assertTrue(after["ok"])
        self.assertEqual(self.calls.names(), ["panel", "recollect"])
        self.assertEqual(self.calls.get("panel")[0], (before, 30))
        self.assertEqual(self.said, [
            'Opening a panel beside System Settings. Drag "Terminal" into the list, '
            "then confirm with Touch ID or your password. Not Now closes it."])

    def test_the_panel_outcome_stands_unless_the_grant_landed(self):
        missing = _report(False, True)
        for outcome in ("dismissed", "timeout", "already_running"):
            self.calls.log.clear()
            result, _report_after = self.run_fix(
                missing, panel=self.calls.fake("panel", (outcome, "")),
                recollect=self.calls.fake("recollect", missing))
            self.assertEqual(result["outcome"], outcome)
            self.assertEqual(self.calls.names(), ["panel", "recollect"], outcome)
        landed, _report_after = self.run_fix(missing, panel=self.calls.fake("panel", ("timeout", "")))
        self.assertEqual(landed["outcome"], "granted")

    def test_a_panel_that_cannot_run_falls_back_to_the_text_guide(self):
        for panel, detail in ((self.calls.fake("panel", ("error", "AppKit is not available: x")),
                               "AppKit is not available: x"),
                              (self.calls.fake("panel", error=RuntimeError("boom")),
                               "RuntimeError: boom")):
            self.calls.log.clear()
            self.said.clear()
            result, _report_after = self.run_fix(_report(False, True), panel=panel)
            self.assertEqual(result["mode"], "open_and_wait")
            self.assertEqual(result["outcome"], "granted")
            self.assertEqual(result["detail"], detail)
            self.assertEqual(self.calls.names(), ["panel", "wait", "recollect"])
            _args, kwargs = self.calls.get("wait")
            self.assertTrue(kwargs["open_pane"])
            self.assertEqual(kwargs["timeout"], 30)
            self.assertEqual(self.said[-1], f"The panel could not run ({detail}). Guiding here instead.")

    def test_over_ssh_or_without_a_display_a_terminal_waits_and_opens_nothing(self):
        for options in ({"ssh": True}, {"gui": False}):
            self.calls.log.clear()
            result, _report_after = self.run_fix(_report(False, True), **options)
            self.assertEqual(result["mode"], "wait")
            self.assertEqual(self.calls.names(), ["wait", "recollect"])
            self.assertFalse(self.calls.get("wait")[1]["open_pane"])

    def test_without_an_app_to_drag_it_opens_each_pane_and_waits_here(self):
        result, _report_after = self.run_fix(_report(False, False, host=_python_host()))
        self.assertEqual(result["mode"], "open_and_wait")
        self.assertEqual(self.calls.names(), ["wait", "recollect"])
        _args, kwargs = self.calls.get("wait")
        self.assertTrue(kwargs["open_pane"])
        self.assertIsNotNone(kwargs["opener"])

    def test_a_wait_that_raises_is_an_error_and_a_failed_reread_keeps_the_report(self):
        before = _report(False, True)
        result, after = self.run_fix(before, gui=False,
                                     wait=self.calls.fake("wait", error=ValueError("bad")),
                                     recollect=self.calls.fake("recollect", error=OSError("ps")))
        self.assertEqual((result["outcome"], result["detail"]), ("error", "ValueError: bad"))
        self.assertIs(after, before)

    def test_the_default_reread_keeps_the_same_host(self):
        before = _report(False, True)
        with mock.patch.object(permissions, "collect", return_value=_report(True, True)) as collect:
            result, after = self.run_fix(before, recollect=None)
        collect.assert_called_once_with(host=before["host"])
        self.assertTrue(after["ok"])
        self.assertEqual(result["outcome"], "granted")


class WaitForGrant(TempHome):
    def run_wait(self, report, answers=None, probe=None, open_pane=True, timeout=600,
                 clock_moves=True):
        self.now = 0.0
        self.opened, self.said, self.slept = [], [], []
        answers = list(answers or [])

        def answer():
            return answers.pop(0) if len(answers) > 1 else answers[0]

        def sleep(seconds):
            self.slept.append(seconds)
            if clock_moves:
                self.now += seconds

        return permissions.wait_for_grant(
            report, open_pane=open_pane, timeout=timeout, interval=1.0,
            opener=self.opened.append, probe=probe or answer, sleep=sleep,
            clock=lambda: self.now, say=self.said.append,
        )

    def assert_unlocked(self):
        held = permissions.acquire_guide_lock()
        self.assertIsNotNone(held)
        held.close()

    def test_opens_each_missing_pane_and_moves_on_as_each_grant_lands(self):
        report = _report(False, False)
        none = {"accessibility": False, "screen_recording": False}
        first = {"accessibility": True, "screen_recording": False}
        both = {"accessibility": True, "screen_recording": True}
        outcome = self.run_wait(report, [none, none, first, first, both])
        self.assertEqual(outcome, "granted")
        self.assertEqual(self.opened, [ACCESSIBILITY_URL, SCREEN_URL])
        self.assertEqual(self.slept, [1.0, 1.0, 1.0])
        row = "If the list also shows python on its own row, turn that on too."
        wait = "Waiting up to 10 minutes for the grant. Ctrl-C stops."
        self.assertEqual(self.said, [
            "Opened System Settings > Privacy & Security > Accessibility.",
            report["accessibility"]["summary"] + ".", row, wait, "Accessibility: on.",
            f"Opened System Settings > Privacy & Security > {SCREEN_LIST}.",
            report["screen_recording"]["summary"] + ".", row, wait, f"{SCREEN_LIST}: on.",
        ])
        for line in self.said:
            line.encode("ascii")
        self.assert_unlocked()

    def test_a_grant_that_lands_early_is_not_opened_again(self):
        outcome = self.run_wait(_report(False, False),
                                [{"accessibility": True, "screen_recording": True}])
        self.assertEqual(outcome, "granted")
        self.assertEqual(self.opened, [ACCESSIBILITY_URL])
        self.assertEqual(self.slept, [])

    def test_times_out_when_the_grant_never_lands(self):
        outcome = self.run_wait(_report(False, False), [{"accessibility": False}], timeout=3)
        self.assertEqual(outcome, "timeout")
        self.assertEqual(self.opened, [ACCESSIBILITY_URL])
        self.assertEqual(self.slept, [1.0, 1.0, 1.0])
        self.assertIn("Waiting up to 3 seconds for the grant. Ctrl-C stops.", self.said)
        self.assert_unlocked()

    def test_a_clock_that_does_not_move_cannot_spin_forever(self):
        outcome = self.run_wait(_report(False, True), [{}], clock_moves=False)
        self.assertEqual(outcome, "timeout")
        self.assertEqual(self.slept, [1.0])

    def test_without_a_display_nothing_is_opened(self):
        outcome = self.run_wait(_report(False, True), [{"accessibility": True}], open_pane=False)
        self.assertEqual(outcome, "granted")
        self.assertEqual(self.opened, [])
        self.assertEqual(self.said[0],
                         "This session has no display at the Mac, so nothing was opened. At the Mac:")

    def test_ctrl_c_stops_the_wait_and_frees_the_lock(self):
        def interrupted():
            raise KeyboardInterrupt

        self.assertEqual(self.run_wait(_report(False, True), probe=interrupted), "dismissed")
        self.assert_unlocked()

    def test_a_second_guide_is_already_running(self):
        held = permissions.acquire_guide_lock()
        self.assertIsNotNone(held)
        try:
            outcome = self.run_wait(_report(False, True), [{"accessibility": True}])
        finally:
            held.close()
        self.assertEqual(outcome, "already_running")
        self.assertEqual(self.opened, [])
        self.assertEqual(self.said, [])

    def test_durations_read_naturally(self):
        self.assertEqual(permissions._duration(600), "10 minutes")
        self.assertEqual(permissions._duration(120), "2 minutes")
        self.assertEqual(permissions._duration(90), "90 seconds")
        self.assertEqual(permissions._duration(30.0), "30 seconds")


class FakeProc:
    def __init__(self, out="", err="", returncode=0, raises=()):
        self.out, self.err, self.returncode = out, err, returncode
        self.raises = list(raises)
        self.inputs, self.timeouts = [], []
        self.killed = 0

    def communicate(self, input=None, timeout=None):
        self.inputs.append(input)
        self.timeouts.append(timeout)
        if self.raises:
            raise self.raises.pop(0)
        return self.out, self.err

    def kill(self):
        self.killed += 1


class RunPanel(TempHome):
    def launch(self, proc, report=None, timeout=600):
        self.started = []

        def popen(argv, **kwargs):
            self.started.append((argv, kwargs))
            return proc

        return permissions.run_panel(report or _report(False, False), timeout=timeout, popen=popen)

    def test_the_spec_goes_in_on_stdin_and_one_json_line_comes_back(self):
        proc = FakeProc(out='objc[1]: a warning\n'
                            '{"outcome": "granted", "granted": ["accessibility", "screen_recording"]}\n')
        self.assertEqual(self.launch(proc), ("granted", ""))
        argv, kwargs = self.started[0]
        self.assertEqual(argv, [sys.executable, "-m", "phone_harness.permission_panel"])
        for stream in ("stdin", "stdout", "stderr"):
            self.assertIs(kwargs[stream], subprocess.PIPE)
        self.assertTrue(kwargs["text"])
        self.assertNotIn("env", kwargs)
        self.assertTrue(proc.inputs[0].endswith("\n"))
        self.assertEqual(json.loads(proc.inputs[0]), {
            "host": {"name": "Terminal", "path": TERMINAL},
            "steps": [
                {"kind": "accessibility", "list": "Accessibility", "url": ACCESSIBILITY_URL,
                 "state": "denied"},
                {"kind": "screen_recording", "list": SCREEN_LIST, "url": SCREEN_URL,
                 "state": "denied"},
            ],
            "note": "If the list also shows python on its own row, turn that on too.",
            "timeout": 600,
            "lock": str(Path(self.home) / "state" / "run" / "permission-guide.lock"),
        })
        self.assertEqual(proc.timeouts, [660])

    def test_the_spec_lists_only_what_is_missing_and_keeps_a_stale_state(self):
        grants = {"accessibility": {"host_path": TERMINAL, "host_cdhash": "old", "host_kind": "app",
                                    "binary_path": PYTHON, "binary_cdhash": "bin1"}}
        report = _report(False, True, host=_host(cdhash="new"), grants=grants)
        steps = permissions.panel_spec(report)["steps"]
        self.assertEqual([(step["kind"], step["state"]) for step in steps], [("accessibility", "stale")])

    def test_the_last_known_outcome_line_wins_and_detail_comes_along(self):
        proc = FakeProc(out='{"outcome": "dismissed"}\n'
                            '{"outcome": "error", "granted": [], "detail": "AppKit is not available: x"}\n'
                            '{"outcome": "maybe"}\n')
        self.assertEqual(self.launch(proc), ("error", "AppKit is not available: x"))

    def test_a_panel_that_says_nothing_is_an_error_with_its_last_stderr_line(self):
        crashed = FakeProc(err="Traceback (most recent call last):\n  File x\n"
                               "ImportError: No module named 'AppKit'\n\n", returncode=1)
        self.assertEqual(self.launch(crashed), ("error", "ImportError: No module named 'AppKit'"))
        silent = FakeProc(out="not json\n", returncode=3)
        self.assertEqual(self.launch(silent), ("error", "the panel exited with status 3"))

    def test_a_panel_that_outlives_its_timeout_is_killed(self):
        proc = FakeProc(raises=[subprocess.TimeoutExpired("python", 660)])
        self.assertEqual(self.launch(proc), ("timeout", ""))
        self.assertEqual(proc.killed, 1)
        late = FakeProc(out='{"outcome": "dismissed"}\n', raises=[subprocess.TimeoutExpired("python", 660)])
        self.assertEqual(self.launch(late), ("dismissed", ""))

    def test_ctrl_c_kills_the_panel_and_counts_as_not_now(self):
        proc = FakeProc(raises=[KeyboardInterrupt()])
        self.assertEqual(self.launch(proc), ("dismissed", "interrupted"))
        self.assertEqual(proc.killed, 1)
        self.assertEqual(len(proc.inputs), 2)

    def test_a_panel_that_cannot_start_is_an_error(self):
        def popen(*_args, **_kwargs):
            raise OSError("Permission denied")

        self.assertEqual(permissions.run_panel(_report(False, True), popen=popen),
                         ("error", "could not start the panel (Permission denied)"))


class OpenSettings(unittest.TestCase):
    def test_open_runs_the_url_on_macos_only(self):
        with mock.patch.object(permissions.sys, "platform", "darwin"), \
                mock.patch.object(permissions.subprocess, "run") as run:
            self.assertTrue(permissions.open_settings(ACCESSIBILITY_URL))
            self.assertEqual(run.call_args.args[0], ["open", ACCESSIBILITY_URL])
            self.assertFalse(permissions.open_settings(""))
            run.side_effect = OSError("no open")
            self.assertFalse(permissions.open_settings(ACCESSIBILITY_URL))
        with mock.patch.object(permissions.sys, "platform", "linux"), \
                mock.patch.object(permissions.subprocess, "run", side_effect=AssertionError("ran")):
            self.assertFalse(permissions.open_settings(ACCESSIBILITY_URL))


class OutcomeLines(unittest.TestCase):
    def line(self, outcome, detail="", report=None, steps=("accessibility", "screen_recording")):
        result = {"mode": "panel", "outcome": outcome, "detail": detail, "steps": list(steps)}
        return permissions.outcome_line(result, report or _report(False, False))

    def test_every_outcome_says_what_happened_and_what_is_next(self):
        self.assertEqual(self.line("granted"),
                         'Granted. If macOS asks to quit "Terminal", choose Later: '
                         "a new phone-harness process already sees the grant.")
        self.assertEqual(self.line("granted", steps=("accessibility",)), "Granted.")
        self.assertEqual(self.line("granted", report=_report(False, False, host=_python_host())),
                         "Granted.")
        self.assertEqual(self.line("dismissed"),
                         "Not now. Run phone-harness doctor ios --fix when you are ready.")
        self.assertIn("Timed out", self.line("timeout"))
        opened = self.line("opened", report=_report(True, False), steps=("screen_recording",))
        self.assertTrue(opened.startswith(f"Opened System Settings > Privacy & Security > {SCREEN_LIST}."))
        self.assertIn(f'"Terminal" ({TERMINAL})', opened)
        self.assertIn("its output is not a terminal", opened)
        self.assertIn("Non-interactive", self.line("skipped"))
        self.assertIn("already open in another terminal", self.line("already_running"))
        self.assertIn("Could not read the permission state", self.line("manual", "unknown"))
        self.assertIn("Could not open System Settings", self.line("manual", "open_failed"))
        self.assertIn("No desktop session here", self.line("manual", "no_desktop"))
        self.assertEqual(self.line("manual", "ssh", report=_report(False, False, host=_ssh_host())),
                         "This is an SSH login, so nothing was opened and this run did not wait.")
        self.assertEqual(self.line("error", "AppKit is not available: x"),
                         "The guide stopped (AppKit is not available: x). "
                         "Run phone-harness doctor ios --fix to try again.")
        self.assertEqual(self.line("none"), "")
        self.assertEqual(permissions.outcome_line(None, _report(True, True)), "")
        for outcome in ("granted", "dismissed", "timeout", "opened", "skipped",
                        "already_running", "manual", "error"):
            self.line(outcome).encode("ascii")


class Doctor(TempHome):
    def setUp(self):
        super().setUp()
        for state in (admin._failures, admin._checks, admin._extra):
            state.clear()

    def check(self, report, **options):
        out = io.StringIO()
        with mock.patch.object(permissions, "collect", return_value=report), \
                contextlib.redirect_stdout(out):
            admin._check_permissions(**options)
        return out.getvalue()

    def test_without_fix_doctor_reads_once_and_points_at_fix(self):
        with mock.patch.object(permissions, "fix") as fix, \
                mock.patch.object(permissions, "open_settings") as opened:
            text = self.check(_report(False, True))
        fix.assert_not_called()
        opened.assert_not_called()
        self.assertIn("[FAIL] " + permissions.ACCESSIBILITY_LABEL, text)
        self.assertIn("[PASS] " + permissions.SCREEN_RECORDING_LABEL, text)
        self.assertIn("Accessibility: " + ACCESSIBILITY_URL, text)
        self.assertIn("Guided fix: phone-harness doctor ios --fix", text)
        self.assertEqual(admin._failures, [permissions.ACCESSIBILITY_LABEL])
        self.assertEqual(admin._extra["permissions"]["accessibility"]["state"], "denied")
        self.assertNotIn("fix", admin._extra)

    def test_doctor_names_a_stale_grant(self):
        report = _report(False, True, host=_host(cdhash="new"), grants={"accessibility": {
            "host_path": TERMINAL, "host_cdhash": "old", "host_kind": "app",
            "binary_path": PYTHON, "binary_cdhash": "bin1",
        }})
        text = self.check(report)
        self.assertIn("stale grant", text)
        self.assertIn("Privacy_Accessibility", text)

    def test_over_ssh_fix_says_why_and_neither_opens_nor_waits(self):
        report = _report(False, False, host=_ssh_host())
        with mock.patch.object(permissions, "gui_session", return_value=False), \
                mock.patch.object(permissions, "over_ssh", return_value=True), \
                mock.patch.object(permissions, "run_panel", side_effect=AssertionError("panel")), \
                mock.patch.object(permissions, "wait_for_grant", side_effect=AssertionError("wait")), \
                mock.patch.object(permissions, "open_settings", side_effect=AssertionError("open")):
            text = self.check(report, fix=True, tty=True)
        self.assertIn("This is an SSH login, so a grant for Python or a terminal app "
                      "does not apply to it.", text)
        self.assertIn("This is an SSH login, so nothing was opened and this run did not wait.", text)
        self.assertNotIn("Guided fix", text)
        self.assertEqual(admin._extra["fix"]["detail"], "ssh")
        self.assertEqual(admin._extra["permissions"]["host"]["via"], "ssh")

    def test_fix_acts_first_and_the_checks_show_the_state_after_it(self):
        before, after = _report(False, False), _report(True, True)
        result = {"mode": "panel", "outcome": "granted", "detail": "",
                  "steps": ["accessibility", "screen_recording"]}

        def fake_fix(report, tty, noninteractive, say):
            self.assertIs(report, before)
            self.assertTrue(tty)
            self.assertFalse(noninteractive)
            say("Opening a panel beside System Settings.")
            return result, after

        with mock.patch.object(permissions, "fix", side_effect=fake_fix):
            text = self.check(before, fix=True, tty=True)
        self.assertEqual(text.splitlines(), [
            "",
            "Opening a panel beside System Settings.",
            "",
            "  [PASS] " + permissions.ACCESSIBILITY_LABEL,
            "  [PASS] " + permissions.SCREEN_RECORDING_LABEL,
            "",
            'Granted. If macOS asks to quit "Terminal", choose Later: '
            "a new phone-harness process already sees the grant.",
            "",
        ])
        self.assertEqual(admin._failures, [])
        self.assertEqual(admin._extra["fix"], result)
        self.assertTrue(admin._extra["permissions"]["ok"])

    def test_a_fix_that_did_not_land_keeps_the_fail_and_says_what_is_next(self):
        report = _report(False, True)
        result = {"mode": "skip", "outcome": "skipped", "detail": "", "steps": ["accessibility"]}
        with mock.patch.object(permissions, "fix", return_value=(result, report)):
            text = self.check(report, fix=True, noninteractive=True)
        self.assertIn("[FAIL] " + permissions.ACCESSIBILITY_LABEL, text)
        self.assertIn("Accessibility: " + ACCESSIBILITY_URL, text)
        self.assertIn("Non-interactive: System Settings was not opened and this run did not wait.", text)
        self.assertNotIn("Guided fix", text)
        # Nothing was said, so no empty guide block.
        self.assertTrue(text.startswith("  [FAIL] "))

    def test_a_fix_that_raises_is_reported_and_doctor_carries_on(self):
        with mock.patch.object(permissions, "fix", side_effect=RuntimeError("boom")):
            text = self.check(_report(False, True), fix=True, tty=True)
        self.assertIn("The guide stopped (RuntimeError: boom). "
                      "Run phone-harness doctor ios --fix to try again.", text)
        self.assertEqual(admin._extra["fix"]["outcome"], "error")

    def test_a_read_that_raises_fails_both_checks(self):
        out = io.StringIO()
        with mock.patch.object(permissions, "collect", side_effect=RuntimeError("no ps")), \
                mock.patch.object(permissions, "fix") as fix, contextlib.redirect_stdout(out):
            admin._check_permissions(fix=True)
        fix.assert_not_called()
        self.assertEqual(admin._failures, [permissions.ACCESSIBILITY_LABEL,
                                           permissions.SCREEN_RECORDING_LABEL])
        self.assertIn("could not check permissions (no ps)", out.getvalue())

    def test_json_puts_only_json_on_stdout_and_the_guide_on_stderr(self):
        before, after = _report(False, False), _report(True, False)
        result = {"mode": "panel", "outcome": "timeout", "detail": "",
                  "steps": ["accessibility", "screen_recording"]}

        def fake_fix(report, tty, noninteractive, say):
            say("Opening a panel beside System Settings.")
            return result, after

        def ios(**options):
            admin._check("pyobjc frameworks (Quartz, Vision, AppKit)", True)
            admin._check_permissions(**options)

        out, err = Tty(), io.StringIO()
        with mock.patch.object(admin, "_doctor_ios", side_effect=ios) as ladder, \
                mock.patch.object(permissions, "collect", return_value=before), \
                mock.patch.object(permissions, "fix", side_effect=fake_fix), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = admin.run_doctor("ios", fix=True, as_json=True)
        self.assertEqual(code, 1)
        # The terminal is measured before stdout is captured for the JSON.
        self.assertTrue(ladder.call_args.kwargs["tty"])
        payload = json.loads(out.getvalue())
        self.assertEqual(set(payload), {"platform", "ok", "checks", "permissions", "fix"})
        self.assertEqual(payload["platform"], "ios")
        self.assertFalse(payload["ok"])
        self.assertEqual([(check["name"], check["ok"]) for check in payload["checks"]], [
            ("pyobjc frameworks (Quartz, Vision, AppKit)", True),
            (permissions.ACCESSIBILITY_LABEL, True),
            (permissions.SCREEN_RECORDING_LABEL, False),
        ])
        self.assertEqual(payload["checks"][2]["hint"], after["screen_recording"]["summary"])
        self.assertEqual(payload["permissions"]["screen_recording"]["state"], "denied")
        self.assertEqual(payload["fix"], result)
        self.assertEqual(err.getvalue(), "\nOpening a panel beside System Settings.\n\n")

    def test_json_without_fix_and_for_android(self):
        out = io.StringIO()
        with mock.patch.object(admin, "_doctor_android",
                               lambda: admin._check("adb found", False, "install adb")), \
                contextlib.redirect_stdout(out):
            code = admin.run_doctor("android", as_json=True)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue()), {
            "platform": "android", "ok": False,
            "checks": [{"name": "adb found", "ok": False, "fatal": True, "hint": "install adb"}],
            "permissions": None, "fix": None,
        })


class Cli(unittest.TestCase):
    def dispatch(self, argv):
        with mock.patch("phone_harness.admin.run_doctor", return_value=0) as doctor, \
                mock.patch.dict(os.environ):
            with self.assertRaises(SystemExit) as caught:
                run_main(argv)
        self.assertEqual(caught.exception.code, 0, argv)
        return doctor.call_args

    def test_doctor_flags_reach_the_doctor_in_any_order_and_after_the_old_spelling(self):
        cases = (
            (["doctor", "ios", "--fix"], ("ios", True, False, False)),
            (["doctor", "--fix", "ios"], ("ios", True, False, False)),
            (["doctor", "ios", "--fix", "--json", "--yes"], ("ios", True, True, True)),
            (["doctor", "--non-interactive", "--fix", "ios"], ("ios", True, False, True)),
            (["doctor", "--json"], (None, False, True, False)),
            (["--doctor", "ios", "--fix"], ("ios", True, False, False)),
            (["--doctor=ios", "--fix", "--yes"], ("ios", True, False, True)),
            (["--doctor", "--json"], (None, False, True, False)),
            (["--session", "sid-1", "doctor", "ios", "--fix"], ("ios", True, False, False)),
        )
        for argv, (platform, fix, as_json, noninteractive) in cases:
            self.assertEqual(self.dispatch(argv),
                             mock.call(platform, fix=fix, as_json=as_json, noninteractive=noninteractive),
                             argv)

    def test_there_is_no_permissions_command_and_doctor_rejects_strays(self):
        for argv in (["permissions"], ["permissions", "--guide"], ["doctor", "ios", "extra"],
                     ["doctor", "--guide"], ["doctor", "ios", "--timeout", "5"]):
            err = io.StringIO()
            with mock.patch("phone_harness.admin.run_doctor", side_effect=AssertionError("ran")), \
                    mock.patch("sys.stderr", err):
                with self.assertRaises(SystemExit) as caught:
                    run_main(argv)
            self.assertEqual(caught.exception.code, 2, argv)
            self.assertIn("error:", err.getvalue(), argv)

    def test_doctor_help_explains_fix_and_is_ascii(self):
        got = _cli("doctor", "--help")
        self.assertEqual(got.returncode, 0, got.stderr)
        for needle in ("--fix", "--json", "--yes, --non-interactive", "beside System Settings",
                       "Touch ID", "From a pipe"):
            self.assertIn(needle, got.stdout)
        got.stdout.encode("ascii")
        top = _cli("--help")
        self.assertIn("phone-harness doctor ios --fix", top.stdout)
        self.assertNotIn("permissions [", top.stdout)

    def test_off_macos_doctor_fix_json_is_json_and_does_not_wait(self):
        if sys.platform == "darwin":
            self.skipTest("needs a non-Mac, so nothing can read TCC or open System Settings")
        got = _cli("doctor", "ios", "--fix", "--json")
        self.assertEqual(got.returncode, 1, got.stderr)
        payload = json.loads(got.stdout)
        self.assertEqual(payload["platform"], "ios")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["checks"][0]["name"], "pyobjc frameworks")
        self.assertIsNone(payload["fix"])
        android = _cli("doctor", "android", "--json")
        self.assertEqual(json.loads(android.stdout)["platform"], "android")

    def test_telemetry_names_are_unchanged(self):
        self.assertEqual(_telemetry_command(["doctor", "ios", "--fix"]), "doctor")
        self.assertEqual(_telemetry_command(["--doctor", "ios", "--fix", "--json"]), "doctor")
        self.assertEqual(_telemetry_command(["permissions", "--guide"]), "usage")

    def test_the_probe_and_the_panel_load_neither_telemetry_nor_the_cli(self):
        code = ("import json, sys, phone_harness.permission_panel, phone_harness.permission_probe; "
                "print(json.dumps(sorted(m for m in sys.modules if m.startswith('phone_harness'))))")
        got = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             env=_env(), timeout=60)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(json.loads(got.stdout), [
            "phone_harness", "phone_harness.permission_panel", "phone_harness.permission_probe"])


if __name__ == "__main__":
    unittest.main()
