"""Text recognition over window captures via Apple's Vision framework.

This is the mirror backend's element tree: OCR gives every visible string a
bounding box, converted here into global screen points ready for tap().
"""
import functools
import os
import sys

import Quartz
import Vision
from Foundation import NSURL, NSLocale


@functools.lru_cache(maxsize=1)
def _supported():
    """Language tags Vision can recognize at the Accurate level.

    Accurate only: the Fast level supports six Latin languages and no CJK at
    all, so anything that ever flips the level for speed silently loses
    non-Latin recognition. Measured: Fast reports 6 languages, Accurate 30.
    """
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    langs, _ = req.supportedRecognitionLanguagesAndReturnError_(None)
    return [str(l) for l in langs or []]


def _known(tag):
    """Does Vision know this tag? Compared on the language prefix, because
    Vision accepts a fuller tag than it advertises: `zh-Hant-TW` works even
    though the supported list only reports `zh-Hant`."""
    base = tag.split("-")[0].lower()
    return any(s.split("-")[0].lower() == base for s in _supported())


@functools.lru_cache(maxsize=1)
def _languages():
    """Recognition languages: the Mac's preferred languages, English last.

    Vision defaults to Latin-script recognition, so a phone showing Chinese
    (or any non-Latin script) OCRs to garbage. The Mac's own language list is
    the best available guess at what the phone shows; English stays in the
    list so a bilingual screen keeps working. PHONE_HARNESS_OCR_LANGS
    overrides it outright ("zh-Hans,en-US") for phones whose language the
    Mac does not share.

    Unknown tags are warned about rather than passed through silently. Vision
    accepts any string, returns Latin-only results, and reads the bogus value
    straight back -- so a typo in the override would look like "OCR stopped
    working" with nothing to go on.
    """
    override = os.environ.get("PHONE_HARNESS_OCR_LANGS")
    if override:
        tags = [l.strip() for l in override.split(",") if l.strip()]
        bad = [t for t in tags if not _known(t)]
        if bad:
            print(f"phone-harness: PHONE_HARNESS_OCR_LANGS has tags Vision does "
                  f"not recognize: {bad}. Supported: {', '.join(_supported())}",
                  file=sys.stderr)
        return tags
    langs = [str(t) for t in NSLocale.preferredLanguages() or []]
    return [*[l for l in langs if not l.startswith("en")], "en-US"]


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
    # Accurate is not just a quality setting here: the Fast level knows six
    # Latin languages and nothing else, so switching it for speed would drop
    # every non-Latin script on the floor. See _supported().
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setRecognitionLanguages_(_languages())
    # And let Vision spot a language the Mac's list missed (macOS 13+): the
    # explicit list above acts as hints, detection covers the rest — a phone
    # is not obliged to speak the same language as the Mac driving it.
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
