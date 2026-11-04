"""Opt-in native Mirroring input, loaded only by this directory's run script."""
import json as _json
import subprocess as _subprocess
from contextlib import contextmanager as _contextmanager
from pathlib import Path as _Path

from Foundation import NSRunLoop as _NSRunLoop, NSDate as _NSDate
from phone_harness import helpers as _h
from phone_harness import background as _background
from phone_harness.transport import Unsupported as _Unsupported

if _h.phone.name != "iphone-mirroring":
    raise RuntimeError("This experiment requires the local iPhone Mirroring backend")

_binary = _Path(__file__).resolve().parent / "native/mirror_events"
_NO_FRONT = False


def native_raw(action, *options, send=True):
    """Call the native helper with explicit options, including the event route.

    Submission is not a phone acknowledgment: check the screen separately.
    Use send=False for a dry run. no_front() also applies to these calls.
    """
    _h.phone._raise_if_not_live()
    args = [str(_binary), str(action), *map(str, options)]
    if _NO_FRONT and action != "info" and "--no-front" not in args:
        args.append("--no-front")
    if send and "--send" not in args:
        args.append("--send")
    result = _subprocess.run(args, capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Native helper failed")
    # Deliver AppKit updates after the child exits: the app lookup can briefly
    # be empty while its window still exists. Keep the normal liveness guards.
    _NSRunLoop.currentRunLoop().runUntilDate_(
        _NSDate.dateWithTimeIntervalSinceNow_(.1))
    _h.phone._raise_if_not_live()
    return _json.loads(result.stdout)


def _native(action, *options):
    route = "--minimal-record" if action == "tap" else "--pid-route"
    return native_raw(action, *options, route)


def norm_point(x, y):
    """Global Mac screen point -> normalized full-window coordinates."""
    win = _background.ensure_window()
    return (x - win["x"]) / win["w"], (y - win["y"]) / win["h"]


def _tap(x, y, *options):
    nx, ny = norm_point(x, y)
    if not (0 <= nx <= 1 and 0 <= ny <= 1):
        raise ValueError("Tap is outside the current Mirroring window")
    return _native("tap", nx, ny, *options)


class _ScratchInput:
    def __getattr__(self, name):
        return getattr(_background, name)

    def tap(self, x, y):
        return _tap(x, y)

    def press(self, combo):
        if combo.lower() == "cmd+1" and not _NO_FRONT:
            return _native("home")
        return _background.press(combo)

    def scroll_wheel(self, dy, x, y, steps=6, dx=0):
        raise _Unsupported("Cursor-free scrolling is unverified; no cursor-warp fallback")


_h.phone.mirror = _ScratchInput()
_h.phone.background = True
_original_supports = _h.phone.supports
_h.phone.supports = lambda op: op != "input.scroll" and _original_supports(op)


def double_tap(x, y):
    """Down/up/down/up at 50 ms spacing in one helper call."""
    return _tap(x, y, "--double")


def native_nav(action):
    """Send Home, app switcher or Spotlight through the native PID key route."""
    if action not in {"home", "app-switcher", "spotlight"}:
        raise ValueError("Expected home, app-switcher or spotlight")
    return _native(action)


def native_probe(action, delta, *options):
    """Submit an experimental gesture; inspect the phone effect separately."""
    if action not in {"scroll", "magnify", "rotate", "translate", "swipe"}:
        raise ValueError("Expected scroll, magnify, rotate, translate or swipe")
    return _native(action, delta, *options)


def _skip_front(psn, wid, opts):
    return 0


@_contextmanager
def no_front():
    """Skip front-process preparation for input inside this synchronous context.

    Preserve event encoding, timing and liveness guards. Does not suppress
    explicit activation/reconnection. This patches process-local functions;
    do not overlap it with another thread's input.
    """
    global _NO_FRONT
    prev_flag = _NO_FRONT
    prev_front = _background._sky._SLPSSetFrontProcessWithOptions
    _NO_FRONT = True
    _background._sky._SLPSSetFrontProcessWithOptions = _skip_front
    try:
        yield
    finally:
        _background._sky._SLPSSetFrontProcessWithOptions = prev_front
        _NO_FRONT = prev_flag
