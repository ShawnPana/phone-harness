"""Image dimensions without a platform toolkit.

screen_info() needs the pixel size of a capture on every platform, but the only
reader used to live in ocr.py behind Apple's Vision framework, so asking for it
off macOS raised "ModuleNotFoundError: No module named 'Quartz'". PNG and JPEG
both carry their size in the header; reading it here keeps the Android backend
free of macOS imports.
"""
import struct

_SOF_SKIP = (0xC4, 0xC8, 0xCC)          # DHT, JPG, DAC — not frame headers


def image_size(path):
    """(width, height) of a PNG or JPEG file, read from its header."""
    with open(path, "rb") as fh:
        head = fh.read(24)
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", head[16:24])
            return int(w), int(h)
        if head[:2] == b"\xff\xd8":
            fh.seek(2)
            while True:
                marker = fh.read(2)
                if len(marker) < 2 or marker[0] != 0xFF:
                    break
                kind = marker[1]
                if kind in (0xD8, 0xD9) or 0xD0 <= kind <= 0xD7:
                    continue
                length = struct.unpack(">H", fh.read(2))[0]
                if 0xC0 <= kind <= 0xCF and kind not in _SOF_SKIP:
                    h, w = struct.unpack(">HH", fh.read(5)[1:5])
                    return int(w), int(h)
                fh.seek(length - 2, 1)
    raise RuntimeError(f"cannot read image dimensions from {path}")
