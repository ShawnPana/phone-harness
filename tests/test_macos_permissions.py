"""Responsible-app detection and permission copy. No Mac, no prompts.

    python -m unittest tests.test_macos_permissions
"""
import io
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from phone_harness import macos_permissions as mp  # noqa: E402
from phone_harness.macos_permissions import AppInfo  # noqa: E402

GROK_HELPER = ("/Applications/Grok Bot.app/Contents/Frameworks/"
               "Grok Bot Helper.app/Contents/MacOS/Grok Bot Helper")
GROK_MAIN = "/Applications/Grok Bot.app/Contents/MacOS/Grok Bot"
TERMINAL = "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"


class ResponsibleApp(unittest.TestCase):
    def test_bundles_outermost_first_and_ignores_a_lookalike_filename(self):
        self.assertEqual(
            mp.app_bundles(GROK_HELPER),
            [("/Applications/Grok Bot.app", "Grok Bot"),
             ("/Applications/Grok Bot.app/Contents/Frameworks/Grok Bot Helper.app",
              "Grok Bot Helper")])
        self.assertEqual(mp.app_bundles("/tmp/notes.app.txt"), [])
        self.assertEqual(mp.app_bundles(""), [])
        self.assertEqual(
            mp.app_bundles("/Applications/Grok Bot.app/Contents/MacOS/Grok Bot"),
            [("/Applications/Grok Bot.app", "Grok Bot")])

    def test_electron_helper_names_the_host_app(self):
        # zsh -> Grok Bot Helper -> Grok Bot.app. The first .app on the walk
        # is the nested helper; the row in System Settings is the host.
        chain = ["/opt/homebrew/bin/python3", "/bin/zsh", GROK_HELPER, GROK_MAIN]
        app = mp.select_responsible_app(chain, None)
        self.assertEqual(app.name, "Grok Bot")
        self.assertEqual(app.bundle_path, "/Applications/Grok Bot.app")
        self.assertEqual(app.helper_name, "Grok Bot Helper")

    def test_tcc_responsible_path_wins_over_an_earlier_helper(self):
        chain = ["/usr/bin/python3", "/bin/zsh", GROK_HELPER]
        app = mp.select_responsible_app(chain, GROK_MAIN)
        self.assertEqual(app.name, "Grok Bot")
        self.assertIsNone(app.helper_name)
        self.assertEqual(app.bundle_path, "/Applications/Grok Bot.app")

    def test_terminal_and_no_app(self):
        app = mp.select_responsible_app(
            ["/usr/bin/python3", "/bin/zsh", TERMINAL, "/sbin/launchd"], None)
        self.assertEqual(app.name, "Terminal")
        self.assertEqual(app.bundle_path,
                         "/System/Applications/Utilities/Terminal.app")
        self.assertIsNone(app.helper_name)
        self.assertIsNone(mp.select_responsible_app(
            ["/usr/bin/python3", "/sbin/launchd"], None))
        self.assertIsNone(mp.select_responsible_app([], None))
        self.assertIsNone(mp.select_responsible_app([], ""))

    def test_responsible_python_falls_back_to_the_chain(self):
        app = mp.select_responsible_app(
            ["/usr/bin/python3", "/Applications/iTerm.app/Contents/MacOS/iTerm2"],
            "/usr/bin/python3")
        self.assertEqual(app.name, "iTerm")
        self.assertIsNone(app.helper_name)

    def test_chain_stops_at_launchd_and_on_a_cycle(self):
        parents = {10: 9, 9: 8, 8: 1}
        paths = {10: "/usr/bin/python3", 9: "/bin/zsh", 8: TERMINAL, 1: "/sbin/launchd"}
        chain = mp.collect_executable_chain(
            10, lambda p: parents.get(p, 0), lambda p: paths.get(p, ""))
        self.assertEqual(chain, ["/usr/bin/python3", "/bin/zsh", TERMINAL, "/sbin/launchd"])

        parents = {5: 4, 4: 5}
        paths = {5: "/a", 4: "/b"}
        chain = mp.collect_executable_chain(
            5, lambda p: parents.get(p, 0), lambda p: paths.get(p, ""))
        self.assertEqual(chain, ["/a", "/b"])

    def test_display_name_from_plist_and_folder_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            bundle = Path(d) / "Grok Bot.app"
            contents = bundle / "Contents"
            contents.mkdir(parents=True)
            (contents / "Info.plist").write_bytes(plistlib.dumps({
                "CFBundleDisplayName": "Grok",
                "CFBundleName": "Grok Bot",
            }))
            self.assertEqual(mp.bundle_display_name(str(bundle)), "Grok")
            helper = bundle / "Contents" / "Frameworks" / "Grok Bot Helper.app"
            (helper / "Contents").mkdir(parents=True)
            app = mp.with_display_names(AppInfo(
                "Grok Bot", str(bundle), "Grok Bot Helper", str(helper)))
            self.assertEqual(app.name, "Grok")
            # Helper plist is missing, so the folder name is kept — and it
            # differs from the host's display name, so it stays in the hint.
            self.assertEqual(app.helper_name, "Grok Bot Helper")
        self.assertEqual(mp.bundle_display_name("/Applications/Missing.app"), "Missing")


class Messages(unittest.TestCase):
    def setUp(self):
        self.grok = AppInfo(
            "Grok Bot", "/Applications/Grok Bot.app",
            "Grok Bot Helper",
            "/Applications/Grok Bot.app/Contents/Frameworks/Grok Bot Helper.app")
        self.terminal = AppInfo("Terminal", "/System/Applications/Utilities/Terminal.app")

    def test_known_app_is_named_and_the_terminal_is_not_assumed(self):
        hint = mp.failure_hint(mp.ACCESSIBILITY, self.grok, fix=False)
        self.assertNotIn("your terminal", hint)
        self.assertNotIn("enable your terminal", hint)
        self.assertIn("Grok Bot", hint)
        self.assertIn("/Applications/Grok Bot.app", hint)
        self.assertIn("Grok Bot Helper", hint)
        self.assertIn("Privacy_Accessibility", hint)
        self.assertIn("Cmd-Q", hint)
        self.assertIn("remove it", hint)
        self.assertIn("phone-harness --doctor ios --fix", hint)

    def test_screen_recording_restart_has_no_accessibility_remove_step(self):
        hint = mp.failure_hint(mp.SCREEN_RECORDING, self.terminal, fix=True)
        self.assertIn('"Terminal"', hint)
        self.assertIn("Privacy_ScreenCapture", hint)
        self.assertIn("Cmd-Q", hint)
        self.assertNotIn("remove it", hint)
        self.assertNotIn("--fix", hint)

    def test_unknown_app_does_not_pretend_it_is_the_terminal(self):
        hint = mp.failure_hint(mp.ACCESSIBILITY, None, fix=False)
        self.assertIn("the app that launched phone-harness", hint)
        self.assertIn("agent app", hint)
        self.assertNotIn("enable your terminal", hint)
        self.assertIn("Privacy_Accessibility", hint)

    def test_blank_capture_says_restart_when_the_preflight_already_passed(self):
        blank = mp.capture_blank_hint(self.grok, preflight_ok=True)
        self.assertIn("looks granted", blank)
        self.assertIn("Grok Bot", blank)
        self.assertNotIn("your terminal", blank)
        self.assertNotIn("enable your terminal", blank)
        missing = mp.capture_blank_hint(None, preflight_ok=False)
        self.assertIn("Privacy_ScreenCapture", missing)

    def test_script_warning_names_the_app(self):
        text = mp.script_warning(["Accessibility", "Screen Recording"], self.grok)
        self.assertIn("Grok Bot", text)
        self.assertIn("Accessibility and Screen Recording", text)
        self.assertNotIn("your terminal", text)

    def test_settings_urls(self):
        self.assertTrue(mp.settings_url(mp.ACCESSIBILITY).endswith("Privacy_Accessibility"))
        self.assertTrue(mp.settings_url(mp.SCREEN_RECORDING).endswith("Privacy_ScreenCapture"))


class ResolvePermissions(unittest.TestCase):
    def _run(self, *, fix, interactive, ax, screen):
        events = []
        state = {"ax": ax, "screen": screen}

        def trusted(key):
            return lambda: state[key]

        def poll(check, timeout):
            events.append(("poll", timeout))
            return check()

        out = mp.resolve_permissions(
            fix, interactive,
            app=AppInfo("Cursor", "/Applications/Cursor.app"),
            ax_trusted=trusted("ax"),
            screen_trusted=trusted("screen"),
            request_ax=lambda: events.append("req-ax"),
            request_screen=lambda: events.append("req-sc"),
            open_pane=lambda kind: events.append("open-" + kind),
            poll=poll,
            echo=lambda line: events.append(("echo", line)),
        )
        return out, events, state

    def test_requests_only_what_is_missing_and_does_not_wait_without_fix(self):
        out, events, _ = self._run(fix=False, interactive=False, ax=False, screen=True)
        self.assertEqual(events[0], "req-ax")
        self.assertNotIn("req-sc", events)
        self.assertFalse(any(isinstance(e, tuple) and e[0] == "poll" for e in events))
        self.assertFalse(any(isinstance(e, str) and e.startswith("open-") for e in events))
        self.assertFalse(out.accessibility)
        self.assertTrue(out.screen_recording)
        self.assertTrue(out.prompt_shown)
        self.assertFalse(out.granted_after_prompt)
        self.assertEqual(out.prompted, (mp.ACCESSIBILITY,))
        echo = next(e[1] for e in events if isinstance(e, tuple) and e[0] == "echo")
        self.assertIn("Cursor", echo)
        self.assertIn("Accessibility", echo)

    def test_both_prompts_fire_before_any_wait(self):
        out, events, _ = self._run(fix=True, interactive=True, ax=False, screen=False)
        self.assertEqual(events[:2], ["req-ax", "req-sc"])
        polls = [e for e in events if isinstance(e, tuple) and e[0] == "poll"]
        self.assertEqual(polls, [("poll", mp.FIX_WAIT_S), ("poll", mp.FIX_WAIT_S)])
        self.assertIn("open-accessibility", events)
        self.assertIn("open-screen_recording", events)
        self.assertLess(events.index("req-sc"), events.index("open-accessibility"))
        self.assertFalse(out.granted_after_prompt)

    def test_fix_without_a_terminal_uses_the_short_wait(self):
        _, events, _ = self._run(fix=True, interactive=False, ax=True, screen=False)
        polls = [e for e in events if isinstance(e, tuple) and e[0] == "poll"]
        self.assertEqual(polls, [("poll", mp.FIX_WAIT_NONINTERACTIVE_S)])
        echo = next(e[1] for e in events if isinstance(e, tuple) and e[0] == "echo")
        self.assertIn("Not a terminal", echo)
        self.assertNotIn(str(int(mp.FIX_WAIT_S)), echo)

    def test_a_grant_during_the_wait_counts_as_granted_afterward(self):
        state = {"ax": False}

        def poll(check, timeout):
            state["ax"] = True
            return check()

        out = mp.resolve_permissions(
            True, True,
            app=None,
            ax_trusted=lambda: state["ax"],
            screen_trusted=lambda: True,
            request_ax=lambda: None,
            request_screen=lambda: None,
            open_pane=lambda kind: None,
            poll=poll,
            echo=lambda line: None,
        )
        self.assertTrue(out.accessibility)
        self.assertTrue(out.granted_after_prompt)
        self.assertEqual(out.prompted, (mp.ACCESSIBILITY,))

    def test_nothing_missing_does_not_prompt(self):
        out, events, _ = self._run(fix=True, interactive=True, ax=True, screen=True)
        self.assertEqual(events, [])
        self.assertFalse(out.prompt_shown)
        self.assertIsNone(out.granted_after_prompt)
        self.assertTrue(out.accessibility and out.screen_recording)

    def test_poll_until_uses_the_injected_clock(self):
        class Clock:
            def __init__(self):
                self.t = 0

            def __call__(self):
                return self.t

            def sleep(self, seconds):
                self.t += seconds

        clock = Clock()
        calls = {"n": 0}

        def check():
            calls["n"] += 1
            return calls["n"] >= 3

        self.assertTrue(mp.poll_until(check, 10, interval_s=1, sleep=clock.sleep, clock=clock))
        self.assertEqual(clock.t, 2)

        clock = Clock()
        self.assertFalse(mp.poll_until(lambda: False, 2, interval_s=1,
                                        sleep=clock.sleep, clock=clock))
        self.assertGreaterEqual(clock.t, 2)


class OffMacAndDoctor(unittest.TestCase):
    def test_script_prompt_is_once_and_quiet_suppresses_it(self):
        app = AppInfo("Cursor", "/Applications/Cursor.app")
        mp.reset_for_tests()
        buf = io.StringIO()
        with mock.patch.object(mp, "accessibility_trusted", return_value=False), \
                mock.patch.object(mp, "screen_capture_access", return_value=True), \
                mock.patch.object(mp, "request_accessibility") as req_ax, \
                mock.patch.object(mp, "request_screen_capture") as req_sc, \
                mock.patch.object(mp, "responsible_app", return_value=app), \
                mock.patch.object(mp.sys, "platform", "darwin"), \
                mock.patch.object(mp.sys, "stderr", buf):
            mp.prompt_if_missing()
            mp.prompt_if_missing()
        self.assertEqual(req_ax.call_count, 1)
        req_sc.assert_not_called()
        self.assertIn('"Cursor"', buf.getvalue())
        self.assertNotIn("Screen Recording", buf.getvalue())
        self.assertNotIn("your terminal", buf.getvalue())

        mp.reset_for_tests()
        buf = io.StringIO()
        with mock.patch.object(mp, "accessibility_trusted", return_value=False), \
                mock.patch.object(mp, "screen_capture_access", return_value=False), \
                mock.patch.object(mp, "request_accessibility"), \
                mock.patch.object(mp, "request_screen_capture"), \
                mock.patch.object(mp.sys, "platform", "darwin"), \
                mock.patch.object(mp.sys, "stderr", buf):
            mp.set_quiet(True)
            mp.prompt_if_missing()
        self.assertEqual(buf.getvalue(), "")
        mp.reset_for_tests()

    def test_ops_that_need_a_prompt(self):
        self.assertTrue(mp.op_needs_permission_prompt("input.tap"))
        self.assertTrue(mp.op_needs_permission_prompt("screen.capture"))
        self.assertTrue(mp.op_needs_permission_prompt("session.state"))
        self.assertFalse(mp.op_needs_permission_prompt("screen.bounds"))

    def test_requests_do_nothing_off_macos(self):
        if sys.platform == "darwin":
            self.skipTest("must not raise a real TCC prompt")
        mp.reset_for_tests()
        with mock.patch("phone_harness.macos_permissions.subprocess.Popen") as popen:
            self.assertFalse(mp.request_accessibility())
            mp.request_screen_capture()
            mp.request_screen_capture()
            popen.assert_not_called()
        self.assertFalse(mp.open_settings(mp.ACCESSIBILITY))
        self.assertIsNone(mp.responsible_app())
        self.assertFalse(mp.accessibility_trusted())
        self.assertFalse(mp.screen_capture_access())
        buf = io.StringIO()
        with mock.patch("sys.stderr", buf):
            mp.prompt_if_missing()
        self.assertEqual(buf.getvalue(), "")

    def test_explain_capture_off_macos_names_no_terminal(self):
        if sys.platform == "darwin":
            self.skipTest("responsible_app would inspect this process")
        text = mp.explain_capture_failure("window capture failed after 3 tries: empty")
        self.assertIn("window capture failed after 3 tries", text)
        self.assertIn("the app that launched phone-harness", text)
        self.assertNotIn("enable your terminal", text)
        ax = mp.explain_accessibility_failure("focus_probe() could not read AXFrontmost.")
        self.assertIn("Privacy_Accessibility", ax)
        self.assertIn("Cmd-Q", ax)


class DoctorCli(unittest.TestCase):
    def test_parse_doctor_args(self):
        from phone_harness.run import parse_doctor_args
        self.assertEqual(parse_doctor_args([]), (None, False))
        self.assertEqual(parse_doctor_args(["ios"]), ("ios", False))
        self.assertEqual(parse_doctor_args(["--fix"]), (None, True))
        self.assertEqual(parse_doctor_args(["ios", "--fix"]), ("ios", True))
        self.assertEqual(parse_doctor_args(["--fix", "android"]), ("android", True))
        err = io.StringIO()
        with mock.patch("sys.stderr", err), self.assertRaises(SystemExit) as raised:
            parse_doctor_args(["--wait"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("unknown option", err.getvalue())
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            parse_doctor_args(["ios", "android"])

    def test_usage_mentions_fix(self):
        from phone_harness.run import USAGE
        self.assertIn("--fix", USAGE)
        self.assertIn("does not hang", USAGE)

    def test_check_records_stable_steps(self):
        from phone_harness import admin
        admin._failures.clear()
        admin._failed_steps.clear()
        with mock.patch("sys.stdout", io.StringIO()):
            admin._check("Accessibility permission (taps & keystrokes)", False,
                         "hint", step="accessibility")
            admin._check("iPhone Mirroring running", False, "hint", fatal=False,
                         step="mirroring_running")
        self.assertEqual(admin._failed_steps, ["accessibility", "mirroring_running"])
        self.assertEqual(admin._failures, ["Accessibility permission (taps & keystrokes)"])

    def test_android_fix_does_not_wait_or_prompt(self):
        from phone_harness import admin
        buf = io.StringIO()
        with mock.patch("phone_harness.admin.shutil.which", return_value=None), \
                mock.patch("sys.stdout", buf):
            code = admin.run_doctor("android", fix=True)
        self.assertEqual(code, 1)
        text = buf.getvalue()
        self.assertIn("does not change Android checks", text)
        self.assertIn("[FAIL] adb found", text)
        self.assertNotIn("your terminal", text)
        self.assertEqual(admin.last_report["failed_steps"], ["adb"])
        self.assertFalse(admin.last_report["prompt_shown"])
        self.assertIsNone(admin.last_report["granted_after_prompt"])

    def test_ios_doctor_without_pyobjc_fails_cleanly(self):
        if sys.platform == "darwin":
            self.skipTest("would request real TCC prompts")
        from phone_harness import admin
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            code = admin.run_doctor("ios", fix=True)
        self.assertEqual(code, 1)
        self.assertIn("pyobjc", buf.getvalue())
        self.assertEqual(admin.last_report["failed_steps"], ["pyobjc"])
        self.assertFalse(admin.last_report["prompt_shown"])
        self.assertNotIn("Waiting up to", buf.getvalue())

    def test_doctor_fields_stay_anonymous(self):
        from phone_harness import admin
        from phone_harness.run import _doctor_fields
        admin.last_report = None
        self.assertEqual(_doctor_fields(), {})
        admin.last_report = {
            "failed_steps": ["accessibility", "screen_recording", "window_capture"],
            "prompt_shown": True,
            "granted_after_prompt": False,
        }
        fields = _doctor_fields()
        self.assertEqual(fields["doctor_failed_steps"],
                         ["accessibility", "screen_recording", "window_capture"])
        self.assertTrue(fields["permission_prompt_shown"])
        self.assertFalse(fields["permission_granted_after_prompt"])
        blob = str(fields)
        self.assertNotIn("Grok", blob)
        self.assertNotIn("/", blob)
        admin.last_report = None


class TelemetryPayload(unittest.TestCase):
    def test_doctor_properties_are_added_and_ordinary_events_omit_them(self):
        from phone_harness import telemetry
        sent = []
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict("os.environ", {"PHONE_HARNESS_HOME": d}), \
                mock.patch.object(telemetry, "is_enabled", return_value=True), \
                mock.patch.object(telemetry, "_send_detached", side_effect=sent.append):
            telemetry.capture_cli_event(
                action="error", command="doctor", exit_code=1,
                doctor_failed_steps=["accessibility"],
                permission_prompt_shown=True,
                permission_granted_after_prompt=False,
            )
            telemetry.capture_cli_event(action="completed", command="script", exit_code=0)
        doctor, script = sent
        self.assertEqual(doctor["properties"]["doctor_failed_steps"], ["accessibility"])
        self.assertTrue(doctor["properties"]["permission_prompt_shown"])
        self.assertFalse(doctor["properties"]["permission_granted_after_prompt"])
        self.assertNotIn("responsible_app", doctor["properties"])
        self.assertNotIn("doctor_failed_steps", script["properties"])
        self.assertNotIn("permission_prompt_shown", script["properties"])


if __name__ == "__main__":
    unittest.main()
