"""iOS backend: the op vocabulary over iPhone Mirroring.

An adapter, not a rewrite. mirror.py and background.py keep their shape and are
imported as they are — they hold the details that were expensive to learn (why
NSWorkspace's frontmost value is a constant, why a slow drag barely registers
on iOS, why the live phone image is opaque to accessibility) and none of that
should move just to change the seam above it.

What iPhone Mirroring cannot do appears here as absence rather than emulation:

  no `nav.back`      iOS has no system Back button. Guessing at an edge swipe
                     returns something the caller cannot tell from a real Back.
  no `apps.current`  the window exposes no app inventory; the foreground app is
  no `apps.list`     only knowable by reading the screen.
  no `tree`          the phone image is a video stream, which is the whole
                     reason screen.text has to recognise glyphs.
  no `raw`           there is no single command channel the way adb is one. The
                     substrate is Quartz: `import Quartz` in your own script,
                     or reach through this backend's .mirror module.

Each raises Unsupported, so a caller can ask rather than discover it by
getting a plausible wrong answer.
"""
import importlib
import os

from . import ocr as _vision
from .transport import Backend


def _load_transport():
    """background drives iPhone Mirroring without ever taking the window to the
    front (SkyLight event records) and is the default: not covering the user's
    screen is what you want unless something is broken. Its private SkyLight
    symbols are not guaranteed across macOS builds, so fall back rather than
    leaving the harness unusable."""
    want_bg = os.environ.get("PHONE_HARNESS_BACKGROUND", "1").lower() not in (
        "0", "false", "no")
    if want_bg:
        try:
            return importlib.import_module(".background", __package__), True
        except Exception:
            pass
    return importlib.import_module(".mirror", __package__), False


class IPhone(Backend):
    name = "iphone-mirroring"

    def __init__(self):
        self.mirror, self.background = _load_transport()
        self._session_watch = None
        self._session_watch_off = False
        self._session_lost = None
        self._note_seen = False

    def send(self, op, **kw):
        """Run `op`. Actions check the stream first and again after, and
        raise if it is no longer live. They do not press Connect: a takeover
        means the user has the phone. A screenshot is not an action: looking
        at the window stays available while an interstitial is up."""
        watch = op in _ACTION_OPS
        if watch:
            self._raise_if_not_live()
        result = super().send(op, **kw)
        if watch:
            self._raise_if_not_live()
        return result

    # --- screen ---------------------------------------------------------

    def _screen_bounds(self):
        return self.mirror.find_window()

    def _screen_require(self):
        """Bounds for gesture maths. Distinct from session.require, which is
        the full connected gate. The liveness check lives on the action
        (see send), not on this bounds read."""
        return self.mirror.ensure_window()

    def _screen_capture(self, path=None):
        return self.mirror.capture(path)

    def _screen_text(self, min_confidence=0.3):
        path, win = self.mirror.capture()
        return [dict(o, source="pixels")
                for o in _vision.recognize(path, win)
                if o["confidence"] >= min_confidence]

    # Pixels are the only source iOS has ever had.
    _screen_text_pixels = _screen_text

    # --- input ----------------------------------------------------------

    def _input_tap(self, x, y):
        self.mirror.tap(x, y)

    def _input_press(self, x, y, duration=0.8):
        self.mirror.long_press(x, y, duration)

    def _input_drag(self, x1, y1, x2, y2, duration=0.35, steps=14):
        self.mirror.drag(x1, y1, x2, y2, duration=duration, steps=steps)

    def _input_scroll(self, x, y, dy, dx=0, steps=6):
        self.mirror.scroll_wheel(dy, x, y, steps=steps, dx=dx)

    def _input_keys(self, combo):
        self.mirror.press(combo)

    def _input_text(self, s, delay=0.03, keystrokes=False):
        self.mirror.type_text(s, delay=delay, keystrokes=keystrokes)

    # --- navigation -----------------------------------------------------

    def _nav_home(self):
        self.mirror.press("cmd+1")
        _sleep(0.8)

    def _nav_recents(self):
        self.mirror.press("cmd+2")
        _sleep(0.8)

    # No _nav_back: iOS has no system Back button.

    def _apps_launch(self, name):
        """Replace Spotlight's query literally, then request the top result.

        The returned name is the request, not proof that an app opened.
        """
        self.mirror.press("cmd+3")
        _sleep(0.9)
        self.mirror.press("cmd+a")
        # Key events run through the active input source. A Korean IME can
        # turn "Settings" into Hangul, sending Return to the wrong result.
        # Use the same literal paste path as type_text(), replacing any old
        # search that Spotlight restored.
        self.mirror.type_text(name)
        _sleep(1.2)
        self.mirror.press("return")
        return name

    # --- session --------------------------------------------------------

    def _session_state(self):
        """'ready' | 'blocked' | 'no-window' | 'not-running'.

        'blocked' means an interstitial is up — iPhone in Use, paused, ended,
        connect, or the Mac login screen — and nothing should be tapped or
        typed until the user clears it. screenshot() still works.

        Accessibility decides, and nothing else may override a structural
        'ready'. The live phone image is a video stream that accessibility
        cannot see into, so a working session exposes no UI inside the
        window, while every interstitial is an ordinary Mac view with labels
        and a button. That holds in any language and for screens Apple has
        not shipped yet. OCR is not used: it reads the phone's own screen
        and cannot tell that text from a Mac interstitial.
        """
        return self._structural_state()

    def _session_detail(self):
        """Labels and button titles accessibility can read in the window.

        Empty when the phone image is live. An empty button title is
        skipped: the same screen has reported 'Connect' and then nothing.
        """
        texts, buttons = [], []
        for role, text in self.mirror.window_ax_content():
            label = " ".join((text or "").split())
            if not label:
                continue
            if role == "AXButton":
                buttons.append(label)
            elif role == "AXStaticText":
                texts.append(label)
        parts = []
        if texts:
            parts.append(" ".join(texts))
        if buttons:
            quoted = ", ".join(f"'{b}'" for b in buttons)
            parts.append(f"buttons: {quoted}")
        return " ".join(parts)

    def _session_require(self):
        """Bounds once the phone is connected and ready.

        First does what the user would do by hand: opens iPhone Mirroring
        when it is not running, and presses the interstitial's own button
        (Connect, Try Again, Continue) once. What is left after that is
        physical — an unlocked phone ("iPhone in Use", "Timed Out"), the Mac
        login prompt, a phone that is not paired — and only the user can
        clear it, so this raises quoting the window rather than pressing
        again. A Connecting screen is not that case, and neither is the blank
        window launch draws before any label: no text and no buttons. Both
        are waited out. A cold launch connects by itself.
        Once the phone is live the window is brought to the front so
        the user can watch the task. One press per call: a caller that retries after the user says
        the phone is locked gets a fresh press.
        """
        state = self._session_state()
        if state == "not-running":
            self.mirror.launch()
            state = self._wait_state(("blocked", "ready"), _LAUNCH_WAIT)
        # Shown before anything else so the user sees what the harness sees,
        # the interstitial included, not only a session that worked.
        self._show()
        if state == "no-window":
            state = self._wait_state(("blocked", "ready"), _WINDOW_WAIT)
        pressed = None
        if state == "blocked":
            pressed = self._press_connect()
            # Wait when the press is in flight: we pressed something, the
            # window says "Connecting to", or it is still the blank window
            # launch draws before any label. A finished interstitial (text
            # or a button, and not Connecting) raises below instead.
            if pressed or not self._finished_interstitial():
                state = self._wait_connected(_CONNECT_WAIT)
        if state == "ready":
            return self.mirror.find_window()
        self._raise_not_live(state, pressed)

    def _raise_if_not_live(self):
        """Raise the session-require error if the stream is no longer live.

        Does not press Connect and does not launch the app. A long-lived
        accessibility observer is drained with a zero timeout so a window
        change already queued is applied first; a missed notification still
        loses to one structural read (no capture, no OCR)."""
        state = self._live_probe()
        if state != "ready":
            self._raise_not_live(state)

    def _live_probe(self):
        """Current liveness, cheaply.

        Two structural reads bracket each guarded action (before and after).
        Each read is one ax_session_hint(): the running app, the phone
        window, and whether that window has any accessibility children. A
        live stream has none. There is no screen capture and no Vision OCR.
        session.state is this same read.

        When the observer is up, a zero-timeout run-loop turn delivers a
        notification that arrived since the last action, and that
        notification's own read is the answer. The turn does not sleep.
        Measured here, where there is no accessibility server: the guard
        around one live action is under 0.01 ms of Python. On a Mac the
        rest is ax_session_hint, twice per action (before and after): one
        lookup of the app, its window, and that window's children. A live
        stream has no children, so the walk stops at the window.
        """
        self._arm_session_watch()
        self._note_seen = False
        if self._session_watch is not None:
            _drain_run_loop()
            if self._note_seen:
                return self._session_lost
        return self._structural_state()

    def _arm_session_watch(self):
        """Install the observer once. A transport with no target (the fakes)
        fails closed and every later probe uses the structural read."""
        if not hasattr(self, "_session_watch"):
            self._session_watch = None
            self._session_watch_off = False
            self._session_lost = None
            self._note_seen = False
        if self._session_watch is not None or self._session_watch_off:
            return

        def on_notify():
            state = self._structural_state()
            self._session_lost = state
            self._note_seen = True

        try:
            self._session_watch = _ax_watch_open(self.mirror, on_notify)
        except _ObserverUnavailable:
            self._session_watch = None
            self._session_watch_off = True

    def _structural_state(self):
        """ready | blocked | no-window | not-running, without capture or OCR.

        Accessibility content in the window is 'blocked'. None is 'ready'.
        Text recognised on the phone's own screen must not change that.
        """
        hint = getattr(self.mirror, "ax_session_hint", None)
        if hint is not None:
            return hint()
        if self.mirror.running_app() is None:
            return "not-running"
        if self.mirror.find_window() is None:
            return "no-window"
        if self.mirror.window_ax_content():
            return "blocked"
        return "ready"

    def _raise_not_live(self, state, pressed=None):
        """The error ensure_device raises, without pressing anything."""
        if state == "not-running":
            raise RuntimeError(
                "iPhone Mirroring would not launch. Please open the iPhone "
                "Mirroring app and connect your phone, then retry.")
        if state == "no-window":
            raise RuntimeError(
                "iPhone Mirroring is open but shows no phone window. Pair "
                "the phone in the app (it has to be nearby, locked, and "
                "signed in to the same Apple Account), then retry.")
        said = (self._session_detail() or "an interstitial with no text").strip()
        did = (f"I pressed {pressed} and it came back with"
               if pressed else "It says")
        # The quote often already ends with a period ("...to connect.").
        # Adding another one produced "connect.. This needs you".
        stop = "" if said.endswith((".", "!", "?")) else "."
        raise RuntimeError(
            f"iPhone Mirroring is not connected. {did}: {said}{stop} "
            f"{_blocked_guidance(said)} "
            "Call screenshot() to see what the window shows.")

    def _show(self):
        """Bring the mirroring window to the front so the user can watch.
        Best effort: a window that will not come forward is not a reason to
        fail the session, since the background build drives it regardless."""
        try:
            self.mirror.show()
        except Exception:
            pass

    def _press_connect(self):
        """Press the interstitial's button through accessibility, once.

        Returns the title pressed, or None when there is nothing safe to
        press: no button, several buttons (the choice is the user's), or a
        login prompt — pressing Unlock without a password does nothing, and
        the password is never ours to type.
        """
        content = self.mirror.window_ax_content()
        if any(role in ("AXTextField", "AXSecureTextField")
               for role, _ in content):
            return None
        buttons = [t for role, t in content if role == "AXButton"]
        if len(buttons) == 1 and buttons[0].lower() not in _NOT_CONNECT_BUTTONS:
            return self.mirror.press_window_button()
        return self.mirror.press_window_button(_CONNECT_BUTTONS)

    def _wait_state(self, wanted, timeout):
        """Poll until the session is in one of `wanted` or time runs out;
        returns the last state seen."""
        deadline = _now() + timeout
        state = self._session_state()
        while state not in wanted and _now() < deadline:
            _sleep(0.5)
            state = self._session_state()
        return state

    def _wait_connected(self, timeout):
        """Wait for the live stream, and fail fast otherwise.

        Used after a Connect press, and when the window is already the
        Connecting screen so there was nothing to press.

        The attempt is watched through an accessibility observer on iPhone
        Mirroring, so the Connecting screen is noticed when it changes rather
        than on a timer. A hang emits nothing, so the wait still gives up at
        `timeout`. If the observer cannot be installed, the same decision is
        made by polling.

        'ready' has to hold for three evaluations in a row. Measured: the
        press empties the window for about half a second before "Connecting
        to" draws, and one empty read in that gap looks live.

        The Connecting screen is in progress when its text contains
        "Connecting to" or it offers a Background button. So is a blocked
        window with no text and no buttons: measured on a cold launch, that
        blank is what is drawn before "Connecting to" appears, and treating
        it as a finished interstitial raised "an interstitial with no text"
        while the stream was still coming up. A blocked interstitial with
        recognizable text or buttons that is not Connecting — "iPhone in
        Use", locked, timed out — is the answer once the attempt has had a
        couple of seconds to draw, so it fails then instead of sitting out
        the cap. The user locking the phone is what fixes it, and the app
        reconnects by itself, so the next session.require finds it live."""
        try:
            return self._wait_connected_events(timeout)
        except _ObserverUnavailable:
            return self._wait_connected_polling(timeout)

    def _wait_connected_events(self, timeout):
        """Event-driven wait. One evaluation on entry, then again on every
        accessibility notification. If a notification is missed, the same
        check runs about once a second so the attempt cannot stall."""
        start = _now()
        streak = 0
        state = "blocked"
        decided = None
        last_event = start

        def evaluate():
            nonlocal streak, state, decided
            state = self._session_state()
            decided, streak = self._connection_choice(
                state, streak, _now() - start)

        def on_notify():
            nonlocal last_event
            last_event = _now()
            evaluate()
            if decided is not None:
                _stop_run_loop()

        watch = _ax_watch_open(self.mirror, on_notify)
        try:
            evaluate()
            if decided is not None:
                return decided
            while _now() - start < timeout:
                remaining = timeout - (_now() - start)
                before = _now()
                _run_loop_slice(min(_EVENT_SLICE, remaining))
                if decided is not None:
                    return decided
                # A hang, or a notification the observer did not deliver.
                if _now() - last_event >= _EVENT_QUIET:
                    evaluate()
                    last_event = _now()
                    if decided is not None:
                        return decided
                if _now() <= before:
                    _sleep(min(_EVENT_SLICE, timeout - (_now() - start)))
            return "blocked" if state == "ready" and streak < 3 else state
        finally:
            watch.close()

    def _wait_connected_polling(self, timeout):
        """The pre-observer wait, used when AXObserver cannot be installed.

        Same decisions as the event wait, including treating a "Connecting
        to" screen with no buttons as still in progress."""
        start = _now()
        _sleep(1.0)
        streak = 0
        state = self._session_state()
        while _now() - start < timeout:
            decided, streak = self._connection_choice(
                state, streak, _now() - start)
            if decided is not None:
                return decided
            _sleep(0.5)
            state = self._session_state()
        return "blocked" if state == "ready" and streak < 3 else state

    def _connection_choice(self, state, streak, elapsed):
        """('ready' | 'blocked' | None, streak) for one look at the window.

        None means the attempt is still in progress. 'ready' only after the
        live stream has held for three looks. 'blocked' is a finished
        interstitial (recognizable text or buttons, and not Connecting)
        once the attempt has had time to draw a label. A blank window is
        not finished."""
        streak = streak + 1 if state == "ready" else 0
        if streak >= 3:
            return "ready", streak
        if (state == "blocked" and elapsed > _ATTEMPT_SETTLE
                and self._finished_interstitial()):
            return "blocked", streak
        return None, streak

    def _finished_interstitial(self):
        """A blocked window that has something to read, and is not Connecting.

        No text and no buttons is transitional: the phone window is up but
        the Connecting label has not been drawn. That holds before a press
        and after one. "Connecting to" or a Background button is the same
        wait, not a failure."""
        if self._still_connecting():
            return False
        return self._recognizable_ui()

    def _recognizable_ui(self):
        """True when the window shows text or a button title."""
        content = self.mirror.window_ax_content()
        if any(t.strip() for _role, t in content if t):
            return True
        return any(t.strip() for t, _el in self.mirror.window_ax_buttons() if t)

    def _still_connecting(self):
        """The Connecting screen, including one that has no buttons.

        The words are the signal. A Background button is the other form of
        the same screen; either one means the press has not finished."""
        content = self.mirror.window_ax_content()
        text = " ".join(t for _role, t in content if t).lower()
        if "connecting to" in text:
            return True
        return any(t.lower() == "background"
                   for t, _el in self.mirror.window_ax_buttons())

    def _session_refocus(self):
        self.mirror.activate()

    # --- interruption ---------------------------------------------------

    def _focus_probe(self):
        return self.mirror.focus_probe()

    def _focus_diff(self, before, after):
        return self.mirror.interruption(before, after)


def _blocked_guidance(said):
    """What to tell the user, from the words already quoted.

    Lock the iPhone only when the window says it is in use, timed out, or
    to lock the iPhone. A Mac Touch ID or password sheet is the user's to
    approve or cancel. Anything else is cleared on the Mac.
    """
    low = said.lower()
    if any(p in low for p in ("in use", "timed out", "lock your iphone")):
        return ("This needs you: LOCK your iPhone, then tell me and I will "
                "try once more. I never type a passcode.")
    if "touch id" in low or _has_word(low, "password"):
        return ("This needs you: the Mac is asking for Touch ID or a password. "
                "Approve it or cancel it. I never type a password.")
    return "This needs you: clear this on the Mac."


def _has_word(text, word):
    """True when `word` is its own word. 'password' does not match 'Passwords'."""
    padded = text.lower()
    for ch in ".,:;!?\"'":
        padded = padded.replace(ch, " ")
    return f" {word} " in f" {padded} "


# Buttons an interstitial offers that mean "try to connect"; pressed once
# per session.require. Lower-case; matched case-insensitively.
_CONNECT_BUTTONS = ("connect", "try again", "retry", "continue", "reconnect")
# A lone button that is not an attempt to connect: "Background" on the
# Connecting screen sends the app behind, "Cancel" gives up.
_NOT_CONNECT_BUTTONS = ("background", "cancel", "quit", "unlock", "ok",
                        "done", "settings")
_LAUNCH_WAIT = 12.0     # app launch to phone window
_WINDOW_WAIT = 6.0      # app running, window still coming up
_CONNECT_WAIT = 12.0    # press to live stream, the cap; a hang emits no events
_ATTEMPT_SETTLE = 2.5   # a press has drawn Connecting and then its outcome by now
_EVENT_SLICE = 0.2      # how long one CFRunLoopRunInMode turn waits
_EVENT_QUIET = 1.0      # re-check when no notification arrives for this long

# Posted on the app element and the phone window. A Connecting screen that
# never exposes a button still changes one of these as its label appears and
# as the live stream replaces it.
_AX_NOTIFICATIONS = (
    "kAXValueChangedNotification",
    "kAXTitleChangedNotification",
    "kAXUIElementDestroyedNotification",
    "kAXCreatedNotification",
    "kAXWindowCreatedNotification",
    "kAXLayoutChangedNotification",
    "kAXFocusedUIElementChangedNotification",
)
_AX_NOTIFICATION_NAMES = (
    "AXValueChanged",
    "AXTitleChanged",
    "AXUIElementDestroyed",
    "AXCreated",
    "AXWindowCreated",
    "AXLayoutChanged",
    "AXFocusedUIElementChanged",
)


class _ObserverUnavailable(Exception):
    """AXObserver could not be installed. The polling wait is the fallback."""


class _AxWatch:
    """An observer and the run-loop source it was scheduled on.

    close() removes both. It is safe to call twice, and a failure while
    removing one does not skip the other."""

    def __init__(self, remove_note, remove_source, notes, callback):
        self._remove_note = remove_note
        self._remove_source = remove_source
        self._notes = list(notes)
        self._callback = callback          # the observer holds this too
        self.closed = False

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self._remove_source()
        except Exception:
            pass
        for el, name in self._notes:
            try:
                self._remove_note(el, name)
            except Exception:
                pass


def _ax_watch_open(mirror, on_notify):
    """Observe iPhone Mirroring and call `on_notify` for each notification.

    `on_notify` takes no arguments. The caller runs the observer's run-loop
    source in short slices via `_run_loop_slice` and must `close()` the
    returned watch.

    Raises _ObserverUnavailable, without probing the app, when this transport
    has no `ax_observer_target`. A test double therefore falls straight
    through to polling and does not consume a scripted frame.
    """
    target = getattr(mirror, "ax_observer_target", None)
    if target is None:
        raise _ObserverUnavailable(
            "this transport has no accessibility observer")
    try:
        opened = target()
    except Exception as exc:
        raise _ObserverUnavailable(
            "could not read the mirroring app for an observer") from exc
    if not opened or len(opened) != 2:
        raise _ObserverUnavailable("the mirroring app is not observable")
    pid, elements = opened
    elements = [el for el in (elements or []) if el is not None]
    if pid is None or not elements:
        raise _ObserverUnavailable("the mirroring app is not observable")

    try:
        import ApplicationServices as ax
        import CoreFoundation as cf
    except Exception as exc:
        raise _ObserverUnavailable(
            "accessibility observer support is unavailable") from exc

    holder = {"fn": on_notify}

    def callback(observer, element, notification, refcon):
        # One bad notification must not kill the run loop. The caller's
        # quiet re-check evaluates again outside the callback.
        try:
            holder["fn"]()
        except Exception:
            pass

    try:
        try:
            created = ax.AXObserverCreate(int(pid), callback, None)
        except TypeError:
            created = ax.AXObserverCreate(int(pid), callback)
    except Exception as exc:
        raise _ObserverUnavailable("AXObserverCreate failed") from exc
    if isinstance(created, tuple):
        err = created[0]
        observer = created[1] if len(created) > 1 else None
    else:
        err, observer = 0, created
    if err or observer is None:
        raise _ObserverUnavailable("AXObserverCreate failed")

    try:
        source = ax.AXObserverGetRunLoopSource(observer)
    except Exception as exc:
        raise _ObserverUnavailable(
            "the observer has no run-loop source") from exc
    if source is None:
        raise _ObserverUnavailable("the observer has no run-loop source")
    try:
        loop = cf.CFRunLoopGetCurrent()
        mode = cf.kCFRunLoopDefaultMode
        cf.CFRunLoopAddSource(loop, source, mode)
    except Exception as exc:
        raise _ObserverUnavailable(
            "could not schedule the observer") from exc

    names = [getattr(ax, const, None) or fallback
             for const, fallback in zip(_AX_NOTIFICATIONS,
                                        _AX_NOTIFICATION_NAMES)]
    added = []

    def remove_source():
        cf.CFRunLoopRemoveSource(loop, source, mode)

    try:
        for el in elements:
            for name in names:
                try:
                    note_err = ax.AXObserverAddNotification(
                        observer, el, name, None)
                except Exception:
                    continue
                if not note_err:
                    added.append((el, name))
        if not added:
            raise _ObserverUnavailable(
                "no accessibility notifications were registered")
    except _ObserverUnavailable:
        try:
            remove_source()
        except Exception:
            pass
        raise

    def remove_note(el, name):
        ax.AXObserverRemoveNotification(observer, el, name)

    return _AxWatch(remove_note, remove_source, added, callback)


# Ops that drive the phone or read it as phone content. Each one refuses
# to run against a paused window. session.require is not here: that is the
# one call allowed to press Connect, and only when the user asks to resume.
# screen.capture is not here either. Looking at the window is read-only and
# works while the session is blocked, as long as the window exists.
_ACTION_OPS = frozenset({
    "screen.text", "screen.text_pixels",
    "input.tap", "input.press", "input.drag", "input.scroll",
    "input.keys", "input.text",
    "nav.home", "nav.recents",
    "apps.launch",
})


def _drain_run_loop():
    """Deliver accessibility notifications already queued. Does not wait.

    Timeout 0 and returnAfterSourceHandled true: one ready source runs, and
    if none is ready this returns immediately. A hang cannot add a sleep to
    every tap.
    """
    import CoreFoundation as cf
    cf.CFRunLoopRunInMode(cf.kCFRunLoopDefaultMode, 0, True)


def _run_loop_slice(seconds):
    """Run the current thread's run loop long enough for one burst of
    accessibility notifications. returnAfterSourceHandled is false so a
    single event does not end the slice early; the callback stops the loop
    itself once the attempt is decided."""
    import CoreFoundation as cf
    cf.CFRunLoopRunInMode(cf.kCFRunLoopDefaultMode, float(seconds), False)


def _stop_run_loop():
    try:
        import CoreFoundation as cf
        cf.CFRunLoopStop(cf.CFRunLoopGetCurrent())
    except Exception:
        pass


def _now():
    import time
    return time.time()


def _sleep(s):
    import time
    time.sleep(s)
