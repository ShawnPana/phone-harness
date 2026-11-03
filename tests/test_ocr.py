"""ocr.recognize runs Apple's Vision on a real image. macOS only.

    python -m unittest tests.test_ocr
"""
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

try:
    import Quartz
    from Foundation import NSURL
    from phone_harness import ocr
except ImportError:
    ocr = None


def _write_blank_png(path, w=64, h=64):
    space = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(None, w, h, 8, 0, space,
                                       Quartz.kCGImageAlphaPremultipliedLast)
    Quartz.CGContextSetRGBFillColor(ctx, 1, 1, 1, 1)
    Quartz.CGContextFillRect(ctx, Quartz.CGRectMake(0, 0, w, h))
    dest = Quartz.CGImageDestinationCreateWithURL(
        NSURL.fileURLWithPath_(path), "public.png", 1, None)
    Quartz.CGImageDestinationAddImage(dest, Quartz.CGBitmapContextCreateImage(ctx), None)
    Quartz.CGImageDestinationFinalize(dest)


@unittest.skipIf(ocr is None, "needs macOS with pyobjc")
class RecognizeRunsVision(unittest.TestCase):
    def test_blank_image_returns_no_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "blank.png")
            _write_blank_png(path)
            got = ocr.recognize(path, {"x": 0, "y": 0, "w": 64, "h": 64})
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main()
