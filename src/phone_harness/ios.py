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

    def _input_scroll(self, x, y, dy, steps=6):
        self.mirror.scroll_wheel(dy, x, y, steps=steps)

    def _input_keys(self, combo):
        self.mirror.press(combo)

    def _input_text(self, s, delay=0.03):
        self.mirror.type_text(s, delay=delay)

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
        self.mirror.type_text(name)
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
        """Bounds if the phone is connected and ready, else raise for the user.

        Never launches the app, taps Connect/Continue, or polls to reconnect —
        resuming mirroring is physical and only the user can do it. Relay the
        message; do not try to tap through the connect screen.
        """
        state = self._session_state()
        if state == "ready":
            self.mirror.activate()
            return self.mirror.find_window()
        if state == "not-running":
            raise RuntimeError(
                "iPhone Mirroring isn't running. Please open the iPhone "
                "Mirroring app and connect your phone, then retry — "
                "reconnecting is physical, so I can't do it for you.")
        if state == "no-window":
            raise RuntimeError(
                "iPhone Mirroring is open but no phone is connected. Please "
                "connect your phone in the app, then retry.")
        said = self._session_detail()
        raise RuntimeError(
            "iPhone Mirroring is not connected — an interstitial is on screen."
            + (f" It says: {said}" if said else "")
            + " This needs you: clear it on the Mac, and if it says 'iPhone in "
            "Use', LOCK your iPhone so mirroring can resume. Then retry. I "
            "will not tap Connect for you.")

    def _session_refocus(self):
        self.mirror.activate()

    # --- interruption ---------------------------------------------------

    def _focus_probe(self):
        return self.mirror.focus_probe()

    def _focus_diff(self, before, after):
        return self.mirror.interruption(before, after)


def _sleep(s):
    import time
    time.sleep(s)
