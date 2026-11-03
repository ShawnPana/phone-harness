"""Tests for Vision OCR text recognition.

    python -m unittest tests.test_ocr
"""
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

try:
    from phone_harness import ocr
except ImportError:
    ocr = None

if ocr is None:
    import types

    class _LazyMeta(type):
        def __getattr__(cls, name):
            if name.startswith("__"):
                raise AttributeError(name)
            return lambda *args, **kwargs: None

    class _LazyModule(types.ModuleType):
        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            obj = _LazyMeta(name, (), {})
            setattr(self, name, obj)
            return obj

    import importlib.machinery
    for _name in ("Quartz", "Vision", "Foundation"):
        if _name in sys.modules:
            continue
        _mod = _LazyModule(_name)
        _mod.__spec__ = importlib.machinery.ModuleSpec(_name, loader=None)
        sys.modules[_name] = _mod
    try:
        from phone_harness import ocr
    except ImportError:
        ocr = None


class TestVisionOCRInitOptions(unittest.TestCase):
    def test_recognize_passes_none_options_to_handler(self):
        """initWithURL_options_ must be called with options=None, not {}.

        On macOS 27 and pyobjc 13, passing {} raises:
        ValueError: NSInvalidArgumentException - key does not exist (#178).
        """
        captured_args = []

        class FakeHandler:
            def performRequests_error_(self, reqs, err):
                return True, None

        class FakeAlloc:
            def initWithURL_options_(self, url, options):
                captured_args.append((url, options))
                if options == {}:
                    raise ValueError("NSInvalidArgumentException - key does not exist")
                return FakeHandler()

        class FakeRequest:
            def setRecognitionLevel_(self, level):
                pass
            def results(self):
                return []

        orig_alloc = getattr(ocr.Vision.VNImageRequestHandler, "alloc", None)
        orig_req = getattr(ocr.Vision.VNRecognizeTextRequest, "alloc", None)
        orig_img_size = ocr.image_size
        try:
            ocr.Vision.VNImageRequestHandler.alloc = staticmethod(lambda: FakeAlloc())
            fake_req_instance = FakeRequest()
            ocr.Vision.VNRecognizeTextRequest.alloc = staticmethod(
                lambda: type("ReqAlloc", (), {"init": lambda s: fake_req_instance})())
            ocr.image_size = lambda path: (400, 800)

            window = {"x": 0, "y": 0, "w": 400, "h": 800}
            results = ocr.recognize("/tmp/test.png", window)
            self.assertEqual(results, [])
            self.assertEqual(len(captured_args), 1)
            _url, options = captured_args[0]
            self.assertIsNone(options, "options must be None (not {}) to prevent NSInvalidArgumentException on macOS 27")
        finally:
            if orig_alloc is not None:
                ocr.Vision.VNImageRequestHandler.alloc = orig_alloc
            else:
                delattr(ocr.Vision.VNImageRequestHandler, "alloc")
            if orig_req is not None:
                ocr.Vision.VNRecognizeTextRequest.alloc = orig_req
            else:
                delattr(ocr.Vision.VNRecognizeTextRequest, "alloc")
            ocr.image_size = orig_img_size

    def test_recognize_coordinate_mapping(self):
        """Bounding box normalization (bottom-left to top-left screen points)."""
        class FakeCandidate:
            def string(self):
                return "Settings"
            def confidence(self):
                return 0.9876

        class FakeBox:
            origin = type("Point", (), {"x": 0.1, "y": 0.8})()
            size = type("Size", (), {"width": 0.2, "height": 0.1})()

        class FakeObservation:
            def topCandidates_(self, n):
                return [FakeCandidate()]
            def boundingBox(self):
                return FakeBox()

        class FakeHandler:
            def performRequests_error_(self, reqs, err):
                return True, None

        class FakeRequest:
            def setRecognitionLevel_(self, level):
                pass
            def results(self):
                return [FakeObservation()]

        orig_alloc = getattr(ocr.Vision.VNImageRequestHandler, "alloc", None)
        orig_req = getattr(ocr.Vision.VNRecognizeTextRequest, "alloc", None)
        orig_img_size = ocr.image_size
        try:
            ocr.Vision.VNImageRequestHandler.alloc = staticmethod(lambda: type("HAlloc", (), {
                "initWithURL_options_": lambda s, u, opt: FakeHandler()
            })())
            fake_req_instance = FakeRequest()
            ocr.Vision.VNRecognizeTextRequest.alloc = staticmethod(
                lambda: type("ReqAlloc", (), {"init": lambda s: fake_req_instance})())
            ocr.image_size = lambda path: (1000, 2000)

            # window is 500x1000 screen points (2x Retina scaling)
            window = {"x": 100, "y": 50, "w": 500, "h": 1000}
            results = ocr.recognize("/tmp/test.png", window)
            self.assertEqual(len(results), 1)
            res = results[0]
            self.assertEqual(res["text"], "Settings")
            self.assertEqual(res["confidence"], 0.988)
            # px = 0.1 * 1000 = 100, pw = 0.2 * 1000 = 200 -> center px = 200 -> screen x = 100 + 200 * 0.5 = 200
            self.assertEqual(res["x"], 200.0)
            self.assertEqual(res["w"], 100.0)
            # py_top = (1.0 - 0.8 - 0.1) * 2000 = 200, ph = 0.1 * 2000 = 200 -> center py = 300 -> screen y = 50 + 300 * 0.5 = 200
            self.assertEqual(res["y"], 200.0)
            self.assertEqual(res["h"], 100.0)
        finally:
            if orig_alloc is not None:
                ocr.Vision.VNImageRequestHandler.alloc = orig_alloc
            else:
                delattr(ocr.Vision.VNImageRequestHandler, "alloc")
            if orig_req is not None:
                ocr.Vision.VNRecognizeTextRequest.alloc = orig_req
            else:
                delattr(ocr.Vision.VNRecognizeTextRequest, "alloc")
            ocr.image_size = orig_img_size


@unittest.skipUnless(sys.platform == "darwin", "Real Vision OCR requires macOS")
class TestRealVisionOCROnMacOS(unittest.TestCase):
    def test_real_vision_request_handler_init_with_none(self):
        """Runs against real Apple Vision and Foundation on macOS CI."""
        import tempfile
        from Foundation import NSURL
        import Vision
        import Quartz

        # Create a small 100x100 PNG
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

            # Test ocr.recognize end-to-end on macOS
            window = {"x": 0, "y": 0, "w": 100, "h": 100}
            results = ocr.recognize(tmp_path, window)
            self.assertIsInstance(results, list)
        finally:
            Path(tmp_path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
