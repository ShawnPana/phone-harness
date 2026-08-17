"""Text recognition over window captures via Apple's Vision framework.

This is the mirror backend's element tree: OCR gives every visible string a
bounding box, converted here into global screen points ready for tap().
"""
import Quartz
import Vision
from Foundation import NSURL


def image_size(path):
    src = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(path), None)
    if src is None:
        raise RuntimeError(f"cannot read image {path}")
    props = Quartz.CGImageSourceCopyPropertiesAtIndex(src, 0, None)
    return int(props["PixelWidth"]), int(props["PixelHeight"])


def recognize(path, window):
    """OCR a capture of `window` ({x, y, w, h} screen points).

    Returns [{text, confidence, x, y, w, h}] where (x, y) is the box center in
    screen points — pass straight to tap(). Vision's normalized boxes have a
    bottom-left origin; screen points have a top-left origin, hence the flip.
    """
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(
        NSURL.fileURLWithPath_(path), {})
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    # Vision defaults to Latin-script recognition, so a phone running in a
    # CJK (or any non-Latin) locale OCRs to garbage. Let Vision detect the
    # language itself where supported (macOS 13+; iPhone Mirroring itself
    # needs macOS 15+, the guard just keeps older hosts harmless).
    if request.respondsToSelector_("setAutomaticallyDetectsLanguage:"):
        request.setAutomaticallyDetectsLanguage_(True)
    ok, err = handler.performRequests_error_([request], None)
    if not ok:
        raise RuntimeError(f"Vision OCR failed: {err}")

    img_w, img_h = image_size(path)
    sx = window["w"] / img_w  # image px -> screen points
    sy = window["h"] / img_h

    out = []
    for obs in request.results() or []:
        cand = obs.topCandidates_(1)
        if not cand:
            continue
        bb = obs.boundingBox()
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
