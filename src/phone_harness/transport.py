"""The one seam every device sits behind.

A backend implements a single method — send(op, **kw) — over the op vocabulary
below. Ops are named for what they MEAN, never for how a platform does them,
which is what lets helpers.py carry no platform knowledge at all: `nav.home`
means the same thing to an iPhone and a Pixel, though one is a Cmd+1 keystroke
into a video stream and the other is a KEYCODE_HOME over adb.

This mirrors browser-harness, where helpers.py has no idea whether the browser
is local or in the cloud because everything funnels through cdp(). Phones have
no shared wire protocol to funnel through — SkyLight event records and adb have
nothing in common — so the shared layer is this vocabulary instead.

    screen.capture      path=None       -> (png_path, bounds)
    screen.bounds                       -> {x, y, w, h, id} or None
    screen.require                      -> bounds, or raise
    screen.text         min_confidence  -> [{text, confidence, source, x, y, w, h}]
    screen.text_pixels  min_confidence  -> same, forced through pixel OCR

        screen.text is the load-bearing op. iOS answers with Vision OCR over a
        capture; a device with an accessibility tree answers from the tree.
        Same contract, same coordinate space, very different fidelity, so each
        box carries `source`: "pixels" means a recogniser inferred it and may
        have misread, "tree" means the OS reported it exactly. It has to be an
        explicit field — Vision returns confidence 1.0 for clean text just as a
        tree does, so confidence cannot tell them apart.

    input.tap           x, y
    input.press         x, y, duration              long press
    input.drag          x1, y1, x2, y2, duration, steps
    input.scroll        x, y, dy, steps             +dy scrolls content up
    input.keys          combo
    input.text          s, delay

        duration and steps stay in the vocabulary rather than being hidden. On
        iOS a fast short drag is a momentum flick and a slow one barely
        registers; over adb the same knob decides whether a list flings past
        rows between captures. Abstracting it away would cost behaviour.

    nav.home / nav.back / nav.recents
    apps.launch  name -> app id
    apps.current      -> app id or None
    apps.list    include_system=False -> [app id]

        Unsupported where the concept does not exist, rather than emulated.
        iPhone Mirroring has no system Back button and exposes no app
        inventory; Android has no Spotlight. A fake Back that guesses at an
        edge swipe returns a result the caller cannot tell from a real one.

    session.state                       -> backend-defined string, 'ready' when usable
    session.require                     -> bounds, or raise telling the USER what to do
    session.refocus                     -> None    (a no-op where nothing needs focus)
    session.detail                      -> str, why the session is unusable

    focus.probe                         -> opaque snapshot
    focus.diff          before, after   -> {raised, stole_focus}

        Whether driving the phone disturbed whatever the user was doing. Two
        separate questions on purpose: taking keyboard focus is a mild
        annoyance, covering the screen is not, and a backend that reaches the
        device directly does neither.

    tree                                -> [node]   where the device has one
    raw                 ...             backend-specific escape hatch

Anything a backend genuinely cannot do raises Unsupported. Callers gate on
supports(op) rather than guessing.
"""
import importlib
import json
import os
from pathlib import Path


class Unsupported(RuntimeError):
    """This device cannot do that op, and retrying will not help.

    Distinct from a failure: iPhone Mirroring has no accessibility tree, so
    `tree` is not a flaky call, it is absent.
    """


OP_NAMES = (
    "screen.capture", "screen.bounds", "screen.require",
    "screen.text", "screen.text_pixels",
    "input.tap", "input.press", "input.drag", "input.scroll",
    "input.keys", "input.text",
    "nav.home", "nav.back", "nav.recents",
    "apps.launch", "apps.current", "apps.list",
    "session.state", "session.require", "session.refocus", "session.detail",
    "focus.probe", "focus.diff",
    "tree", "raw",
)


class Backend:
    """Dispatches send() to _screen_capture, _input_tap, _nav_home, and so on.

    A subclass defines only the ops it can honour; everything it omits becomes
    Unsupported automatically, so a backend never keeps a capability list in
    sync with its own code.
    """

    name = "backend"

    def _slot(self, op):
        return "_" + op.replace(".", "_")

    def send(self, op, **kw):
        fn = getattr(self, self._slot(op), None)
        if fn is None:
            raise Unsupported(f"{self.name} cannot {op!r}")
        return fn(**kw)

    def supports(self, op):
        return getattr(self, self._slot(op), None) is not None

    def ops(self):
        """Every op this backend implements, filtered against the vocabulary so
        internal helpers can never masquerade as ops."""
        return [op for op in OP_NAMES if self.supports(op)]


CONFIG_DIR = Path(os.environ.get("PHONE_HARNESS_CONFIG_DIR",
                                 Path.home() / ".config" / "phone-harness"))


def default_platform():
    """The platform to use when nothing says otherwise: the one saved by
    `phone-harness use <platform>`, else ios."""
    try:
        return json.loads((CONFIG_DIR / "config.json").read_text()).get(
            "platform") or "ios"
    except (OSError, ValueError):
        return "ios"


def set_default_platform(platform):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = CONFIG_DIR / "config.json"
    try:
        cfg = json.loads(path.read_text())
    except (OSError, ValueError):
        cfg = {}
    cfg["platform"] = platform
    path.write_text(json.dumps(cfg, indent=2) + "\n")


def connect(platform=None, **kw):
    """Build a backend. Explicit argument, else PHONE_HARNESS_PLATFORM, else
    the default saved by `phone-harness use <platform>`, else ios.

    Returns an object rather than installing a module global, so a script can
    hold two devices at once instead of being limited to one per process.
    helpers.py binds one of these as its default.
    """
    platform = (platform
                or os.environ.get("PHONE_HARNESS_PLATFORM")
                or default_platform()).lower()
    if platform in ("ios", "iphone", "ipad"):
        return importlib.import_module(".ios", __package__).IPhone(**kw)
    if platform == "android":
        return importlib.import_module(".android", __package__).Android(**kw)
    raise ValueError(f"unknown platform {platform!r}")
