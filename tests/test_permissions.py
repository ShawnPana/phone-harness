"""macOS permission onboarding. Preflight, deep links, and the guide are mocked."""
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
from phone_harness.run import _telemetry_command, main  # noqa: E402

TERMINAL = "/System/Applications/Utilities/Terminal.app"
TERMINAL_BIN = TERMINAL + "/Contents/MacOS/Terminal"
EDITOR = "/Applications/Editor.app"
HELPER = EDITOR + "/Contents/Helpers/Plugin Host.app/Contents/MacOS/plugin-host"
PYTHON = "/Users/me/.local/share/uv/tools/phone-harness/bin/python"


def _host(kind="app", path=TERMINAL, executable=TERMINAL_BIN, cdhash="host1", name="Terminal"):
    return {"kind": kind, "name": name, "path": path, "executable": executable, "cdhash": cdhash}


def _binary(path=PYTHON, cdhash="bin1", adhoc=False, inside_host=False):
    return {"path": path, "cdhash": cdhash, "adhoc": adhoc, "inside_host": inside_host}


def _report(accessibility, screen, host=None, binary=None, version=(15, 0, 0), grants=None):
    return permissions.build_report(
        accessibility, screen, host or _host(), binary or _binary(), version, grants or {},
    )


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

    def test_walk_stops_at_the_nearest_app_and_keeps_its_outer_bundle(self):
        exes = {10: PYTHON, 20: HELPER, 30: TERMINAL_BIN}
        parents = {10: 20, 20: 30, 30: 1}
        host = permissions.host_target(
            pid=10, parent_of=parents.get, executable_of=exes.get, self_exe=PYTHON,
        )
        self.assertEqual(host["kind"], "app")
        self.assertEqual(host["path"], EDITOR)
        self.assertEqual(host["name"], "Editor")
        self.assertEqual(host["executable"], HELPER)

    def test_walk_passes_a_root_login_and_lands_on_terminal(self):
        exes = {10: PYTHON, 20: "/bin/zsh", 30: "/usr/bin/login", 40: TERMINAL_BIN}
        parents = {10: 20, 20: 30, 30: 40, 40: 1}
        host = permissions.host_target(
            pid=10, parent_of=parents.get, executable_of=exes.get, self_exe=PYTHON,
        )
        self.assertEqual(host["path"], TERMINAL)
        self.assertEqual(host["name"], "Terminal")

    def test_no_app_means_the_python_binary_is_the_grant(self):
        exes = {10: PYTHON, 20: "/bin/zsh"}
        parents = {10: 20, 20: 1}
        host = permissions.host_target(
            pid=10, parent_of=parents.get, executable_of=exes.get, self_exe=PYTHON,
        )
        self.assertEqual(host, {
            "kind": "binary", "name": "python", "path": PYTHON, "executable": PYTHON,
        })

    def test_deep_link_follows_the_os_version(self):
        modern = permissions.settings_url("accessibility", (15, 0, 0))
        self.assertEqual(
            modern,
            "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Accessibility",
        )
        self.assertTrue(permissions.settings_url("screen_recording", (13, 0)).endswith("Privacy_ScreenCapture"))
        legacy = permissions.settings_url("accessibility", (12, 6, 0))
        self.assertTrue(legacy.startswith("x-apple.systempreferences:com.apple.preference.security?"))
        self.assertEqual(permissions.list_name("accessibility", (26, 0)), "Accessibility")
        self.assertEqual(permissions.list_name("accessibility", (27, 0, 0)), "Device Control and Data Access")
        self.assertEqual(permissions.list_name("screen_recording", (27, 0)), "Screen Recording")

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
        saved = permissions.updated_grants({}, report)
        again = permissions.updated_grants(saved, report)
        self.assertEqual(saved, again)
        self.assertEqual(saved["accessibility"]["host_cdhash"], "host1")
        self.assertEqual(saved["accessibility"]["binary_cdhash"], "bin1")

    def test_denied_names_the_app_the_binary_and_the_pane(self):
        report = _report(False, True)
        entry = report["accessibility"]
        self.assertEqual(entry["state"], "denied")
        self.assertIn('turn on "Terminal"', entry["summary"])
        self.assertIn("drag it in", entry["summary"])
        self.assertIn("toggle is already on", entry["summary"])
        self.assertIn("Privacy_Accessibility", entry["suggestion"])
        self.assertIn(PYTHON, permissions.explain(report))
        self.assertIn("phone-harness permissions --guide", permissions.explain(report))
        self.assertNotIn("Screen Recording:", permissions.explain(report))

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
        # A denial that keeps the same signature is a revoked toggle, not a stale grant.
        same = _report(False, True, grants=grants)
        # grants stored host_cdhash old, host cdhash is host1, that IS a change.
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
        self.assertNotIn("lists the Python binary separately", permissions.explain(report))

    def test_no_launching_app_tells_the_user_to_grant_the_binary(self):
        report = _report(False, False, host=_host(kind="binary", path=PYTHON, executable=PYTHON,
                                                  name="python", cdhash="bin1"))
        text = permissions.explain(report)
        self.assertIn("No launching app", text)
        self.assertIn(PYTHON, text)

    def test_unknown_when_the_frameworks_are_missing(self):
        report = _report(None, None)
        self.assertEqual(report["accessibility"]["state"], "unknown")
        self.assertFalse(report["ok"])
        self.assertIn("pyobjc", report["accessibility"]["summary"])
        self.assertEqual(report["accessibility"]["url"], "")

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

    def test_json_status_shape(self):
        report = _report(False, True)
        payload = json.loads(permissions.format_json(report, "timeout"))
        self.assertEqual(payload["accessibility"]["state"], "denied")
        self.assertEqual(payload["screen_recording"]["state"], "granted")
        self.assertEqual(payload["guide"], "timeout")
        self.assertEqual(payload["host"]["name"], "Terminal")
        self.assertFalse(payload["ok"])
        text = permissions.format_text(report)
        self.assertIn("[FAIL] " + permissions.ACCESSIBILITY_LABEL, text)
        self.assertIn("[PASS] " + permissions.SCREEN_RECORDING_LABEL, text)
        text.encode("ascii")


class Guide(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self._env = mock.patch.dict(os.environ, {"PHONE_HARNESS_HOME": self.home.name})
        self._env.start()
        self.addCleanup(self._env.stop)
        self._platform = mock.patch.object(permissions.sys, "platform", "darwin")
        self._platform.start()
        self.addCleanup(self._platform.stop)

    def _run(self, state, **kw):
        opens, said = [], []
        clock = {"t": 0.0}

        def probe(kind):
            return state[kind]

        def sleep(seconds):
            clock["t"] += seconds
            if clock["t"] >= 1:
                state["accessibility"] = True
            if clock["t"] >= 2:
                state["screen_recording"] = True

        def frozen_sleep(seconds):
            clock["t"] += seconds

        options = dict(
            timeout=30, interval=1, opener=opens.append, probe=probe,
            sleep=kw.pop("sleep", sleep), clock=lambda: clock["t"], say=said.append,
            host=_host(), identify=lambda _path: {"cdhash": "bin1", "adhoc": False},
            version=(15, 0, 0),
        )
        options.update(kw)
        # identify must hash the host too. The lambda above ignores path and
        # returns bin1, while the host dict already carries host1.
        name, report = permissions.run_guide(**options)
        return name, report, opens, said

    def test_already_granted_does_not_open_or_wait(self):
        name, report, opens, said = self._run(
            {"accessibility": True, "screen_recording": True}, wait=True, open_pane=True,
        )
        self.assertEqual(name, "not_needed")
        self.assertTrue(report["ok"])
        self.assertEqual(opens, [])
        self.assertEqual(said, [])

    def test_guide_opens_each_missing_pane_and_stops_when_granted(self):
        state = {"accessibility": False, "screen_recording": False}
        name, report, opens, said = self._run(state, wait=True, open_pane=True)
        self.assertEqual(name, "granted")
        self.assertTrue(report["ok"])
        self.assertEqual(len(opens), 2)
        self.assertIn("Privacy_Accessibility", opens[0])
        self.assertIn("Privacy_ScreenCapture", opens[1])
        self.assertTrue(any("Accessibility granted" in line for line in said))
        self.assertTrue(any("Screen Recording granted" in line for line in said))
        saved = json.loads((Path(self.home.name) / "state" / "permissions.json").read_text())
        self.assertEqual(saved["grants"]["accessibility"]["host_path"], TERMINAL)

    def test_timeout_reports_the_pane_still_missing(self):
        clock = {"t": 0.0}

        def sleep(seconds):
            clock["t"] += seconds

        name, report, opens, _said = self._run(
            {"accessibility": False, "screen_recording": False},
            wait=True, open_pane=True, timeout=2, sleep=sleep, clock=lambda: clock["t"],
        )
        self.assertEqual(name, "timeout")
        self.assertFalse(report["ok"])
        self.assertEqual(len(opens), 1)
        self.assertIn("Privacy_Accessibility", opens[0])
        self.assertIn("Timed out", permissions.format_text(report, name))

    def test_noninteractive_does_not_open_or_sleep(self):
        slept = []
        name, report, opens, _said = self._run(
            {"accessibility": False, "screen_recording": True},
            wait=False, open_pane=False, sleep=lambda seconds: slept.append(seconds),
        )
        self.assertEqual(name, "skipped")
        self.assertEqual(opens, [])
        self.assertEqual(slept, [])
        self.assertIn("Non-interactive", permissions.format_text(report, name))

    def test_a_pipe_opens_the_first_missing_pane_and_returns(self):
        slept = []
        name, _report, opens, _said = self._run(
            {"accessibility": True, "screen_recording": False},
            wait=False, open_pane=True, sleep=lambda seconds: slept.append(seconds),
        )
        self.assertEqual(name, "opened")
        self.assertEqual(len(opens), 1)
        self.assertIn("Privacy_ScreenCapture", opens[0])
        self.assertEqual(slept, [])

    def test_a_second_guide_does_not_open_another_pane(self):
        held = permissions.acquire_guide_lock()
        self.assertIsNotNone(held)
        try:
            name, _report, opens, _said = self._run(
                {"accessibility": False, "screen_recording": False},
                wait=True, open_pane=True,
            )
        finally:
            held.close()
        self.assertEqual(name, "already_running")
        self.assertEqual(opens, [])

    def test_off_macos_the_guide_is_not_needed(self):
        self._platform.stop()
        try:
            name, report = permissions.run_guide(wait=True, open_pane=True, opener=lambda _url: (_ for _ in ()).throw(AssertionError("opened")))
        finally:
            self._platform.start()
        self.assertEqual(name, "not_needed")
        self.assertFalse(report["needed"])
        self.assertTrue(report["ok"])


class DoctorAndCli(unittest.TestCase):
    def test_doctor_names_a_stale_grant_and_does_not_open_settings(self):
        report = _report(False, True, host=_host(cdhash="new"), grants={"accessibility": {
            "host_path": TERMINAL, "host_cdhash": "old", "host_kind": "app",
            "binary_path": PYTHON, "binary_cdhash": "bin1",
        }})
        opened = []
        admin._failures.clear()
        buf = io.StringIO()
        with mock.patch.object(permissions, "collect", return_value=report), \
                mock.patch.object(permissions, "open_settings", side_effect=opened.append), \
                mock.patch("sys.stdout", buf):
            admin._check_permissions()
        text = buf.getvalue()
        self.assertIn("[FAIL] " + permissions.ACCESSIBILITY_LABEL, text)
        self.assertIn("stale grant", text)
        self.assertIn("Privacy_Accessibility", text)
        self.assertIn("phone-harness permissions --guide", text)
        self.assertIn(permissions.ACCESSIBILITY_LABEL, admin._failures)
        self.assertNotIn(permissions.SCREEN_RECORDING_LABEL, admin._failures)
        self.assertEqual(opened, [])

    def test_collect_remembers_a_grant_and_flags_the_next_signature_change(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        hashes = {"host": "aaa"}

        def identify(path):
            if path and path.endswith(".app"):
                return {"cdhash": hashes["host"], "adhoc": False}
            return {"cdhash": "bbb", "adhoc": False}

        with mock.patch.dict(os.environ, {"PHONE_HARNESS_HOME": home.name}), \
                mock.patch.object(permissions.sys, "platform", "darwin"):
            first = permissions.collect(
                preflight={"accessibility": True, "screen_recording": True},
                host=_host(cdhash=""), identify=identify, version=(15, 0, 0),
            )
            self.assertTrue(first["ok"])
            hashes["host"] = "ccc"
            second = permissions.collect(
                preflight={"accessibility": False, "screen_recording": True},
                host=_host(cdhash=""), identify=identify, version=(15, 0, 0),
            )
        self.assertEqual(second["accessibility"]["state"], "stale")
        self.assertEqual(second["accessibility"]["reason"], "host_cdhash")

    def test_cli_on_this_machine_is_json_and_does_not_wait(self):
        if sys.platform == "darwin":
            self.skipTest("needs a non-Mac so the command cannot touch TCC")
        env = os.environ.copy()
        env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
        env["PHONE_HARNESS_TELEMETRY"] = "0"
        with tempfile.TemporaryDirectory() as home:
            env["PHONE_HARNESS_HOME"] = home
            got = subprocess.run(
                [sys.executable, "-m", "phone_harness.run", "permissions", "--json"],
                capture_output=True, text=True, env=env, timeout=15,
            )
            guided = subprocess.run(
                [sys.executable, "-m", "phone_harness.run", "permissions", "--guide", "--json"],
                capture_output=True, text=True, env=env, timeout=15,
            )
            quiet = subprocess.run(
                [sys.executable, "-m", "phone_harness.run", "permissions", "--guide", "--yes"],
                capture_output=True, text=True, env=env, timeout=15,
            )
            bad = subprocess.run(
                [sys.executable, "-m", "phone_harness.run", "permissions", "--no-such"],
                capture_output=True, text=True, env=env, timeout=15,
            )
            help_out = subprocess.run(
                [sys.executable, "-m", "phone_harness.run", "permissions", "--help"],
                capture_output=True, text=True, env=env, timeout=15,
            )
        self.assertEqual(got.returncode, 0, got.stderr)
        payload = json.loads(got.stdout)
        self.assertFalse(payload["needed"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["accessibility"]["state"], "not_required")
        self.assertNotIn("guide", payload)
        self.assertEqual(guided.returncode, 0, guided.stderr)
        self.assertEqual(json.loads(guided.stdout)["guide"], "not_needed")
        self.assertEqual(quiet.returncode, 0, quiet.stderr)
        self.assertIn("not required", quiet.stdout)
        self.assertEqual(bad.returncode, 2)
        self.assertIn("--no-such", bad.stderr)
        self.assertNotIn("--probe", help_out.stdout)
        help_out.stdout.encode("ascii")

    def test_yes_and_a_pipe_do_not_wait_when_a_grant_is_missing(self):
        report = _report(False, False)
        with mock.patch.object(permissions.sys, "platform", "darwin"), \
                mock.patch.object(permissions, "run_guide", return_value=("skipped", report)) as guide, \
                mock.patch("sys.stdout", io.StringIO()):
            code = permissions.cli(["--guide", "--non-interactive", "--json"])
        self.assertEqual(code, 1)
        self.assertFalse(guide.call_args.kwargs["wait"])
        self.assertFalse(guide.call_args.kwargs["open_pane"])

        out = io.StringIO()
        with mock.patch.object(permissions.sys, "platform", "darwin"), \
                mock.patch.object(permissions, "run_guide", return_value=("opened", report)) as guide, \
                mock.patch("sys.stdout", out):
            code = permissions.cli(["--guide", "--json", "--timeout", "9"])
        self.assertEqual(code, 1)
        self.assertFalse(guide.call_args.kwargs["wait"])
        self.assertTrue(guide.call_args.kwargs["open_pane"])
        self.assertEqual(guide.call_args.kwargs["timeout"], 9)
        self.assertEqual(json.loads(out.getvalue())["guide"], "opened")

    def test_a_terminal_guide_waits(self):
        report = _report(True, True)

        class Tty(io.StringIO):
            def isatty(self):
                return True

        with mock.patch.object(permissions.sys, "platform", "darwin"), \
                mock.patch.object(permissions, "run_guide", return_value=("granted", report)) as guide, \
                mock.patch("sys.stdout", Tty()):
            code = permissions.cli(["--guide"])
        self.assertEqual(code, 0)
        self.assertTrue(guide.call_args.kwargs["wait"])
        self.assertTrue(guide.call_args.kwargs["open_pane"])

    def test_probe_exits_before_telemetry_and_the_update_check(self):
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", ["phone-harness", "permissions", "--probe", "accessibility"]), \
                mock.patch.object(permissions, "live_preflight",
                                  return_value={"accessibility": True, "screen_recording": False}), \
                mock.patch("phone_harness.telemetry.capture_cli_event") as cap, \
                mock.patch("phone_harness.update.maybe_notify", side_effect=AssertionError("update")), \
                mock.patch("sys.stdout", buf):
            with self.assertRaises(SystemExit) as caught:
                main()
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(buf.getvalue().strip(), "granted")
        cap.assert_not_called()

    def test_probe_words_and_fresh_process_parsing(self):
        with mock.patch.object(permissions, "live_preflight",
                               return_value={"accessibility": False, "screen_recording": None}):
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                self.assertEqual(permissions.emit_probe("accessibility"), 1)
            self.assertEqual(buf.getvalue().strip(), "denied")
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                self.assertEqual(permissions.emit_probe("shell"), 2)

        def fake_run(cmd, **_kw):
            self.assertEqual(cmd[-2:], ["--probe", "screen_recording"])
            return mock.Mock(stdout="noise\ngranted\n", returncode=0)

        with mock.patch.object(permissions.subprocess, "run", fake_run):
            self.assertTrue(permissions.fresh_probe("screen_recording"))
        with mock.patch.object(permissions.subprocess, "run",
                               return_value=mock.Mock(stdout="denied\n", returncode=1)):
            self.assertFalse(permissions.fresh_probe("accessibility"))
        with mock.patch.object(permissions.subprocess, "run",
                               return_value=mock.Mock(stdout="unknown\n", returncode=1)):
            self.assertIsNone(permissions.fresh_probe("accessibility"))

    def test_permissions_is_its_own_telemetry_command(self):
        self.assertEqual(_telemetry_command(["permissions", "--json"]), "permissions")
        self.assertEqual(_telemetry_command(["permissions", "--guide"]), "permissions")


if __name__ == "__main__":
    unittest.main()
