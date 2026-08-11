"""Phone control — one flat API over any device.

Core helpers live here. Agent-editable helpers live in
PH_AGENT_WORKSPACE/agent_helpers.py (defaults to <repo>/agent-workspace).

Everything below is a thin wrapper over send(), which is defined first on
purpose: the helpers are conveniences, not a wall. Drop to send() for anything
they don't cover, and below that to the backend itself — `import Quartz` on
iOS. The agent picks its own altitude.

Nothing in this file knows which platform it is driving. Platform differences
live inside one backend each, behind the ops documented in transport.py, so
`nav.home` and `screen.text` mean the same thing everywhere even though an
iPhone answers them with a Cmd+1 keystroke and Vision OCR.
"""
import hashlib, importlib.util, os, time
from pathlib import Path

from . import transport
from .transport import Unsupported          # re-exported for agent scripts

CORE_DIR = Path(__file__).resolve().parent
REPO_ROOT = CORE_DIR.parent.parent
AGENT_WORKSPACE = Path(
    os.environ.get("PH_AGENT_WORKSPACE", REPO_ROOT / "agent-workspace"))

# The default device. connect() returns a fresh backend, so a script needing
# two phones at once can hold them side by side instead of being limited to
# this one.
phone = transport.connect()
connect = transport.connect


def send(op, **kw):
    """Raw transport. send('input.tap', x=100, y=200), send('screen.text').

    The op vocabulary is documented in transport.py. Unsupported means this
    device genuinely cannot do it, not that the call failed.
    """
    return phone.send(op, **kw)


def supports(op):
    """True if this device implements `op`. Gate optional work on it."""
    return phone.supports(op)


def ops():
    """Every op the current device implements."""
    return phone.ops()


# --- session / state --------------------------------------------------------

def connection_state():
    """Backend-defined state string; 'ready' means usable.

    On iPhone Mirroring: 'ready' | 'blocked' | 'no-window' | 'not-running',
    where 'blocked' means an interstitial is up and nothing should be tapped
    or typed until the user clears it.
    """
    return send("session.state")


def ensure_device():
    """Screen bounds if the device is usable, else raise telling the user what
    they physically need to do. Never reconnects — that is the user's job."""
    return send("session.require")


ensure_mirroring = ensure_device      # the old iOS-flavoured name still works


def activate():
    """Refocus if the transport needs it. A no-op where nothing needs focus."""
    return send("session.refocus")


def find_window():
    """Screen bounds {x, y, w, h, id}, or None if there is no device."""
    return send("screen.bounds")


def screen_info():
    """{window, frontmost, img_px} — bounds, focus state, capture size."""
    from . import ocr as _vision
    path, win = send("screen.capture")
    w, h = _vision.image_size(path)
    return {"window": win, "frontmost": bool(send("focus.probe")[0]),
            "img_px": [w, h]}


def screenshot(path=None):
    """Capture the screen to a PNG and return its path. View it to see the
    phone; combine with ocr() for coordinates."""
    p, _ = send("screen.capture", path=path)
    return p


# --- did we interrupt the user ----------------------------------------------

def focus_probe():
    """Opaque snapshot for focus.diff. Cheap; take one before and after."""
    return send("focus.probe")


def interruption(before, after):
    """{raised, stole_focus} between two focus_probe() readings.

    raised is the failure that matters — the window covered whatever the user
    was looking at. stole_focus alone is tolerable: keystrokes are rerouted
    but the screen is untouched.
    """
    return send("focus.diff", before=before, after=after)


# --- reading the screen -----------------------------------------------------

def ocr(min_confidence=0.3):
    """All visible text with tap-ready centers: [{text, confidence, source,
    x, y, w, h}]. Prefer this over eyeballing screenshots for anything with a
    text label. `source` is "pixels" when a recogniser inferred the string and
    "tree" when the OS reported it exactly."""
    return send("screen.text", min_confidence=min_confidence)


def ocr_pixels(min_confidence=0.3):
    """Force pixel OCR even where a tree exists — for canvas, games, and
    WebViews without accessibility, whose text a tree cannot see."""
    return send("screen.text_pixels", min_confidence=min_confidence)


def find_text(query, exact=False):
    """Visible text matching query (case-insensitive substring by default)."""
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


# --- accessibility tree (where the device has one) --------------------------

def ui():
    """The accessibility tree. Exact where ocr() is inferred. Raises
    Unsupported on iPhone Mirroring, whose window is an opaque video stream —
    gate on supports('tree')."""
    return send("tree")


def find_nodes(query=None, exact=False, clickable_only=False, nodes=None):
    """Tree nodes matching `query` across text, description and resource-id."""
    items = ui() if nodes is None else nodes
    if clickable_only:
        items = [n for n in items if n["clickable"]]
    if query is None:
        return items
    q = query.lower()
    return [n for n in items
            if any((f.lower() == q if exact else q in f.lower())
                   for f in (n["text"], n["desc"], n["id"]) if f)]


def tap_node(node):
    """Tap a tree node's center."""
    tap(node["x"], node["y"])
    return node


def tap_ui(query, index=0, exact=False, clickable_only=False):
    """Find an element in the accessibility tree and tap it.

    Preferred over tap_text() where a tree exists: exact bounds, and it sees
    elements with no visible label — an icon carrying only a
    content-description is invisible to OCR but present here.
    """
    nodes = ui()
    hits = find_nodes(query, exact=exact, clickable_only=clickable_only,
                      nodes=nodes)
    if not hits:
        visible = [n["text"] or n["desc"] or n["id"]
                   for n in nodes if n["text"] or n["desc"] or n["id"]][:30]
        raise RuntimeError(f"no element matches {query!r}; saw: {visible}")
    return tap_node(hits[index])


# --- input ------------------------------------------------------------------

def tap(x, y):
    return send("input.tap", x=x, y=y)


def long_press(x, y, duration=0.8):
    return send("input.press", x=x, y=y, duration=duration)


def drag(x1, y1, x2, y2, duration=0.35, steps=14):
    return send("input.drag", x1=x1, y1=y1, x2=x2, y2=y2,
                duration=duration, steps=steps)


def press(combo):
    """press('return'), press('cmd+1')."""
    return send("input.keys", combo=combo)


def type_text(text, delay=0.03):
    """Type into the focused field. Tap the field and let the keyboard appear
    first — text sent before it has focus goes nowhere."""
    return send("input.text", s=text, delay=delay)


# --- gestures relative to the screen ----------------------------------------

def _win():
    """Bounds for gesture maths. screen.require, not session.require: reading
    the rect should not run the full interstitial check on every swipe."""
    return send("screen.require")


def swipe(direction, distance=0.4):
    """swipe('up'|'down'|'left'|'right') — a touch-drag centered on screen.
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
    drag(cx - dx / 2, cy - dy / 2, cx + dx / 2, cy + dy / 2,
         duration=0.12, steps=6)


def scroll(amount=300):
    """Scroll at screen center. Positive scrolls content down the way a
    trackpad two-finger-up does; use swipe() when momentum matters."""
    w = _win()
    send("input.scroll", x=w["x"] + w["w"] / 2, y=w["y"] + w["h"] / 2,
         dy=-amount)


# --- scrolling through lists ------------------------------------------------
#
# End-of-list is decided by whether the SCREEN MOVED, never by whether the
# caller's parser found new items. A dense list or a missed row must not read
# as "done" — only the content going still (after a settle window that lets
# lazy-loaded content arrive) means the end.

def _content_texts(min_conf=0.4, top_frac=0.06, bottom_frac=0.92):
    """Visible text within the scrollable band, excluding the volatile status
    bar (clock/battery) at top and the nav/home strip at bottom — a clock that
    ticks over would read as movement and stop end-detection ever firing."""
    win = _win()
    top = win["y"] + win["h"] * top_frac
    bot = win["y"] + win["h"] * bottom_frac
    return [o for o in ocr()
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
    content, ready to parse. `moved` is False when overlap >= moved_thresh:
    the list didn't advance. The default 0.6 sits in the empirical gap between
    real forward progress (overlap < ~0.45) and overscroll bounce at a
    boundary (overlap > ~0.7), which springs the content and would otherwise
    read as movement and defeat end-detection.
    """
    w = _win()
    sign = {"up": -1, "down": 1}.get(direction)  # 'up' reveals content below
    if sign is None:
        raise ValueError(f"direction must be 'up' or 'down', got {direction!r}")
    before = _text_set(_content_texts())
    send("input.scroll", x=w["x"] + w["w"] / 2, y=w["y"] + w["h"] / 2,
         dy=sign * int(w["h"] * amount), steps=10)
    time.sleep(0.4)
    prev_boxes, prev = None, None
    deadline = time.time() + settle
    while time.time() < deadline:
        boxes = _content_texts()
        cur = _text_set(boxes)
        if cur == prev:                 # two identical reads = settled
            break
        prev, prev_boxes = cur, boxes
        time.sleep(0.35)
    after = prev or frozenset()
    return {"moved": _overlap(before, after) < moved_thresh,
            "overlap": round(_overlap(before, after), 3),
            "before": before, "after": after, "boxes": prev_boxes or []}


def scroll_until(done, direction="up", amount=0.6, max_scrolls=60, settle=2.5):
    """Scroll until `done(boxes)` is truthy or the list stops moving.

    `done` receives the current content and returns a truthy value to stop;
    that value is returned. Returns None if the end is reached first.
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
            activate()
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
            activate()
    return {"items": order, "stop": "max-scrolls", "scrolls": max_scrolls}


# --- navigation -------------------------------------------------------------

def home():
    """Go to the Home Screen."""
    return send("nav.home")


def back():
    """System Back. Unsupported on iPhone Mirroring — iOS has no Back button,
    and faking one with an edge swipe is indistinguishable from a real result.
    Gate on supports('nav.back')."""
    return send("nav.back")


def app_switcher():
    return send("nav.recents")


def open_app(name):
    """Launch an app. Returns the app id that was launched."""
    result = send("apps.launch", name=name)
    wait_stable()
    return result


def current_app():
    """Foreground app id. Unsupported where the device exposes no inventory."""
    return send("apps.current")


def list_apps(include_system=False):
    """Installed app ids."""
    return send("apps.list", include_system=include_system)


def shell(cmd, binary=False, timeout=60):
    """Backend escape hatch. Unsupported on iOS, where the substrate is Quartz
    rather than a command channel — `import Quartz` in your own script."""
    return send("raw", cmd=cmd, binary=binary, timeout=timeout)


# --- timing -----------------------------------------------------------------

def wait(seconds=1.0):
    time.sleep(seconds)


def wait_stable(timeout=6.0, interval=0.5, settle=2):
    """Wait until `settle` consecutive captures are identical (animation done).
    The status-bar clock ticks once a minute, so near-misses are rare."""
    prev, same = None, 0
    deadline = time.time() + timeout
    while time.time() < deadline:
        path, _ = send("screen.capture")
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
