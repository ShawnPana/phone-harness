"""screen_info() must not need the macOS-only ocr module for PNG captures.

    python -m unittest tests.test_screen_info
"""
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from phone_harness import helpers


def _write_png(path, w, h):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw))
                + chunk(b"IEND", b""))


class TestScreenInfoWithoutVision(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.close()
        self.path = tmp.name
        self.addCleanup(Path(self.path).unlink)

    def test_png_size_reads_ihdr(self):
        _write_png(self.path, 1080, 23)
        self.assertEqual(helpers._png_size(self.path), (1080, 23))

    def test_png_size_none_for_non_png(self):
        Path(self.path).write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 40)
        self.assertIsNone(helpers._png_size(self.path))

    def test_screen_info_does_not_import_ocr_for_png(self):
        _write_png(self.path, 7, 5)
        win = {"x": 0, "y": 0, "w": 7, "h": 5}
        replies = {"screen.capture": (self.path, win), "focus.probe": (True,)}
        # None in sys.modules makes `from . import ocr` raise ImportError,
        # as it does on hosts without Quartz.
        with mock.patch.dict(sys.modules, {"phone_harness.ocr": None}), \
             mock.patch.object(helpers, "send", side_effect=lambda op, *a, **k: replies[op]):
            info = helpers.screen_info()
        self.assertEqual(info, {"window": win, "frontmost": True, "img_px": [7, 5]})


if __name__ == "__main__":
    unittest.main()
