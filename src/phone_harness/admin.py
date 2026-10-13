"""Diagnostics: `phone-harness --doctor` walks the ladder for the phone the
helpers would drive — the config default, or `--doctor ios|android`."""
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class Step:
    """One `--doctor` check.

    `fn` takes no arguments and returns `None` (this step doesn't apply
    right now — print nothing), a 3-tuple `(label, ok, hint)`, or a
    4-tuple `(label, ok, hint, fatal)` that overrides this Step's own
    `fatal` for this particular result.
    """
    fn: Callable[[], object]
    fatal: bool = True
    stop_if_failed: bool = False


def run_steps(steps):
    """Run a `--doctor` check ladder in order.

    Args:
        steps: The `Step`s to run, in the order they should be checked.

    Returns:
        The labels of every step that failed with `fatal` true — this is
        what the overall doctor verdict is based on.
    """
    failures = []
    for step in steps:
        try:
            result = step.fn()
        except Exception as e:
            # A step whose body crashes (a bad import, an unguarded
            # attribute access) is exactly what a doctor check exists to
            # report — never let it become an uncaught traceback.
            result = (getattr(step.fn, "__name__", "check"), False,
                       f"unexpected error: {e}")
        if result is None:
            continue
        label, ok, hint, *rest = result
        fatal = rest[0] if rest else step.fatal
        mark = "PASS" if ok else ("FAIL" if fatal else "WARN")
        print(f"  [{mark}] {label}" + (f" — {hint}" if not ok and hint else ""))
        if not ok and fatal:
            failures.append(label)
        if not ok and step.stop_if_failed:
            break
    return failures


def run_doctor(platform=None):
    """Entry point for `phone-harness --doctor [ios|android]`.

    Runs the ios or android check ladder and prints a final verdict line.

    Args:
        platform: `"ios"` or `"android"` to check a specific phone type;
            `None` to check whichever the config default is set to.

    Returns:
        Process exit code: 0 if every fatal check passed, 1 otherwise.
    """
    from . import config
    platform = (platform or config.get("platform")).lower()
    # Note when the caller asked for a platform other than the configured
    # default, so the output is clear about which one is "normal" for them.
    print(f"phone-harness doctor — {platform}"
          f"{'' if platform == config.get('platform') else '  (default is ' + config.get('platform') + ')'}\n")
    failures = _doctor_android() if platform == "android" else _doctor_ios()
    ok = not failures
    print("\nall clear" if ok else "\nfix the FAILs above, then re-run")
    return 0 if ok else 1


# --- iPhone: pyobjc -> permissions -> Mirroring -> capture -> OCR -----------

def _doctor_ios():
    """Walk the iPhone check ladder, stopping early once a step blocks the
    next one from meaning anything (e.g. no window -> nothing to capture).

    Ladder: pyobjc installed -> Accessibility + Screen Recording permissions
    -> Mirroring app installed/running -> mirroring window found -> a test
    window capture -> OCR on that capture -> iPhone session state.

    Returns:
        The labels of every fatal check that failed.
    """
    ctx = {}

    def check_pyobjc():
        try:
            # ApplicationServices lives here, in the same guarded import as
            # the other three frameworks, so a missing module is a FAIL
            # line instead of an uncaught crash on the next statement
            # (GitHub issue #4).
            import Quartz, Vision, AppKit, ApplicationServices  # noqa: F401
        except ImportError as e:
            return ("pyobjc frameworks", False,
                    f"pip install pyobjc-framework-Quartz pyobjc-framework-Vision "
                    f"pyobjc-framework-Cocoa ({e})")
        return ("pyobjc frameworks (Quartz, Vision, AppKit, ApplicationServices)",
                 True, "")

    def check_accessibility():
        from ApplicationServices import AXIsProcessTrusted
        return ("Accessibility permission (taps & keystrokes)", AXIsProcessTrusted(),
                "System Settings > Privacy & Security > Accessibility: enable your terminal")

    def check_screen_recording():
        import Quartz as Q
        return ("Screen Recording permission (seeing the phone)",
                bool(Q.CGPreflightScreenCaptureAccess()),
                "System Settings > Privacy & Security > Screen Recording: enable your terminal")

    def check_mirroring_installed():
        from . import mirror
        return (f"{mirror.APP_NAME} installed", Path(mirror.APP_PATH).exists(),
                "requires macOS Sequoia+ with a paired iPhone")

    def check_mirroring_running():
        from . import mirror
        # Not fatal: the app auto-launches on first real use.
        return (f"{mirror.APP_NAME} running", mirror.running_app() is not None,
                "will auto-launch on first use — not fatal")

    def check_window():
        from . import mirror
        win = mirror.find_window()
        ctx["win"] = win
        return ("mirroring window found", win is not None,
                "open iPhone Mirroring once manually to pair the phone")

    def check_capture():
        win = ctx["win"]
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
        # Never crash here: a missing Screen Recording grant is exactly
        # what this check exists to report.
        r = subprocess.run(["screencapture", "-x", "-o", "-l", str(win["id"]), path],
                           capture_output=True)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        # A capture with no Screen Recording grant still "succeeds" but
        # writes a near-empty/blank image, so size is the real signal.
        good = r.returncode == 0 and size > 20_000
        if good:
            ctx["capture_path"] = path  # handed to check_ocr; cleaned up there
        elif os.path.exists(path):
            os.unlink(path)
        return (f"window capture works ({size} bytes)", good,
                "capture failed or is blank — Screen Recording permission "
                "needs a terminal restart to take effect")

    def check_ocr():
        path = ctx.get("capture_path")
        if not path:
            # Nothing to OCR — print nothing rather than a misleading FAIL.
            return None
        win = ctx["win"]
        try:
            from . import ocr
            n = len(ocr.recognize(path, win))
            return (f"Vision OCR works ({n} text boxes)", True, "")
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def check_session_state():
        from . import ios
        state = ios.IPhone().send("session.state")
        # Not fatal: an interstitial (locked Mac, "iPhone in Use", etc.) is
        # a transient state the user clears themselves, not a setup defect.
        return (f"session state: {state}", state == "ready",
                "an interstitial is up (iPhone in Use / Connect / Mac Locked) — "
                "clear it on the Mac; lock the iPhone if it says in use")

    return run_steps([
        Step(check_pyobjc, stop_if_failed=True),
        Step(check_accessibility),
        Step(check_screen_recording),
        Step(check_mirroring_installed),
        Step(check_mirroring_running, fatal=False),
        Step(check_window, stop_if_failed=True),
        Step(check_capture),
        Step(check_ocr),
        Step(check_session_state, fatal=False),
    ])


# --- Android: adb -> a phone -> authorised -> awake -> tree ------------------

# Pick the right install command for this OS, falling back to a generic
# Linux package-manager hint for anything that isn't darwin/win32.
_ADB_INSTALL = {"darwin": "brew install android-platform-tools",
                "win32": "winget install Google.PlatformTools"}.get(sys.platform, "apt install adb (or your distro's android-tools)")
_SCRCPY_INSTALL = {"darwin": "brew install scrcpy", "win32": "winget install Genymobile.scrcpy"}.get(sys.platform, "apt install scrcpy")


def _doctor_android():
    """Walk the Android check ladder, stopping early once a step blocks the
    next one from meaning anything (e.g. no adb -> no phone to talk to).

    Ladder: adb found -> (optional) scrcpy found -> a phone reachable and
    in a ready/locked state -> screen bounds readable -> accessibility
    tree readable -> list of remembered phones.

    Returns:
        The labels of every fatal check that failed.
    """
    ctx = {}

    def check_adb():
        from . import config
        adb = str(config.get("android.adb"))
        found = shutil.which(adb)
        if not found:
            # No adb binary means every later check has nothing to talk to.
            return (f"adb found ({adb})", False,
                    _ADB_INSTALL + ", or set android.adb to the binary")
        return (f"adb found ({found})", True, "")

    def check_scrcpy():
        return ("scrcpy found (optional: live mirror during `android awake`)",
                bool(shutil.which("scrcpy")), _SCRCPY_INSTALL + " — not required")

    def check_reachable():
        from . import android
        phone = android.Android()
        ctx["phone"] = phone
        state = phone.send("session.state")
        ctx["state"] = state
        # Human-readable fix for each adb/device state the backend reports.
        hints = {
            "no-device": "plug in with USB debugging on and tap Allow, or "
                         "Wireless debugging + `phone-harness android pair CODE`",
            "unauthorized": "tap Allow on the phone's 'Allow USB debugging?' prompt",
            "offline": "unplug/replug, or toggle Wireless debugging off and on",
            "locked": "unlock the phone (`phone-harness android awake` keeps it so)",
            "no-adb": "adb did not answer",
        }
        ctx["hints"] = hints
        # "locked" counts as reachable (adb still works on a locked phone);
        # check_unlocked below reports that case on its own line, and the
        # gate below the first run_steps() call stops before screen/tree.
        return (f"a phone is reachable and ready (state: {state})",
                state in ("ready", "locked"), hints.get(state, ""))

    def check_unlocked():
        if ctx.get("state") != "locked":
            return None  # only relevant when the phone is actually locked
        return ("phone unlocked", False, ctx["hints"]["locked"], False)  # non-fatal

    def check_screen_bounds():
        from . import android
        b = ctx["phone"].send("screen.bounds")
        ctx["bounds"] = b
        label = android._phone_label(b["id"]) if b else "?"
        return (f"talking to {label} ({b['id']}), screen {b['w']}x{b['h']}", bool(b), "")

    def check_tree():
        try:
            n = len(ctx["phone"].send("tree"))
            return (f"accessibility tree readable ({n} nodes)", n > 0, "")
        except RuntimeError as e:
            # Some screens (constantly animating) make uiautomator refuse
            # the tree; that's a known limitation, not a setup problem.
            return ("accessibility tree readable", False, str(e)[:120], False)

    def check_remembered_phones():
        from . import config
        reg = config.devices_of("android")
        return (f"remembered phones: {', '.join(reg['phones']) or 'none'}; primary: "
                f"{reg['primary'] or 'none'}", True, "")

    failures = run_steps([
        Step(check_adb, stop_if_failed=True),
        Step(check_scrcpy, fatal=False),
        Step(check_reachable),
        Step(check_unlocked, fatal=False),
    ])
    if ctx.get("state") != "ready":
        # Anything other than fully "ready" (locked, unauthorized, etc.)
        # means screen/tree calls below would just fail too.
        return failures
    return failures + run_steps([
        Step(check_screen_bounds),
        Step(check_tree),
        Step(check_remembered_phones),
    ])
