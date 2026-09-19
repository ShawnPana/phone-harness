"""Text recognition over screen captures.

This is the pixel backends' element tree: OCR gives every visible string a
bounding box, converted here into the caller's coordinate space ready for
tap(). Two engines behind one function:

  Vision    Apple's framework, on macOS. Accurate, fast, already installed.
  RapidOCR  ONNX models on every other OS (`phone-harness[iphone]` pulls it
            in off macOS). Slower on first call while the models load.

recognize() returns [{text, confidence, x, y, w, h}] where (x, y) is the box
centre in the coordinate space of `window` ({x, y, w, h}) — screen points for
the mirroring window, screenshot pixels for the CoreDevice backend, whose
window is the image itself.
"""
import sys


def image_size(path):
    """(w, h) of a PNG or JPEG without an image library."""
    with open(path, "rb") as f:
        head = f.read(32)
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return (int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big"))
        if head[:2] == b"\xff\xd8":
            f.seek(2)
            while True:
                marker = f.read(2)
                if len(marker) < 2 or marker[0] != 0xFF:
                    break
                if marker[1] in (0xC0, 0xC1, 0xC2):
                    f.read(3)
                    h = int.from_bytes(f.read(2), "big")
                    w = int.from_bytes(f.read(2), "big")
                    return w, h
                size = int.from_bytes(f.read(2), "big")
                f.seek(size - 2, 1)
    raise RuntimeError(f"cannot read image {path}")


def engine():
    """'vision' | 'rapidocr' | None (nothing installed)."""
    if sys.platform == "darwin":
        try:
            import Vision  # noqa: F401
            return "vision"
        except ImportError:
            pass
    for mod in ("rapidocr_onnxruntime", "rapidocr"):
        try:
            __import__(mod)
            return "rapidocr"
        except ImportError:
            continue
    return None


def recognize(path, window):
    eng = engine()
    if eng == "vision":
        return _vision(path, window)
    if eng == "rapidocr":
        return _rapid(path, window)
    raise RuntimeError("no OCR engine: on macOS install pyobjc-framework-Vision, elsewhere "
                       "`pip install rapidocr-onnxruntime` (part of phone-harness[iphone])")


# --- Vision (macOS) ---------------------------------------------------------

def _vision(path, window):
    import Vision
    from Foundation import NSURL
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(
        NSURL.fileURLWithPath_(path), {})
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    ok, err = handler.performRequests_error_([request], None)
    if not ok:
        raise RuntimeError(f"Vision OCR failed: {err}")

    img_w, img_h = image_size(path)
    sx = window["w"] / img_w  # image px -> window units
    sy = window["h"] / img_h

    out = []
    for obs in request.results() or []:
        cand = obs.topCandidates_(1)
        if not cand:
            continue
        bb = obs.boundingBox()          # normalized, bottom-left origin
        px = bb.origin.x * img_w
        py_top = (1.0 - bb.origin.y - bb.size.height) * img_h
        pw = bb.size.width * img_w
        ph = bb.size.height * img_h
        out.append({
            "text": str(cand[0].string()),
            "confidence": round(float(cand[0].confidence()), 3),
            "x": round(window["x"] + (px + pw / 2) * sx, 1),
            "y": round(window["y"] + (py_top + ph / 2) * sy, 1),
            "w": round(pw * sx, 1),
            "h": round(ph * sy, 1),
        })
    return out


# --- RapidOCR (Linux, Windows) ----------------------------------------------

_rapid_engine = None


def _rapid(path, window):
    global _rapid_engine
    if _rapid_engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            from rapidocr import RapidOCR
        _rapid_engine = RapidOCR()
    result = _rapid_engine(path)
    # rapidocr_onnxruntime returns (list, elapse); rapidocr>=2 returns an
    # object with .boxes/.txts/.scores. Normalise both to rows.
    rows = []
    if isinstance(result, tuple):
        rows = result[0] or []
    elif result is not None and hasattr(result, "boxes"):
        if result.boxes is not None:
            rows = list(zip(result.boxes, result.txts, result.scores))
    img_w, img_h = image_size(path)
    sx = window["w"] / img_w
    sy = window["h"] / img_h
    out = []
    for box, text, score in rows:
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        px, py_top = min(xs), min(ys)
        pw, ph = max(xs) - px, max(ys) - py_top
        out.append({
            "text": str(text),
            "confidence": round(float(score), 3),
            "x": round(window["x"] + (px + pw / 2) * sx, 1),
            "y": round(window["y"] + (py_top + ph / 2) * sy, 1),
            "w": round(pw * sx, 1),
            "h": round(ph * sy, 1),
        })
    return out
