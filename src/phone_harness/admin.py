"""Diagnostics: `phone-harness --doctor` walks the ladder for the phone the
helpers would drive — the config default, or `--doctor ios|android`.

`--fix` asks macOS for the missing Accessibility and Screen Recording grants
and opens the matching Settings pane. It waits only when stdin is a terminal.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _check(label, ok, hint="", fatal=True, step=None):
    """Print a check; a fatal failure is remembered for the verdict.

    `step` is a stable id for telemetry (no paths, no app names). The label
    stays human and can carry a byte count or a session state.
    """
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {hint}" if not ok and hint else ""))
    if not ok:
        _failed_steps.append(step or label)
        if fatal:
            _failures.append(label)
    return ok


_failures = []
_failed_steps = []
# Filled by the iOS permission pass. Android leaves the defaults. Read by
# the CLI after doctor exits so telemetry can say which step failed and
# whether a prompt was asked for — never which app.
_permission_meta = {"prompt_shown": False, "granted_after_prompt": None}
last_report = None


def run_doctor(platform=None, fix=False):
    from . import config
    global last_report
    platform = (platform or config.get("platform")).lower()
    print(f"phone-harness doctor — {platform}"
          f"{'' if platform == config.get('platform') else '  (default is ' + config.get('platform') + ')'}\n")
    _failures.clear()
    _failed_steps.clear()
    _permission_meta["prompt_shown"] = False
    _permission_meta["granted_after_prompt"] = None
    try:
        if platform == "android":
            if fix:
                print("`--fix` requests macOS permissions; it does not change Android checks.\n")
            _doctor_android()
        else:
            _doctor_ios(fix=fix)
    finally:
        last_report = {
            "platform": platform,
            "failed_steps": list(_failed_steps),
            "prompt_shown": _permission_meta["prompt_shown"],
            "granted_after_prompt": _permission_meta["granted_after_prompt"],
        }
    ok = not _failures
    print("\nall clear" if ok else "\nfix the FAILs above, then re-run")
    return 0 if ok else 1


# --- iPhone: pyobjc -> permissions -> Mirroring -> capture -> OCR -----------

def _doctor_ios(fix=False):
    from . import macos_permissions as mp
    # The script-path warning would otherwise print again when session.state
    # captures the window below.
    mp.set_quiet(True)
    try:
        return _doctor_ios_body(fix, mp)
    finally:
        mp.set_quiet(False)


def _doctor_ios_body(fix, mp):
    try:
        import Quartz, Vision, AppKit  # noqa: F401
        _check("pyobjc frameworks (Quartz, Vision, AppKit)", True, step="pyobjc")
    except ImportError as e:
        _check("pyobjc frameworks", False,
               f"pip install pyobjc-framework-Quartz pyobjc-framework-Vision "
               f"pyobjc-framework-Cocoa ({e})", step="pyobjc")
        return

    outcome = mp.resolve_permissions(
        fix, mp.stdin_is_terminal(),
        app=mp.responsible_app(),
        ax_trusted=mp.accessibility_trusted,
        screen_trusted=mp.screen_capture_access,
        request_ax=mp.request_accessibility,
        request_screen=mp.request_screen_capture,
        open_pane=mp.open_settings,
        poll=mp.poll_until,
        echo=print,
    )
    _permission_meta["prompt_shown"] = outcome.prompt_shown
    _permission_meta["granted_after_prompt"] = outcome.granted_after_prompt
    app = outcome.app
    _check("Accessibility permission (taps & keystrokes)", outcome.accessibility,
           "" if outcome.accessibility else mp.failure_hint(mp.ACCESSIBILITY, app, fix=fix),
           step="accessibility")
    _check("Screen Recording permission (seeing the phone)", outcome.screen_recording,
           "" if outcome.screen_recording else mp.failure_hint(mp.SCREEN_RECORDING, app, fix=fix),
           step="screen_recording")

    from . import mirror
    _check(f"{mirror.APP_NAME} installed", Path(mirror.APP_PATH).exists(),
           "requires macOS Sequoia+ with a paired iPhone", step="mirroring_installed")

    running = mirror.running_app() is not None
    _check(f"{mirror.APP_NAME} running", running,
           "will auto-launch on first use — not fatal", fatal=False,
           step="mirroring_running")

    win = mirror.find_window()
    _check("mirroring window found", win is not None,
           "open iPhone Mirroring once manually to pair the phone",
           step="mirroring_window")
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
               mp.capture_blank_hint(app, preflight_ok=outcome.screen_recording),
               step="window_capture")
        if good:
            from . import ocr
            n = len(ocr.recognize(path, win))
            _check(f"Vision OCR works ({n} text boxes)", True, step="vision_ocr")
    finally:
        if os.path.exists(path):
            os.unlink(path)

    from . import ios
    try:
        state = ios.IPhone().send("session.state")
    except Exception as e:
        # A missing Screen Recording grant makes capture raise. That is the
        # previous check's job to explain; don't dump a traceback on top.
        _check("session state", False, str(e)[:400], fatal=False, step="session_state")
        return
    _check(f"session state: {state}", state == "ready",
           "an interstitial is up (iPhone in Use / Connect / Mac Locked) — "
           "clear it on the Mac; lock the iPhone if it says in use", fatal=False,
           step="session_state")


# --- Android: adb -> a phone -> authorised -> awake -> tree ------------------

_ADB_INSTALL = {"darwin": "brew install android-platform-tools",
                "win32": "winget install Google.PlatformTools"}.get(sys.platform, "apt install adb (or your distro's android-tools)")
_SCRCPY_INSTALL = {"darwin": "brew install scrcpy", "win32": "winget install Genymobile.scrcpy"}.get(sys.platform, "apt install scrcpy")


def _doctor_android():
    from . import config
    adb = str(config.get("android.adb"))
    if not shutil.which(adb):
        _check(f"adb found ({adb})", False,
               _ADB_INSTALL + ", or set android.adb to the binary", step="adb")
        return
    _check(f"adb found ({shutil.which(adb)})", True, step="adb")
    _check("scrcpy found (optional: live mirror during `android awake`)",
           bool(shutil.which("scrcpy")), _SCRCPY_INSTALL + " — not required",
           fatal=False, step="scrcpy")

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
           state in ("ready", "locked"), hints.get(state, ""), step="phone_ready")
    if state == "locked":
        _check("phone unlocked", False, hints["locked"], fatal=False, step="phone_unlocked")
    if state not in ("ready",):
        return

    b = phone.send("screen.bounds")
    label = android._phone_label(b["id"]) if b else "?"
    _check(f"talking to {label} ({b['id']}), screen {b['w']}x{b['h']}", bool(b),
           step="phone_identity")
    try:
        n = len(phone.send("tree"))
        _check(f"accessibility tree readable ({n} nodes)", n > 0, step="accessibility_tree")
    except RuntimeError as e:
        _check("accessibility tree readable", False, str(e)[:120], fatal=False,
               step="accessibility_tree")
    reg = config.devices_of("android")
    _check(f"remembered phones: {', '.join(reg['phones']) or 'none'}; primary: "
           f"{reg['primary'] or 'none'}", True, step="remembered_phones")
