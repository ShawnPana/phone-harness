"""Diagnostics: `phone-harness --doctor` walks the ladder for the phone the
helpers would drive — the config default, or `--doctor ios|android`.
`--fix` also walks the user through missing macOS permissions; `--json`
prints the result for a script."""
import contextlib
import io
import json
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
    _checks.append({"name": label, "ok": bool(ok), "fatal": bool(fatal),
                    "hint": "" if ok else hint})
    if not ok and fatal:
        _failures.append(label)
    return ok


_failures = []
_checks = []
# The permission report and what --fix did, for --json.
_extra = {}


def _say(line):
    print(line, flush=True)


def run_doctor(platform=None, fix=False, as_json=False, noninteractive=False):
    from . import config
    platform = (platform or config.get("platform")).lower()
    # Decided before --json swaps stdout: a person at a terminal can still
    # use the panel when the result is printed as JSON.
    tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
    _failures.clear()
    _checks.clear()
    _extra.clear()
    options = {"fix": fix, "tty": tty, "noninteractive": noninteractive}
    if as_json:
        options["say"] = lambda line: print(line, file=sys.stderr, flush=True)
        with contextlib.redirect_stdout(io.StringIO()):
            _ladder(platform, options)
        ok = not _failures
        print(json.dumps({
            "platform": platform,
            "ok": ok,
            "checks": list(_checks),
            "permissions": _extra.get("permissions"),
            "fix": _extra.get("fix"),
        }, indent=2))
        return 0 if ok else 1
    print(f"phone-harness doctor — {platform}"
          f"{'' if platform == config.get('platform') else '  (default is ' + config.get('platform') + ')'}\n")
    _ladder(platform, options)
    ok = not _failures
    print("\nall clear" if ok else "\nfix the FAILs above, then re-run")
    if not ok:
        print(CLOUD_HINT)
    return 0 if ok else 1


def _ladder(platform, options):
    if platform == "android":
        _doctor_android()
    else:
        _doctor_ios(**options)


CLOUD_HINT = ("\nNo working phone here? A cloud phone needs no hardware: "
              "`phone-harness cloud login`, `phone-harness cloud ls`, then "
              "`phone-harness cloud start PHONE` (a cloud iPhone needs no adb; "
              "a cloud Android needs adb). "
              "No install possible? Use the HTTPS API: "
              "https://github.com/ShawnPana/phone-harness/blob/main/setup/api.md")


def _check_permissions(fix=False, tty=False, noninteractive=False, say=None):
    """Accessibility and Screen Recording, with the app or binary to grant.

    Without `fix` this is one read: nothing opens and nothing waits. With
    `fix`, permissions.fix() acts first (the panel, a wait in this terminal,
    one opened pane, or text only) and the checks show the state after it.
    """
    from . import permissions
    try:
        report = permissions.collect()
    except Exception as exc:
        _check(permissions.ACCESSIBILITY_LABEL, False,
               f"could not check permissions ({exc})")
        _check(permissions.SCREEN_RECORDING_LABEL, False, "could not check permissions")
        return
    result = None
    if fix:
        out = say or _say
        said = []

        # The guide's lines are a block of their own between the checks.
        def tell(line):
            if not said:
                out("")
            said.append(line)
            out(line)

        try:
            result, report = permissions.fix(report, tty=tty, noninteractive=noninteractive,
                                             say=tell)
        except Exception as exc:
            result = {"mode": "", "outcome": "error", "detail": f"{type(exc).__name__}: {exc}",
                      "steps": []}
        if said:
            out("")
        _extra["fix"] = result
    _extra["permissions"] = permissions.report_payload(report)
    for label, ok, hint in permissions.doctor_lines(report):
        _check(label, ok, hint)
    lines = [text for text in (permissions.explain(report, hint=not fix),
                               permissions.outcome_line(result, report)) if text]
    if lines:
        print()
        print("\n".join(lines))
        print()


# --- iPhone: pyobjc -> permissions -> Mirroring -> capture -> OCR -----------

def _doctor_ios(fix=False, tty=False, noninteractive=False, say=None):
    try:
        import Quartz, Vision, AppKit  # noqa: F401
        _check("pyobjc frameworks (Quartz, Vision, AppKit)", True)
    except ImportError as e:
        _check("pyobjc frameworks", False,
               f"pip install pyobjc-framework-Quartz pyobjc-framework-Vision "
               f"pyobjc-framework-Cocoa ({e})")
        return

    _check_permissions(fix=fix, tty=tty, noninteractive=noninteractive, say=say)

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
               "capture failed or is blank. The Screen Recording check above "
               "names the app or Python binary; if its switch is already on, "
               "the grant is stale and has to be removed and added again "
               "(`phone-harness doctor ios --fix` walks through it)")
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
