"""session.require on iPhone Mirroring does the user's first two moves —
open the app, press Connect — and no more. No phone needed: the mirroring
transport is faked and the clock does not tick.

    python -m unittest tests.test_ios_session
"""
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

try:
    from phone_harness import ios
except ImportError:                      # no pyobjc: stub the frameworks so the
    ios = None                           # session machine can still be tested

if ios is None:
    import types

    class _LazyModule(types.ModuleType):
        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            obj = type(name, (), {})
            setattr(self, name, obj)
            return obj

    import importlib.machinery
    for _name in ("Quartz", "Vision", "Foundation", "AppKit",
                  "ApplicationServices", "objc", "CoreFoundation"):
        if _name in sys.modules:
            continue
        _mod = _LazyModule(_name)
        _mod.__spec__ = importlib.machinery.ModuleSpec(_name, loader=None)
        sys.modules[_name] = _mod
    try:
        from phone_harness import ios
    except ImportError:
        ios = None

WIN = {"x": 0, "y": 0, "w": 400, "h": 800, "id": 1}
CONNECT = [("AXImage", "iPhoneOff"), ("AXStaticText", "Timed Out"),
           ("AXStaticText", "iPhone Mirroring timed out due to iPhone use "
                            "while connecting.\n\nLock your iPhone before "
                            "connecting."), ("AXButton", "Connect")]
IN_USE = [("AXStaticText", "iPhone in Use"),
          ("AXStaticText", "Lock your iPhone to continue."),
          ("AXButton", "Try Again")]
CONNECTING = [("AXStaticText", "Connecting to"),
              ("AXStaticText", "iPhone 17 Pro Max"), ("AXButton", "Background")]
# The Connecting screen from a live attempt: the words, and no button at all.
# Treating "no Background button" as a finished failure gave up while this
# was still on screen.
CONNECTING_PLAIN = [("AXStaticText", "Connecting to"),
                    ("AXStaticText", "iPhone 17 Pro Max")]
# Cold launch draws the phone window before any label. Accessibility sees
# a node, so the session is blocked, but there is nothing to read and
# nothing to press. That is not a finished interstitial.
BLANK = [("AXImage", "")]
IN_USE_RETRYING = [("AXStaticText", "iPhone in Use"),
                   ("AXStaticText", "Lock your iPhone to connect.")]
LOGIN = [("AXStaticText", "Enter the Mac login password"),
         ("AXSecureTextField", ""), ("AXButton", "Unlock")]
# macOS draws this sheet over the mirroring window. Blocked is correct.
# The words are a Mac password prompt, not "lock your iPhone".
TOUCH_ID = [("AXStaticText", "Passwords"),
            ("AXStaticText", "Authenticate to access Passwords. Touch ID or "
                             "enter your password to continue."),
            ("AXButton", "Home Screen"), ("AXButton", "App Switcher"),
            ("AXButton", "Cancel")]
LIVE = []


class FakeMirror:
    """Scripted: each session-state probe pops the next frame. A frame is
    None (app not running), "no-window", or the AX content of the window."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.current = self.frames[0]
        self.launched = 0
        self.pressed = []
        self.activated = 0
        self.actions = []

    def _advance(self):
        if len(self.frames) > 1:
            self.frames.pop(0)
        self.current = self.frames[0]

    def running_app(self):
        self._advance()
        return None if self.current is None else object()

    def find_window(self, on_screen=False):
        return None if self.current in (None, "no-window") else dict(WIN)

    def window_ax_content(self):
        return [] if self.current in (None, "no-window") else list(self.current)

    def window_ax_buttons(self):
        return [(t, object()) for r, t in self.window_ax_content()
                if r == "AXButton"]

    def press_window_button(self, titles=None):
        buttons = [t for t, _ in self.window_ax_buttons()]
        if titles:
            buttons = [t for t in buttons if t.lower() in
                       {x.lower() for x in titles}]
        elif len(buttons) != 1:
            return None
        if not buttons:
            return None
        self.pressed.append(buttons[0])
        return buttons[0]

    def launch(self, timeout=12.0):
        self.launched += 1
        return self.find_window()

    def capture(self, path=None):
        self.actions.append("capture")
        return "/dev/null", dict(WIN)

    def ax_session_hint(self):
        """Same one-step advance as running_app(), then the structural state.
        A live frame is []; anything else with text is an interstitial."""
        self.running_app()
        if self.current is None:
            return "not-running"
        if self.current == "no-window":
            return "no-window"
        if self.current:
            return "blocked"
        return "ready"

    def tap(self, x, y):
        self.actions.append("tap")

    def long_press(self, x, y, duration=0.8):
        self.actions.append("long_press")

    def drag(self, x1, y1, x2, y2, duration=0.35, steps=14):
        self.actions.append("drag")

    def scroll_wheel(self, dy, x, y, steps=6, dx=0):
        self.actions.append("scroll")

    def press(self, combo):
        self.actions.append("press")

    def type_text(self, text, delay=0.03, keystrokes=False):
        self.actions.append("type")

    def activate(self):
        self.activated += 1

    def show(self):
        self.shown = getattr(self, "shown", 0) + 1

    def ensure_window(self, timeout=5.0):
        return self.find_window()


class FakeVision:
    @staticmethod
    def recognize(path, win):
        return []


@unittest.skipIf(ios is None, "pyobjc not installed")
class SessionRequire(unittest.TestCase):
    def setUp(self):
        self._sleep, self._now, self._vision = ios._sleep, ios._now, ios._vision
        self._open, self._slice = ios._ax_watch_open, ios._run_loop_slice
        self._drain = ios._drain_run_loop
        self.clock = [0.0]
        ios._sleep = lambda s: self.clock.__setitem__(0, self.clock[0] + s)
        ios._now = lambda: self.clock[0]
        ios._vision = FakeVision

    def tearDown(self):
        ios._sleep, ios._now, ios._vision = self._sleep, self._now, self._vision
        ios._ax_watch_open, ios._run_loop_slice = self._open, self._slice
        ios._drain_run_loop = self._drain

    def watch_events(self, notify):
        """A fake observer. Each run-loop slice advances the fake clock and,
        when `notify` is true, delivers one accessibility notification."""
        holder = {}

        class Watch:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        def opener(mirror, on_notify):
            watch = Watch()
            holder["watch"] = watch
            holder["on_notify"] = on_notify
            return watch

        def slice(seconds):
            self.clock[0] += seconds
            if notify:
                holder["on_notify"]()

        ios._ax_watch_open = opener
        ios._run_loop_slice = slice
        return holder

    def phone(self, *frames):
        p = ios.IPhone.__new__(ios.IPhone)
        p.mirror = FakeMirror(frames)
        p.background = True
        return p

    def test_ready_does_nothing(self):
        p = self.phone(LIVE)
        self.assertEqual(p._session_require(), WIN)
        self.assertEqual(p.mirror.launched, 0)
        self.assertEqual(p.mirror.pressed, [])
        self.assertEqual(p.mirror.shown, 1, "the window is shown once")

    def test_launches_then_presses_connect_then_ready(self):
        p = self.phone(None, None, "no-window", CONNECT, CONNECT,
                       CONNECTING, CONNECTING, LIVE)
        self.assertEqual(p._session_require(), WIN)
        self.assertEqual(p.mirror.launched, 1)
        self.assertEqual(p.mirror.pressed, ["Connect"])

    def test_phone_in_use_presses_once_and_raises_with_the_screen(self):
        p = self.phone(CONNECT, CONNECT, CONNECTING, CONNECTING, CONNECTING,
                       CONNECTING, CONNECTING, CONNECTING, CONNECTING,
                       CONNECTING, IN_USE)
        with self.assertRaises(RuntimeError) as cm:
            p._session_require()
        msg = str(cm.exception)
        self.assertIn("pressed Connect", msg)
        self.assertIn("iPhone in Use", msg)
        self.assertIn("LOCK your iPhone", msg)
        self.assertEqual(p.mirror.pressed, ["Connect"])
        self.assertLess(self.clock[0], ios._CONNECT_WAIT, "stopped before the cap")

    def test_transient_ready_after_the_press_is_not_a_connection(self):
        # Measured: the window reads empty for ~0.5s between the press and
        # the Connecting screen. One such poll must not count as live.
        p = self.phone(CONNECT, CONNECT, LIVE, CONNECTING, CONNECTING,
                       IN_USE_RETRYING, IN_USE_RETRYING, IN_USE)
        with self.assertRaises(RuntimeError) as cm:
            p._session_require()
        self.assertIn("iPhone in Use", str(cm.exception))
        self.assertEqual(p.mirror.pressed, ["Connect"])

    def test_a_slow_connect_is_waited_for(self):
        p = self.phone(CONNECT, CONNECT, CONNECTING, CONNECTING, CONNECTING,
                       CONNECTING, CONNECTING, CONNECTING, CONNECTING, LIVE)
        self.assertEqual(p._session_require(), WIN)
        self.assertEqual(p.mirror.pressed, ["Connect"])

    def test_in_use_fails_fast_and_shows_the_window_first(self):
        # The app says "iPhone in Use" ~2s after the press and retries by
        # itself; waiting out the cap would only delay the user's lock.
        p = self.phone(CONNECT, CONNECT, CONNECTING, CONNECTING,
                       IN_USE_RETRYING)
        with self.assertRaises(RuntimeError) as cm:
            p._session_require()
        msg = str(cm.exception)
        self.assertIn("iPhone in Use", msg)
        self.assertIn("LOCK your iPhone", msg)
        self.assertNotIn("..", msg)
        self.assertLess(self.clock[0], 5.0, "gave up within a few seconds")
        self.assertEqual(p.mirror.shown, 1, "the window was raised even so")

    def test_connecting_screen_background_button_is_not_pressed(self):
        p = self.phone(CONNECTING)
        with self.assertRaises(RuntimeError) as cm:
            p._session_require()
        self.assertEqual(p.mirror.pressed, [])
        msg = str(cm.exception)
        self.assertIn("Connecting to", msg)
        self.assertIn("clear this on the Mac", msg)
        self.assertNotIn("LOCK your iPhone", msg)
        self.assertGreaterEqual(self.clock[0], ios._CONNECT_WAIT,
                                "a Connecting screen is waited out to the cap")

    def test_launch_blank_then_connecting_then_live(self):
        # The blank window comes first, then "Connecting to", then the stream.
        # Raising on the blank ("an interstitial with no text") gave up
        # while the phone was still connecting.
        p = self.phone(None, None, BLANK, BLANK,
                       *([CONNECTING_PLAIN] * 6), LIVE)
        self.assertEqual(p._session_require(), WIN)
        self.assertEqual(p.mirror.launched, 1)
        self.assertEqual(p.mirror.pressed, [])
        self.assertGreater(self.clock[0], ios._ATTEMPT_SETTLE)
        self.assertLess(self.clock[0], ios._CONNECT_WAIT)

    def test_launch_blank_for_the_whole_cap_raises(self):
        p = self.phone(None, None, BLANK)
        with self.assertRaises(RuntimeError) as cm:
            p._session_require()
        msg = str(cm.exception)
        self.assertIn("an interstitial with no text", msg)
        self.assertIn("clear this on the Mac", msg)
        self.assertNotIn("..", msg)
        self.assertEqual(p.mirror.launched, 1)
        self.assertEqual(p.mirror.pressed, [])
        self.assertGreaterEqual(self.clock[0], ios._CONNECT_WAIT)
        self.assertLess(self.clock[0], ios._CONNECT_WAIT + 1)

    def test_launch_into_connecting_with_no_button_waits_then_is_ready(self):
        # Cold start. The app is not running; once launched it connects by
        # itself and the window says "Connecting to" with no Connect button.
        # That used to raise in about a second. Wait, and the stream comes up.
        p = self.phone(None, None, *([CONNECTING_PLAIN] * 6), LIVE)
        self.assertEqual(p._session_require(), WIN)
        self.assertEqual(p.mirror.launched, 1)
        self.assertEqual(p.mirror.pressed, [])
        self.assertGreater(self.clock[0], ios._ATTEMPT_SETTLE)
        self.assertLess(self.clock[0], ios._CONNECT_WAIT)

    def test_login_prompt_is_never_pressed(self):
        p = self.phone(LOGIN)
        with self.assertRaises(RuntimeError) as cm:
            p._session_require()
        msg = str(cm.exception)
        self.assertEqual(p.mirror.pressed, [])
        self.assertIn("login password", msg)
        self.assertIn("Touch ID or a password", msg)
        self.assertIn("never type a password", msg)
        self.assertNotIn("LOCK your iPhone", msg)

    def test_touch_id_sheet_asks_the_user_to_approve_or_cancel(self):
        p = self.phone(TOUCH_ID)
        with self.assertRaises(RuntimeError) as cm:
            p._session_require()
        msg = str(cm.exception)
        self.assertEqual(p.mirror.pressed, [])
        self.assertIn("Touch ID", msg)
        self.assertIn("Approve it or cancel it", msg)
        self.assertIn("never type a password", msg)
        self.assertIn("screenshot()", msg)
        self.assertNotIn("LOCK your iPhone", msg)
        self.assertNotIn("..", msg)

    def test_launch_without_a_window_raises(self):
        p = self.phone(None, None, "no-window")
        with self.assertRaises(RuntimeError) as cm:
            p._session_require()
        self.assertEqual(p.mirror.launched, 1)
        self.assertIn("no phone window", str(cm.exception))

    def test_connecting_without_a_button_keeps_waiting_then_connects(self):
        # No notifications arrive. The once-a-second re-check must still see
        # "Connecting to" (there is no Background button) and wait it out.
        holder = self.watch_events(notify=False)
        p = self.phone(CONNECT, CONNECT, *([CONNECTING_PLAIN] * 6), LIVE)
        self.assertEqual(p._session_require(), WIN)
        self.assertEqual(p.mirror.pressed, ["Connect"])
        self.assertGreater(self.clock[0], ios._ATTEMPT_SETTLE)
        self.assertLess(self.clock[0], ios._CONNECT_WAIT)
        self.assertTrue(holder["watch"].closed)

    def test_iphone_in_use_fails_fast_on_an_accessibility_event(self):
        holder = self.watch_events(notify=True)
        p = self.phone(CONNECT, CONNECT, CONNECTING_PLAIN, IN_USE)
        with self.assertRaises(RuntimeError) as cm:
            p._session_require()
        self.assertIn("iPhone in Use", str(cm.exception))
        self.assertIn("pressed Connect", str(cm.exception))
        self.assertLess(self.clock[0], 5.0, "gave up within a few seconds")
        self.assertGreater(self.clock[0], ios._ATTEMPT_SETTLE)
        self.assertTrue(holder["watch"].closed)
        self.assertEqual(p.mirror.pressed, ["Connect"])

    def test_observer_setup_failure_falls_back_and_waits_out_connecting(self):
        # Observer setup fails closed. Polling then treats the button-less
        # Connecting screen as still in progress and waits for the stream.
        def boom(mirror, on_notify):
            raise ios._ObserverUnavailable("AXObserverCreate failed")

        ios._ax_watch_open = boom
        p = self.phone(CONNECT, CONNECT, *([CONNECTING_PLAIN] * 5), LIVE)
        self.assertEqual(p._session_require(), WIN)
        self.assertEqual(p.mirror.pressed, ["Connect"])
        self.assertGreater(self.clock[0], ios._ATTEMPT_SETTLE)
        self.assertLess(self.clock[0], ios._CONNECT_WAIT)

    def test_scroll_raises_once_the_window_is_iphone_in_use(self):
        p = self.phone(IN_USE)
        with self.assertRaises(RuntimeError) as cm:
            p.send("input.scroll", x=1, y=1, dy=-10, dx=0)
        msg = str(cm.exception)
        self.assertIn("iPhone in Use", msg)
        self.assertIn("Try Again", msg)
        self.assertIn("LOCK your iPhone", msg)
        self.assertIn("passcode", msg)
        self.assertIn("screenshot()", msg)
        self.assertEqual(p.mirror.pressed, [])
        self.assertNotIn("scroll", p.mirror.actions)

    def test_a_scroll_that_loses_the_phone_raises_afterwards(self):
        # Before the gesture the stream is live. The gesture runs. The
        # window is then "iPhone in Use", so the scroll does not return
        # success, and Connect is not pressed.
        p = self.phone(LIVE, LIVE, IN_USE)
        with self.assertRaises(RuntimeError) as cm:
            p.send("input.scroll", x=1, y=1, dy=-10, dx=0)
        self.assertIn("iPhone in Use", str(cm.exception))
        self.assertEqual(p.mirror.actions, ["scroll"])
        self.assertEqual(p.mirror.pressed, [])

    def test_live_actions_are_not_interrupted(self):
        p = self.phone(LIVE)
        p.send("input.scroll", x=1, y=1, dy=-10, dx=0)
        p.send("input.tap", x=1, y=2)
        p.send("input.press", x=1, y=2, duration=0.2)
        p.send("input.drag", x1=0, y1=0, x2=3, y2=4)
        p.send("input.keys", combo="return")
        p.send("input.text", s="hi")
        p.send("nav.home")
        p.send("apps.launch", name="Settings")
        p.send("screen.capture")
        self.assertEqual(
            p.mirror.actions,
            ["scroll", "tap", "long_press", "drag", "press", "type",
             "press", "press", "press", "type", "press", "capture"])
        self.assertEqual(p.mirror.pressed, [])

    def test_observer_notification_of_iphone_in_use_raises_before_the_action(self):
        holder = {}

        class Watch:
            def close(self):
                self.closed = True

        def opener(mirror, on_notify):
            holder["notify"] = on_notify
            holder["watch"] = Watch()
            return holder["watch"]

        def drain():
            if holder.get("fire"):
                holder["notify"]()

        ios._ax_watch_open = opener
        ios._drain_run_loop = drain
        p = self.phone(LIVE)
        p.send("input.tap", x=1, y=1)
        p.mirror.frames = [IN_USE]
        p.mirror.current = IN_USE
        holder["fire"] = True
        with self.assertRaises(RuntimeError) as cm:
            p.send("input.scroll", x=1, y=1, dy=-10, dx=0)
        self.assertIn("iPhone in Use", str(cm.exception))
        self.assertIn("LOCK your iPhone", str(cm.exception))
        self.assertNotIn("scroll", p.mirror.actions)
        self.assertEqual(p.mirror.pressed, [])

    def test_screenshot_works_when_the_window_is_blocked(self):
        p = self.phone(IN_USE)
        path, win = p.send("screen.capture")
        self.assertEqual(path, "/dev/null")
        self.assertEqual(win, WIN)
        self.assertEqual(p.mirror.actions, ["capture"])
        self.assertEqual(p.mirror.pressed, [])
        with self.assertRaises(RuntimeError) as cm:
            p.send("screen.text")
        self.assertIn("iPhone in Use", str(cm.exception))
        self.assertIn("screenshot()", str(cm.exception))
        self.assertEqual(p.mirror.actions, ["capture"])


@unittest.skipIf(ios is None, "Apple frameworks unavailable")
class SpotlightLaunch(unittest.TestCase):
    """A non-Latin input source must not rewrite an app search."""

    class SearchField:
        def __init__(self, query="", ime=False, paste_error=False):
            self.query = query
            self.ime = ime
            self.paste_error = paste_error
            self.selected = False
            self.committed = []

        def press(self, key):
            if key == "cmd+a":
                self.selected = True
            elif key == "return":
                self.committed.append(self.query)

        def type_text(self, text, keystrokes=False):
            if not keystrokes and self.paste_error:
                raise RuntimeError("paste unavailable")
            inserted = "ㄴㄷㅅㅅㅑㅜㅎㄴ" if keystrokes and self.ime else text
            self.query = inserted if self.selected else self.query + inserted
            self.selected = False

    def launch(self, field, name):
        from unittest.mock import patch
        phone = ios.IPhone.__new__(ios.IPhone)
        phone.mirror = field
        with patch.object(ios, "_sleep"):
            return phone._apps_launch(name)

    def test_korean_input_source_preserves_english_app_name(self):
        field = self.SearchField(ime=True)
        self.assertEqual(self.launch(field, "Settings"), "Settings")
        self.assertEqual(field.committed, ["Settings"])

    def test_previous_search_is_replaced(self):
        field = self.SearchField(query="old search")
        self.launch(field, "Notes")
        self.assertEqual(field.committed, ["Notes"])

    def test_non_ascii_app_name_survives_input_source(self):
        field = self.SearchField(ime=True)
        self.launch(field, "카카오톡")
        self.assertEqual(field.committed, ["카카오톡"])

    def test_failed_paste_does_not_submit_stale_search(self):
        field = self.SearchField(query="old search", paste_error=True)
        with self.assertRaisesRegex(RuntimeError, "paste unavailable"):
            self.launch(field, "Settings")
        self.assertEqual(field.committed, [])


if __name__ == "__main__":
    unittest.main()
