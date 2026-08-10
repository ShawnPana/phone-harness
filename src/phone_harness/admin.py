"""Diagnostics: `phone-harness --doctor` walks the permission/session ladder."""
import os, subprocess, tempfile
from pathlib import Path


_failures = []


def _check(label, ok, hint="", fatal=True):
    """Print a check result and, unless it is explicitly non-fatal, remember a
    failure. The verdict is computed from `_failures`, not from the call sites
    remembering to fold each result into a running flag."""
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {hint}" if not ok and hint else ""))
    if not ok and fatal:
        _failures.append(label)
    return ok


def run_doctor():
    print("phone-harness doctor\n")
    _failures.clear()

    try:
        import Quartz, Vision, AppKit  # noqa: F401
        _check("pyobjc frameworks (Quartz, Vision, AppKit)", True)
    except ImportError as e:
        _check("pyobjc frameworks", False,
               f"pip install pyobjc-framework-Quartz pyobjc-framework-Vision ({e})")
        return 1

    from ApplicationServices import AXIsProcessTrusted
    _check(
        "Accessibility permission (taps & keystrokes)", AXIsProcessTrusted(),
        "System Settings > Privacy & Security > Accessibility: enable your terminal")

    import Quartz as Q
    _check(
        "Screen Recording permission (seeing the phone)",
        bool(Q.CGPreflightScreenCaptureAccess()),
        "System Settings > Privacy & Security > Screen Recording: enable your terminal")

    from . import mirror
    _check(f"{mirror.APP_NAME} installed", Path(mirror.APP_PATH).exists(),
                 "requires macOS Sequoia+ with a paired iPhone")

    running = mirror.running_app() is not None
    _check(f"{mirror.APP_NAME} running", running,
           "will auto-launch on first use — not fatal", fatal=False)

    win = mirror.find_window()
    # Running with no window is the common case when the phone is locked or the
    # session was never started, so the hint has to tell those two apart.
    _check("mirroring window found", win is not None,
           "iPhone Mirroring is running but has no window — click it and "
           "connect your phone (it may be waiting on the lock screen)"
           if running else
           "open iPhone Mirroring once manually to pair the phone")

    if win:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
        try:
            subprocess.run(
                ["screencapture", "-x", "-o", "-l", str(win["id"]), path],
                check=True)
            size = os.path.getsize(path)
            _check(f"window capture works ({size} bytes)", size > 20_000,
                         "capture is blank — Screen Recording permission "
                         "needs a terminal restart to take effect")
            if size > 20_000:
                from . import ocr
                n = len(ocr.recognize(path, win))
                _check(f"Vision OCR works ({n} text boxes)", True)
        finally:
            os.unlink(path)

    if win is None:
        print("\n  [SKIP] window capture and Vision OCR — no window to test "
              "against;\n         these are the checks that prove the harness "
              "can actually see the phone")

    print("\nall clear" if not _failures
          else "\nfix the FAILs above, then re-run")
    print("\nnote: these are the permissions currently known to be required. A "
          "fresh\nmachine may still prompt for more the first time an action "
          "runs — approve\nthem in System Settings if a step silently does "
          "nothing despite this passing.")
    return 0 if not _failures else 1
