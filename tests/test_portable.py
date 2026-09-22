"""What must work on a host without Apple frameworks: an Android phone driven
from Windows or Linux. The frameworks are hidden from the import system in a
subprocess, the way a machine that never had them looks."""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")

# Every top-level pyobjc name the package touches. Hidden, `find_spec` says
# "not installed", which is what android.py keys its Vision fallback on.
NO_APPLE = textwrap.dedent("""
    import sys, importlib.abc
    BLOCKED = {"Quartz", "Vision", "Foundation", "AppKit", "Cocoa", "ApplicationServices", "objc"}
    _real = list(sys.meta_path)
    class Gate(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in BLOCKED:
                return None
            for f in _real:
                spec = f.find_spec(name, path, target)
                if spec:
                    return spec
    sys.meta_path[:] = [Gate()]
""")

# A real 720x1280 PNG, as `adb exec-out screencap -p` hands back.
FAKE_SCREEN = textwrap.dedent("""
    import struct, tempfile, zlib
    def _chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    raw = b"".join(b"\\x00" + b"\\x00\\x00\\x00" * 720 for _ in range(1280))
    png = (b"\\x89PNG\\r\\n\\x1a\\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", 720, 1280, 8, 2, 0, 0, 0))
           + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b""))
    shot = tempfile.mktemp(suffix=".png")
    open(shot, "wb").write(png)
""")


class WithoutAppleFrameworks(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.env = {**os.environ, "PYTHONPATH": SRC, "PHONE_HARNESS_HOME": self.home.name,
                    "PHONE_HARNESS_TELEMETRY": "0", "PHONE_HARNESS_PLATFORM": "android"}

    def run_py(self, body):
        return subprocess.run([sys.executable, "-c", NO_APPLE + body], capture_output=True,
                              text=True, env=self.env)

    def test_screen_info_needs_no_quartz(self):
        # Seen in the field: open an app, check focus, list the tree, then crash in
        # screen_info() with `ModuleNotFoundError: No module named 'Quartz'`, because
        # the capture's size was read through the Vision OCR module.
        r = self.run_py(FAKE_SCREEN + textwrap.dedent("""
            from phone_harness import helpers
            helpers.send = lambda op, **kw: {
                "screen.capture": (shot, {"x": 0, "y": 0, "w": 720, "h": 1280}),
                "focus.probe": (True, None)}[op]
            print(helpers.screen_info())
        """))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Quartz", r.stderr)
        self.assertIn("'img_px': [720, 1280]", r.stdout)

    def test_the_helpers_import_and_bind_android(self):
        r = self.run_py("from phone_harness import helpers; print(type(helpers.phone).__name__)")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "Android"), r.stderr)

    def test_a_non_png_capture_is_a_clear_error(self):
        r = self.run_py(textwrap.dedent("""
            import tempfile
            from phone_harness.helpers import _png_size
            p = tempfile.mktemp(); open(p, "w").write("not an image")
            try:
                _png_size(p)
            except RuntimeError as e:
                print("RuntimeError:", e)
        """))
        self.assertIn("RuntimeError: cannot read image", r.stdout, r.stderr)
        self.assertIn("not a PNG", r.stdout)


if __name__ == "__main__":
    unittest.main()
