"""iPad backend — QuickTime display + WebDriverAgent input.

Two transports, one backend. The op vocabulary is the same contract every
backend implements, and helpers.py never knows the difference.

Display:  qt_display.py  — QuickTime Player USB mirroring window
Input:    wda.py         — WebDriverAgent HTTP over USB tunnel

QuickTime is VIEW-ONLY. Every input op goes through WDA. Screen ops
capture the QuickTime window and OCR it with Vision (same pipeline as
iPhone Mirroring).

Coordinate translation is the bridge between the two. The screen helpers
operate in global macOS screen points (QuickTime window coordinates).
WDA operates in iPad-native points. translate() converts between them.

Unsupported by design (same as iPhone Mirroring):
  - nav.back     No system back button on iPad
  - tree         No accessibility tree through QuickTime window
  - shell        No adb-style shell on iOS
  - raw          No unified command channel
"""

import os
import time

from . import ocr as _vision
from . import qt_display
from . import wda
from .transport import Backend


class IPad(Backend):
    name = "ipad"

    def __init__(self):
        self.display = qt_display
        self._wda = wda.client()

    # ---- Coordinate translation -----------------------------------------

    def _translate_point(self, sx, sy):
        """Global screen point → iPad-native point.

        The QuickTime window shows the iPad screen at SOME resolution.
        That resolution may be scaled from the iPad's native resolution,
        especially on smaller Mac screens. We need to measure the ratio
        between the window's pixel dimensions and the iPad's native
        point dimensions.

        TODO: Measure empirically. The ratio is:
          scale_x = iPad_width / window_width
          scale_y = iPad_height / window_height

        For now, assume 1:1 (only works when QuickTime window matches
        iPad resolution exactly, which it often does on large displays).
        Once measured, cache the ratio from the first capture's image
        dimensions.
        """
        win = self.display.find_window()
        if win is None:
            raise RuntimeError("No QuickTime window for coordinate translation")

        # Screen point → local window point
        local_x = sx - win["x"]
        local_y = sy - win["y"]

        # TODO: Multiply by scale ratio once measured
        return local_x, local_y

    # ---- Screen ---------------------------------------------------------

    def _screen_bounds(self):
        return self.display.find_window()

    def _screen_require(self):
        return self.display.ensure_window()

    def _screen_capture(self, path=None):
        return self.display.capture(path)

    def _screen_text(self, min_confidence=0.3):
        path, win = self.display.capture()
        return [
            dict(o, source="pixels")
            for o in _vision.recognize(path, win)
            if o["confidence"] >= min_confidence
        ]

    # Pixels are the only source — same as iPhone Mirroring
    _screen_text_pixels = _screen_text

    # ---- Input (all WDA) ------------------------------------------------

    def _input_tap(self, x, y):
        ix, iy = self._translate_point(x, y)
        self._wda.tap(ix, iy)

    def _input_press(self, x, y, duration=0.8):
        ix, iy = self._translate_point(x, y)
        self._wda.long_press(ix, iy, duration)

    def _input_drag(self, x1, y1, x2, y2, duration=0.35, steps=14):
        ix1, iy1 = self._translate_point(x1, y1)
        ix2, iy2 = self._translate_point(x2, y2)
        self._wda.drag(ix1, iy1, ix2, iy2, duration)

    def _input_scroll(self, x, y, dy, dx=0, steps=6):
        """Scroll at (x,y) by (dy,dx) in content space.

        WDA's native scroll operates on elements, not screen coordinates.
        For now, use a drag gesture. This is a momentum flick, not a
        precise scroll — match Android's scroll() if possible.

        TODO: Measure whether a short drag with the right parameters
        gives a reliable scroll distance on iPad. If not, find the
        scrollable element at (x,y) via WDA's element tree and call
        /wda/scroll on it.
        """
        ix, iy = self._translate_point(x, y)

        # dx, dy are content-space deltas. WDA's drag is finger motion.
        # Since scroll and swipe are inverted in English ("scroll down"
        # means "swipe up"), we negate dy.
        end_x = ix + dx
        end_y = iy - dy
        self._wda.drag(ix, iy, end_x, end_y, duration=0.3)

    def _input_keys(self, combo):
        """Press a single key. Chords are unsupported.

        TODO: Map key names to WDA key codes.
        'enter' → \uE007
        'backspace' → \uE003
        'tab' → \uE004
        """
        key = combo.lower()
        if key in ("enter", "return"):
            self._wda.press_key("enter")
        elif key == "tab":
            self._wda.press_key("tab")
        else:
            raise RuntimeError(
                f"iPad backend: unknown key {combo!r}. "
                f"Supported: enter, tab."
            )

    def _input_text(self, s, delay=0.03, keystrokes=False):
        """Type text into the focused field.

        WDA types via keystrokes, which go through iOS autocorrect.
        This matches iPhone Mirroring's keystrokes=True behavior.

        TODO: The keystrokes parameter lets the caller choose between
        paste (exact text, no autocorrect) and type (key events,
        autocorrect applies). WDA's /keys endpoint is keystrokes-only.
        For paste behavior, we'd need to set UIPasteboard and send a
        paste command, or use a custom WDA endpoint.

        For now, all text goes through keystrokes. If autocorrect
        rewrites words ("Thu" → "thru"), the caller can work around it.
        """
        self._wda.type_text(s)

    # ---- Navigation -----------------------------------------------------

    def _nav_home(self):
        self._wda.home()
        time.sleep(0.5)

    def _nav_recents(self):
        self._wda.app_switcher()
        time.sleep(0.5)

    # No _nav_back. iPad has no system back button.

    # ---- Apps -----------------------------------------------------------

    def _apps_launch(self, name):
        """Launch an app by name. Like open_app() on iPhone Mirroring.

        TODO: Build a name-to-bundle-id map. For now, try common apps.
        WDA's appLauncher needs a bundle id, not a display name.

        Common bundle ids:
        - com.apple.mobilesafari (Safari)
        - com.apple.mobilesms (Messages)
        - com.apple.mobilemail (Mail)
        - com.apple.camera (Camera)
        - com.apple.Photos (Photos)
        - com.apple.AppStore (App Store)
        - com.apple.Preferences (Settings)

        For app names that don't match, we could:
        1. Use ideviceinstaller -l to list installed apps
        2. Search the list by display name
        3. Or use the iPad's Spotlight (like iPhone Mirroring does with Cmd+3)
        """
        # Common app name → bundle id
        _KNOWN_APPS = {
            "safari": "com.apple.mobilesafari",
            "messages": "com.apple.mobilesms",
            "mail": "com.apple.mobilemail",
            "camera": "com.apple.camera",
            "photos": "com.apple.Photos",
            "app store": "com.apple.AppStore",
            "settings": "com.apple.Preferences",
            "notes": "com.apple.mobilenotes",
            "calendar": "com.apple.mobilecal",
            "reminders": "com.apple.reminders",
            "maps": "com.apple.Maps",
            "music": "com.apple.Music",
            "facetime": "com.apple.facetime",
        }

        bundle_id = _KNOWN_APPS.get(name.lower())
        if bundle_id is None:
            raise RuntimeError(
                f"Unknown app {name!r}. Pass a bundle ID directly, or add "
                f"it to the _KNOWN_APPS map in ipad.py. "
                f"Known apps: {', '.join(sorted(_KNOWN_APPS))}"
            )

        self._wda.launch_app(bundle_id)
        return bundle_id

    def _apps_current(self):
        return self._wda.current_bundle_id()

    def _apps_list(self, include_system=False):
        """List installed apps via ideviceinstaller.

        TODO: This requires libimobiledevice (brew install libimobiledevice).
        If ideviceinstaller is not available, raise with a clear message.
        """
        import subprocess

        try:
            result = subprocess.run(
                ["ideviceinstaller", "-l"],
                capture_output=True, text=True, timeout=30,
            )
        except FileNotFoundError:
            raise RuntimeError(
                "ideviceinstaller not found. Install it: "
                "brew install libimobiledevice"
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("ideviceinstaller timed out. Is the iPad connected?")

        if result.returncode != 0:
            raise RuntimeError(
                f"ideviceinstaller failed: {result.stderr.strip()}"
            )

        # Parse output. Format:
        # com.apple.mobilesafari - Safari 17.0
        # com.apple.Preferences - Settings 17.0
        apps = []
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(" - ", 1)
            bundle_id = parts[0].strip()
            if not include_system and bundle_id.startswith("com.apple."):
                continue
            apps.append(bundle_id)
        return apps

    # ---- Session --------------------------------------------------------

    def _session_state(self):
        """'ready' when QuickTime window is present AND WDA is reachable.

        Unlike iPhone Mirroring, QuickTime doesn't show interstitials
        (Connected, iPhone in Use, Timed Out). If the window is visible
        and WDA responds, we're good.
        """
        if self.display.find_window() is None:
            return "no-window"
        if not self._wda.is_ready():
            return "wda-unreachable"
        return "ready"

    def _session_require(self):
        """Ensure everything is set up. Returns bounds or raises with
        human-readable guidance.

        This is the load-bearing first call. It:
        1. Ensures QuickTime is running + iPad camera selected
        2. Waits for WDA to be reachable
        3. Creates a WDA session if needed
        """
        # Step 1: QuickTime display
        win = self.display.ensure_window()

        # Step 2: WDA
        if not self._wda.is_ready():
            # Try once more with a wait
            self._wda.wait_ready(timeout=10)

        # Step 3: WDA session
        try:
            self._wda.session_id  # triggers create_session if needed
        except RuntimeError as e:
            raise RuntimeError(
                f"WDA session failed: {e}\n\n"
                f"WebDriverAgent is running on the iPad but creates no session. "
                f"This usually means the device is locked or WDA needs to be "
                f"rebuilt. Unlock the iPad and try again."
            ) from e

        return win

    def _session_detail(self):
        """Why the session is not ready, as a string."""
        if self.display.find_window() is None:
            return ("No QuickTime movie window. Open QuickTime, "
                    "File → New Movie Recording, select iPad as camera.")
        if not self._wda.is_ready():
            return (f"WDA unreachable at {wda.WDA_URL}. "
                    f"Is iproxy running? Run: iproxy 8100 8100")
        return ""

    def _session_refocus(self):
        """Bring QuickTime to front so user can watch."""
        self.display.show()

    # ---- Interruption ---------------------------------------------------

    def _focus_probe(self):
        return self.display.focus_probe()

    def _focus_diff(self, before, after):
        return self.display.interruption(before, after)