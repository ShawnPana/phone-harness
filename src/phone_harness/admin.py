"""Diagnostics: `phone-harness doctor` walks the ladder for the phone the
helpers would drive.

With no platform argument, that is the same choice as connect(): an attached
cloud iPhone (a control link) is checked over HTTPS — the control URL, then
screen.text, screen.capture, and screen.bounds — and adb is not consulted.
`doctor android` and PHONE_HARNESS_PLATFORM still check the local phone.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _check(label, ok, hint="", fatal=True):
    """Print a check; a fatal failure is remembered for the verdict."""
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {hint}" if not ok and hint else ""))
    if not ok and fatal:
        _failures.append(label)
    return ok


_failures = []


def _doctor_target(platform):
    """Which phone to check, using the same choice as connect().

    An explicit platform (`doctor android`) or PHONE_HARNESS_PLATFORM names
    the local phone. With neither, an attached cloud iPhone control link is
    the phone the helpers drive, over HTTPS. Returns ("cloud-iphone", session)
    or ("android"|"ios", None).
    """
    from . import config
    if platform is None:
        platform, source = config.lookup("platform")
        if not str(source).startswith("env:"):
            from . import cloud
            rented = cloud.attached()
            if rented and rented["link"]["kind"] == "control":
                return "cloud-iphone", rented
            if rented:
                platform = "android"
    name = str(platform).lower()
    if name == "android":
        return "android", None
    return "ios", None


def run_doctor(platform=None):
    from . import config
    kind, session = _doctor_target(platform)
    label = "cloud iphone" if kind == "cloud-iphone" else kind
    default = config.get("platform")
    print(f"phone-harness doctor — {label}"
          f"{'' if label == default else '  (default is ' + str(default) + ')'}\n")
    _failures.clear()
    if kind == "cloud-iphone":
        _doctor_cloud_iphone(session)
    elif kind == "android":
        _doctor_android()
    else:
        _doctor_ios()
    ok = not _failures
    print("\nall clear" if ok else "\nfix the FAILs above, then re-run")
    return 0 if ok else 1


def _short(exc):
    return str(exc).splitlines()[0][:160]


def _doctor_cloud_iphone(session):
    """The attached cloud iPhone connect() would drive. HTTPS only: no adb."""
    from .remote import Remote
    link = session["link"]
    url = str(link.get("url") or "")
    phone = Remote(url, link.get("token") or "")
    try:
        phone._call("GET", "/ops", timeout=20)
    except Exception as exc:
        _check(f"control URL reachable ({url})", False, _short(exc)
               or "the control URL did not answer")
        return
    _check(f"control URL reachable ({url})", True)
    _check_cloud_text(phone)
    _check_cloud_capture(phone)
    _check_cloud_bounds(phone)


def _check_cloud_text(phone):
    try:
        boxes = phone.send("screen.text")
    except Exception as exc:
        _check("screen.text", False, _short(exc))
        return
    ok = isinstance(boxes, list)
    n = len(boxes) if ok else 0
    _check(f"screen.text ({n} text boxes)", ok, "screen.text did not return text boxes")


def _check_cloud_capture(phone):
    path = None
    try:
        path, bounds = phone.send("screen.capture")
        size = os.path.getsize(path) if path and os.path.exists(path) else 0
        ok = size > 0 and isinstance(bounds, dict)
        _check(f"screen.capture ({size} bytes)", ok, "capture failed or is empty")
    except Exception as exc:
        _check("screen.capture", False, _short(exc))
    finally:
        if path and os.path.exists(path):
            os.unlink(path)


def _check_cloud_bounds(phone):
    try:
        bounds = phone.send("screen.bounds")
    except Exception as exc:
        _check("screen.bounds", False, _short(exc))
        return
    ok = isinstance(bounds, dict) and bounds.get("w") and bounds.get("h")
    shown = f"{bounds.get('w')}x{bounds.get('h')}" if isinstance(bounds, dict) else "missing"
    _check(f"screen.bounds ({shown})", bool(ok), "screen.bounds did not return a screen size")


# --- iPhone: pyobjc -> permissions -> Mirroring -> capture -> OCR -----------

def _doctor_ios():
    try:
        import Quartz, Vision, AppKit  # noqa: F401
        _check("pyobjc frameworks (Quartz, Vision, AppKit)", True)
    except ImportError as e:
        _check("pyobjc frameworks", False,
               f"pip install pyobjc-framework-Quartz pyobjc-framework-Vision "
               f"pyobjc-framework-Cocoa ({e})")
        return

    from ApplicationServices import AXIsProcessTrusted
    _check("Accessibility permission (taps & keystrokes)", AXIsProcessTrusted(),
           "System Settings > Privacy & Security > Accessibility: enable your terminal")

    import Quartz as Q
    _check("Screen Recording permission (seeing the phone)",
           bool(Q.CGPreflightScreenCaptureAccess()),
           "System Settings > Privacy & Security > Screen Recording: enable your terminal")

    from . import mirror
    _check(f"{mirror.APP_NAME} installed", Path(mirror.APP_PATH).exists(),
           "requires macOS Sequoia+ with a paired iPhone")

    running = mirror.running_app() is not None
    _check(f"{mirror.APP_NAME} running", running,
           "will auto-launch on first use — not fatal", fatal=False)

    win = mirror.find_window()
    _check("mirroring window found", win is not None,
           "open iPhone Mirroring once manually to pair the phone")
    if not win:
        return

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        path = f.name
    try:
        # Never crash here: a missing Screen Recording grant is exactly what
        # this check exists to report.
        r = subprocess.run(["screencapture", "-x", "-o", "-l", str(win["id"]), path],
                           capture_output=True)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        good = r.returncode == 0 and size > 20_000
        _check(f"window capture works ({size} bytes)", good,
               "capture failed or is blank — Screen Recording permission "
               "needs a terminal restart to take effect")
        if good:
            from . import ocr
            n = len(ocr.recognize(path, win))
            _check(f"Vision OCR works ({n} text boxes)", True)
    finally:
        if os.path.exists(path):
            os.unlink(path)

    from . import ios
    state = ios.IPhone().send("session.state")
    _check(f"session state: {state}", state == "ready",
           "an interstitial is up (iPhone in Use / Connect / Mac Locked) — "
           "clear it on the Mac; lock the iPhone if it says in use", fatal=False)


# --- Android: adb -> a phone -> authorised -> awake -> tree ------------------

_ADB_INSTALL = {"darwin": "brew install android-platform-tools",
                "win32": "winget install Google.PlatformTools"}.get(sys.platform, "apt install adb (or your distro's android-tools)")
_SCRCPY_INSTALL = {"darwin": "brew install scrcpy", "win32": "winget install Genymobile.scrcpy"}.get(sys.platform, "apt install scrcpy")


def _doctor_android():
    from . import config
    adb = str(config.get("android.adb"))
    if not shutil.which(adb):
        _check(f"adb found ({adb})", False,
               _ADB_INSTALL + ", or set android.adb to the binary")
        return
    _check(f"adb found ({shutil.which(adb)})", True)
    _check("scrcpy found (optional: live mirror during `android awake`)",
           bool(shutil.which("scrcpy")), _SCRCPY_INSTALL + " — not required", fatal=False)

    from . import android
    phone = android.Android()
    state = phone.send("session.state")
    hints = {
        "no-device": "plug in with USB debugging on and tap Allow, or "
                     "Wireless debugging + `phone-harness android pair CODE`",
        "unauthorized": "tap Allow on the phone's 'Allow USB debugging?' prompt",
        "offline": "unplug/replug, or toggle Wireless debugging off and on",
        "locked": "unlock the phone (`phone-harness android awake` keeps it so)",
        "no-adb": "adb did not answer",
    }
    _check(f"a phone is reachable and ready (state: {state})",
           state in ("ready", "locked"), hints.get(state, ""))
    if state == "locked":
        _check("phone unlocked", False, hints["locked"], fatal=False)
    if state not in ("ready",):
        return

    b = phone.send("screen.bounds")
    label = android._phone_label(b["id"]) if b else "?"
    _check(f"talking to {label} ({b['id']}), screen {b['w']}x{b['h']}", bool(b))
    try:
        n = len(phone.send("tree"))
        _check(f"accessibility tree readable ({n} nodes)", n > 0)
    except RuntimeError as e:
        _check("accessibility tree readable", False, str(e)[:120], fatal=False)
    reg = config.devices_of("android")
    _check(f"remembered phones: {', '.join(reg['phones']) or 'none'}; primary: "
           f"{reg['primary'] or 'none'}", True)
