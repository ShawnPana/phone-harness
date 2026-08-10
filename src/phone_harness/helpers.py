"""Phone control via iPhone Mirroring.

Core helpers live here. Agent-editable helpers live in
PH_AGENT_WORKSPACE/agent_helpers.py (defaults to <repo>/agent-workspace).
Raw Quartz is always available: `import Quartz` in your script for anything
these helpers don't cover.
"""
import hashlib, importlib, importlib.util, os, time
from pathlib import Path

from . import ocr as _ocr

# Every helper below rests on two primitives — capture + tap — so the transport
# is swappable. PHONE_HARNESS_BACKGROUND=1 selects the background backend, which
# drives iPhone Mirroring without ever taking focus (SkyLight event records);
# the default is the classic mirror backend that must be frontmost.
_BACKGROUND = os.environ.get("PHONE_HARNESS_BACKGROUND") in ("1", "true", "yes")
mirror = importlib.import_module(
    ".background" if _BACKGROUND else ".mirror", __package__)

tap = mirror.tap
long_press = mirror.long_press
drag = mirror.drag
press = mirror.press
type_text = mirror.type_text
activate = mirror.activate
find_window = mirror.find_window

CORE_DIR = Path(__file__).resolve().parent
REPO_ROOT = CORE_DIR.parent.parent
AGENT_WORKSPACE = Path(
    os.environ.get("PH_AGENT_WORKSPACE", REPO_ROOT / "agent-workspace"))


# --- session / state ---

# Distinctive text on the not-connected interstitials. Reconnecting past any of
# these is a PHYSICAL action only the user can do (open the app, and if it says
# "iPhone in Use", LOCK the phone). The agent must never tap through them.
_BLOCKED_MARKERS = ("iphone in use", "lock your iphone", "mirroring ended",
                    "to connect")


def connection_state():
    """'ready' | 'blocked' | 'no-window' | 'not-running'.

    'blocked' means a connect / 'iPhone in Use' / paused interstitial is on
    screen. Cheap to call; use it to decide whether to proceed."""
    if mirror.running_app() is None:
        return "not-running"
    if mirror.find_window() is None:
        return "no-window"
    path, win = mirror.capture()  # window exists, so this won't launch anything
    texts = " ".join(o["text"] for o in _ocr.recognize(path, win)).lower()
    return "blocked" if any(m in texts for m in _BLOCKED_MARKERS) else "ready"


def ensure_mirroring():
    """Return the mirroring window if the phone is connected and ready.

    Never launches the app, taps Connect/Continue, or polls to reconnect —
    resuming mirroring is physical and only the user can do it. If the session
    isn't ready, raises with instructions for the user. STOP and relay that
    message; do not try to tap through the connect screen yourself.
    """
    state = connection_state()
    if state == "ready":
        mirror.activate()
        return mirror.find_window()
    if state == "not-running":
        raise RuntimeError(
            "iPhone Mirroring isn't running. Please open the iPhone Mirroring "
            "app and connect your phone, then retry — reconnecting is physical, "
            "so I can't do it for you.")
    if state == "no-window":
        raise RuntimeError(
            "iPhone Mirroring is open but no phone is connected. Please connect "
            "your phone in the app, then retry.")
    raise RuntimeError(
        "iPhone Mirroring is not connected — it's showing a connect / 'iPhone "
        "in Use' screen. This needs you: open iPhone Mirroring and connect the "
        "phone, and if it says 'iPhone in Use', LOCK your iPhone so mirroring "
        "can resume. Then retry. I will not tap Connect for you.")


def screen_info():
    """{window, frontmost, img_px} — bounds in screen points, capture size in px."""
    path, win = mirror.capture()
    w, h = _ocr.image_size(path)
    return {"window": win, "frontmost": mirror.is_frontmost(), "img_px": [w, h]}


def screenshot(path=None):
    """Capture the phone window to a PNG and return its path. View it to see
    the phone; combine with ocr() for coordinates."""
    p, _ = mirror.capture(path)
    return p


# --- reading the screen ---

def ocr(min_confidence=0.3):
    """All visible text with tap-ready screen-point centers:
    [{text, confidence, x, y, w, h}]. This is the element tree — prefer it
    over eyeballing screenshots for anything with a text label."""
    path, win = mirror.capture()
    return [o for o in _ocr.recognize(path, win)
            if o["confidence"] >= min_confidence]


def find_text(query, exact=False):
    """OCR results matching query (case-insensitive substring by default)."""
    q = query.lower()
    return [o for o in ocr()
            if (o["text"].lower() == q if exact else q in o["text"].lower())]


def tap_text(query, index=0, exact=False):
    """Find text on screen and tap its center. Raises with what IS visible on
    failure, so the next step is informed."""
    hits = find_text(query, exact=exact)
    if not hits:
        visible = [o["text"] for o in ocr()][:30]
        raise RuntimeError(f"no visible text matches {query!r}; saw: {visible}")
    hit = hits[index]
    tap(hit["x"], hit["y"])
    return hit


# --- gestures relative to the phone window ---

def _win():
    return mirror.ensure_window()


def swipe(direction, distance=0.4):
    """swipe('up'|'down'|'left'|'right') — a touch-drag centered in the window.
    Direction is finger motion: swipe('up') moves content up (scrolls down)."""
    w = _win()
    cx, cy = w["x"] + w["w"] / 2, w["y"] + w["h"] / 2
    dx = {"left": -1, "right": 1}.get(direction, 0) * w["w"] * distance
    dy = {"up": -1, "down": 1}.get(direction, 0) * w["h"] * distance
    if not dx and not dy:
        raise ValueError(f"unknown direction {direction!r}")
    # Fast, short drag = a momentum flick. A slow drag barely registers on iOS
    # (it won't even flip a Home-Screen page); the flick is what snaps pages
    # and carousels. For scrolling lists use scroll()/scroll_collect() instead.
    mirror.drag(cx - dx / 2, cy - dy / 2, cx + dx / 2, cy + dy / 2,
                duration=0.12, steps=6)


def scroll(amount=300):
    """Scroll-gesture at window center. Positive scrolls content down the way
    a trackpad two-finger-up does; use swipe() when momentum matters."""
    w = _win()
    mirror.scroll_wheel(-amount, w["x"] + w["w"] / 2, w["y"] + w["h"] / 2)


# --- scrolling through lists ---
#
# End-of-list is decided by whether the SCREEN MOVED, never by whether the
# caller's parser found new items. A dense list or a missed OCR row must not
# read as "done" — only the pixels going still (after a settle window that lets
# lazy-loaded content arrive) means the end.

def _content_texts(min_conf=0.4, top_frac=0.06, bottom_frac=0.92):
    """OCR of the scrollable content area, excluding the volatile status bar
    (clock/battery) at top and the nav/home strip at bottom."""
    path, win = mirror.capture()
    top = win["y"] + win["h"] * top_frac
    bot = win["y"] + win["h"] * bottom_frac
    return [o for o in _ocr.recognize(path, win)
            if top < o["y"] < bot and o["confidence"] >= min_conf]


def _text_set(boxes):
    return frozenset(o["text"].strip() for o in boxes if o["text"].strip())


def _overlap(a, b):
    """Jaccard overlap of two text sets: ~1.0 = same screen, low = it moved."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def scroll_screen(direction="up", amount=0.6, settle=2.5, moved_thresh=0.6):
    """One scroll gesture, then wait for the screen to settle (so a lazy-load
    spinner resolves before we judge movement).

    Returns {moved, overlap, before, after, boxes} — `boxes` is the settled
    content OCR, ready to parse. `moved` is False when overlap >= moved_thresh:
    the list didn't advance. The default 0.6 sits in the empirical gap between
    real forward progress (overlap < ~0.45) and overscroll bounce at a boundary
    (overlap > ~0.7), which springs the content and would otherwise read as
    movement and defeat end-detection.
    """
    w = mirror.ensure_window()
    sign = {"up": -1, "down": 1}.get(direction)  # 'up' reveals content below
    if sign is None:
        raise ValueError(f"direction must be 'up' or 'down', got {direction!r}")
    before = _text_set(_content_texts())
    # Wheel scroll, not drag: a slow touch-drag barely moves an iOS list and
    # bounces back, while wheel events advance it deterministically (proven on
    # long lists). amount is a fraction of window height.
    mirror.scroll_wheel(sign * int(w["h"] * amount),
                        w["x"] + w["w"] / 2, w["y"] + w["h"] / 2, steps=10)
    time.sleep(0.4)
    prev_boxes, prev = None, None
    deadline = time.time() + settle
    while time.time() < deadline:
        boxes = _content_texts()
        cur = _text_set(boxes)
        if cur == prev:                 # two identical captures = settled
            break
        prev, prev_boxes = cur, boxes
        time.sleep(0.35)
    after = prev or frozenset()
    return {"moved": _overlap(before, after) < moved_thresh,
            "overlap": round(_overlap(before, after), 3),
            "before": before, "after": after, "boxes": prev_boxes or []}


def scroll_until(done, direction="up", amount=0.6, max_scrolls=60, settle=2.5):
    """Scroll until `done(boxes)` is truthy or the list stops moving.

    `done` receives the current content OCR (list of boxes) and returns a
    truthy value to stop; that value is returned. Returns None if the end of
    the list is reached first.
    """
    boxes = _content_texts()
    hit = done(boxes)
    if hit:
        return hit
    stale = 0
    for _ in range(max_scrolls):
        res = scroll_screen(direction, amount, settle)
        hit = done(res["boxes"])
        if hit:
            return hit
        if res["moved"]:
            stale = 0
        else:
            stale += 1
            if stale >= 2:              # confirmed still after a retry
                return None
            time.sleep(0.8)
            mirror.activate()
    return None


def scroll_collect(extract=None, key=None, direction="up", amount=0.6,
                   max_scrolls=400, end_after=3, settle=2.5, on_progress=None):
    """Scroll a list top-to-bottom, extracting and de-duping items each screen,
    until the list reaches its true end.

    - extract(boxes) -> list of items for the current screen. Default returns
      each content text line, so a bare scroll_collect() gathers all text.
    - key(item) -> hashable de-dup key (default: the item itself).
    - Stops after `end_after` consecutive non-moving scrolls (the settle window
      already gave lazy-load a chance), or `max_scrolls`.

    Returns {items, stop, scrolls}. `stop` is 'reached-end' or 'max-scrolls'.
    Use amount < 1.0 so screens overlap and no row falls between captures.
    """
    extract = extract or (lambda boxes: [o["text"].strip() for o in boxes
                                         if o["text"].strip()])
    key = key or (lambda x: x)
    seen, order = set(), []

    def ingest(boxes):
        new = 0
        for item in extract(boxes):
            k = key(item)
            if k in seen:
                continue
            seen.add(k)
            order.append(item)
            new += 1
        return new

    ingest(_content_texts())
    stale = 0
    for i in range(1, max_scrolls + 1):
        res = scroll_screen(direction, amount, settle)
        new = ingest(res["boxes"])
        if on_progress:
            on_progress(i, len(order), new, res["moved"], res["overlap"])
        if res["moved"]:
            stale = 0
        else:
            stale += 1
            if stale >= end_after:
                return {"items": order, "stop": "reached-end", "scrolls": i}
            time.sleep(0.8)            # extra grace for a slow lazy-load
            mirror.activate()
    return {"items": order, "stop": "max-scrolls", "scrolls": max_scrolls}


# --- navigation ---

def home():
    """Go to the iPhone Home Screen (Cmd+1)."""
    press("cmd+1")
    time.sleep(0.8)


def app_switcher():
    press("cmd+2")
    time.sleep(0.8)


def open_app(name):
    """Open an app via Spotlight (Cmd+3): type name, return, wait for launch."""
    press("cmd+3")
    time.sleep(0.9)
    type_text(name)
    time.sleep(1.2)  # let results populate before committing
    press("return")
    wait_stable()


# --- timing ---

def wait(seconds=1.0):
    time.sleep(seconds)


def wait_stable(timeout=6.0, interval=0.5, settle=2):
    """Wait until `settle` consecutive captures are identical (animation done).
    The status-bar clock ticks once a minute, so near-misses are rare."""
    prev, same = None, 0
    deadline = time.time() + timeout
    while time.time() < deadline:
        path, _ = mirror.capture()
        digest = hashlib.md5(Path(path).read_bytes()).hexdigest()
        same = same + 1 if digest == prev else 0
        if same >= settle - 1:
            return True
        prev = digest
        time.sleep(interval)
    return False


def _load_agent_helpers():
    p = AGENT_WORKSPACE / "agent_helpers.py"
    if not p.exists():
        return
    spec = importlib.util.spec_from_file_location("phone_harness_agent_helpers", p)
    if not spec or not spec.loader:
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, value in vars(module).items():
        if not name.startswith("_"):
            globals()[name] = value


_load_agent_helpers()
