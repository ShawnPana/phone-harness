"""iOS backend: the op vocabulary over iPhone Mirroring.

An adapter, not a rewrite. mirror.py and background.py keep their shape and are
imported as they are — they hold the details that were expensive to learn (why
NSWorkspace's frontmost value is a constant, why a slow drag barely registers
on iOS, why the live phone image is opaque to accessibility) and none of that
should move just to change the seam above it.

What iPhone Mirroring cannot do appears here as absence rather than emulation:

  no `nav.back`      iOS has no system Back button. Guessing at an edge swipe
                     returns something the caller cannot tell from a real Back.
  no `apps.current`  the window exposes no app inventory; the foreground app is
  no `apps.list`     only knowable by reading the screen.
  no `tree`          the phone image is a video stream, which is the whole
                     reason screen.text has to recognise glyphs.
  no `raw`           there is no single command channel the way adb is one. The
                     substrate is Quartz: `import Quartz` in your own script,
                     or reach through this backend's .mirror module.

Each raises Unsupported, so a caller can ask rather than discover it by
getting a plausible wrong answer.
"""
import importlib
import os

from . import ocr as _vision
from .transport import Backend

# Fallback only. The primary check is structural (see _session_state), and a
# phrase list cannot be complete: it missed "Connection Paused" and the Mac
# login screen in English, and misses every interstitial on a non-English
# system.
_BLOCKED_MARKERS = ("iphone in use", "lock your iphone", "mirroring ended",
                    "to connect", "connection paused", "connection interrupted",
                    "is locked", "enter the mac login", "try again")


def _load_transport():
    """background drives iPhone Mirroring without ever taking the window to the
    front (SkyLight event records) and is the default: not covering the user's
    screen is what you want unless something is broken. Its private SkyLight
    symbols are not guaranteed across macOS builds, so fall back rather than
    leaving the harness unusable."""
    want_bg = os.environ.get("PHONE_HARNESS_BACKGROUND", "1").lower() not in (
        "0", "false", "no")
    if want_bg:
        try:
            return importlib.import_module(".background", __package__), True
        except Exception:
            pass
    return importlib.import_module(".mirror", __package__), False


class IPhone(Backend):
    name = "iphone-mirroring"

    def __init__(self):
        self.mirror, self.background = _load_transport()

    # --- screen ---------------------------------------------------------

    def _screen_bounds(self):
        return self.mirror.find_window()

    def _screen_require(self):
        """Bounds for gesture maths. Distinct from session.require, which is
        the full connected gate — reading the window rect should not put the
        session through an interstitial check on every swipe."""
        return self.mirror.ensure_window()

    def _screen_capture(self, path=None):
        return self.mirror.capture(path)

    def _screen_text(self, min_confidence=0.3):
        path, win = self.mirror.capture()
        return [dict(o, source="pixels")
                for o in _vision.recognize(path, win)
                if o["confidence"] >= min_confidence]

    # Pixels are the only source iOS has ever had.
    _screen_text_pixels = _screen_text

    # --- input ----------------------------------------------------------

    def _input_tap(self, x, y):
        self.mirror.tap(x, y)

    def _input_press(self, x, y, duration=0.8):
        self.mirror.long_press(x, y, duration)

    def _input_drag(self, x1, y1, x2, y2, duration=0.35, steps=14):
        self.mirror.drag(x1, y1, x2, y2, duration=duration, steps=steps)

    def _input_scroll(self, x, y, dy, dx=0, steps=6):
        self.mirror.scroll_wheel(dy, x, y, steps=steps, dx=dx)

    def _input_keys(self, combo):
        self.mirror.press(combo)

    def _input_text(self, s, delay=0.03, keystrokes=False):
        self.mirror.type_text(s, delay=delay, keystrokes=keystrokes)

    # --- navigation -----------------------------------------------------

    def _nav_home(self):
        self.mirror.press("cmd+1")
        _sleep(0.8)

    def _nav_recents(self):
        self.mirror.press("cmd+2")
        _sleep(0.8)

    # No _nav_back: iOS has no system Back button.

    def _apps_launch(self, name):
        """Spotlight (Cmd+3): type the name, let results populate, commit."""
        self.mirror.press("cmd+3")
        _sleep(0.9)
        # Keystrokes on purpose: Spotlight is the load-bearing path behind
        # open_app and demonstrably works this way. Move it to paste only once
        # that path has miles on it.
        self.mirror.type_text(name, keystrokes=True)
        _sleep(1.2)
        self.mirror.press("return")
        return name

    # --- session --------------------------------------------------------

    def _session_state(self):
        """'ready' | 'blocked' | 'no-window' | 'not-running'.

        'blocked' means an interstitial is up — iPhone in Use, paused, ended,
        connect, or the Mac login screen — and nothing should be tapped or
        typed until the user clears it.

        Detected structurally: the live phone image is a video stream that
        accessibility cannot see into, so a working session exposes no UI
        inside the window, while every interstitial is an ordinary Mac view
        with labels and a button. That holds in any language and for screens
        Apple has not shipped yet, where matching known phrases does neither —
        the old list missed both "Connection Paused" and the Mac login prompt,
        and reported 'ready' for a password field.
        """
        if self.mirror.running_app() is None:
            return "not-running"
        if self.mirror.find_window() is None:
            return "no-window"
        if self.mirror.window_ax_content():
            return "blocked"
        path, win = self.mirror.capture()   # window exists, launches nothing
        texts = " ".join(o["text"] for o in _vision.recognize(path, win)).lower()
        return "blocked" if any(m in texts for m in _BLOCKED_MARKERS) else "ready"

    def _session_detail(self):
        """What the interstitial says, quoted rather than guessed at.

        Button titles are not used: the same screen reported 'Connect' once and
        an empty title a minute later.
        """
        return " ".join(t for role, t in self.mirror.window_ax_content()
                        if role == "AXStaticText" and t)

    def _session_require(self):
        """Bounds once the phone is connected and ready.

        First does what the user would do by hand: opens iPhone Mirroring
        when it is not running, and presses the interstitial's own button
        (Connect, Try Again, Continue) once. What is left after that is
        physical — an unlocked phone ("iPhone in Use", "Timed Out"), the Mac
        login prompt, a phone that is not paired — and only the user can
        clear it, so this raises quoting the window rather than pressing
        again. Once the phone is live the window is brought to the front so
        the user can watch the task. One press per call: a caller that retries after the user says
        the phone is locked gets a fresh press.
        """
        state = self._session_state()
        if state == "not-running":
            self.mirror.launch()
            state = self._wait_state(("blocked", "ready"), _LAUNCH_WAIT)
        # Shown before anything else so the user sees what the harness sees,
        # the interstitial included, not only a session that worked.
        self._show()
        if state == "no-window":
            state = self._wait_state(("blocked", "ready"), _WINDOW_WAIT)
        pressed = None
        if state == "blocked":
            pressed = self._press_connect()
            if pressed:
                state = self._wait_connected(_CONNECT_WAIT)
        if state == "ready":
            return self.mirror.find_window()
        if state == "not-running":
            raise RuntimeError(
                "iPhone Mirroring would not launch. Please open the iPhone "
                "Mirroring app and connect your phone, then retry.")
        if state == "no-window":
            raise RuntimeError(
                "iPhone Mirroring is open but shows no phone window. Pair "
                "the phone in the app (it has to be nearby, locked, and "
                "signed in to the same Apple Account), then retry.")
        said = self._session_detail() or "an interstitial with no text"
        did = (f"I pressed {pressed} and it came back with"
               if pressed else "It says")
        raise RuntimeError(
            f"iPhone Mirroring is not connected. {did}: {said}. This needs "
            "you: if it mentions the iPhone being in use or timing out, LOCK "
            "your iPhone, then tell me and I will try once more. I never "
            "type a passcode.")

    def _show(self):
        """Bring the mirroring window to the front so the user can watch.
        Best effort: a window that will not come forward is not a reason to
        fail the session, since the background build drives it regardless."""
        try:
            self.mirror.show()
        except Exception:
            pass

    def _press_connect(self):
        """Press the interstitial's button through accessibility, once.

        Returns the title pressed, or None when there is nothing safe to
        press: no button, several buttons (the choice is the user's), or a
        login prompt — pressing Unlock without a password does nothing, and
        the password is never ours to type.
        """
        content = self.mirror.window_ax_content()
        if any(role in ("AXTextField", "AXSecureTextField")
               for role, _ in content):
            return None
        buttons = [t for role, t in content if role == "AXButton"]
        if len(buttons) == 1 and buttons[0].lower() not in _NOT_CONNECT_BUTTONS:
            return self.mirror.press_window_button()
        return self.mirror.press_window_button(_CONNECT_BUTTONS)

    def _wait_state(self, wanted, timeout):
        """Poll until the session is in one of `wanted` or time runs out;
        returns the last state seen."""
        deadline = _now() + timeout
        state = self._session_state()
        while state not in wanted and _now() < deadline:
            _sleep(0.5)
            state = self._session_state()
        return state

    def _wait_connected(self, timeout):
        """After a press: wait for the live stream, and fail fast otherwise.

        'ready' has to hold for three polls in a row. Measured: the press
        empties the window for about half a second before "Connecting to"
        draws, and a single poll in that gap reads as live.

        Measured timeline of a failed attempt: 0.3s blank, 0.9s "Connecting
        to" (its only button is "Background"), 2.0s "iPhone in Use" with no
        button at all. So once the attempt is a couple of seconds old and the
        window is an interstitial that is not the Connecting screen, the
        answer is known: raise now rather than sit out the timeout. The user
        locking the phone is what fixes it, and the app reconnects by itself
        when that happens, so the next session.require finds it live."""
        start = _now()
        _sleep(1.0)
        streak = 0
        state = self._session_state()
        while _now() - start < timeout:
            streak = streak + 1 if state == "ready" else 0
            if streak >= 3:
                return "ready"
            if state == "blocked" and _now() - start > _ATTEMPT_SETTLE:
                titles = {t.lower() for t, _ in self.mirror.window_ax_buttons()}
                if not titles & set(_NOT_CONNECT_BUTTONS):
                    break
            _sleep(0.5)
            state = self._session_state()
        return "blocked" if state == "ready" and streak < 3 else state

    def _session_refocus(self):
        self.mirror.activate()

    # --- interruption ---------------------------------------------------

    def _focus_probe(self):
        return self.mirror.focus_probe()

    def _focus_diff(self, before, after):
        return self.mirror.interruption(before, after)


# Buttons an interstitial offers that mean "try to connect"; pressed once
# per session.require. Lower-case; matched case-insensitively.
_CONNECT_BUTTONS = ("connect", "try again", "retry", "continue", "reconnect")
# A lone button that is not an attempt to connect: "Background" on the
# Connecting screen sends the app behind, "Cancel" gives up.
_NOT_CONNECT_BUTTONS = ("background", "cancel", "quit", "unlock", "ok",
                        "done", "settings")
_LAUNCH_WAIT = 12.0     # app launch to phone window
_WINDOW_WAIT = 6.0      # app running, window still coming up
_CONNECT_WAIT = 12.0    # press to live stream, the cap; a failure shows in ~3s
_ATTEMPT_SETTLE = 2.5   # a press has drawn Connecting and then its outcome by now


def _now():
    import time
    return time.time()


def _sleep(s):
    import time
    time.sleep(s)
