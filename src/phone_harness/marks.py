"""Draw numbered badges over tap targets on a screenshot.

The harness never decides what an icon means. It only numbers the candidates
that targets.py found and puts those numbers where the agent can see them.
Pillow is imported here, not at package import, so text-only helpers keep
working even if the imaging dependency is missing.
"""
import os
import tempfile


def draw_marks(image_path, targets, window, grid=False, max_marks=120,
               out_path=None):
    """Annotate `image_path` and return (path, (width, height), omitted).

    `window` is the capture's {x, y, w, h} in screen points; target boxes are
    in that same space, while the image is in pixels. The conversion here is
    what makes Retina captures and a moved mirroring window line up.
    """
    from PIL import Image, ImageDraw, ImageFont

    image = Image.open(image_path).convert("RGBA")
    img_w, img_h = image.size
    draw = ImageDraw.Draw(image)

    step = max(50, round(min(img_w, img_h) / 12 / 50) * 50)
    if grid:
        _draw_grid(draw, img_w, img_h, step, ImageFont)

    badge = max(28, img_w // 32)
    font_size = max(14, int(badge * 0.6))
    font = _font(ImageFont, font_size)

    sx = img_w / window["w"] if window.get("w") else 1.0
    sy = img_h / window["h"] if window.get("h") else 1.0
    ox, oy = window.get("x", 0), window.get("y", 0)

    placed = []
    drawn = targets[:max_marks] if max_marks is not None else list(targets)
    for target in drawn:
        if target.get("w", 0) <= 0 or target.get("h", 0) <= 0:
            continue
        cx = (target["x"] - ox) * sx
        cy = (target["y"] - oy) * sy
        tw = target["w"] * sx
        th = target["h"] * sy
        left, top = cx - tw / 2, cy - th / 2
        right, bottom = cx + tw / 2, cy + th / 2

        draw.rectangle([left, top, right, bottom],
                       outline=(220, 30, 30, 200), width=2)
        bx, by = _place_badge(left - badge / 2, top - badge / 2, badge,
                              img_w, img_h, placed)
        placed.append((bx, by, bx + badge, by + badge))
        draw.rounded_rectangle(
            [bx, by, bx + badge, by + badge], radius=max(3, badge // 4),
            fill=(220, 30, 30, 235), outline=(255, 255, 255, 255), width=2)
        _draw_number(draw, bx, by, badge, target.get("id", 0), font)

    if out_path is None:
        fd, out_path = tempfile.mkstemp(prefix="phone-marks-", suffix=".png")
        os.close(fd)
    out_path = str(out_path)
    image.save(out_path)
    omitted = max(0, len(targets) - len(drawn))
    return out_path, (img_w, img_h), omitted


def _font(ImageFont, size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                       # Pillow older than 10.1
        return ImageFont.load_default()


def _draw_number(draw, bx, by, badge, number, font):
    text = str(number)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((bx + (badge - tw) / 2 - bbox[0],
               by + (badge - th) / 2 - bbox[1]),
              text, font=font, fill=(255, 255, 255, 255))


def _place_badge(x, y, size, img_w, img_h, placed):
    x = min(max(0, x), max(0, img_w - size))
    y = min(max(0, y), max(0, img_h - size))
    for _ in range(8):
        if not any(_overlap(x, y, size, other) for other in placed):
            return x, y
        x += size + 2
        if x + size > img_w:
            x = 0
            y += size + 2
        y = min(y, max(0, img_h - size))
    return x, y


def _overlap(x, y, size, other):
    x2, y2 = x + size, y + size
    return not (x2 <= other[0] or other[2] <= x
                or y2 <= other[1] or other[3] <= y)


def _draw_grid(draw, img_w, img_h, step, ImageFont):
    font = _font(ImageFont, max(10, img_w // 90))
    line = (0, 200, 255, 110)
    for x in range(0, img_w, step):
        draw.line([(x, 0), (x, img_h)], fill=line, width=1)
        _grid_label(draw, str(x), x + 2, 2, font)
    for y in range(0, img_h, step):
        draw.line([(0, y), (img_w, y)], fill=line, width=1)
        _grid_label(draw, str(y), 2, y + 2, font)


def _grid_label(draw, text, x, y, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.rectangle([x - 1, y - 1, x + tw + 2, y + th + 2],
                   fill=(0, 0, 0, 160))
    draw.text((x - bbox[0], y - bbox[1]), text, font=font,
              fill=(255, 255, 255, 255))
