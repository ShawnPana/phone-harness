"""The permission panel and the fresh-process probe. AppKit, Quartz, objc,
the process launcher and the clock are fakes: no window, no Settings, no TCC."""
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from phone_harness import permission_panel as pp  # noqa: E402
from phone_harness import permission_probe as probe  # noqa: E402

TERMINAL = "/System/Applications/Utilities/Terminal.app"
AX_URL = "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Accessibility"
SR_URL = "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_ScreenCapture"
AX_STEP = {"kind": "accessibility", "list": "Accessibility", "url": AX_URL, "state": "denied"}
SR_STEP = {"kind": "screen_recording", "list": "Screen & System Audio Recording", "url": SR_URL,
           "state": "denied"}
COPY, LINK, GENERIC = 1, 2, 4
OUTSIDE, WITHIN = 0, 1
SETTINGS_PID = 4242


class Recorder:
    """An AppKit object: every call is recorded, and `setX_(v)` keeps v as X."""

    def __init__(self, **state):
        self.calls = []
        self.state = dict(state)

    def __getattr__(self, name):
        if name.startswith("_") or name.startswith("ph_"):
            raise AttributeError(name)

        def call(*args):
            self.calls.append((name, args))
            if name.startswith("set") and name.endswith("_") and len(args) == 1:
                self.state[name[3:-1]] = args[0]
                return None
            if name.endswith("Anchor") or name.startswith("constraint"):
                return Recorder()
            return None
        return call

    def called(self, name):
        return [args for called, args in self.calls if called == name]


class ObjCBase(Recorder):
    """Base for the Python subclasses the panel defines (NSImageView,
    NSButton, NSObject): takes the `protocols=` class keyword like PyObjC."""
    protocols = None

    def __init_subclass__(cls, protocols=None, **kw):
        super().__init_subclass__(**kw)
        cls.protocols = protocols

    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self

    def initWithFrame_(self, frame):
        self.state["Frame"] = frame
        return self


class ImageView(ObjCBase):
    def bounds(self):
        return ((0.0, 0.0), (96.0, 96.0))

    def image(self):
        return self.state.get("Image")


class Button(ObjCBase):
    @classmethod
    def buttonWithTitle_target_action_(cls, title, target, action):
        button = cls()
        button.title, button.target, button.action = title, target, action
        return button


class Window(Recorder):
    TITLE_BAR = 28.0

    def __init__(self, rect, style):
        super().__init__()
        (x, y), (w, h) = rect
        self.style = style
        self.origin = (float(x), float(y))
        self.size = (float(w), float(h))

    def frame(self):
        return (self.origin, self.size)

    def setFrameOrigin_(self, origin):
        self.calls.append(("setFrameOrigin_", (origin,)))
        self.origin = (float(origin[0]), float(origin[1]))

    def setContentSize_(self, size):
        self.calls.append(("setContentSize_", (size,)))
        self.size = (float(size[0]), float(size[1]) + self.TITLE_BAR)


class Content(Recorder):
    def __init__(self, size):
        super().__init__()
        self.size = size

    def fittingSize(self):
        return self.size


class Item(Recorder):
    def __init__(self, writer):
        super().__init__()
        self.writer = writer


class Workspace(Recorder):
    def iconForFile_(self, path):
        self.calls.append(("iconForFile_", (path,)))
        return ("icon", path)


class Image(Recorder):
    def imageWithSymbolConfiguration_(self, config):
        return ("sized", config)


class Screen:
    def __init__(self, frame, visible):
        self._frame, self._visible = frame, visible

    def frame(self):
        return self._frame

    def visibleFrame(self):
        return self._visible


class RunningApp:
    def __init__(self, pid):
        self.pid = pid

    def processIdentifier(self):
        return self.pid


class Alloc:
    """`X.alloc().initSomething_(...)` for a fake class."""

    def __init__(self, make):
        self.make = make

    def alloc(self):
        return self

    def __getattr__(self, name):
        if name.startswith("init"):
            return lambda *args: self.make(*args)
        raise AttributeError(name)


def fake_appkit(content_size=(300.0, 240.0)):
    A = types.SimpleNamespace(
        NSApplicationActivationPolicyAccessory=1,
        NSWindowStyleMaskTitled=1, NSWindowStyleMaskClosable=2,
        NSWindowStyleMaskUtilityWindow=16, NSWindowStyleMaskNonactivatingPanel=128,
        NSBackingStoreBuffered=2, NSFloatingWindowLevel=3,
        NSWindowCollectionBehaviorCanJoinAllSpaces=1,
        NSWindowCollectionBehaviorFullScreenAuxiliary=256,
        NSImageScaleProportionallyUpOrDown=3,
        NSUserInterfaceLayoutOrientationHorizontal=0, NSUserInterfaceLayoutOrientationVertical=1,
        NSLayoutAttributeCenterX=9, NSTextAlignmentCenter=2,
        NSRunLoopCommonModes="kCFRunLoopCommonModes",
        NSDragOperationNone=0, NSDragOperationCopy=COPY, NSDragOperationLink=LINK,
        NSDragOperationGeneric=GENERIC,
        NSDraggingContextOutsideApplication=OUTSIDE, NSDraggingContextWithinApplication=WITHIN,
        NSImageView=ImageView, NSButton=Button, NSObject=ObjCBase,
    )
    A.app = Recorder()
    A.workspace = Workspace()
    A.windows = []
    A.timers = []
    A.run_loop = Recorder()
    A.content = Content(content_size)
    A.running = [RunningApp(SETTINGS_PID)]
    A.screens = [Screen(((0.0, 0.0), (1440.0, 900.0)), ((0.0, 0.0), (1440.0, 875.0)))]
    A.symbol = Image()

    def make_window(rect, style, backing, defer):
        window = Window(rect, style)
        A.windows.append(window)
        return window

    def make_timer(interval, target, selector, info, repeats):
        timer = Recorder(interval=interval, target=target, selector=selector, repeats=repeats)
        A.timers.append(timer)
        return timer

    A.NSMakeRect = lambda x, y, w, h: ((x, y), (w, h))
    A.NSApplication = types.SimpleNamespace(sharedApplication=lambda: A.app)
    A.NSPanel = Alloc(make_window)
    A.NSView = Alloc(lambda frame: A.content)
    A.NSDraggingItem = Alloc(Item)
    A.NSWorkspace = types.SimpleNamespace(sharedWorkspace=lambda: A.workspace)
    A.NSURL = types.SimpleNamespace(
        fileURLWithPath_isDirectory_=lambda path, isdir: ("file-url", path, isdir),
        URLWithString_=lambda text: ("url", text),
    )
    A.NSFont = types.SimpleNamespace(systemFontOfSize_=lambda size: ("font", size),
                                     boldSystemFontOfSize_=lambda size: ("bold", size))
    A.NSColor = types.SimpleNamespace(secondaryLabelColor=lambda: "secondary",
                                      systemGreenColor=lambda: "green")
    A.NSTextField = types.SimpleNamespace(wrappingLabelWithString_=lambda text: Recorder())
    A.NSStackView = types.SimpleNamespace(stackViewWithViews_=lambda views: Recorder(views=views))
    A.NSImage = types.SimpleNamespace(
        imageWithSystemSymbolName_accessibilityDescription_=lambda name, desc: A.symbol)
    A.NSImageSymbolConfiguration = types.SimpleNamespace(
        configurationWithPointSize_weight_=lambda size, weight: ("config", size, weight))
    A.NSTimer = types.SimpleNamespace(timerWithTimeInterval_target_selector_userInfo_repeats_=make_timer)
    A.NSRunLoop = types.SimpleNamespace(currentRunLoop=lambda: A.run_loop)
    A.NSRunningApplication = types.SimpleNamespace(
        runningApplicationsWithBundleIdentifier_=lambda bundle: (
            list(A.running) if bundle == pp.SETTINGS_BUNDLE else []))
    A.NSScreen = types.SimpleNamespace(screens=lambda: list(A.screens))
    return A


def settings_window(x, y, w, h, pid=SETTINGS_PID, layer=0, alpha=1.0):
    return {"kCGWindowOwnerPID": pid, "kCGWindowLayer": layer, "kCGWindowAlpha": alpha,
            "kCGWindowBounds": {"X": x, "Y": y, "Width": w, "Height": h}}


def fake_quartz(windows):
    Q = types.SimpleNamespace(kCGWindowListOptionOnScreenOnly=1,
                              kCGWindowListExcludeDesktopElements=16, kCGNullWindowID=0)
    Q.windows = list(windows)
    Q.queries = []

    def copy(options, window_id):
        Q.queries.append((options, window_id))
        return list(Q.windows)
    Q.CGWindowListCopyWindowInfo = copy
    return Q


FAKE_OBJC = types.SimpleNamespace(protocolNamed=lambda name: ("protocol", name))


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Proc:
    def __init__(self, out, done=True):
        self.out, self.done, self.killed = out, done, False

    def poll(self):
        return 0 if self.done else None

    def communicate(self, *args, **kw):
        return self.out, ""

    def kill(self):
        self.killed = True
        self.done = True


class Launcher:
    """Stands in for subprocess.Popen: each probe prints `self.state`."""

    def __init__(self):
        self.state = {"accessibility": False, "screen_recording": False}
        self.calls = []
        self.hang = False
        self.procs = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        proc = Proc(json.dumps(self.state) + "\n", done=not self.hang)
        self.procs.append(proc)
        return proc


def make_panel(steps=None, windows=None, note="", timeout=600.0, appkit=None):
    A = appkit or fake_appkit()
    Q = fake_quartz([settings_window(100, 100, 700, 600)] if windows is None else windows)
    clock = Clock()
    launcher = Launcher()
    emitted = []
    spec = {"host": {"name": "Terminal", "path": TERMINAL},
            "steps": [AX_STEP, SR_STEP] if steps is None else steps,
            "note": note, "timeout": timeout}
    panel = pp.Panel(spec, A, Q, FAKE_OBJC, popen=launcher, clock=clock, emit=emitted.append,
                     probe_argv=["python", "-m", "phone_harness.permission_probe"])
    panel.build()
    panel.start()
    return panel, A, Q, clock, launcher, emitted


def advance(panel, clock, seconds, step=0.25):
    end = clock.now + seconds
    while clock.now < end - 1e-9 and not panel.finished:
        clock.now = round(clock.now + step, 6)
        panel.heartbeat()


class Flow(unittest.TestCase):
    def test_steps_run_in_order_and_a_grant_that_already_landed_is_skipped(self):
        flow = pp.GuideFlow([AX_STEP, SR_STEP], "Terminal")
        self.assertEqual(flow.start(0.0), [("open", AX_URL), ("render",)])
        view = flow.view()
        self.assertEqual(view["step"], "Step 1 of 2")
        self.assertEqual(view["title"], "Accessibility")
        self.assertEqual(view["body"], "Drag Terminal into the Accessibility list in System Settings. "
                                       "If Terminal is already there, turn it on.")
        self.assertEqual(view["hint"], "phone-harness uses this to tap and type in iPhone Mirroring.")
        self.assertTrue(view["draggable"])
        self.assertEqual(flow.probed(1.0, {"accessibility": False, "screen_recording": False}), [])
        self.assertEqual(flow.probed(2.0, {"accessibility": True, "screen_recording": True}),
                         [("render",)])
        self.assertEqual(flow.phase, "granted")
        self.assertEqual((flow.view()["icon"], flow.view()["title"]), ("check", "Accessibility is on"))
        self.assertFalse(flow.view()["draggable"])
        self.assertEqual(flow.tick(3.0), [])
        # Screen Recording landed too, so the flow goes straight to done.
        self.assertEqual(flow.tick(3.3), [("render",)])
        self.assertEqual(flow.phase, "done")
        self.assertEqual(flow.view()["title"], "All set")
        self.assertIn("choose Later", flow.view()["hint"])
        self.assertEqual(flow.view()["buttons"], False)
        self.assertEqual(flow.tick(5.4), [("quit", "granted")])
        self.assertEqual(flow.outcome, "granted")

    def test_next_missing_pane_opens_after_the_checkmark(self):
        flow = pp.GuideFlow([AX_STEP, SR_STEP], "Terminal")
        flow.start(0.0)
        flow.probed(1.0, {"accessibility": True, "screen_recording": False})
        self.assertEqual(flow.tick(2.3), [("open", SR_URL), ("render",)])
        self.assertEqual(flow.view()["step"], "Step 2 of 2")
        self.assertEqual(flow.view()["title"], "Screen & System Audio Recording")
        self.assertEqual(flow.view()["hint"], "phone-harness uses this to see the iPhone Mirroring window.")

    def test_a_drop_asks_for_touch_id_and_a_stall_explains_the_switch(self):
        flow = pp.GuideFlow([AX_STEP], "Terminal", note="If the list also shows python3.12 on its own row, turn that on too.")
        flow.start(0.0)
        self.assertEqual(flow.view()["step"], "")
        self.assertEqual(flow.dropped(1.0, inside=True), [("render",)])
        self.assertEqual(flow.view()["body"], "Confirm with Touch ID or your password.")
        self.assertIn("python3.12", flow.view()["note"])
        self.assertEqual(flow.tick(20.0), [])
        self.assertEqual(flow.tick(21.0), [("render",)])
        self.assertEqual(flow.phase, "drag")
        self.assertIn("turn its switch on", flow.view()["hint"])
        self.assertIn("minus button", flow.view()["hint"])

    def test_a_drop_elsewhere_or_no_drop_says_where_to_drop(self):
        flow = pp.GuideFlow([AX_STEP], "Terminal")
        flow.start(0.0)
        self.assertEqual(flow.dropped(1.0, inside=False), [("render",)])
        self.assertEqual(flow.phase, "drag")
        self.assertEqual(flow.view()["hint"], "Drop Terminal onto the list in System Settings.")
        flow2 = pp.GuideFlow([AX_STEP], "Terminal")
        flow2.start(0.0)
        self.assertEqual(flow2.missed(1.0), [("render",)])
        self.assertEqual(flow2.view()["hint"], "Drop Terminal onto the list in System Settings.")
        self.assertEqual(flow2.dropped(2.0), [("render",)])
        self.assertEqual(flow2.phase, "confirm")
        self.assertEqual(flow2.missed(3.0), [])

    def test_a_stale_entry_says_remove_and_drag_again(self):
        flow = pp.GuideFlow([dict(AX_STEP, state="stale")], "Terminal")
        flow.start(0.0)
        body = flow.view()["body"]
        self.assertIn("out of date", body)
        self.assertIn("minus button", body)
        self.assertIn("drag Terminal in again", body)

    def test_not_now_timeout_and_reopen(self):
        flow = pp.GuideFlow([AX_STEP], "Terminal", timeout=600)
        flow.start(0.0)
        self.assertEqual(flow.reopen(5.0), [("open", AX_URL)])
        self.assertEqual(flow.tick(599.0), [])
        self.assertEqual(flow.tick(600.0), [("quit", "timeout")])
        self.assertEqual(flow.not_now(601.0), [])
        self.assertEqual(flow.reopen(601.0), [])

        later = pp.GuideFlow([AX_STEP], "Terminal")
        later.start(0.0)
        self.assertEqual(later.not_now(1.0), [("quit", "dismissed")])

        done = pp.GuideFlow([AX_STEP], "Terminal")
        done.start(0.0)
        done.probed(1.0, {"accessibility": True})
        done.tick(2.5)
        self.assertEqual(done.phase, "done")
        self.assertEqual(done.not_now(3.0), [("quit", "granted")])

    def test_unknown_kinds_are_dropped_and_no_steps_is_all_set(self):
        flow = pp.GuideFlow([{"kind": "camera", "url": "x"}], "Terminal")
        self.assertEqual(flow.start(0.0), [("render",)])
        self.assertEqual(flow.phase, "done")
        self.assertEqual(flow.view()["hint"], "")


class Geometry(unittest.TestCase):
    def test_settings_rect_is_the_largest_normal_visible_window_of_the_pid(self):
        windows = [
            settings_window(0, 0, 2000, 2000, pid=1),
            settings_window(10, 10, 300, 300),
            settings_window(20, 20, 900, 700),
            settings_window(0, 0, 3000, 3000, layer=25),
            settings_window(0, 0, 3000, 3000, alpha=0),
            settings_window(0, 0, 50, 3000),
            {"kCGWindowOwnerPID": SETTINGS_PID, "kCGWindowBounds": "junk"},
            {"kCGWindowOwnerPID": "x"},
        ]
        self.assertEqual(pp.settings_rect(windows, SETTINGS_PID), (20.0, 20.0, 900.0, 700.0))
        self.assertIsNone(pp.settings_rect([], SETTINGS_PID))
        self.assertIsNone(pp.settings_rect(None, SETTINGS_PID))

    def test_cg_to_appkit_flips_y_on_the_main_display(self):
        self.assertEqual(pp.to_appkit((100, 100, 700, 600), 900), (100, 200, 700, 600))

    def test_panel_goes_right_then_left_then_inside_and_stays_on_screen(self):
        screens = [(0.0, 0.0, 1440.0, 875.0)]
        self.assertEqual(pp.place_beside((100, 200, 700, 600), (300, 268), screens), (816, 532))
        self.assertEqual(pp.place_beside((800, 200, 700, 600), (300, 268), screens), (484, 532))
        self.assertEqual(pp.place_beside((100, 200, 1300, 600), (300, 268), screens), (1132, 532))
        # A short window near the bottom: the panel is kept above the margin.
        self.assertEqual(pp.place_beside((100, 0, 700, 100), (300, 268), screens), (816, 8))
        # The screen that shows most of System Settings decides.
        two = [(0.0, 0.0, 1440.0, 875.0), (1440.0, 0.0, 1920.0, 1055.0)]
        self.assertEqual(pp.place_beside((1500, 200, 700, 600), (300, 268), two), (2216, 532))
        self.assertEqual(pp.place_beside((100, 200, 700, 600), (300, 268), []), (816, 532))

    def test_contains(self):
        self.assertTrue(pp.contains((0, 0, 10, 10), (5, 5)))
        self.assertFalse(pp.contains((0, 0, 10, 10), (11, 5)))


class PanelWindow(unittest.TestCase):
    def test_a_nonactivating_floating_panel_for_an_accessory_app(self):
        panel, A, _Q, _clock, _launcher, _emitted = make_panel()
        self.assertEqual(A.app.called("setActivationPolicy_"), [(A.NSApplicationActivationPolicyAccessory,)])
        window = A.windows[0]
        self.assertTrue(window.style & A.NSWindowStyleMaskNonactivatingPanel)
        self.assertTrue(window.style & A.NSWindowStyleMaskClosable)
        self.assertEqual(window.state["Level"], A.NSFloatingWindowLevel)
        self.assertIs(window.state["HidesOnDeactivate"], False)
        self.assertIs(window.state["ReleasedWhenClosed"], False)
        self.assertTrue(window.state["CollectionBehavior"] & A.NSWindowCollectionBehaviorCanJoinAllSpaces)
        self.assertIs(window.state["Delegate"], panel.target)
        timer = A.timers[0]
        self.assertEqual((timer.state["selector"], timer.state["target"]), ("heartbeat:", panel.target))
        self.assertEqual(A.run_loop.called("addTimer_forMode_"), [(timer, A.NSRunLoopCommonModes)])
        self.assertEqual(A.workspace.called("openURL_"), [(("url", AX_URL),)])
        self.assertEqual(A.workspace.called("iconForFile_"), [(TERMINAL,)])
        icon = panel.views["icon"]
        self.assertEqual(icon.state["Image"], ("icon", TERMINAL))
        self.assertIn(("protocol", "NSDraggingSource"), type(icon).protocols)
        self.assertTrue(icon.acceptsFirstMouse_(None))
        self.assertEqual(panel.views["body"].state["StringValue"],
                         "Drag Terminal into the Accessibility list in System Settings. "
                         "If Terminal is already there, turn it on.")
        self.assertEqual(panel.views["caption"].state["StringValue"], "Terminal")
        self.assertIs(panel.views["note"].state["Hidden"], True)
        buttons = (panel.views["open_button"], panel.views["later_button"])
        self.assertEqual([(b.title, b.action) for b in buttons],
                         [("Open Settings", "openSettings:"), ("Not Now", "notNow:")])
        self.assertTrue(all(b.target is panel.target and b.acceptsFirstMouse_(None) for b in buttons))

    def test_opens_beside_system_settings_and_follows_it(self):
        panel, A, Q, clock, _launcher, _emitted = make_panel()
        window = A.windows[0]
        self.assertEqual(window.called("orderFrontRegardless"), [()])
        # Settings at CG (100, 100, 700x600) on a 900 pt tall display.
        self.assertEqual(window.origin, (816.0, 532.0))
        moves = len(window.called("setFrameOrigin_"))
        advance(panel, clock, 1.0)
        self.assertEqual(len(window.called("setFrameOrigin_")), moves)
        Q.windows = [settings_window(300, 150, 700, 600)]
        advance(panel, clock, 0.25)
        self.assertEqual(window.origin, (1016.0, 482.0))
        Q.windows = [settings_window(800, 150, 700, 600)]
        advance(panel, clock, 0.25)
        self.assertEqual(window.origin, (484.0, 482.0))
        self.assertEqual(window.called("orderFrontRegardless"), [()])

    def test_without_system_settings_it_waits_then_centers(self):
        A = fake_appkit()
        A.running = []
        panel, A, _Q, clock, _launcher, _emitted = make_panel(appkit=A)
        window = A.windows[0]
        self.assertEqual(window.called("orderFrontRegardless"), [])
        advance(panel, clock, 2.75)
        self.assertEqual(window.called("orderFrontRegardless"), [])
        advance(panel, clock, 0.25)
        self.assertEqual(window.called("orderFrontRegardless"), [()])
        self.assertEqual(window.origin, ((1440 - 300) / 2, (875 - 268) / 2))

    def test_drag_writes_the_bundle_url_and_the_icon_is_the_source(self):
        panel, A, _Q, _clock, _launcher, _emitted = make_panel()
        icon = panel.views["icon"]
        event = object()
        icon.mouseDown_(event)
        icon.mouseDragged_(event)
        icon.mouseDragged_(event)
        sessions = icon.called("beginDraggingSessionWithItems_event_source_")
        self.assertEqual(len(sessions), 1)
        items, got_event, source = sessions[0]
        self.assertIs(got_event, event)
        self.assertIs(source, icon)
        self.assertEqual(items[0].writer, ("file-url", TERMINAL, True))
        self.assertEqual(items[0].called("setDraggingFrame_contents_"),
                         [(((0.0, 0.0), (96.0, 96.0)), ("icon", TERMINAL))])
        icon.mouseDown_(event)
        icon.mouseDragged_(event)
        self.assertEqual(len(icon.called("beginDraggingSessionWithItems_event_source_")), 2)

    def test_the_drag_offers_nothing_away_from_system_settings(self):
        panel, _A, _Q, _clock, _launcher, _emitted = make_panel()
        icon = panel.views["icon"]
        every = COPY | LINK | GENERIC
        icon.mouseDown_(None)
        icon.mouseDragged_(None)
        mask = icon.draggingSession_sourceOperationMaskForDraggingContext_
        self.assertEqual(mask(None, OUTSIDE), every)
        # Settings is (100, 200, 700x600) in AppKit points; the panel is at (816, 532).
        icon.draggingSession_movedToPoint_(None, (1300.0, 100.0))
        self.assertEqual(mask(None, OUTSIDE), 0)
        self.assertEqual(mask(None, WITHIN), every)
        icon.draggingSession_movedToPoint_(None, (810.0, 700.0))
        self.assertEqual(mask(None, OUTSIDE), every)
        icon.draggingSession_movedToPoint_(None, (500.0, 500.0))
        self.assertEqual(mask(None, OUTSIDE), every)
        icon.draggingSession_movedToPoint_(None, (900.0, 600.0))
        self.assertEqual(mask(None, OUTSIDE), every)
        panel.settings = None
        icon.draggingSession_movedToPoint_(None, (1300.0, 100.0))
        self.assertEqual(mask(None, OUTSIDE), every)

    def test_a_drop_on_settings_asks_for_touch_id(self):
        panel, _A, _Q, _clock, _launcher, _emitted = make_panel()
        icon = panel.views["icon"]
        icon.mouseDown_(None)
        icon.mouseDragged_(None)
        icon.draggingSession_endedAtPoint_operation_(None, (1300.0, 100.0), COPY)
        self.assertEqual(panel.views["hint"].state["StringValue"],
                         "Drop Terminal onto the list in System Settings.")
        self.assertEqual(panel.flow.phase, "drag")
        icon.draggingSession_endedAtPoint_operation_(None, (500.0, 500.0), 0)
        self.assertEqual(panel.flow.phase, "drag")
        icon.draggingSession_endedAtPoint_operation_(None, (500.0, 500.0), GENERIC)
        self.assertEqual(panel.flow.phase, "confirm")
        self.assertEqual(panel.views["body"].state["StringValue"], "Confirm with Touch ID or your password.")
        self.assertIsNone(panel.drag_point)

    def test_probes_every_second_then_check_next_pane_all_set_and_quit(self):
        panel, A, _Q, clock, launcher, emitted = make_panel()
        argv, kw = launcher.calls[0]
        self.assertEqual(argv, ["python", "-m", "phone_harness.permission_probe"])
        self.assertEqual((kw["stdout"], kw["stdin"]), (subprocess.PIPE, subprocess.DEVNULL))
        advance(panel, clock, 3.0)
        starts = len(launcher.calls)
        self.assertEqual(starts, 4)
        launcher.state = {"accessibility": True, "screen_recording": False}
        advance(panel, clock, 1.25)
        self.assertEqual(panel.flow.phase, "granted")
        icon = panel.views["icon"]
        self.assertEqual(icon.state["Image"], ("sized", ("config", 64.0, 0.0)))
        self.assertEqual(icon.state["ContentTintColor"], "green")
        self.assertEqual(panel.views["title"].state["StringValue"], "Accessibility is on")
        advance(panel, clock, 1.25)
        self.assertEqual(A.workspace.called("openURL_")[-1], (("url", SR_URL),))
        self.assertEqual(panel.views["title"].state["StringValue"], "Screen & System Audio Recording")
        self.assertEqual(icon.state["Image"], ("icon", TERMINAL))
        self.assertIsNone(icon.state["ContentTintColor"])
        launcher.state = {"accessibility": True, "screen_recording": True}
        advance(panel, clock, 3.0)
        self.assertEqual(panel.views["title"].state["StringValue"], "All set")
        self.assertEqual(emitted, [])
        advance(panel, clock, 2.0)
        self.assertEqual(emitted, [{"outcome": "granted", "granted": ["accessibility", "screen_recording"]}])
        self.assertEqual(A.app.called("terminate_"), [(None,)])
        self.assertEqual(A.timers[0].called("invalidate"), [()])
        self.assertEqual(A.windows[0].called("orderOut_"), [(None,)])
        spawned = len(launcher.calls)
        advance(panel, clock, 5.0)
        self.assertEqual(len(launcher.calls), spawned)

    def test_a_probe_that_hangs_is_killed(self):
        panel, _A, _Q, clock, launcher, _emitted = make_panel()
        launcher.hang = True
        advance(panel, clock, 1.0)
        stuck = launcher.procs[-1]
        advance(panel, clock, pp.PROBE_LIMIT - 0.25)
        self.assertFalse(stuck.killed)
        advance(panel, clock, 0.25)
        self.assertTrue(stuck.killed)

    def test_not_now_and_the_close_button_dismiss(self):
        panel, A, _Q, _clock, _launcher, emitted = make_panel()
        panel.target.notNow_(None)
        self.assertEqual(emitted, [{"outcome": "dismissed", "granted": []}])
        self.assertEqual(A.app.called("terminate_"), [(None,)])
        panel.target.notNow_(None)
        self.assertEqual(len(emitted), 1)

        panel, A, _Q, _clock, _launcher, emitted = make_panel()
        panel.target.windowWillClose_(None)
        self.assertEqual(emitted, [{"outcome": "dismissed", "granted": []}])

    def test_open_settings_reopens_the_pane_and_the_timeout_ends_it(self):
        panel, A, _Q, clock, _launcher, emitted = make_panel(timeout=30)
        panel.target.openSettings_(None)
        self.assertEqual(A.workspace.called("openURL_"), [(("url", AX_URL),), (("url", AX_URL),)])
        advance(panel, clock, 30.0)
        self.assertEqual(emitted, [{"outcome": "timeout", "granted": []}])

    def test_an_error_in_a_callback_ends_the_panel_with_error(self):
        panel, A, _Q, _clock, _launcher, emitted = make_panel()

        def broken(url):
            raise RuntimeError("no workspace")
        A.workspace.openURL_ = broken
        panel.target.openSettings_(None)
        self.assertEqual(emitted, [{"outcome": "error", "granted": [],
                                    "detail": "RuntimeError: no workspace"}])
        self.assertEqual(A.app.called("terminate_"), [(None,)])

    def test_a_closed_pipe_does_not_keep_the_panel_alive(self):
        panel, A, _Q, _clock, _launcher, _emitted = make_panel()

        def gone(_result):
            raise BrokenPipeError("terminal closed")
        panel.emit = gone
        panel.target.notNow_(None)
        self.assertEqual(A.app.called("terminate_"), [(None,)])


class EntryPoint(unittest.TestCase):
    def test_a_bad_spec_is_an_error(self):
        emitted = []
        self.assertEqual(pp.run_spec("not json", emit=emitted.append), 2)
        self.assertEqual(emitted[0]["outcome"], "error")
        self.assertIn("bad spec", emitted[0]["detail"])
        self.assertEqual(pp.run_spec("[1]", emit=emitted.append), 2)

    def test_a_second_panel_reports_already_running_without_appkit(self):
        emitted = []
        spec = json.dumps({"steps": [AX_STEP], "lock": "/tmp/held"})
        load = mock.Mock(side_effect=AssertionError("AppKit loaded"))
        self.assertEqual(pp.run_spec(spec, load=load, lock=lambda path: None, emit=emitted.append), 0)
        self.assertEqual(emitted, [{"outcome": "already_running", "granted": []}])
        load.assert_not_called()

    def test_missing_appkit_or_a_failed_build_is_an_error(self):
        emitted = []

        def missing():
            raise ImportError("No module named 'AppKit'")
        self.assertEqual(pp.run_spec(json.dumps({"steps": [AX_STEP]}), load=missing,
                                     emit=emitted.append), 0)
        self.assertEqual(emitted[0]["outcome"], "error")
        self.assertIn("AppKit is not available", emitted[0]["detail"])

        emitted.clear()
        broken = fake_appkit()
        del broken.NSPanel
        self.assertEqual(pp.run_spec(json.dumps({"steps": [AX_STEP]}),
                                     load=lambda: (broken, fake_quartz([]), FAKE_OBJC),
                                     emit=emitted.append), 0)
        self.assertEqual(emitted[0]["outcome"], "error")
        self.assertIn("NSPanel", emitted[0]["detail"])

    def test_a_run_holds_the_lock_and_runs_the_app(self):
        A = fake_appkit()
        released = []
        held = types.SimpleNamespace(close=lambda: released.append(True))
        spec = json.dumps({"host": {"name": "Terminal", "path": TERMINAL},
                           "steps": [AX_STEP], "lock": "/tmp/lock"})
        code = pp.run_spec(spec, load=lambda: (A, fake_quartz([]), FAKE_OBJC),
                           lock=lambda path: held, emit=lambda result: None,
                           popen=Launcher(), clock=Clock())
        self.assertEqual(code, 0)
        self.assertEqual(A.app.called("run"), [()])
        self.assertEqual(released, [True])

    def test_take_lock_is_exclusive_until_closed(self):
        if sys.platform == "win32":
            self.skipTest("no fcntl")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "run", "permission-guide.lock")
            first = pp.take_lock(path)
            self.assertIsNotNone(first)
            self.assertIsNone(pp.take_lock(path))
            first.close()
            again = pp.take_lock(path)
            self.assertIsNotNone(again)
            again.close()


class Probe(unittest.TestCase):
    def test_native_symbols_answer_without_pyobjc(self):
        class Fn:
            def __init__(self, value):
                self.value = value

            def __call__(self):
                return self.value

        def loader(path):
            if path.endswith("/ApplicationServices"):
                raise OSError("not here")
            if path.endswith("/HIServices"):
                return types.SimpleNamespace(AXIsProcessTrusted=Fn(1))
            return types.SimpleNamespace(CGPreflightScreenCaptureAccess=Fn(0))

        with mock.patch.object(probe, "_pyobjc", side_effect=AssertionError("pyobjc")):
            self.assertEqual(probe.preflight(loader), {"accessibility": True, "screen_recording": False})

    def test_pyobjc_answers_when_the_symbols_cannot_load(self):
        def loader(path):
            raise OSError("gone")
        with mock.patch.object(probe, "_pyobjc", side_effect=lambda kind: kind == "accessibility"):
            self.assertEqual(probe.preflight(loader), {"accessibility": True, "screen_recording": False})

    def test_parse_reads_the_last_json_line_and_keeps_only_booleans(self):
        text = 'warning: noise\n{"accessibility": false}\n{"accessibility": true, "screen_recording": null}\n'
        self.assertEqual(probe.parse(text), {"accessibility": True, "screen_recording": None})
        self.assertEqual(probe.parse('{"accessibility": "yes", "screen_recording": 1}'),
                         {"accessibility": None, "screen_recording": None})
        self.assertIsNone(probe.parse("no json here\n{broken\n"))
        self.assertIsNone(probe.parse(None))

    def test_main_prints_one_json_line(self):
        out = io.StringIO()
        with mock.patch.object(probe, "preflight",
                               return_value={"accessibility": True, "screen_recording": None}), \
                mock.patch("sys.stdout", out):
            self.assertEqual(probe.main(), 0)
        self.assertEqual(out.getvalue().count("\n"), 1)
        self.assertEqual(json.loads(out.getvalue()), {"accessibility": True, "screen_recording": None})

    def test_the_module_runs_as_its_own_process(self):
        if sys.platform == "darwin":
            self.skipTest("reads the real grants on a Mac")
        env = dict(os.environ, PYTHONPATH=SRC + os.pathsep + os.environ.get("PYTHONPATH", ""))
        got = subprocess.run([sys.executable, "-m", "phone_harness.permission_probe"],
                             capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(json.loads(got.stdout), {"accessibility": None, "screen_recording": None})


if __name__ == "__main__":
    unittest.main()
