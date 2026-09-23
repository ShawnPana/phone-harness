"""Diagnostics: `phone-harness --doctor` walks the ladder for the phone the
helpers would drive — the config default, or `--doctor ios|android|coredevice`.
`ios` means iPhone Mirroring on a Mac and the USB CoreDevice backend elsewhere."""
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


def run_doctor(platform=None):
    from . import config
    platform = (platform or config.get("platform")).lower()
    print(f"phone-harness doctor — {platform}"
          f"{'' if platform == config.get('platform') else '  (default is ' + config.get('platform') + ')'}\n")
    _failures.clear()
    if platform == "android":
        _doctor_android()
    elif platform in ("coredevice", "ios-usb", "usb") or (
            platform in ("ios", "iphone", "ipad") and sys.platform != "darwin"):
        _doctor_coredevice()
    else:
        _doctor_ios()
    ok = not _failures
    print("\nall clear" if ok else "\nfix the FAILs above, then re-run")
    return 0 if ok else 1


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


# --- iPhone over USB (CoreDevice): python -> library -> usbmuxd -> phone ->
#     trust -> iOS version -> Developer Mode -> image -> session -> capture+OCR

_USBMUXD_INSTALL = {
    "linux": "apt install usbmuxd (or your distro's usbmuxd) and replug the phone",
    "win32": "install iTunes or the Apple Devices app (they provide the Apple Mobile Device service)",
}.get(sys.platform, "is the phone plugged in and unlocked?")


def _doctor_coredevice():
    import asyncio
    from . import config, ocr
    _check(f"Python {sys.version_info.major}.{sys.version_info.minor} (3.13+ needed for the USB tunnel)",
           sys.version_info >= (3, 13), "reinstall with a newer Python: uv tool install --python 3.13 'phone-harness[iphone]'")
    try:
        import pymobiledevice3  # noqa: F401
        _check("pymobiledevice3 installed", True)
    except ImportError:
        _check("pymobiledevice3 installed", False, "pip install 'phone-harness[iphone]'")
        return
    eng = ocr.engine()
    _check(f"OCR engine: {eng or 'none'}", eng is not None,
           "pip install rapidocr-onnxruntime (part of phone-harness[iphone])")

    from . import coredevice, coredevice_daemon as D
    serial = config.get("coredevice.serial") or config.devices_of("coredevice").get("primary")
    connection = str(config.get("coredevice.connection") or "auto")
    info = asyncio.run(D.probe(serial, connection))
    if info.get("connection") == "wifi":
        _check(f"a Wi-Fi pairing is saved ({', '.join(info['wifi_records']) or 'none'})",
               bool(info["wifi_records"]),
               "plug the phone in once and run `phone-harness ios pair --wifi`")
        if not info["wifi_records"]:
            return
        eps = ", ".join(f"{h}:{p}" for h, p in info["wifi_endpoints"])
        _check(f"the phone advertises on this Wi-Fi ({eps or 'not found'})", bool(info["wifi_endpoints"]),
               "same network as this computer, Wi-Fi on, unlocked; or `awake --address IP:PORT`")
        if not info["wifi_endpoints"]:
            return
        _check("Developer Mode and the developer image cannot be checked over Wi-Fi; "
               "awake reports them if they are missing", True)
    else:
        _check("USB device service present (usbmuxd / Apple Mobile Device)", info["usbmuxd"],
               _USBMUXD_INSTALL)
        if not info["usbmuxd"]:
            return
        _check(f"an iPhone on USB ({', '.join(info['devices']) or 'none'})", bool(info["udid"]),
               "plug the phone in with a data cable and unlock it (on Linux usbmuxd starts when it appears)"
               if info["error"] != "several-devices" else
               "several phones: phone-harness config set coredevice.serial UDID")
        if not info["udid"]:
            return
        _check("this computer is trusted by the phone", bool(info["paired"]),
               "phone-harness ios pair — then tap Trust and enter the passcode on the phone")
        if not info["paired"]:
            return
        _check(f"{info['name']} ({info['model']}) on iOS {info['ios']}: 27 or later",
               D.ios_at_least(info["ios"], 27, 0),
               "screen streaming has only been seen working on iOS 27; older phones report no media features")
        _check("Developer Mode on", bool(info["developer_mode"]),
               "on the phone: Settings > Privacy & Security > Developer Mode "
               "(`phone-harness ios reveal` if it is not listed)")
        if not info["developer_mode"]:
            return
        _check("developer image mounted (awake mounts it if not)", bool(info["ddi_mounted"]),
               "`phone-harness ios awake` will download and mount it", fatal=False)

    st = coredevice._state()
    _check(f"session running over {(st or {}).get('connection') or connection} (phone-harness ios awake)",
           bool(st and st.get("ready")),
           "run `phone-harness ios awake --bg` and re-run the doctor")
    if not (st and st.get("ready")):
        return
    phone = coredevice.CoreDevice()
    try:
        path, win = phone.send("screen.capture")
        size = os.path.getsize(path)
        _check(f"screenshot works ({win['w']}x{win['h']} px, {size} bytes)", size > 1000)
        n = len(ocr.recognize(path, win))
        _check(f"OCR works ({n} text boxes)", True)
    except Exception as e:
        _check("screenshot + OCR", False, str(e)[:160])
        return
    state = phone.send("session.state")
    _check(f"session state: {state}", state == "ready",
           "unlock the phone on the phone itself" if state == "locked" else "", fatal=False)
