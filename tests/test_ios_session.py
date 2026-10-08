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
except ImportError:                      # no pyobjc: nothing to test here
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
IN_USE_RETRYING = [("AXStaticText", "iPhone in Use"),
                   ("AXStaticText", "Lock your iPhone to connect.")]
LOGIN = [("AXStaticText", "Enter the Mac login password"),
         ("AXSecureTextField", ""), ("AXButton", "Unlock")]
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
        return "/dev/null", dict(WIN)

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
        self.clock = [0.0]
        ios._sleep = lambda s: self.clock.__setitem__(0, self.clock[0] + s)
        ios._now = lambda: self.clock[0]
        ios._vision = FakeVision

    def tearDown(self):
        ios._sleep, ios._now, ios._vision = self._sleep, self._now, self._vision

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
        self.assertIn("iPhone in Use", str(cm.exception))
        self.assertLess(self.clock[0], 5.0, "gave up within a few seconds")
        self.assertEqual(p.mirror.shown, 1, "the window was raised even so")

    def test_connecting_screen_background_button_is_not_pressed(self):
        p = self.phone(CONNECTING)
        with self.assertRaises(RuntimeError) as cm:
            p._session_require()
        self.assertEqual(p.mirror.pressed, [])
        self.assertIn("Connecting to", str(cm.exception))

    def test_login_prompt_is_never_pressed(self):
        p = self.phone(LOGIN)
        with self.assertRaises(RuntimeError) as cm:
            p._session_require()
        self.assertEqual(p.mirror.pressed, [])
        self.assertIn("login password", str(cm.exception))

    def test_launch_without_a_window_raises(self):
        p = self.phone(None, None, "no-window")
        with self.assertRaises(RuntimeError) as cm:
            p._session_require()
        self.assertEqual(p.mirror.launched, 1)
        self.assertIn("no phone window", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
