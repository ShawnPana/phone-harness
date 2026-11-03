"""Tests for Vision OCR text recognition.

    python -m unittest tests.test_ocr
"""
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))


class TestVisionOCRInitOptions(unittest.TestCase):
    def test_recognize_uses_none_options_in_source(self):
        """Ensure initWithURL_options_ passes None rather than {} (#178).

        On macOS 27 and pyobjc 13, passing {} raises:
        ValueError: NSInvalidArgumentException - key does not exist.
        Passing None (nil) is the documented Apple Vision way when no options are provided.
        """
        ocr_source = (SRC / "phone_harness" / "ocr.py").read_text(encoding="utf-8")
        self.assertIn("NSURL.fileURLWithPath_(path), None)", ocr_source)
        self.assertNotIn("NSURL.fileURLWithPath_(path), {})", ocr_source)


@unittest.skipUnless(sys.platform == "darwin", "Real Vision OCR requires macOS")
class TestRealVisionOCROnMacOS(unittest.TestCase):
    def test_real_vision_request_handler_init_with_none(self):
        """Runs against real Apple Vision and Foundation on macOS CI."""
        import tempfile
        from Foundation import NSURL
        import Vision
        import Quartz
        from phone_harness import ocr

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp_path = f.name

        try:
            # Generate a 100x100 blank bitmap image with Quartz
            color_space = Quartz.CGColorSpaceCreateDeviceRGB()
            ctx = Quartz.CGBitmapContextCreate(
                None, 100, 100, 8, 400, color_space,
                Quartz.kCGImageAlphaPremultipliedLast)
            Quartz.CGContextSetRGBFillColor(ctx, 1.0, 1.0, 1.0, 1.0)
            Quartz.CGContextFillRect(ctx, Quartz.CGRectMake(0, 0, 100, 100))
            img = Quartz.CGBitmapContextCreateImage(ctx)
            dest = Quartz.CGImageDestinationCreateWithURL(
                NSURL.fileURLWithPath_(tmp_path), "public.png", 1, None)
            Quartz.CGImageDestinationAddImage(dest, img, None)
            Quartz.CGImageDestinationFinalize(dest)

            # Test initWithURL_options_ with None (must not raise NSInvalidArgumentException)
            url = NSURL.fileURLWithPath_(tmp_path)
            handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, None)
            self.assertIsNotNone(handler)

            # Test ocr.recognize end-to-end on real macOS
            window = {"x": 0, "y": 0, "w": 100, "h": 100}
            results = ocr.recognize(tmp_path, window)
            self.assertIsInstance(results, list)
        finally:
            Path(tmp_path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
