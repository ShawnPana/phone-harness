"""Pure-function tests for the CoreDevice backend. No phone, no
pymobiledevice3 beyond its key table (skipped when it is not installed)."""
import os
import struct
import tempfile
import unittest
import zlib

os.environ.setdefault("PHONE_HARNESS_HOME", tempfile.mkdtemp())

from phone_harness import ocr  # noqa: E402
from phone_harness.coredevice_daemon import (  # noqa: E402
    ios_at_least, png_size, to_touch, KEY_NAMES, MODIFIERS)

try:
    import pymobiledevice3  # noqa: F401
    HAVE_PMD = True
except ImportError:
    HAVE_PMD = False


def _png(w, h):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(
            ">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00" * (3 * w) for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class Coordinates(unittest.TestCase):
    def test_corners_span_the_digitizer(self):
        self.assertEqual(to_touch(0, 0, 750, 1334), (0, 0))
        self.assertEqual(to_touch(749, 1333, 750, 1334), (65535, 65535))

    def test_centre_is_centre(self):
        x, y = to_touch(374.5, 666.5, 750, 1334)
        self.assertAlmostEqual(x, 32768, delta=1)
        self.assertAlmostEqual(y, 32768, delta=1)

    def test_out_of_range_clamps(self):
        self.assertEqual(to_touch(-50, 5000, 750, 1334), (0, 65535))


class Images(unittest.TestCase):
    def test_png_size_from_bytes_and_file(self):
        data = _png(12, 34)
        self.assertEqual(png_size(data), (12, 34))
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(data)
        try:
            self.assertEqual(ocr.image_size(f.name), (12, 34))
        finally:
            os.unlink(f.name)

    def test_not_a_png(self):
        with self.assertRaises(ValueError):
            png_size(b"hello")


class Versions(unittest.TestCase):
    def test_ios_at_least(self):
        self.assertTrue(ios_at_least("27.0", 27))
        self.assertTrue(ios_at_least("27.1.2", 27))
        self.assertTrue(ios_at_least("17.4", 17, 4))
        self.assertFalse(ios_at_least("17.1.1", 17, 4))
        self.assertFalse(ios_at_least("26.2", 27))
        self.assertFalse(ios_at_least(None, 27))


@unittest.skipUnless(HAVE_PMD, "pymobiledevice3 not installed")
class Keys(unittest.TestCase):
    def test_named_keys_and_chords(self):
        from phone_harness.coredevice_daemon import key_usages
        self.assertEqual(key_usages("return"), {KEY_NAMES["return"]})
        self.assertEqual(key_usages("cmd+1"), {MODIFIERS["cmd"], 30})
        self.assertEqual(key_usages("Cmd+Shift+3"), {227, 225, 32})

    def test_shifted_character_adds_shift(self):
        from phone_harness.coredevice_daemon import key_usages
        self.assertEqual(key_usages("a"), {4})
        self.assertEqual(key_usages("A"), {4, 225})
        self.assertEqual(key_usages("cmd++"), {227, 46, 225})

    def test_unknown_key_raises(self):
        from phone_harness.coredevice_daemon import key_usages
        with self.assertRaises(ValueError):
            key_usages("cmd+nosuchkey")


class RapidRows(unittest.TestCase):
    """The RapidOCR adapter's geometry, with the engine faked."""

    def test_boxes_become_centres_in_window_units(self):
        data = _png(100, 200)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(data)
        rows = [([[10, 20], [50, 20], [50, 40], [10, 40]], "hello", 0.9)]

        class Fake:
            def __call__(self, path):
                return rows, 0.0
        saved = ocr._rapid_engine
        ocr._rapid_engine = Fake()
        try:
            out = ocr._rapid(f.name, {"x": 0, "y": 0, "w": 100, "h": 200})
        finally:
            ocr._rapid_engine = saved
            os.unlink(f.name)
        self.assertEqual(out, [{"text": "hello", "confidence": 0.9,
                                "x": 30.0, "y": 30.0, "w": 40.0, "h": 20.0}])


if __name__ == "__main__":
    unittest.main()
