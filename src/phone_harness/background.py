"""Background backend — drive iPhone Mirroring WITHOUT bringing it to the front.

The default (mirror.py) backend must make the iPhone Mirroring window frontmost
before every action: it captures with `screencapture` and injects with
`CGEventPost`, both of which target the focused app, so automation steals your
screen for its whole run.

This backend removes that. Two pieces, both proven to work with the window
unfocused and another app frontmost:

  EYES  - CGWindowListCreateImage captures a specific window by id even when it
          is not the active app (and even when occluded).

  HANDS - the input path macOS actually gates on is the app being *active*, not
          merely its window being key. A normal CGEvent can't cross that. But
          the window server accepts a synthesized event record delivered
          straight to a process via SkyLight's SLPSPostEventRecordTo (the same
          mechanism yabai uses to focus windows without raising them). Written
          with a real location, it lands a positioned tap/drag in iPhone
          Mirroring while your frontmost app never changes.

The event-record layout is yabai's (window_manager_make_key_window): a 0xf8
buffer, length at 0x04, CGSEventType at 0x08 (1=down, 2=up, 6=dragged),
location at 0x10 and windowLocation at 0x20 as CGPoints, window id at 0x3c.
yabai blanks the location to signal "focus"; we write the real point to make
it a click. Every write stays inside the buffer, so a wrong value is a no-op,
never a crash.

Select with PHONE_HARNESS_BACKGROUND=1. Coordinates use the same global
screen-point convention as the mirror backend and screen(), so every helper on
top (tap_text, swipe, scroll_collect) works unchanged — just without focus.
"""
import ctypes, os, struct, subprocess, tempfile, time
from contextlib import contextmanager
from pathlib import Path

from Foundation import NSBundle

# Framework builds of Python (Homebrew, python.org) re-exec inside Python.app,
# whose Info.plist has CFBundlePackageType=APPL and no LSBackgroundOnly. Any
# call into HIServices (such as GetProcessForPID) runs _RegisterApplication on
# the calling process, which reads that Info.plist and registers Python with
# launchservicesd and WindowServer as a Foreground GUI app (foreground=1),
# popping a Python icon into the Dock/Cmd+Tab and stealing frontmost status.
# Marking the in-memory bundle dictionary LSBackgroundOnly before any
# registration check-in keeps Python background-only (foreground=0).
try:
    _bundle_info = NSBundle.mainBundle().infoDictionary()
    if _bundle_info is not None:
        _bundle_info["LSBackgroundOnly"] = "1"
except Exception:
    pass

import objc
import Quartz
from AppKit import NSRunningApplication

from . import mirror

APP_NAME = "iPhone Mirroring"
BUNDLE_ID = "com.apple.ScreenContinuity"
TMP = Path(tempfile.gettempdir()) / "phone-harness"
TMP.mkdir(exist_ok=True)

# --- SkyLight & CoreServices private API ---
_libc = ctypes.CDLL(None)
_libc.mach_absolute_time.restype = ctypes.c_uint64
_sky = ctypes.CDLL("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight")
_cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_cs = ctypes.CDLL("/System/Library/Frameworks/CoreServices.framework/CoreServices")


class _PSN(ctypes.Structure):
    _fields_ = [("hi", ctypes.c_uint32), ("lo", ctypes.c_uint32)]


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


_sky.SLSMainConnectionID.argtypes = []
_sky.SLSMainConnectionID.restype = ctypes.c_uint32
_sky.SLSMainConnectionID()
_sky._SLPSSetFrontProcessWithOptions.argtypes = [
    ctypes.POINTER(_PSN), ctypes.c_uint32, ctypes.c_uint32]
_sky.SLPSPostEventRecordTo.argtypes = [ctypes.POINTER(_PSN), ctypes.c_void_p]
_sky.SLEventSetWindowLocation.argtypes = [ctypes.c_void_p, _CGPoint]
_sky.SLEventSetWindowLocation.restype = ctypes.c_int

_cf.CFNumberCreate.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p]
_cf.CFNumberCreate.restype = ctypes.c_void_p
_cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
_cf.CFArrayGetCount.restype = ctypes.c_long
_cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
_cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
_cf.CFRelease.argtypes = [ctypes.c_void_p]

_cs._LSCopyMatchingApplicationsWithItems.argtypes = [
    ctypes.c_int, ctypes.c_int,
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)]
_cs._LSCopyMatchingApplicationsWithItems.restype = ctypes.c_void_p
_cs._LSASNExtractHighAndLowParts.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
_cs._LSASNExtractHighAndLowParts.restype = ctypes.c_uint32
_kLSPIDKey = ctypes.c_void_p.in_dll(_cs, "_kLSPIDKey")


def _psn_for_pid(pid):
    """Resolve a PID's ProcessSerialNumber via CoreServices without calling
    HIServices.GetProcessForPID (which calls INIT_Processes -> _RegisterApplication
    on the calling Python process and promotes it to a foreground GUI app)."""
    psn = _PSN()
    pid_val = ctypes.c_int64(int(pid))
    num = _cf.CFNumberCreate(None, 4, ctypes.byref(pid_val))  # kCFNumberSInt64Type = 4
    num_ptr = ctypes.c_void_p(num)
    arr = _cs._LSCopyMatchingApplicationsWithItems(
        -2, 1, ctypes.byref(_kLSPIDKey), ctypes.byref(num_ptr))
    if arr and _cf.CFArrayGetCount(arr) >= 1:
        asn = _cf.CFArrayGetValueAtIndex(arr, 0)
        hi, lo = ctypes.c_uint32(), ctypes.c_uint32()
        _cs._LSASNExtractHighAndLowParts(
            asn, ctypes.byref(hi), ctypes.byref(lo))
        psn.hi, psn.lo = hi.value, lo.value
    if arr:
        _cf.CFRelease(arr)
    if num:
        _cf.CFRelease(num)
    return psn


_KCPS_USER_GENERATED = 0x200
# CGSEventType values (share the CGEventType numbering)
_LMOUSE_DOWN, _LMOUSE_UP, _LMOUSE_DRAGGED = 1, 2, 6


# --- window / app state (reused from mirror; none of these activate) ---

def find_window():
    """The phone window, on the active Space or not.

    SkyLight event records are addressed to a process and a window id, so this
    backend reaches the window wherever it is. mirror.py keeps the on-screen
    default because it posts CGEvents at global screen coordinates, which land
    on whatever is actually in front.

    Inheriting mirror's default is why a terminal on a different Space from the
    phone made the harness report a disconnected phone (#8) — the window was
    there the whole time, just not on the Space being asked about.
    """
    return mirror.find_window(on_screen=False)


running_app = mirror.running_app
window_ax_content = mirror.window_ax_content
window_ax_buttons = mirror.window_ax_buttons
press_window_button = mirror.press_window_button
ax_observer_target = mirror.ax_observer_target
ax_session_hint = mirror.ax_session_hint
launch = mirror.launch
focus_probe = mirror.focus_probe
interruption = mirror.interruption
frontmost_window = mirror.frontmost_window


# Shared with the mirror backend. Nothing here needs focus, but screen_info()
# reports the value and a wrong answer sends people hunting for focus bugs
# that are not there.
is_frontmost = mirror.is_frontmost


def activate():
    """No-op: the whole point of this backend is never to take focus."""
    return None


# session.require shows the window once at the start of a task so the user
# can watch; that is the only time this backend raises it on purpose.
show = mirror.activate


def ensure_window(timeout=5.0):
    win = find_window()
    if win is None:
        if running_app() is None:
            raise RuntimeError(
                f"{APP_NAME} isn't running — open it and connect your phone.")
        raise RuntimeError(
            f"{APP_NAME} has no phone window — connect your phone, then retry.")
    return win


# --- capture (eyes), no focus ---

def capture(path=None, retries=2):
    """Capture the mirroring window as a PNG without activating it.

    Returns (path, window_bounds), matching mirror.capture()."""
    path = str(path or TMP / "background.png")
    last = None
    for _ in range(retries + 1):
        win = find_window() or ensure_window()
        img = Quartz.CGWindowListCreateImage(
            Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow,
            win["id"],
            Quartz.kCGWindowImageBoundsIgnoreFraming
            | Quartz.kCGWindowImageNominalResolution)
        if img is not None and Quartz.CGImageGetWidth(img) > 0:
            url = Quartz.CFURLCreateWithFileSystemPath(
                None, path, Quartz.kCFURLPOSIXPathStyle, False)
            dest = Quartz.CGImageDestinationCreateWithURL(url, "public.png", 1, None)
            Quartz.CGImageDestinationAddImage(dest, img, None)
            if Quartz.CGImageDestinationFinalize(dest):
                return path, win
            last = "could not encode PNG"
        elif _screencapture(win, path):
            # CGWindowListCreateImage renders nothing for a window that is not
            # on the active Space. `screencapture -l` still reaches it, with
            # live frames rather than a stale backing store. It costs a
            # subprocess, so it stays the fallback. Its image is Retina where
            # the fast path is point-resolution; ocr.recognize derives the
            # scale from the window, so both work.
            return path, win
        else:
            last = "CGWindowListCreateImage returned nothing, screencapture failed"
        time.sleep(0.3)
    raise RuntimeError(f"background capture failed: {last}")


def _screencapture(win, path):
    """Window capture via the screencapture binary. True if it wrote a PNG."""
    if os.path.exists(path):
        os.remove(path)
    subprocess.run(["screencapture", "-x", "-o", "-l", str(win["id"]), path],
                   capture_output=True)
    return os.path.exists(path) and os.path.getsize(path) > 1000


# --- input (hands), no focus ---

def _post(pid, wid, etype, gx, gy, lx, ly):
    """Deliver one synthesized mouse event record to the process by pid."""
    buf = (ctypes.c_uint8 * 0xf8)()
    buf[0x04] = 0xf8                       # record length
    buf[0x3a] = 0x10                       # yabai's required flag byte
    struct.pack_into("<I", buf, 0x3c, wid)          # target window id
    struct.pack_into("<dd", buf, 0x10, gx, gy)      # location (global points)
    struct.pack_into("<dd", buf, 0x20, lx, ly)      # windowLocation (local)
    buf[0x08] = etype
    psn = _psn_for_pid(pid)
    _sky._SLPSSetFrontProcessWithOptions(ctypes.byref(psn), wid, _KCPS_USER_GENERATED)
    _sky.SLPSPostEventRecordTo(ctypes.byref(psn), ctypes.byref(buf))


def _ctx():
    win = find_window() or ensure_window()
    return running_app().processIdentifier(), win


def _emit(etype, gx, gy, pid, win):
    _post(pid, win["id"], etype, gx, gy, gx - win["x"], gy - win["y"])


def tap(x, y):
    pid, win = _ctx()
    _emit(_LMOUSE_DOWN, x, y, pid, win)
    time.sleep(0.05)
    _emit(_LMOUSE_UP, x, y, pid, win)


def long_press(x, y, duration=0.8):
    pid, win = _ctx()
    _emit(_LMOUSE_DOWN, x, y, pid, win)
    time.sleep(duration)
    _emit(_LMOUSE_UP, x, y, pid, win)


def drag(x1, y1, x2, y2, duration=0.35, steps=14):
    """Touch-drag (an iOS swipe), delivered with the window unfocused."""
    pid, win = _ctx()
    _emit(_LMOUSE_DOWN, x1, y1, pid, win)
    time.sleep(0.02)
    for i in range(1, steps + 1):
        t = i / steps
        _emit(_LMOUSE_DRAGGED, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, pid, win)
        time.sleep(duration / steps)
    _emit(_LMOUSE_UP, x2, y2, pid, win)


_SCROLL_PT_PER_EVENT = 7
_SCROLL_EVENT_DELTA = 12
_SCROLL_STEP_INTERVAL = 0.008
_SCROLL_PHASE_BEGAN, _SCROLL_PHASE_CHANGED, _SCROLL_PHASE_ENDED = 1, 2, 4


def _target_event_to_window(ev, wid, gx, gy, lx, ly):
    """Stamp a CGEvent with window-id and window-local coordinates so
    CGEventPostToPid delivers it to the mirroring window without moving the
    physical cursor.

    Posting directly to a PID bypasses WindowServer's cursor hit-test, which
    normally populates field 51 (kCGEventWindowNumber) and the event record's
    window-local location. Without field 51 set, -[NSEvent _initWithCGEvent:]
    calls [NSApp windowWithWindowNumber:0], gets nil, and drops the event in
    -[NSApplication sendEvent:]. Furthermore, CGEventSetLocation (backed by
    SLEventSetLocation) resets the internal windowLocation to (-1, -1), causing
    -[NSThemeFrame _hitTestForEvent:] to miss CaptureView unless
    SLEventSetWindowLocation is called afterwards with (lx, ly).
    """
    Quartz.CGEventSetLocation(ev, Quartz.CGPointMake(gx, gy))
    Quartz.CGEventSetIntegerValueField(ev, 51, wid)
    Quartz.CGEventSetIntegerValueField(ev, 52, wid)
    Quartz.CGEventSetIntegerValueField(ev, 91, wid)
    _sky.SLEventSetWindowLocation(objc.pyobjc_id(ev), _CGPoint(lx, ly))
    Quartz.CGEventSetTimestamp(ev, _libc.mach_absolute_time())


def _scroll_event(wid, gx, gy, lx, ly, phase, pt_y, pt_x):
    ev = Quartz.CGEventCreateScrollWheelEvent(
        None, Quartz.kCGScrollEventUnitPixel, 2, 0, 0)
    sf = lambda f, v: Quartz.CGEventSetIntegerValueField(ev, f, v)
    sf(88, 1)                                           # IsContinuous
    sf(99, phase)                                       # ScrollPhase
    sf(96, pt_y)                                        # PointDeltaAxis1
    sf(93, int(pt_y * 65536 / 10))                      # FixedPtDeltaAxis1
    sf(11, -1 if pt_y < 0 else (1 if pt_y > 0 else 0))  # DeltaAxis1
    sf(97, pt_x)                                        # PointDeltaAxis2
    if pt_x:
        sf(94, int(pt_x * 65536 / 10))                  # FixedPtDeltaAxis2
        sf(12, -1 if pt_x < 0 else 1)                   # DeltaAxis2
    _target_event_to_window(ev, wid, gx, gy, lx, ly)
    return ev


def scroll_wheel(dy, x, y, steps=6, dx=0):
    """Scroll at (x, y) in the background without warping the mouse cursor or
    raising the window.

    Delivers a window-targeted kCGEventMouseMoved (so UniversalHID updates the
    virtual pointer position inside CaptureViewController) followed by a phased
    trackpad scroll gesture (began -> changed -> ended) via CGEventPostToPid.
    """
    if dy == 0 and dx == 0:
        return
    pid, win = _ctx()
    wid = win["id"]
    _make_key(pid, wid)

    lx, ly = x - win["x"], y - win["y"]
    mv = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventMouseMoved, Quartz.CGPointMake(x, y), 0)
    _target_event_to_window(mv, wid, x, y, lx, ly)
    Quartz.CGEventPostToPid(pid, mv)
    time.sleep(0.04)

    dist = max(abs(dy), abs(dx))
    n = max(1, round(dist / _SCROLL_PT_PER_EVENT))
    pt_y = (_SCROLL_EVENT_DELTA if dy > 0 else -_SCROLL_EVENT_DELTA) if dy else 0
    pt_x = (_SCROLL_EVENT_DELTA if dx > 0 else -_SCROLL_EVENT_DELTA) if dx else 0
    sequence = (
        [(_SCROLL_PHASE_BEGAN, pt_y, pt_x)]
        + [(_SCROLL_PHASE_CHANGED, pt_y, pt_x)] * n
        + [(_SCROLL_PHASE_ENDED, 0, 0)]
    )
    for phase, py, px in sequence:
        Quartz.CGEventPostToPid(
            pid, _scroll_event(wid, x, y, lx, ly, phase, py, px))
        time.sleep(_SCROLL_STEP_INTERVAL)


# --- keyboard (background), via make-key + CGEventPostToPid ---
#
# Keyboard is delivered differently from mouse: the keystroke goes to the key
# window's text responder. Making the window key (yabai's focus record: a
# down/up pair with a blanked location) and then posting the key event to the
# process by pid lands the text with no activation — verified by typing into
# Spotlight while another app stayed frontmost.

def _make_key(pid, wid):
    buf = (ctypes.c_uint8 * 0xf8)()
    buf[0x04] = 0xf8
    buf[0x3a] = 0x10
    struct.pack_into("<I", buf, 0x3c, wid)
    for i in range(0x10):
        buf[0x20 + i] = 0xff            # blanked location => "focus", not a click
    psn = _psn_for_pid(pid)
    _sky._SLPSSetFrontProcessWithOptions(ctypes.byref(psn), wid, _KCPS_USER_GENERATED)
    buf[0x08] = _LMOUSE_DOWN
    _sky.SLPSPostEventRecordTo(ctypes.byref(psn), ctypes.byref(buf))
    buf[0x08] = _LMOUSE_UP
    _sky.SLPSPostEventRecordTo(ctypes.byref(psn), ctypes.byref(buf))


def _key_edge(pid, wid, code, down, flags=0):
    _make_key(pid, wid)                 # keep the window key across the keystroke
    ev = Quartz.CGEventCreateKeyboardEvent(None, code, down)
    if flags:
        # Mac-side consumers only. iPhone Mirroring forwards raw HID keycodes
        # to iOS and drops the flag mask, so a modifier expressed as a flag
        # never reaches the phone — it has to be a key that is held.
        Quartz.CGEventSetFlags(ev, flags)
    Quartz.CGEventPostToPid(pid, ev)
    time.sleep(0.03)


def _key(pid, wid, code, flags=0):
    for down in (True, False):
        _key_edge(pid, wid, code, down, flags)


@contextmanager
def _holding(pid, wid, mods):
    """Hold real modifier keys down around the body. See mirror._holding."""
    acc = 0
    for m in mods:
        acc |= mirror._MODIFIERS[m]
        _key_edge(pid, wid, mirror._MOD_KEYCODES[m], True, acc)
    try:
        yield acc
    finally:
        # Unconditional: a latched modifier corrupts every later keystroke, and
        # the damage surfaces somewhere else entirely.
        for m in reversed(mods):
            # Clear the bit *before* posting the release. A key-up still
            # carrying its own flag reads as "still held", which latches shift
            # on and turns the rest of the string into 1,200 -> !<@)).
            acc &= ~mirror._MODIFIERS[m]
            _key_edge(pid, wid, mirror._MOD_KEYCODES[m], False, acc)


def press(combo):
    """press('return'), press('cmd+1'), press('cmd+3') — no focus change."""
    pid, win = _ctx()
    parts = combo.lower().split("+")
    key, mods = parts[-1], parts[:-1]
    if key not in mirror._KEYCODES:
        raise ValueError(f"unknown key {key!r}")
    for m in mods:
        if m not in mirror._MOD_KEYCODES:
            raise ValueError(f"unknown modifier {m!r}")
    with _holding(pid, win["id"], mods) as flags:
        _key(pid, win["id"], mirror._KEYCODES[key], flags)


def _type_keystrokes(text, delay=0.03):
    """Type via real keycodes, no focus change. Subject to iOS autocorrect,
    which rewrites words as they are typed."""
    pid, win = _ctx()
    for i, line in enumerate(text.split("\n")):
        if i:
            _key(pid, win["id"], mirror._KEYCODES["return"])
        for ch in line:
            code, shifted = mirror._keycode_for(ch)
            if code is None:
                raise ValueError(f"cannot type {ch!r} via keycodes")
            with _holding(pid, win["id"], ["shift"] if shifted else []) as flags:
                _key(pid, win["id"], code, flags)
            time.sleep(delay)


def type_text(text, delay=0.03, keystrokes=False):
    """Type into the focused iOS field, no focus change.

    Pastes by default so the text arrives exactly as written, past autocorrect
    and keyboard layout both. keystrokes=True sends real key events instead.
    """
    if keystrokes or not text:
        return _type_keystrokes(text, delay)
    mirror.paste_with(press, text)
