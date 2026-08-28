"""iPhone Mirroring transport: window discovery, focus, capture, input.

All public coordinates are global screen points — the same space
`screencapture -R` and CGEvent use. The mirroring window is a video stream:
macOS accessibility sees nothing inside it, so input is synthesized at the
HID level and the window must be frontmost or events are swallowed.
"""
import subprocess, tempfile, time
from contextlib import contextmanager

import ApplicationServices as _AS
from pathlib import Path

import Quartz
from AppKit import NSRunningApplication

APP_NAME = "iPhone Mirroring"
BUNDLE_ID = "com.apple.ScreenContinuity"
APP_PATH = "/System/Applications/iPhone Mirroring.app"

TMP = Path(tempfile.gettempdir()) / "phone-harness"
TMP.mkdir(exist_ok=True)


# --- window / app state ---

def find_window(on_screen=True):
    """{x, y, w, h, id} of the mirroring window in screen points, or None.

    on_screen=False also finds the window when it is on another Space, hidden,
    or minimized. Only backends that address the window by id can use that:
    SkyLight event records reach it wherever it is, while CGEvents are posted
    at global screen coordinates and would land on whatever is actually in
    front. This module posts CGEvents, so it keeps the on-screen default.

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
    opts = (Quartz.kCGWindowListOptionOnScreenOnly if on_screen
            else Quartz.kCGWindowListOptionAll)
    wins = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []
    cands = [w for w in wins if w.get("kCGWindowOwnerPID") == pid
             and w.get("kCGWindowLayer", 1) == 0]
    if not on_screen:
        # Off the active Space the list also carries the app's other windows:
        # a Settings sheet, a Welcome dialog, and four unnamed 1512x33 strips.
        # The phone window is the one whose title is the app's own name. Both
        # strings are localized together, so this still picks it out where a
        # comparison against the English constant would not.
        named = [w for w in cands
                 if w.get("kCGWindowName") == w.get("kCGWindowOwnerName")]
        cands = named or cands
    # No size filter. The old `Width < 100` guard was there to skip panels and
    # toolbars, but PID + layer 0 already excludes those. What the guard did
    # hit was a real mirroring window that macOS had shrunk — with Stage
    # Manager on, an inactive window is parked in the left rail at about
    # 38x130, which is on-screen and genuinely the phone. The guard discarded
    # it, so every time another app took the stage the harness reported a
    # disconnected phone (#8). Window list order is front to back, so the
    # first candidate is the frontmost.
    for w in cands:
        b = w["kCGWindowBounds"]
        return {"x": b["X"], "y": b["Y"], "w": b["Width"], "h": b["Height"],
                "id": int(w["kCGWindowNumber"])}
    return None


def focus_probe():
    """(app_frontmost, depth) — the two interruptions a user actually notices.

    app_frontmost  does iPhone Mirroring have keyboard focus / the menu bar.
                   Read from the accessibility API, which reports it live.
                   NSWorkspace.frontmostApplication() cannot be used: it is
                   delivered by a workspace notification, and a process with no
                   run loop never receives one — measured returning the same
                   stale pid across two deliberate app switches.
    depth          our window's index in the front-to-back on-screen window
                   list, so 0 means nothing is drawn over it and None means it
                   is not on screen at all.

    The two are read from different sources on purpose. Deriving focus from
    z-order makes "took focus but stayed behind" unrepresentable, which is
    exactly the state worth telling apart from "jumped in front".
    """
    app = running_app()
    if app is None:
        return None, None
    pid = app.processIdentifier()
    el = _AS.AXUIElementCreateApplication(pid)
    err, val = _AS.AXUIElementCopyAttributeValue(el, "AXFrontmost", None)
    front = None if err else bool(val)
    wins = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    layer0 = [w for w in wins if w.get("kCGWindowLayer", 1) == 0]
    depth = next((i for i, w in enumerate(layer0)
                  if w.get("kCGWindowOwnerPID") == pid), None)
    return front, depth


def interruption(before, after):
    """{raised, stole_focus} between two focus_probe() readings.

    Raises when either reading has no focus value rather than reporting a
    clean result. AXFrontmost errors when Accessibility permission is missing,
    and a probe that silently answers "nothing happened" because it could not
    look is worse than no probe at all.
    """
    b_front, b_depth = before
    a_front, a_depth = after
    if b_front is None or a_front is None:
        raise RuntimeError(
            "focus_probe() could not read AXFrontmost — grant the terminal "
            "Accessibility permission. Refusing to report an unmeasured "
            "result as no interruption.")
    raised = a_depth is not None and (b_depth is None or a_depth < b_depth)
    return {"raised": bool(raised), "stole_focus": bool(a_front and not b_front)}


def running_app():
    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(BUNDLE_ID)
    return apps[0] if apps else None


def window_ax_content():
    """Native UI drawn inside the mirroring window, as [(role, text)].

    A connected session returns []. The phone image is a video stream and
    accessibility cannot see into it — that is the same property that forces
    the harness to read the screen with OCR.

    The interstitials macOS draws in that window when the session is not
    usable (iPhone in Use, paused, ended, locked, connect) are ordinary Mac
    views, so they come back with their labels and buttons. Detecting them by
    the presence of real UI rather than by searching for known phrases is what
    makes this work on a non-English system, and on whatever screen Apple adds
    next.

    Observed: 0 elements live, 4 on "iPhone in Use". Button titles are not
    dependable — the same screen reported 'Connect' once and an empty title a
    minute later — so callers should read AXStaticText for anything shown to
    a user.
    """
    app = running_app()
    if app is None:
        return []
    win = find_window()
    if win is None:
        return []
    err, ax_wins = _AS.AXUIElementCopyAttributeValue(
        _AS.AXUIElementCreateApplication(app.processIdentifier()),
        "AXWindows", None)
    if err or not ax_wins:
        return []

    def geom(node):
        e1, pos = _AS.AXUIElementCopyAttributeValue(node, "AXPosition", None)
        e2, size = _AS.AXUIElementCopyAttributeValue(node, "AXSize", None)
        if e1 or e2 or pos is None or size is None:
            return None
        ok, p = _AS.AXValueGetValue(pos, _AS.kAXValueCGPointType, None)
        ok2, sz = _AS.AXValueGetValue(size, _AS.kAXValueCGSizeType, None)
        return (p.x, p.y, sz.width, sz.height) if ok and ok2 else None

    # Match the phone window by geometry. The app also owns a Settings sheet
    # and a Welcome dialog; counting their contents would report a healthy
    # session as blocked.
    target = None
    for w in ax_wins:
        g = geom(w)
        if g and abs(g[0] - win["x"]) < 4 and abs(g[1] - win["y"]) < 4 \
                and abs(g[2] - win["w"]) < 4 and abs(g[3] - win["h"]) < 4:
            target = w
            break
    if target is None:
        return []

    out = []

    def walk(node, depth=0):
        if depth > 6 or len(out) > 40:
            return
        e, role = _AS.AXUIElementCopyAttributeValue(node, "AXRole", None)
        role = "" if e else str(role)
        if role in ("AXStaticText", "AXButton", "AXTextField",
                    "AXSecureTextField", "AXImage"):
            parts = []
            for attr in ("AXTitle", "AXValue", "AXDescription"):
                ea, v = _AS.AXUIElementCopyAttributeValue(node, attr, None)
                if not ea and isinstance(v, str) and v.strip():
                    parts.append(v.strip())
            out.append((role, " ".join(parts)))
        ek, kids = _AS.AXUIElementCopyAttributeValue(node, "AXChildren", None)
        for k in (kids or []):
            walk(k, depth + 1)

    walk(target)
    return out


def frontmost_window():
    """The frontmost normal-layer window, or None — i.e. *which* app is in
    front. Queried live from the window list, which needs no run loop."""
    wins = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID) or []
    for w in wins:                      # front-to-back order
        if w.get("kCGWindowLayer", 1) == 0:
            return w
    return None


def is_frontmost():
    """Does iPhone Mirroring have both keyboard focus AND the frontmost window.

    Reads AXFrontmost through focus_probe(), not
    NSWorkspace.frontmostApplication(): that value arrives by workspace
    notification, and a process with no run loop never receives one, so it
    reports whatever was true when the process first looked. Believing it is
    how input ends up in the wrong app — activate() sees a stale "already
    frontmost", skips focusing, and every CGEvent after that lands in
    whatever the user actually has focused.

    But AXFrontmost alone is not enough either, and requiring z-order too is
    not optional the way it might look. Measured on a real steal, AXFrontmost
    reported iPhone Mirroring true while the frontmost window still belonged
    to the user's terminal — that used to read here as "no change, still
    focused". It is not: the background backend's SkyLight path
    (_SLPSSetFrontProcessWithOptions in background._post/_make_key) makes
    iPhone Mirroring the front *process* without raising its *window*, and
    once that happens AXFrontmost reports true indefinitely even while the
    terminal's window stays on top and keeps receiving keyboard and scroll
    events — those are routed by window z-order, not by AX. So a steal that
    only moves the window (AX stays true, z-order changes) is exactly the
    case this has to catch, and it is caught by requiring both signals to
    agree: AXFrontmost true AND the window list's frontmost normal-layer
    window owned by this process's PID. A window lookup that finds nothing
    (app not running, or genuinely no window) counts as not frontmost too.
    """
    front, _ = focus_probe()
    if not front:
        return False
    app = running_app()
    if app is None:
        return False
    win = frontmost_window()
    return bool(win) and win.get("kCGWindowOwnerPID") == app.processIdentifier()


def _ax_window_element(app, win):
    """The AXUIElement for `win`, matched by geometry against the app's
    AXWindows the way window_ax_content() does, or None.

    Used to AXRaise the phone window directly when activateWithOptions_
    alone can't move it — see activate().
    """
    err, ax_wins = _AS.AXUIElementCopyAttributeValue(
        _AS.AXUIElementCreateApplication(app.processIdentifier()),
        "AXWindows", None)
    if err or not ax_wins or win is None:
        return None
    for w in ax_wins:
        e1, pos = _AS.AXUIElementCopyAttributeValue(w, "AXPosition", None)
        e2, size = _AS.AXUIElementCopyAttributeValue(w, "AXSize", None)
        if e1 or e2 or pos is None or size is None:
            continue
        ok, p = _AS.AXValueGetValue(pos, _AS.kAXValueCGPointType, None)
        ok2, _sz = _AS.AXValueGetValue(size, _AS.kAXValueCGSizeType, None)
        if ok and ok2 and abs(p.x - win["x"]) < 4 and abs(p.y - win["y"]) < 4:
            return w
    return None


def activate(timeout=2.5):
    """Bring iPhone Mirroring frontmost and *confirm* it. Does NOT launch it —
    opening the app and connecting the phone is the user's job, not the agent's.

    Raises rather than returning unfocused: a silent failure here means every
    subsequent tap and drag is delivered to some other app, which is both
    confusing to debug and potentially destructive.
    """
    app = running_app()
    if app is None:
        raise RuntimeError(
            f"{APP_NAME} isn't running — open it and connect your phone.")
    if is_frontmost():
        return
    # Re-assert, don't just wait. A single activateWithOptions_ loses to any
    # app that grabs focus back — a full-screen menu-bar app can reclaim it
    # within a second — and then this timed out against a Mac where simply
    # asking again would have won on the second try.
    #
    # activateWithOptions_ alone is not enough any more, now that is_frontmost
    # checks z-order too. Measured directly: once background.py's SkyLight
    # calls (_SLPSSetFrontProcessWithOptions, used by every background tap and
    # keystroke to target events at the process without raising its window)
    # have left AXFrontmost true for this app, activateWithOptions_ becomes a
    # no-op — Cocoa sees an app that already reports itself frontmost and
    # skips the raise, so the window never moves and the loop above would spin
    # for the full timeout doing nothing. AXRaise on the window's own
    # AXUIElement works even in that stuck state, so it runs alongside
    # activateWithOptions_ on every iteration rather than only as a fallback.
    win = find_window()
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.activateWithOptions_(1 << 1)  # NSApplicationActivateIgnoringOtherApps
        el = _ax_window_element(app, win) if win else None
        if el is not None:
            _AS.AXUIElementPerformAction(el, "AXRaise")
        time.sleep(0.08)
        if is_frontmost():
            return
    # is_frontmost() now requires both AXFrontmost and z-order to agree, so a
    # failure can be either signal (or both) — name the one that's actually
    # missing rather than a generic "still has focus", since the two failure
    # modes need different things from the user (bring the window forward vs.
    # give it keyboard focus).
    front, _ = focus_probe()
    win = frontmost_window() or {}
    owner = win.get("kCGWindowOwnerName", "unknown")
    if front and owner != APP_NAME:
        detail = (f"AXFrontmost is true but {owner!r} still owns the "
                  f"frontmost window — {APP_NAME} has focus without being "
                  f"raised")
    else:
        detail = f"{owner!r} still has focus"
    raise RuntimeError(
        f"could not bring {APP_NAME} frontmost after {timeout:.1f}s — "
        f"{detail}. Input would be swallowed, so nothing was sent.")


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


# A plain CGEventCreateScrollWheelEvent (the old implementation below this
# comment used to be) is what iPhone Mirroring stopped honouring on macOS 26,
# along with every shape tried in 404e923's "ruled out" list. What it still
# accepts is an event sequence shaped like an actual trackpad two-finger
# scroll: IsContinuous set, a ScrollPhase field walking began -> changed* ->
# ended, and — the single most important detail, found the hard way — a
# NONZERO delta on the 'began' event too. A began event carrying 0 does
# nothing at all, even though the changed events that follow carry the real
# motion.
#
# Distance is controlled by the NUMBER of 'changed' events, not by the size of
# each one's delta: pt=-1 and pt=-30 both moved a test list ~95-105 screen
# points over 14 events at 16ms spacing, and 28/56 events moved ~192/~384
# points — linear in count, flat in magnitude. So _SCROLL_PT_PER_EVENT below
# is points-per-event, and the delta field is just held at a fixed nonzero
# value.
#
# Measured on macOS 26.2 (25C56) with the window frontmost and is_frontmost()
# verified true; see the calibration method in issue #51.
_SCROLL_PT_PER_EVENT = 7        # screen points moved per 'changed' event
_SCROLL_EVENT_DELTA = 9         # per-event delta magnitude; any nonzero value
                                 # measured the same distance — this is the one
                                 # that was actually measured
_SCROLL_STEP_INTERVAL = 0.016   # seconds between posted events (~60Hz, a real
                                 # trackpad's rate)
_SCROLL_PHASE_BEGAN, _SCROLL_PHASE_CHANGED, _SCROLL_PHASE_ENDED = 1, 2, 4

_libc = None


def _mach_absolute_time():
    # A real trackpad gesture's events carry a live host timestamp; the
    # working recipe was measured with one attached to every event, so it
    # stays rather than risk finding out CGEventSetTimestamp(0) matters too.
    global _libc
    if _libc is None:
        import ctypes
        _libc = ctypes.CDLL(None)
        _libc.mach_absolute_time.restype = ctypes.c_uint64
    return _libc.mach_absolute_time()


def _scroll_event(x, y, phase, pt):
    """One event in a phased trackpad-scroll gesture at (x, y).

    Most fields here are load-bearing, found by exhaustive trial against a
    device (see 404e923's "ruled out" list): IsContinuous plus a ScrollPhase
    walking began/changed/ended is what makes this read as a gesture instead
    of a plain wheel click, and a nonzero delta has to be on every phase (see
    scroll_wheel). PointDeltaAxis2 is the one exception — it is set to a small
    cross-axis value because a real two-finger scroll always carries one, not
    because it changes anything: the identical recipe with it left at 0
    measured the same shift (-150px for dy=-140). It stays for fidelity to a
    real event, not necessity. MomentumPhase (field 123) and fields
    137/45/101/87 are deliberately NOT set — setting any of those makes
    iPhone Mirroring silently ignore the whole event.
    """
    ev = Quartz.CGEventCreateScrollWheelEvent(
        None, Quartz.kCGScrollEventUnitPixel, 2, 0, 0)
    set_field = lambda f, v: Quartz.CGEventSetIntegerValueField(ev, f, v)
    set_field(88, 1)                                    # IsContinuous
    set_field(99, phase)                                # ScrollPhase
    set_field(96, pt)                                   # PointDeltaAxis1
    set_field(93, int(pt * 65536 / 10))                 # FixedPtDeltaAxis1
    set_field(11, -1 if pt < 0 else (1 if pt > 0 else 0))  # DeltaAxis1
    set_field(97, -1 if pt else 0)                      # PointDeltaAxis2
    Quartz.CGEventSetLocation(ev, Quartz.CGPointMake(x, y))
    Quartz.CGEventSetTimestamp(ev, _mach_absolute_time())
    return ev


def scroll_wheel(dy, x, y, steps=6):
    """Phased scroll-wheel gesture at (x, y). dy is screen points; positive
    scrolls content up (finger down) — matches the sign scroll()/
    scroll_screen() already assume. `steps` is accepted for call-signature
    compatibility with older callers and ignored: distance is now controlled
    by event count (see _SCROLL_PT_PER_EVENT), not by dividing dy across a
    fixed number of posts.

    Needs the window frontmost, same as every other gesture in this module —
    _focus() below asserts it. background.py's scroll_wheel calls this
    directly rather than reimplementing it, because this is the one gesture
    that backend cannot deliver without briefly focusing.
    """
    _focus()
    _post_mouse(Quartz.kCGEventMouseMoved, x, y)
    time.sleep(0.1)
    if dy == 0:
        return
    n = max(1, round(abs(dy) / _SCROLL_PT_PER_EVENT))
    pt = _SCROLL_EVENT_DELTA if dy > 0 else -_SCROLL_EVENT_DELTA
    sequence = ([(_SCROLL_PHASE_BEGAN, pt)]
                + [(_SCROLL_PHASE_CHANGED, pt)] * n
                + [(_SCROLL_PHASE_ENDED, 0)])
    for phase, p in sequence:
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, _scroll_event(x, y, phase, p))
        time.sleep(_SCROLL_STEP_INTERVAL)


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
# The physical keys behind those flags. Left-hand only: a synthesized event has
# no physical side, so kVK_RightShift and friends (54, 60, 61, 62) would be dead
# weight. kVK_CapsLock (57) is left out on purpose — it is a toggle with its own
# flag mask and synthesizes unreliably, and shift covers every case we want.
_MOD_KEYCODES = {
    "cmd": 55, "shift": 56, "alt": 58, "option": 58, "ctrl": 59,
}


def _post_key(code, down, flags=0):
    ev = Quartz.CGEventCreateKeyboardEvent(None, code, down)
    if flags:
        # Only Mac-side consumers ever read these. Set them so macOS's own
        # state machine stays consistent; iOS never sees them.
        Quartz.CGEventSetFlags(ev, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


@contextmanager
def _holding(mods):
    """Hold real modifier keys down around the body.

    iPhone Mirroring forwards raw HID keycodes to iOS and drops the CGEvent
    flag mask, so a flag set on the key event reaches the Mac app but never the
    phone. That is why cmd+1 (a menu shortcut the Mac app handles) works while
    cmd+v aimed at an iOS text field arrives as a bare 'v'. Shift is a key you
    hold, not a bit you set.
    """
    acc = 0
    for m in mods:
        acc |= _MODIFIERS[m]
        _post_key(_MOD_KEYCODES[m], True, acc)
        time.sleep(0.01)
    try:
        yield acc
    finally:
        # Unconditional: a modifier left latched by an exception mid-string
        # corrupts every later keystroke in the session, and the damage shows
        # up somewhere else entirely.
        for m in reversed(mods):
            # Clear the bit *before* posting the release. A key-up still
            # carrying its own flag reads as "still held", which latches shift
            # on and turns the rest of the string into 1,200 -> !<@)).
            acc &= ~_MODIFIERS[m]
            _post_key(_MOD_KEYCODES[m], False, acc)
            time.sleep(0.01)


def press(combo):
    """press('return'), press('cmd+1'), press('cmd+3')."""
    _focus()
    parts = combo.lower().split("+")
    key, mods = parts[-1], parts[:-1]
    if key not in _KEYCODES:
        raise ValueError(f"unknown key {key!r}")
    for m in mods:
        if m not in _MOD_KEYCODES:
            raise ValueError(f"unknown modifier {m!r}")
    with _holding(mods) as flags:
        for down in (True, False):
            _post_key(_KEYCODES[key], down, flags)
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


def paste_with(press_fn, text):
    """Put text on the Mac clipboard and paste it into the phone.

    Takes the press function rather than calling one, because the two input
    paths reach the phone differently (posted to the window vs posted to the
    pid) and only the caller knows which is in play. The clipboard round-trip
    itself is identical, and worth having in exactly one place.

    The typed text stays on the Mac clipboard afterwards. Restoring the
    previous contents was a race: the phone pulls the clipboard through the
    Mirroring session some unknown time after cmd+v, and nothing reports
    when it has — restore too early and the phone pastes what was on the
    clipboard BEFORE (a URL, a token, a password) into the field. A
    deterministic "your clipboard now holds what was typed" beats a
    sometimes-leak. Set ios.restore_clipboard=true in config to restore
    after a generous settle anyway (ios.paste_settle seconds).
    """
    from . import config
    prior = subprocess.run(["pbpaste"], capture_output=True).stdout
    subprocess.run(["pbcopy"], input=text.encode())
    # Fail here rather than pasting stale clipboard contents into the phone.
    if subprocess.run(["pbpaste"], capture_output=True).stdout != text.encode():
        raise RuntimeError("clipboard did not take the text; cannot paste")
    time.sleep(0.15)   # the Mac clipboard has to reach the phone
    press_fn("cmd+v")
    if config.get("ios.restore_clipboard"):
        time.sleep(float(config.get("ios.paste_settle")))
        subprocess.run(["pbcopy"], input=prior)


def _type_keystrokes(text, delay=0.03):
    """Type via real keycodes (US layout). Newline presses return. Raises on
    characters with no keycode (emoji etc.).

    Subject to iOS autocorrect, which rewrites words as they are typed --
    'Thu' becomes 'thru', 'Fri' becomes 'Friday'. Prefer type_text's paste
    path for anything whose exact wording matters.
    """
    _focus()
    for i, line in enumerate(text.split("\n")):
        if i:
            press("return")
        for ch in line:
            code, shifted = _keycode_for(ch)
            if code is None:
                raise ValueError(f"cannot type {ch!r} via keycodes")
            with _holding(["shift"] if shifted else []) as flags:
                for down in (True, False):
                    _post_key(code, down, flags)
                    time.sleep(0.01)
            time.sleep(delay)


def type_text(text, delay=0.03, keystrokes=False):
    """Type text into the focused iOS field.

    Pastes by default, which is immune to both autocorrect and keyboard layout
    -- the text arrives exactly as given. Set keystrokes=True for fields that
    need real key events.

    Either path needs a focused field. Neither can tell whether it got one:
    text sent to an unfocused field goes nowhere, silently, so verify with a
    capture afterwards.
    """
    if keystrokes or not text:
        return _type_keystrokes(text, delay)
    paste_with(press, text)
