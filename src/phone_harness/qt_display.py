"""QuickTime Player display transport for USB-connected iPad.

QuickTime Player mirrors a USB-connected iPad's screen as a video feed
in a "Movie Recording" window. This module captures that window and OCRs
it — the same approach as mirror.py for iPhone Mirroring, but the source
window is QuickTime instead of com.apple.ScreenContinuity.

Unlike iPhone Mirroring, clicking in the QuickTime window does NOT
send touches to the iPad. This module is display-only. Touch input goes
through wda.py (WebDriverAgent).

Interface mirrors mirror.py on purpose: same function signatures, same
coordinate conventions (global macOS screen points). This lets ipad.py
swap between QuickTime and a future display source without changing its
own logic.
"""

import subprocess
import tempfile
import time
from pathlib import Path

import Quartz
from AppKit import NSRunningApplication

APP_NAME = "QuickTime Player"
BUNDLE_ID = "com.apple.QuickTimePlayerX"
APP_PATH = "/System/Applications/QuickTime Player.app"

TMP = Path(tempfile.gettempdir()) / "phone-harness"
TMP.mkdir(exist_ok=True)


# ---- App state ----------------------------------------------------------


def running_app():
    """QuickTime Player NSRunningApplication, or None."""
    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(
        BUNDLE_ID
    )
    return apps[0] if apps else None


# ---- Window discovery ---------------------------------------------------


def find_window(on_screen=True):
    """{x, y, w, h, id} of the QuickTime movie window, or None.

    QuickTime can have multiple windows: the main player window, a
    recording controls HUD, etc. The movie window is the one whose
    title matches "Movie Recording" and has layer 0 (normal window).

    TODO: The window title may be localized on non-English macOS.
    On a Chinese system the title might not contain "Movie Recording."
    Use the same PID+layer approach as mirror.py, then filter by size:
    the movie window is the largest one. Or check kCGWindowIsOnscreen
    plus the presence of a video layer child.
    """
    app = running_app()
    if app is None:
        return None
    pid = app.processIdentifier()
    opts = (
        Quartz.kCGWindowListOptionOnScreenOnly
        if on_screen
        else Quartz.kCGWindowListOptionAll
    )
    wins = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []

    # QuickTime has four windows:
    # - "Movie Recording" (the one we want, layer 0)
    # - "" (empty title, layer 0 — the controls HUD that floats separately)
    # - "" (empty title, layer 1000 — floating controls)
    # - "" (layer 25 — a 1x1 invisible window)
    #
    # The safest filter: PID match + layer 0 + non-empty title.
    # On a screen with Stage Manager the window might be tiny when
    # inactive — don't discard small windows (same bug as mirror.py #8).
    cands = [
        w
        for w in wins
        if w.get("kCGWindowOwnerPID") == pid
        and w.get("kCGWindowLayer", 1) == 0
        and w.get("kCGWindowName", "").strip()
    ]

    for w in cands:
        b = w["kCGWindowBounds"]
        return {
            "x": b["X"],
            "y": b["Y"],
            "w": b["Width"],
            "h": b["Height"],
            "id": int(w["kCGWindowNumber"]),
        }
    return None


def ensure_window():
    """Bounds of the movie window. Launches QuickTime + selects iPad camera
    if needed. Raises when the iPad is not connected or QuickTime cannot
    find it.

    Similar to mirror.py's ensure_window() — does what the user would do
    by hand, once. Calls launch() if the app isn't running, then polls
    for the window.
    """
    app = running_app()
    if app is None:
        launch()

    deadline = time.time() + 12  # generous: QuickTime + camera init
    while time.time() < deadline:
        win = find_window()
        if win is not None:
            return win
        time.sleep(0.5)

    # QuickTime is open but no movie window. The iPad camera might not be
    # selected. Try to select it.
    _select_ipad_camera()
    time.sleep(2)
    win = find_window()
    if win is not None:
        return win

    # TODO: Better diagnostics. Run `system_profiler SPCameraDataType`
    # to check if the iPad appears as a camera at all. If not, the USB
    # cable is the problem.
    raise RuntimeError(
        "QuickTime is open but no iPad screen is visible. "
        "Is the iPad connected via USB? In QuickTime, "
        "File → New Movie Recording, then select iPad as the camera. "
        "If iPad does not appear in the camera list, check the cable."
    )


# ---- Capture ------------------------------------------------------------


def capture(path=None):
    """Capture the QuickTime movie window to PNG. Returns (png_path, bounds).

    Uses CGWindowListCreateImage to capture the specific window by id,
    even when QuickTime is not frontmost. Same approach as background.py.
    """
    win = find_window()
    if win is None:
        raise RuntimeError("QuickTime movie window not found. Is the iPad connected?")

    png_path = str(
        Path(path) if path else TMP / f"ipad-{int(time.time())}.png"
    )

    # kCGWindowListOptionIncludingWindow captures one window by id
    img = Quartz.CGWindowListCreateImage(
        Quartz.CGRectMake(win["x"], win["y"], win["w"], win["h"]),
        Quartz.kCGWindowListOptionIncludingWindow,
        win["id"],
        Quartz.kCGWindowImageDefault,
    )
    if img is None:
        raise RuntimeError("Failed to capture QuickTime window")

    url = Quartz.CFURLCreateFromFileSystemRepresentation(
        None, png_path.encode("utf-8"), len(png_path.encode("utf-8")), False
    )
    dest = Quartz.CGImageDestinationCreateWithURL(
        url, "public.png", 1, None
    )
    Quartz.CGImageDestinationAddImage(dest, img, None)
    Quartz.CGImageDestinationFinalize(dest)

    return png_path, win


# ---- Launch & camera selection ------------------------------------------


def launch():
    """Open QuickTime Player and start a new movie recording with iPad as camera.

    TODO: AppleScript is the most reliable way to select the camera.
    Fall back to opening the app and telling the user to select iPad.
    """
    # Open the app
    try:
        subprocess.run(["open", "-a", "QuickTime Player"], check=True, timeout=10)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Could not launch QuickTime Player: {e}") from e

    time.sleep(2)

    # Try to create a new movie recording via AppleScript
    try:
        _select_ipad_camera()
    except Exception:
        # If AppleScript fails, QuickTime is still open. The user can
        # select the camera manually. ensure_window() will guide them.
        pass


def _select_ipad_camera():
    """AppleScript: File > New Movie Recording, then select iPad camera.

    TODO: Implement and test. The iPad's camera name varies:
    - "iPad" (most models)
    - "iPad Pro" (iPad Pro)
    - "iPad mini"
    - Possibly localized on non-English systems

    Strategy: iterate the video recording devices list, pick the one
    whose name contains "iPad" (case-insensitive). If there are multiple,
    use the first one. If none, raise — the iPad isn't connected.
    """
    # TODO: AppleScript implementation
    # applescript = '''
    # tell application "QuickTime Player"
    #     activate
    #     set rec to new movie recording
    #     set cameras to name of video recording devices
    #     repeat with cam in cameras
    #         if cam contains "iPad" then
    #             set camera of rec to cam
    #             exit repeat
    #         end if
    #     end repeat
    # end tell
    # '''
    # subprocess.run(["osascript", "-e", applescript], check=True)
    raise NotImplementedError("AppleScript camera selection not yet implemented")


def show():
    """Bring QuickTime to the front so the user can see the iPad screen."""
    try:
        subprocess.run(
            ["open", "-a", "QuickTime Player"], check=True, timeout=5
        )
    except Exception:
        pass


def activate():
    """Make QuickTime the active application."""
    show()


def focus_probe():
    """Snapshot for interruption detection.
    TODO: Implement when ipad.py needs it. For now, no-op.
    """
    return (running_app() is not None, 0)


def interruption(before, after):
    """Detect if driving the iPad disturbed the user's work.
    Since WDA input doesn't change macOS focus, this is always clean.
    """
    return {"raised": False, "stole_focus": False}


# ---- Accessibility observer (not used) ----------------------------------

# QuickTime's movie window is a video stream — same as iPhone Mirroring.
# macOS accessibility sees nothing inside it. OCR is the only text source.


def ax_session_hint():
    """Not implemented for QuickTime. Always returns 'ready' if the window
    exists, since unlike iPhone Mirroring, QuickTime doesn't show
    interstitials (Connected/Disconnected/Unexpectedly ended).
    """
    if find_window() is None:
        return "no-window"
    return "ready"


def ax_observer_target():
    """QuickTime has no observer target. Returns None so ios.py's
    _ax_watch_open raises _ObserverUnavailable and falls back to polling.
    """
    return None


def window_ax_content():
    """Empty. QuickTime's movie view has no accessibility labels."""
    return []


def window_ax_buttons():
    """Empty."""
    return []


def press_window_button():
    """Not meaningful for QuickTime."""
    raise RuntimeError("QuickTime window has no connect button")


def type_text(s, delay=0.03, keystrokes=False):
    """Typing into the QuickTime window does nothing to the iPad."""
    raise RuntimeError(
        "Cannot type into QuickTime window — use WDA for input"
    )


def press(combo):
    """Key presses into the QuickTime window do nothing to the iPad."""
    raise RuntimeError(
        "Cannot press keys into QuickTime window — use WDA for input"
    )


def tap(x, y):
    """Tapping the QuickTime window does NOT touch the iPad."""
    raise RuntimeError(
        "Cannot tap QuickTime window — use WDA for input"
    )


def long_press(x, y, duration=0.8):
    raise RuntimeError(
        "Cannot long-press QuickTime window — use WDA for input"
    )


def drag(x1, y1, x2, y2, duration=0.35, steps=14):
    raise RuntimeError(
        "Cannot drag in QuickTime window — use WDA for input"
    )


def scroll_wheel(dy, x, y, steps=6, dx=0):
    raise RuntimeError(
        "Cannot scroll QuickTime window — use WDA for input"
    )