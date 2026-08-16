"""Diagnostics: `phone-harness --doctor` walks the permission/session ladder."""
import os, subprocess, sys, tempfile
from pathlib import Path


def _check(label, ok, hint=""):
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {hint}" if not ok and hint else ""))
    return ok


def run_doctor():
    print("phone-harness doctor\n")
    ok = True

    try:
        import Quartz, Vision, AppKit  # noqa: F401
        ok &= _check("pyobjc frameworks (Quartz, Vision, AppKit)", True)
    except ImportError as e:
        _check("pyobjc frameworks", False,
               f"pip install pyobjc-framework-Quartz pyobjc-framework-Vision ({e})")
        return 1

    from ApplicationServices import AXIsProcessTrusted
    ok &= _check(
        "Accessibility permission (taps & keystrokes)", AXIsProcessTrusted(),
        "System Settings > Privacy & Security > Accessibility: enable your terminal")

    import Quartz as Q
    ok &= _check(
        "Screen Recording permission (seeing the phone)",
        bool(Q.CGPreflightScreenCaptureAccess()),
        "System Settings > Privacy & Security > Screen Recording: enable your terminal")

    from . import mirror
    ok &= _check(f"{mirror.APP_NAME} installed", Path(mirror.APP_PATH).exists(),
                 "requires macOS Sequoia+ with a paired iPhone")

    running = mirror.running_app() is not None
    _check(f"{mirror.APP_NAME} running", running,
           "will auto-launch on first use — not fatal")

    win = mirror.find_window()
    _check("mirroring window found", win is not None,
           "open iPhone Mirroring once manually to pair the phone")

    if win:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
        try:
            r = subprocess.run(
                ["screencapture", "-x", "-o", "-l", str(win["id"]), path],
                capture_output=True)
            size = os.path.getsize(path) if os.path.exists(path) else 0
            ok &= _check(f"window capture works ({size} bytes)",
                         r.returncode == 0 and size > 20_000,
                         "capture failed/blank — Screen Recording permission "
                         "needs a terminal restart to take effect")
            if r.returncode == 0 and size > 20_000:
                from . import ocr
                n = len(ocr.recognize(path, win))
                _check(f"Vision OCR works ({n} text boxes)", True)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    print("\nall clear" if ok else "\nfix the FAILs above, then re-run")
    print("\nnote: these are the permissions currently known to be required. A "
          "fresh\nmachine may still prompt for more the first time an action "
          "runs — approve\nthem in System Settings if a step silently does "
          "nothing despite this passing.")
    return 0 if ok else 1
