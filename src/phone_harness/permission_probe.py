"""Read the Accessibility and Screen Recording grants in a new process.

A running process can keep the answer macOS gave it first (Screen Recording
does), so a check that has to notice a grant landing runs this module as its
own interpreter:

    python -m phone_harness.permission_probe

It prints one JSON line, {"accessibility": ..., "screen_recording": ...},
each true, false, or null when it could not be read. It never prompts.
Standard library first (ctypes), pyobjc only as a fallback, and nothing
else from phone_harness is imported, so a poll every second stays cheap.
"""
import ctypes
import json
import sys

KINDS = ("accessibility", "screen_recording")

_SYMBOLS = {
    "accessibility": (
        ("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices",
         "/System/Library/Frameworks/ApplicationServices.framework/Frameworks/"
         "HIServices.framework/HIServices"),
        "AXIsProcessTrusted",
    ),
    "screen_recording": (
        ("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics",),
        "CGPreflightScreenCaptureAccess",
    ),
}


def _native(paths, name, loader):
    for path in paths:
        try:
            fn = getattr(loader(path), name)
        except (OSError, AttributeError):
            continue
        fn.restype = ctypes.c_bool
        fn.argtypes = []
        try:
            return bool(fn())
        except Exception:
            return None
    return None


def _pyobjc(kind):
    try:
        if kind == "accessibility":
            from ApplicationServices import AXIsProcessTrusted
            return bool(AXIsProcessTrusted())
        import Quartz
        fn = getattr(Quartz, "CGPreflightScreenCaptureAccess", None)
        return None if fn is None else bool(fn())
    except Exception:
        return None


def preflight(loader=None):
    """{kind: True/False/None} for this process."""
    loader = loader or ctypes.CDLL
    out = {}
    for kind in KINDS:
        paths, name = _SYMBOLS[kind]
        value = _native(paths, name, loader)
        out[kind] = _pyobjc(kind) if value is None else value
    return out


def parse(text):
    """The last JSON object line a probe printed, or None."""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            return {kind: data.get(kind) if isinstance(data.get(kind), bool) else None
                    for kind in KINDS}
    return None


def main():
    sys.stdout.write(json.dumps(preflight()) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
