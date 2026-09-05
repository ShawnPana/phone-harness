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


def image_point(x, y, image_size=None):
    """Convert a screenshot pixel point to a global screen point.

    Pass the pixel coordinates observed in the most recent screenshot. The
    current window and capture size are queried every call so window movement,
    resizing, and Retina/non-Retina captures are handled correctly.
    """
    info = screen_info()
    iw, ih = image_size or info["img_px"]
    win = info["window"]
    if iw <= 0 or ih <= 0:
        raise ValueError("image dimensions must be positive")
    return (win["x"] + x * win["w"] / iw,
            win["y"] + y * win["h"] / ih)


def tap_image_point(x, y, image_size=None):
    """Tap a point identified in screenshot pixel coordinates.

    This is for icon-only controls that OCR cannot locate. It converts the
    point using the current window geometry, then sends the normal tap event.
    """
    gx, gy = image_point(x, y, image_size=image_size)
    tap(gx, gy)
    return {"image": {"x": x, "y": y}, "screen": {"x": gx, "y": gy}}


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
    "tree" when the OS reported it exactly.

    Nothing is filtered. `min_confidence` is accepted and ignored: every box
    carries its own `confidence`, and what counts as trustworthy depends on
    the screen and the script -- Chinese text scores lower than English, and a
    cutoff tuned on one silently drops the other.
    """
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


def type_text(text, delay=0.03, keystrokes=False):
    """Type into the focused field. Tap the field and let the keyboard appear
    first — text sent before it has focus goes nowhere, silently, so check a
    capture afterwards.

    Pastes by default so the text arrives exactly as written; keystrokes=True
    sends real key events for fields that need them."""
    return send("input.text", s=text, delay=delay, keystrokes=keystrokes)


# --- gestures relative to the screen ----------------------------------------

def _win():
    """Bounds for gesture maths. screen.require, not session.require: reading
    the rect should not run the full interstitial check on every swipe."""
    return send("screen.require")


# Directions name WHAT YOU WANT TO SEE, not which way a finger moves. Finger
# metaphors invert with the Mac's natural-scroll setting and with whoever is
# describing them; "show me what is further down the list" does not.
#
#   down  -> reveal content further down     up    -> reveal content above
#   right -> reveal content further right    left  -> reveal content to the left
#
# Verified independent of com.apple.swipescrolldirection: the same deltas move
# the phone the same way whether natural scrolling is on or off.
# The tuple is the (dx, dy) SIGN sent to input.scroll to achieve that reveal.
# A negative dy reveals content further down: the same convention the old
# {"up": -1} table used, just named after the result instead of the finger.
_DIRECTIONS = {"down": (0, -1), "up": (0, 1), "right": (-1, 0), "left": (1, 0)}


def _point(at, win):
    """Where the gesture is aimed. Defaults to the window centre.

    The centre is fine for a full-screen list and wrong for everything else: a
    horizontal strip, a carousel, an inner scroll view, a sheet over a page.
    Aiming at the centre of Weather hits the temperature readout, which does
    not scroll, while the hourly strip below it does.

    `at` may be (x, y) in screen points, or any box from ocr()/find_text()/
    find_nodes() — so `scroll("right", at=find_text("Now")[0])` reads the way
    you would say it.
    """
    if at is None:
        return win["x"] + win["w"] / 2, win["y"] + win["h"] / 2
    if isinstance(at, dict):
        return at["x"], at["y"]
    x, y = at
    return x, y


def _delta(direction, amount, win):
    try:
        sx, sy = _DIRECTIONS[direction]
    except KeyError:
        raise ValueError(
            f"direction must be one of {sorted(_DIRECTIONS)}, got {direction!r}"
        ) from None
    return int(sx * win["w"] * amount), int(sy * win["h"] * amount)


def swipe(direction, distance=0.4, at=None):
    """swipe('up'|'down'|'left'|'right') — a momentum touch-drag.

    `direction` is FINGER MOTION, which is the opposite of scroll()'s: this is
    the one place the two verbs deliberately disagree, because English does.
    "swipe up for the next video" means the thumb goes up; "scroll down the
    page" means show me what is below. Both describe the same outcome, and
    each function follows the convention its own word already carries.

        swipe("up")     thumb up    (how everyone says "next")
        scroll("down")  see below

    Use scroll() for anything scrollable. On macOS 26 a vertical touch-drag is
    dropped by iPhone Mirroring, so swipe("up")/swipe("down") move nothing in a
    list or a feed -- measured on Settings and on TikTok. Horizontal survives,
    so swipe("left")/swipe("right") stay the way to flip Home Screen pages and
    carousels, which a scroll cannot do.

    `at` aims the gesture; defaults to the window centre.
    """
    w = _win()
    cx, cy = _point(at, w)
    dx = {"left": -1, "right": 1}.get(direction, 0) * w["w"] * distance
    dy = {"up": -1, "down": 1}.get(direction, 0) * w["h"] * distance
    if not dx and not dy:
        raise ValueError(
            f"direction must be one of ['down', 'left', 'right', 'up'], "
            f"got {direction!r}")
    # Fast, short drag = a momentum flick. A slow drag barely registers on iOS
    # (it won't even flip a Home-Screen page); the flick is what snaps pages
    # and carousels. For scrolling lists use scroll()/scroll_collect() instead.
    drag(cx - dx / 2, cy - dy / 2, cx + dx / 2, cy + dy / 2,
         duration=0.12, steps=6)


def scroll(direction="down", amount=0.3, at=None):
    """Scroll the screen. `direction` is what you want to SEE:
    'down' reveals content further down the list, 'right' what is off to the
    right. `amount` is a fraction of the screen.

    `at` aims the gesture — (x, y) or a box from ocr()/find_text(). Only the
    scroll view under that point moves, so pass it whenever the thing you want
    to scroll is not the full-screen list: a horizontal strip, a carousel, an
    inner list. Defaults to the window centre.

    Use swipe() when momentum matters.
    """
    w = _win()
    dx, dy = _delta(direction, amount, w)
    px, py = _point(at, w)
    send("input.scroll", x=px, y=py, dx=dx, dy=dy)


# --- scrolling through lists ------------------------------------------------
#
# End-of-list is decided by whether the SCREEN MOVED, never by whether the
# caller's parser found new items. A dense list or a missed row must not read
# as "done" — only the content going still (after a settle window that lets
# lazy-loaded content arrive) means the end.

def _content_texts():
    """Every visible text box. Nothing cropped, nothing filtered.

    This used to crop the status bar and home strip and drop low-confidence
    boxes, to keep a ticking clock from tripping the old movement test. That
    test is gone. scroll_collect de-dupes, so a clock ticking over costs at
    most one spurious item a minute -- while the crop was hiding nav bars and
    tab bars from every walk, and the cutoff was dropping Chinese rows.
    """
    return ocr()


def _text_set(boxes):
    return frozenset(o["text"].strip() for o in boxes if o["text"].strip())


def scroll_screen(direction="down", amount=0.6, settle=2.5, moved_thresh=None,
                  at=None):
    """One scroll gesture, then wait for the screen to settle (so a lazy-load
    spinner resolves before we judge movement).

    Returns what is on screen afterwards, and what was there before:

      before / after   the OCR text sets
      boxes            the settled content, ready to parse

    Nothing here compares them for you. It used to return a Jaccard overlap of
    the two sets, which is a comparison this function picked on your behalf --
    and it was wrong in exactly the case it mattered, reporting "completely
    different" for two screens that both had no readable text. Compare them
    however suits what you asked for, or take a screenshot and look.

    `moved_thresh` is accepted and ignored; it tuned an old text-overlap test.
    """
    w = _win()
    dx, dy = _delta(direction, amount, w)
    px, py = _point(at, w)

    before = _text_set(_content_texts())

    send("input.scroll", x=px, y=py, dx=dx, dy=dy, steps=10)
    time.sleep(0.4)

    # Settle: wait until two reads of the content are IDENTICAL, or the
    # caller's window runs out. Exact equality, so there is nothing to tune --
    # a screen that never settles (a playing video) simply uses its budget.
    boxes, prev_set, deadline = _content_texts(), None, time.time() + settle
    while time.time() < deadline:
        cur_set = _text_set(boxes)
        if cur_set == prev_set:
            break
        prev_set = cur_set
        time.sleep(0.25)
        boxes = _content_texts()

    return {"before": before, "after": _text_set(boxes), "boxes": boxes}


def scroll_until(done, direction="down", amount=0.6, max_scrolls=60,
                 settle=2.5, at=None):
    """Scroll until `done(boxes)` is truthy, or the screen stops changing.

    `done` receives the current content and returns a truthy value to stop;
    that value is returned. Returns None if the end is reached first.

    YOUR predicate decides success. The stop condition is only ever "the screen
    stopped changing" — never a guess about whether a scroll counts as a scroll,
    because that depends on the app: a list translates, a feed swaps to the next
    item, and both are progress.
    """
    boxes = _content_texts()
    hit = done(boxes)
    if hit:
        return hit
    stale = 0
    for _ in range(max_scrolls):
        res = scroll_screen(direction, amount, settle, at=at)
        hit = done(res["boxes"])
        if hit:
            return hit
        if res["after"] != res["before"]:
            stale = 0
        else:
            stale += 1
            if stale >= 2:      # the content came back byte-identical twice
                return None
            time.sleep(0.8)
            activate()
    return None


def scroll_collect(extract=None, key=None, direction="down", amount=0.6,
                   max_scrolls=400, end_after=3, settle=2.5, on_progress=None,
                   at=None):
    """Scroll a list top-to-bottom, extracting and de-duping items each screen,
    until the list reaches its true end.

    - on_progress(scroll_index, total_items, new_items) if you want a running
      count.
    - extract(boxes) -> list of items for the current screen. Default returns
      each content text line, so a bare scroll_collect() gathers all text.
    - key(item) -> hashable de-dup key (default: the item itself).
    - Stops after `end_after` consecutive scrolls that yield NO NEW ITEMS (the
      settle window already gave lazy-load a chance), or `max_scrolls`.

    The end condition is your extractor running dry, not a pixel judgement
    about whether a scroll counts as a scroll. That works on a list, which
    translates, and on a feed, which swaps to the next item -- both are
    progress, and only your extractor knows whether progress produced anything
    you wanted.

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
        res = scroll_screen(direction, amount, settle, at=at)
        new = ingest(res["boxes"])
        if on_progress:
            on_progress(i, len(order), new)
        if new:
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


def wait_for_text(query, timeout=10.0, exact=False, interval=0.5):
    """Poll until `query` is visible; -> its box or None. The verify step
    after an action: wait for the thing you expect to appear rather than
    sleeping and hoping. Cheap where the device has a tree, a capture+OCR
    per poll where it doesn't."""
    deadline = time.time() + timeout
    while True:
        hits = find_text(query, exact=exact)
        if hits:
            return hits[0]
        if time.time() >= deadline:
            return None
        time.sleep(interval)


def wait_for_app(app_id, timeout=10.0, interval=0.3):
    """Poll until `app_id` is the foreground app; -> True/False. A ~0.1s check
    on Android; Unsupported where the device exposes no foreground app."""
    deadline = time.time() + timeout
    while True:
        if send("apps.current") == app_id:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(interval)


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
