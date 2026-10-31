"""A panel beside System Settings that walks through the missing grants.

`phone-harness doctor ios --fix` runs this module in its own process when it
is in a terminal on the Mac. It writes the steps as JSON on stdin and reads
one JSON line back: {"outcome": ..., "granted": [...]}, where the outcome is
granted, dismissed, timeout, already_running, or error.

The panel shows the app macOS asks about. Dragging that icon into the list
in System Settings adds the app, and macOS asks for Touch ID or the
password. A new process checks the grant every second, because a running
process can keep the answer it got first. AppKit, Quartz, objc, the process
launcher and the clock are passed in, so the flow, the placement and the drag
can be tested without a window server.
"""
import functools
import json
import os
import signal
import subprocess
import sys
import time
import types

from .permission_probe import parse as parse_probe

KINDS = ("accessibility", "screen_recording")
OUTCOMES = ("granted", "dismissed", "timeout", "already_running", "error")
SETTINGS_BUNDLE = "com.apple.systempreferences"

TIMEOUT = 600.0
HEARTBEAT = 0.25
PROBE_EVERY = 1.0
PROBE_LIMIT = 8.0
SHOW_AFTER = 3.0
GRANTED_PAUSE = 1.2
DONE_PAUSE = 2.0
STALL = 20.0

WIDTH = 300
INSET = 18
ICON = 96
GAP = 16
MARGIN = 8

_PURPOSE = {
    "accessibility": "phone-harness uses this to tap and type in iPhone Mirroring.",
    "screen_recording": "phone-harness uses this to see the iPhone Mirroring window.",
}


class GuideFlow:
    """What the panel says and does next. No AppKit.

    Every event returns effects for the controller: ("open", url),
    ("render",) or ("quit", outcome). Steps are the missing grants, in
    order; one that a probe already reports as granted is skipped.
    """

    def __init__(self, steps, app, note="", timeout=TIMEOUT,
                 granted_pause=GRANTED_PAUSE, done_pause=DONE_PAUSE, stall=STALL):
        self.steps = [dict(step) for step in steps or () if step.get("kind") in KINDS]
        self.app = app or "the app"
        self.note = note or ""
        self.timeout = float(timeout)
        self.granted_pause = granted_pause
        self.done_pause = done_pause
        self.stall = stall
        self.index = 0
        self.phase = "idle"
        self.hint = ""
        self.since = 0.0
        self.deadline = None
        self.granted = set()
        self.outcome = None

    @property
    def step(self):
        return self.steps[self.index] if self.steps else None

    def start(self, now):
        self.deadline = now + self.timeout
        if not self.steps:
            return self._enter("done", now)
        return self._open_step(0, now)

    def probed(self, now, states):
        """A fresh probe answered. Only a grant moves the flow."""
        if self.phase not in ("drag", "confirm") or not states:
            return []
        for kind in KINDS:
            if states.get(kind) is True:
                self.granted.add(kind)
        if self.step["kind"] in self.granted:
            return self._enter("granted", now)
        return []

    def dropped(self, now, inside=None):
        """The drag ended with a drop. `inside` is False when it landed
        outside System Settings, None when that is not known."""
        if self.phase not in ("drag", "confirm"):
            return []
        if inside is False:
            self.hint = f"Drop {self.app} onto the list in System Settings."
            return [("render",)]
        return self._enter("confirm", now)

    def missed(self, now):
        """The drag ended without a drop."""
        if self.phase != "drag":
            return []
        self.hint = f"Drop {self.app} onto the list in System Settings."
        return [("render",)]

    def tick(self, now):
        if self.phase in ("drag", "confirm") and now >= self.deadline:
            return self._close("timeout")
        if self.phase == "confirm" and now - self.since >= self.stall:
            effects = self._enter("drag", now)
            self.hint = (f"Still off. If {self.app} is in the list, turn its switch on. "
                         f"If the switch is on, remove {self.app} with the minus button "
                         f"and drag it in again.")
            return effects
        if self.phase == "granted" and now - self.since >= self.granted_pause:
            for index in range(self.index + 1, len(self.steps)):
                if self.steps[index]["kind"] not in self.granted:
                    return self._open_step(index, now)
            return self._enter("done", now)
        if self.phase == "done" and now - self.since >= self.done_pause:
            return self._close("granted")
        return []

    def not_now(self, now):
        if self.phase == "closed":
            return []
        return self._close("granted" if self.phase == "done" else "dismissed")

    def reopen(self, now):
        if self.phase in ("drag", "confirm", "granted"):
            return [("open", self.step["url"])]
        return []

    def view(self):
        """Texts and switches for one render."""
        app = self.app
        later = (f"If macOS asks to quit {app}, choose Later."
                 if any(step["kind"] == "screen_recording" for step in self.steps) else "")
        if self.phase in ("done", "closed"):
            return _view("", "All set", "phone-harness can now see and control iPhone Mirroring.",
                         hint=later, icon="check")
        step = self.step
        name = step.get("list") or step["kind"]
        counter = f"Step {self.index + 1} of {len(self.steps)}" if len(self.steps) > 1 else ""
        if self.phase == "granted":
            return _view(counter, f"{name} is on", "", icon="check", buttons=True)
        if self.phase == "confirm":
            hint = self.hint or (later if step["kind"] == "screen_recording" else "")
            return _view(counter, name, "Confirm with Touch ID or your password.", hint=hint,
                         caption=app, note=self.note, draggable=True, buttons=True)
        if step.get("state") == "stale":
            body = (f"{app} is in the {name} list, but that entry is out of date. Select it, "
                    f"remove it with the minus button, then drag {app} in again.")
        else:
            body = (f"Drag {app} into the {name} list in System Settings. "
                    f"If {app} is already there, turn it on.")
        return _view(counter, name, body, hint=self.hint or _PURPOSE[step["kind"]],
                     caption=app, note=self.note, draggable=True, buttons=True)

    def _open_step(self, index, now):
        self.index = index
        self._enter("drag", now)
        return [("open", self.step["url"]), ("render",)]

    def _enter(self, phase, now):
        self.phase = phase
        self.since = now
        self.hint = ""
        return [("render",)]

    def _close(self, outcome):
        self.phase = "closed"
        self.outcome = outcome
        return [("quit", outcome)]


def _view(step, title, body, hint="", icon="app", caption="", note="",
          draggable=False, buttons=False):
    return {"step": step, "title": title, "body": body, "hint": hint, "icon": icon,
            "caption": caption, "note": note, "draggable": draggable, "buttons": buttons}


# --- where the panel goes ----------------------------------------------------

def settings_rect(windows, pid):
    """The largest normal on-screen window of `pid`, as (x, y, w, h) in
    CGWindowList coordinates (origin at the top left of the main display)."""
    best = None
    for window in windows or ():
        try:
            if int(window.get("kCGWindowOwnerPID", -1)) != pid:
                continue
            if int(window.get("kCGWindowLayer", -1)) != 0:
                continue
            if float(window.get("kCGWindowAlpha", 1) or 0) <= 0:
                continue
            bounds = window.get("kCGWindowBounds") or {}
            rect = tuple(float(bounds.get(key, 0)) for key in ("X", "Y", "Width", "Height"))
        except (TypeError, ValueError, AttributeError):
            continue
        if rect[2] < 100 or rect[3] < 100:
            continue
        if best is None or rect[2] * rect[3] > best[2] * best[3]:
            best = rect
    return best


def to_appkit(rect, primary_height):
    """CGWindowList (top-left origin, y down) to AppKit (bottom-left, y up)."""
    x, y, w, h = rect
    return (x, primary_height - y - h, w, h)


def _overlap(a, b):
    width = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    height = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return max(0.0, width) * max(0.0, height)


def place_beside(settings, size, screens, gap=GAP, margin=MARGIN):
    """Origin for a window of `size` next to `settings`, in AppKit coordinates.

    Right of System Settings when it fits, else left, top edges aligned,
    kept inside the visible frame of the screen that shows most of it.
    `screens` are visible frames as (x, y, w, h).
    """
    sx, sy, sw, sh = settings
    width, height = size
    x = sx + sw + gap
    y = sy + sh - height
    if not screens:
        return (x, y)
    vx, vy, vw, vh = max(screens, key=lambda screen: _overlap(screen, settings))
    if x + width > vx + vw - margin:
        left = sx - gap - width
        x = left if left >= vx + margin else vx + vw - margin - width
    y = min(max(y, vy + margin), vy + vh - margin - height)
    return (x, y)


def contains(rect, point):
    x, y, w, h = rect
    px, py = point
    return x <= px <= x + w and y <= py <= y + h


def _rect(value):
    (x, y), (w, h) = value
    return (float(x), float(y), float(w), float(h))


# --- the AppKit side ----------------------------------------------------------

def _guarded(method):
    """An exception in a callback ends the panel with "error" instead of
    being swallowed by the run loop."""
    @functools.wraps(method)
    def wrapper(self, *args):
        if self.finished:
            return None
        try:
            return method(self, *args)
        except Exception as exc:
            self._finish("error", f"{type(exc).__name__}: {exc}")
            return None
    return wrapper


def emit_line(result):
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()


class Panel:
    """Owns the window, the timer, and the probe processes. The flow decides."""

    def __init__(self, spec, appkit, quartz, objc, popen=None, clock=None, emit=None,
                 probe_argv=None):
        host = spec.get("host") or {}
        self.host_path = host.get("path") or ""
        self.app_name = host.get("name") or "the app"
        self.flow = GuideFlow(spec.get("steps"), self.app_name, note=spec.get("note"),
                              timeout=spec.get("timeout") or TIMEOUT)
        self.AppKit = appkit
        self.Quartz = quartz
        self.objc = objc
        self.popen = popen or subprocess.Popen
        self.clock = clock or time.monotonic
        self.emit = emit or emit_line
        self.probe_argv = probe_argv or [sys.executable, "-m", "phone_harness.permission_probe"]
        self.app = None
        self.window = None
        self.target = None
        self.timer = None
        self.views = {}
        self.app_icon = None
        self.check_image = None
        self.probe = None
        self.probe_started = None
        self.settings = None
        self.drag_point = None
        self.shown = False
        self.started = 0.0
        self.finished = False

    def build(self):
        A = self.AppKit
        shims = _shims(A, self.objc)
        self.app = A.NSApplication.sharedApplication()
        self.app.setActivationPolicy_(A.NSApplicationActivationPolicyAccessory)
        self.target = shims.Target.alloc().init()
        self.target.ph_owner = self

        style = (A.NSWindowStyleMaskTitled | A.NSWindowStyleMaskClosable
                 | A.NSWindowStyleMaskUtilityWindow | A.NSWindowStyleMaskNonactivatingPanel)
        window = A.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            A.NSMakeRect(0, 0, WIDTH, 360), style, A.NSBackingStoreBuffered, False)
        window.setTitle_("phone-harness")
        window.setFloatingPanel_(True)
        window.setLevel_(A.NSFloatingWindowLevel)
        window.setHidesOnDeactivate_(False)
        window.setBecomesKeyOnlyIfNeeded_(True)
        window.setReleasedWhenClosed_(False)
        window.setCollectionBehavior_(A.NSWindowCollectionBehaviorCanJoinAllSpaces
                                      | A.NSWindowCollectionBehaviorFullScreenAuxiliary)
        window.setDelegate_(self.target)
        self.window = window

        views = self.views
        views["step"] = self._label(A.NSFont.systemFontOfSize_(11), secondary=True)
        views["title"] = self._label(A.NSFont.boldSystemFontOfSize_(15))
        views["body"] = self._label(A.NSFont.systemFontOfSize_(13))
        icon = shims.Icon.alloc().initWithFrame_(A.NSMakeRect(0, 0, ICON, ICON))
        icon.ph_owner = self
        icon.setTranslatesAutoresizingMaskIntoConstraints_(False)
        icon.setImageScaling_(A.NSImageScaleProportionallyUpOrDown)
        icon.setToolTip_(f"Drag {self.app_name} into the list in System Settings")
        icon.widthAnchor().constraintEqualToConstant_(ICON).setActive_(True)
        icon.heightAnchor().constraintEqualToConstant_(ICON).setActive_(True)
        views["icon"] = icon
        views["caption"] = self._label(A.NSFont.systemFontOfSize_(12), secondary=True)
        views["hint"] = self._label(A.NSFont.systemFontOfSize_(11), secondary=True)
        views["note"] = self._label(A.NSFont.systemFontOfSize_(11), secondary=True)
        views["open_button"] = shims.Button.buttonWithTitle_target_action_(
            "Open Settings", self.target, "openSettings:")
        views["later_button"] = shims.Button.buttonWithTitle_target_action_(
            "Not Now", self.target, "notNow:")

        buttons = A.NSStackView.stackViewWithViews_([views["open_button"], views["later_button"]])
        buttons.setOrientation_(A.NSUserInterfaceLayoutOrientationHorizontal)
        buttons.setSpacing_(8.0)
        stack = A.NSStackView.stackViewWithViews_([
            views["step"], views["title"], views["body"], icon, views["caption"],
            views["hint"], views["note"], buttons,
        ])
        stack.setOrientation_(A.NSUserInterfaceLayoutOrientationVertical)
        stack.setAlignment_(A.NSLayoutAttributeCenterX)
        stack.setSpacing_(8.0)
        stack.setEdgeInsets_((16.0, float(INSET), 16.0, float(INSET)))
        stack.setDetachesHiddenViews_(True)
        stack.setTranslatesAutoresizingMaskIntoConstraints_(False)
        content = A.NSView.alloc().initWithFrame_(A.NSMakeRect(0, 0, WIDTH, 360))
        content.addSubview_(stack)
        for edge in ("leadingAnchor", "trailingAnchor", "topAnchor", "bottomAnchor"):
            getattr(stack, edge)().constraintEqualToAnchor_(getattr(content, edge)()).setActive_(True)
        stack.widthAnchor().constraintEqualToConstant_(WIDTH).setActive_(True)
        window.setContentView_(content)
        views["content"] = content

        self.app_icon = A.NSWorkspace.sharedWorkspace().iconForFile_(self.host_path)
        self.check_image = _check_image(A)
        self.timer = A.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            HEARTBEAT, self.target, "heartbeat:", None, True)
        # Common modes: the timer keeps firing while a drag is being tracked.
        A.NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, A.NSRunLoopCommonModes)

    def _label(self, font, secondary=False):
        A = self.AppKit
        label = A.NSTextField.wrappingLabelWithString_("")
        label.setFont_(font)
        label.setAlignment_(A.NSTextAlignmentCenter)
        label.setPreferredMaxLayoutWidth_(float(WIDTH - 2 * INSET))
        if secondary:
            label.setTextColor_(A.NSColor.secondaryLabelColor())
        return label

    def start(self):
        self.started = self.clock()
        self._apply(self.flow.start(self.started))
        self.heartbeat()

    def run(self):
        self.app.run()

    # Callbacks from the shims. Each is guarded.

    @_guarded
    def heartbeat(self):
        now = self.clock()
        self._follow(now)
        self._apply(self._poll_probe(now))
        self._apply(self.flow.tick(now))

    @_guarded
    def begin_drag(self, view, event):
        if not self.flow.view()["draggable"] or not self.host_path:
            return
        A = self.AppKit
        self.drag_point = None
        url = A.NSURL.fileURLWithPath_isDirectory_(self.host_path, True)
        item = A.NSDraggingItem.alloc().initWithPasteboardWriter_(url)
        item.setDraggingFrame_contents_(view.bounds(), view.image())
        view.beginDraggingSessionWithItems_event_source_([item], event, view)

    def drag_moved(self, point):
        self.drag_point = point

    def drag_mask(self, context):
        """The operations the drag offers now. Away from System Settings it
        offers none, so letting go over the Desktop, Finder or the Trash
        cannot copy or move the app. Until the drag leaves the panel, and
        whenever System Settings cannot be found, it offers all three."""
        A = self.AppKit
        offered = drag_operations(A)
        try:
            if context == A.NSDraggingContextWithinApplication or self.drag_point is None:
                return offered
            point = (float(self.drag_point[0]), float(self.drag_point[1]))
            if self.window is not None and contains(_rect(self.window.frame()), point):
                return offered
            near = self._inside_settings(point, pad=GAP + 8)
            return offered if near is not False else A.NSDragOperationNone
        except Exception:
            return offered

    @_guarded
    def drag_ended(self, point, operation):
        now = self.clock()
        self.drag_point = None
        if not operation:
            self._apply(self.flow.missed(now))
        else:
            self._apply(self.flow.dropped(now, self._inside_settings(point)))

    @_guarded
    def not_now(self):
        self._apply(self.flow.not_now(self.clock()))

    @_guarded
    def open_settings(self):
        self._apply(self.flow.reopen(self.clock()))

    @_guarded
    def window_closed(self):
        self._apply(self.flow.not_now(self.clock()))

    # Internals.

    def _apply(self, effects):
        for effect in effects:
            if self.finished:
                return
            if effect[0] == "open":
                A = self.AppKit
                A.NSWorkspace.sharedWorkspace().openURL_(A.NSURL.URLWithString_(effect[1]))
            elif effect[0] == "render":
                self._render()
            elif effect[0] == "quit":
                self._finish(effect[1])

    def _render(self):
        view = self.flow.view()
        for key in ("step", "title", "body", "caption", "hint", "note"):
            text = view.get(key) or ""
            self.views[key].setStringValue_(text)
            self.views[key].setHidden_(not text)
        icon = self.views["icon"]
        if view["icon"] == "check" and self.check_image is not None:
            icon.setImage_(self.check_image)
            icon.setContentTintColor_(self.AppKit.NSColor.systemGreenColor())
        else:
            icon.setImage_(self.app_icon)
            icon.setContentTintColor_(None)
        for key in ("open_button", "later_button"):
            self.views[key].setHidden_(not view["buttons"])
        self._fit()

    def _fit(self):
        """Size to the text, keeping the top edge where it is, so a panel
        the user moved stays put until System Settings moves."""
        (x, y), (_w, h) = self.window.frame()
        self.window.setContentSize_(self.views["content"].fittingSize())
        (_x, _y), (_w, new_h) = self.window.frame()
        self.window.setFrameOrigin_((x, y + h - new_h))

    def _screens(self):
        """(frame, visible frame) per display; the first one holds the menu bar."""
        return [(_rect(screen.frame()), _rect(screen.visibleFrame()))
                for screen in (self.AppKit.NSScreen.screens() or ())]

    def _settings_frame(self):
        apps = self.AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(
            SETTINGS_BUNDLE)
        if not apps:
            return None
        quartz = self.Quartz
        windows = quartz.CGWindowListCopyWindowInfo(
            quartz.kCGWindowListOptionOnScreenOnly | quartz.kCGWindowListExcludeDesktopElements,
            quartz.kCGNullWindowID)
        return settings_rect(windows, int(apps[0].processIdentifier()))

    def _follow(self, now):
        rect = self._settings_frame()
        if rect != self.settings:
            self.settings = rect
            if rect is not None:
                self._place()
        if not self.shown and (rect is not None or now - self.started >= SHOW_AFTER):
            if rect is None:
                self._center()
            self.window.orderFrontRegardless()
            self.shown = True

    def _place(self):
        screens = self._screens()
        if not screens or self.settings is None:
            return
        settings = to_appkit(self.settings, screens[0][0][3])
        (_x, _y), (w, h) = self.window.frame()
        self.window.setFrameOrigin_(place_beside(settings, (w, h), [v for _f, v in screens]))

    def _center(self):
        screens = self._screens()
        if not screens:
            return
        vx, vy, vw, vh = screens[0][1]
        (_x, _y), (w, h) = self.window.frame()
        self.window.setFrameOrigin_((vx + (vw - w) / 2, vy + (vh - h) / 2))

    def _inside_settings(self, point, pad=0.0):
        """Whether `point` (AppKit coordinates) is on System Settings, or
        within `pad` of it. None when System Settings is not on screen."""
        screens = self._screens()
        if self.settings is None or not screens:
            return None
        x, y, w, h = to_appkit(self.settings, screens[0][0][3])
        return contains((x - pad, y - pad, w + 2 * pad, h + 2 * pad), point)

    def _poll_probe(self, now):
        effects = []
        proc = self.probe
        if proc is not None:
            if proc.poll() is None:
                if now - self.probe_started < PROBE_LIMIT:
                    return effects
                proc.kill()
                proc.communicate()
                self.probe = None
            else:
                out, _err = proc.communicate()
                self.probe = None
                effects = self.flow.probed(now, parse_probe(out))
        due = self.probe_started is None or now - self.probe_started >= PROBE_EVERY
        if self.probe is None and due and self.flow.phase in ("drag", "confirm"):
            try:
                self.probe = self.popen(self.probe_argv, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True)
            except OSError:
                self.probe = None
            self.probe_started = now
        return effects

    def _finish(self, outcome, detail=""):
        if self.finished:
            return
        self.finished = True
        for step in (self._stop_timer, self._stop_probe, self._hide):
            try:
                step()
            except Exception:
                pass
        result = {"outcome": outcome, "granted": sorted(self.flow.granted)}
        if detail:
            result["detail"] = detail
        try:
            self.emit(result)
        except Exception:
            # The terminal that started this is gone. Quit anyway.
            pass
        if self.app is not None:
            self.app.terminate_(None)

    def _stop_timer(self):
        if self.timer is not None:
            self.timer.invalidate()

    def _stop_probe(self):
        if self.probe is not None and self.probe.poll() is None:
            self.probe.kill()
            self.probe.communicate()
        self.probe = None

    def _hide(self):
        if self.window is not None:
            self.window.orderOut_(None)


def _check_image(appkit):
    make = getattr(appkit.NSImage, "imageWithSystemSymbolName_accessibilityDescription_", None)
    image = make("checkmark.circle.fill", "Granted") if make else None
    config = getattr(appkit, "NSImageSymbolConfiguration", None)
    if image is not None and config is not None:
        sized = image.imageWithSymbolConfiguration_(config.configurationWithPointSize_weight_(64.0, 0.0))
        image = sized or image
    return image


def drag_operations(appkit):
    """What System Settings accepts when an app is dropped on its list."""
    return appkit.NSDragOperationCopy | appkit.NSDragOperationLink | appkit.NSDragOperationGeneric


def _protocols(objc, name):
    try:
        return [objc.protocolNamed(name)]
    except Exception:
        return []


_SHIMS = {}


def _shims(appkit, objc):
    """The three Objective-C subclasses. They only forward to the Panel.

    Objective-C class names are global to the process, so they are defined
    once per AppKit module.
    """
    cached = _SHIMS.get(id(appkit))
    if cached is not None and cached[0] is appkit:
        return cached[1]
    offered = drag_operations(appkit)

    class PHGrantIcon(appkit.NSImageView, protocols=_protocols(objc, "NSDraggingSource")):
        def acceptsFirstMouse_(self, event):
            return True

        def mouseDown_(self, event):
            self.ph_dragging = False

        def mouseDragged_(self, event):
            owner = getattr(self, "ph_owner", None)
            if owner is None or getattr(self, "ph_dragging", False):
                return
            self.ph_dragging = True
            owner.begin_drag(self, event)

        def draggingSession_sourceOperationMaskForDraggingContext_(self, session, context):
            owner = getattr(self, "ph_owner", None)
            return offered if owner is None else owner.drag_mask(context)

        def draggingSession_movedToPoint_(self, session, point):
            owner = getattr(self, "ph_owner", None)
            if owner is not None:
                owner.drag_moved(point)

        def draggingSession_endedAtPoint_operation_(self, session, point, operation):
            self.ph_dragging = False
            owner = getattr(self, "ph_owner", None)
            if owner is not None:
                owner.drag_ended(point, operation)

    class PHGrantButton(appkit.NSButton):
        def acceptsFirstMouse_(self, event):
            return True

    class PHGrantTarget(appkit.NSObject, protocols=_protocols(objc, "NSWindowDelegate")):
        def heartbeat_(self, timer):
            self.ph_owner.heartbeat()

        def notNow_(self, sender):
            self.ph_owner.not_now()

        def openSettings_(self, sender):
            self.ph_owner.open_settings()

        def windowWillClose_(self, notification):
            self.ph_owner.window_closed()

    shims = types.SimpleNamespace(Icon=PHGrantIcon, Button=PHGrantButton, Target=PHGrantTarget)
    _SHIMS[id(appkit)] = (appkit, shims)
    return shims


# --- entry point --------------------------------------------------------------

def take_lock(path):
    """An exclusive lock on `path`, or None when another process holds it.
    The lock goes away with the process."""
    try:
        import fcntl
    except ImportError:
        return types.SimpleNamespace(close=lambda: None)
    handle = None
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        handle = open(path, "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if handle is not None:
            handle.close()
        return None
    return handle


def _frameworks():
    import AppKit
    import Quartz
    import objc
    return AppKit, Quartz, objc


def run_spec(text, load=None, lock=None, emit=None, **panel_options):
    """Run one panel from the JSON spec `doctor --fix` writes. Returns the exit code."""
    emit = emit or emit_line
    try:
        spec = json.loads(text or "{}")
        if not isinstance(spec, dict):
            raise ValueError("not an object")
    except ValueError as exc:
        emit({"outcome": "error", "granted": [], "detail": f"bad spec: {exc}"})
        return 2
    held = None
    if spec.get("lock"):
        held = (lock or take_lock)(spec["lock"])
        if held is None:
            emit({"outcome": "already_running", "granted": []})
            return 0
    try:
        appkit, quartz, objc = (load or _frameworks)()
    except Exception as exc:
        emit({"outcome": "error", "granted": [], "detail": f"AppKit is not available: {exc}"})
        return 0
    panel = Panel(spec, appkit, quartz, objc, emit=emit, **panel_options)
    try:
        panel.build()
        panel.start()
    except Exception as exc:
        panel._finish("error", f"{type(exc).__name__}: {exc}")
        return 0
    if not panel.finished:
        panel.run()
    if held is not None:
        held.close()
    return 0


def main():
    # Ctrl-C in the terminal ends the panel at once. Python's own handler
    # would wait for the run loop to hand control back.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    return run_spec(sys.stdin.read())


if __name__ == "__main__":
    sys.exit(main())
