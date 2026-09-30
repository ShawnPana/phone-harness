"""Capture dimensions read without a platform toolkit."""
import struct
import sys
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from phone_harness.imaging import image_size  # noqa: E402


def _png(path, w, h):
    def chunk(kind, body):
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body)))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                     + chunk(b"IEND", b""))
    return path


class Png(unittest.TestCase):
    def test_reads_ihdr(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = _png(Path(d) / "shot.png", 1152, 2376)
            self.assertEqual(image_size(p), (1152, 2376))

    def test_rejects_other_formats(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "shot.gif"
            p.write_bytes(b"GIF89a" + b"\0" * 20)
            with self.assertRaises(RuntimeError):
                image_size(p)


class Jpeg(unittest.TestCase):
    def test_reads_sof0(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "shot.jpg"
            sof = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" \
                + struct.pack(">HH", 480, 640) + b"\x03" + b"\0" * 9
            p.write_bytes(b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 4)
                          + b"\0\0" + sof)
            self.assertEqual(image_size(p), (640, 480))


if __name__ == "__main__":
    unittest.main()
