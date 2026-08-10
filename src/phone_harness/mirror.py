"""iPhone Mirroring transport: window discovery, focus, capture, input.

All public coordinates are global screen points — the same space
`screencapture -R` and CGEvent use. The mirroring window is a video stream:
macOS accessibility sees nothing inside it, so input is synthesized at the
HID level and the window must be frontmost or events are swallowed.
"""
import subprocess, tempfile, time
from pathlib import Path

import Quartz
from AppKit import NSRunningApplication, NSWorkspace

APP_NAME = "iPhone Mirroring"
BUNDLE_ID = "com.apple.ScreenContinuity"
APP_PATH = "/System/Applications/iPhone Mirroring.app"

TMP = Path(tempfile.gettempdir()) / "phone-harness"
TMP.mkdir(exist_ok=True)


# --- window / app state ---

def find_window():
    """{x, y, w, h, id} of the mirroring window in screen points, or None.

    Windows are matched by the owner PID of the com.apple.ScreenContinuity
    process, never by kCGWindowOwnerName. The owner name is localized — macOS
    reports "iPhone镜像" on a Simplified Chinese system and "iPhone鏡像輸出" on
    a Traditional Chinese one — so an equality test against the English
    APP_NAME finds nothing, and the harness reports a permanently disconnected
    phone on any non-English Mac. The bundle id behind running_app() is never
    localized.
    """
    app = running_app()
    if app is None:
        return None                     # not running, so it owns no windows
    pid = app.processIdentifier()
    wins = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    for w in wins:
        if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer", 1) == 0:
            b = w["kCGWindowBounds"]
            if b["Width"] < 100:  # ignore panels/toolbars
                continue
            return {"x": b["X"], "y": b["Y"], "w": b["Width"], "h": b["Height"],
                    "id": int(w["kCGWindowNumber"])}
    return None


def running_app():
    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(BUNDLE_ID)
    return apps[0] if apps else None


def is_frontmost():
    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    return bool(front and front.bundleIdentifier() == BUNDLE_ID)


def activate():
    """Bring iPhone Mirroring frontmost. Does NOT launch it — opening the app
    and connecting the phone is the user's job, not the agent's."""
    app = running_app()
    if app is None:
        raise RuntimeError(
            f"{APP_NAME} isn't running — open it and connect your phone.")
    if not is_frontmost():
        app.activateWithOptions_(1 << 1)  # NSApplicationActivateIgnoringOtherApps
        time.sleep(0.5)


def ensure_window(timeout=5.0):
    """Return the mirroring window bounds. Does not launch or connect anything —
    if the app isn't running or has no phone window, raises so the user can
    connect it themselves.

    The window exists even while the session shows a paused/connect
    interstitial — detecting those is connection_state()'s job.
    """
    win = find_window()
    if win is None:
        activate()  # frontmost if running; raises if not running
        deadline = time.time() + timeout
        while win is None and time.time() < deadline:
            time.sleep(0.5)
            win = find_window()
        if win is None:
            raise RuntimeError(
                f"{APP_NAME} has no phone window — connect your phone in the "
                "app, then retry.")
    return win


# --- capture ---

def _run_capture(args, path):
    r = subprocess.run(args, capture_output=True)
    ok = (r.returncode == 0 and Path(path).exists()
          and Path(path).stat().st_size > 1000)
    return ok, (r.stderr.decode(errors="replace").strip() or "empty capture")


def capture(path=None, retries=2):
    """Capture the mirroring window as a PNG. Returns (path, window_bounds).

    `screencapture -l <id>` grabs only the window (no shadow, even if covered)
    but fails with "could not create image from window" when the window is not
    frontmost/composited. So we activate first, and fall back to a region
    capture of the window rect, which succeeds regardless of window backing
    (the app is frontmost by then, so nothing occludes the rect).
    """
    path = str(path or TMP / "window.png")
    last = None
    for attempt in range(retries + 1):
        win = find_window() or ensure_window()
        # -l: window-only, ideal when it works
        ok, last = _run_capture(
            ["screencapture", "-x", "-o", "-l", str(win["id"]), path], path)
        if ok:
            return path, win
        # not backed — bring it frontmost and retry the region instead
        activate()
        win = find_window() or win
        region = f"{int(win['x'])},{int(win['y'])},{int(win['w'])},{int(win['h'])}"
        ok, last = _run_capture(
            ["screencapture", "-x", "-R", region, path], path)
        if ok:
            return path, win
        time.sleep(0.3)
    raise RuntimeError(f"window capture failed after {retries + 1} tries: {last}")


# --- input primitives ---

def _post_mouse(etype, x, y):
    ev = Quartz.CGEventCreateMouseEvent(
        None, etype, Quartz.CGPointMake(x, y), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def _focus():
    activate()


def tap(x, y):
    _focus()
    _post_mouse(Quartz.kCGEventMouseMoved, x, y)
    time.sleep(0.1)
    _post_mouse(Quartz.kCGEventLeftMouseDown, x, y)
    time.sleep(0.06)
    _post_mouse(Quartz.kCGEventLeftMouseUp, x, y)


def long_press(x, y, duration=0.8):
    _focus()
    _post_mouse(Quartz.kCGEventMouseMoved, x, y)
    time.sleep(0.1)
    _post_mouse(Quartz.kCGEventLeftMouseDown, x, y)
    time.sleep(duration)
    _post_mouse(Quartz.kCGEventLeftMouseUp, x, y)


def drag(x1, y1, x2, y2, duration=0.35, steps=14):
    """Touch-drag (what iOS sees as a swipe)."""
    _focus()
    _post_mouse(Quartz.kCGEventMouseMoved, x1, y1)
    time.sleep(0.1)
    _post_mouse(Quartz.kCGEventLeftMouseDown, x1, y1)
    for i in range(1, steps + 1):
        t = i / steps
        _post_mouse(Quartz.kCGEventLeftMouseDragged,
                    x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
        time.sleep(duration / steps)
    _post_mouse(Quartz.kCGEventLeftMouseUp, x2, y2)


def scroll_wheel(dy, x, y, steps=6):
    """Scroll-gesture at (x, y). Positive dy scrolls content up (finger down)."""
    _focus()
    _post_mouse(Quartz.kCGEventMouseMoved, x, y)
    time.sleep(0.1)
    for _ in range(steps):
        ev = Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitPixel, 1, int(dy / steps))
        Quartz.CGEventSetLocation(ev, Quartz.CGPointMake(x, y))
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.03)


_KEYCODES = {
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51,
    "backspace": 51, "escape": 53, "esc": 53,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26,
    "8": 28, "9": 25, "0": 29,
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
    "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45,
    "m": 46,
}
_MODIFIERS = {
    "cmd": Quartz.kCGEventFlagMaskCommand,
    "shift": Quartz.kCGEventFlagMaskShift,
    "alt": Quartz.kCGEventFlagMaskAlternate,
    "option": Quartz.kCGEventFlagMaskAlternate,
    "ctrl": Quartz.kCGEventFlagMaskControl,
}


def press(combo):
    """press('return'), press('cmd+1'), press('cmd+3')."""
    _focus()
    parts = combo.lower().split("+")
    key, mods = parts[-1], parts[:-1]
    if key not in _KEYCODES:
        raise ValueError(f"unknown key {key!r}")
    flags = 0
    for m in mods:
        flags |= _MODIFIERS[m]
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(None, _KEYCODES[key], down)
        if flags:
            Quartz.CGEventSetFlags(ev, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.03)


# iPhone Mirroring forwards raw HID keycodes to iOS and ignores the unicode
# payload CGEventKeyboardSetUnicodeString attaches, so typing must go through
# real keycodes (US layout).
_SHIFTED = {
    "A": "a", "B": "b", "C": "c", "D": "d", "E": "e", "F": "f", "G": "g",
    "H": "h", "I": "i", "J": "j", "K": "k", "L": "l", "M": "m", "N": "n",
    "O": "o", "P": "p", "Q": "q", "R": "r", "S": "s", "T": "t", "U": "u",
    "V": "v", "W": "w", "X": "x", "Y": "y", "Z": "z",
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7",
    "*": "8", "(": "9", ")": "0", "_": "-", "+": "=", ":": ";", '"': "'",
    "<": ",", ">": ".", "?": "/", "~": "`", "{": "[", "}": "]", "|": "\\",
}
_PUNCT_KEYCODES = {
    ".": 47, ",": 43, "/": 44, ";": 41, "'": 39, "[": 33, "]": 30,
    "\\": 42, "-": 27, "=": 24, "`": 50, " ": 49,
}


def _keycode_for(ch):
    """(keycode, needs_shift) for a character, or (None, False) if untypable."""
    shifted = ch in _SHIFTED
    base = _SHIFTED.get(ch, ch)
    code = _KEYCODES.get(base, _PUNCT_KEYCODES.get(base))
    return code, shifted


def type_text(text, delay=0.03):
    """Type text into the focused iOS field via real keycodes (US layout).
    \n presses return. Raises on characters with no keycode (emoji etc.)."""
    _focus()
    for i, line in enumerate(text.split("\n")):
        if i:
            press("return")
        for ch in line:
            code, shifted = _keycode_for(ch)
            if code is None:
                raise ValueError(f"cannot type {ch!r} via keycodes")
            for down in (True, False):
                ev = Quartz.CGEventCreateKeyboardEvent(None, code, down)
                if shifted:
                    Quartz.CGEventSetFlags(ev, Quartz.kCGEventFlagMaskShift)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                time.sleep(0.01)
            time.sleep(delay)
