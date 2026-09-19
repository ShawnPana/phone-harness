"""CoreDevice backend: an iPhone over USB from any OS, no mirroring window.

The transport is the same set of developer services Xcode uses — screenshot,
HID touch and keyboard, pasteboard, app launch — reached through
pymobiledevice3 over a USB tunnel. The expensive state (tunnel, media stream,
services) lives in coredevice_daemon.py, started by `phone-harness ios awake`;
this module is the thin client that turns ops into one-line requests to it.

Compared with the macOS iPhone Mirroring backend:

  screen.capture   a real PNG from the phone (~0.3s), not a window grab.
                   Coordinates are screenshot pixels with origin (0, 0); the
                   bounds are the image size, so tap(x, y) takes the pixel you
                   saw in the screenshot. Retina scaling does not exist here.
  screen.text      the same OCR contract; Vision on a Mac, RapidOCR elsewhere.
  focus.*          nothing on the computer is touched, ever.
  apps.launch      by bundle id or name, through the app service — no Spotlight.
  apps.list        exists (iPhone Mirroring cannot see the app inventory).
  session.state    'not-running' until the daemon is up; 'locked' when the
                   lock screen is recognised on the capture; else 'ready'.
                   The phone exposes no lock-state API on the tested build,
                   so this is read off the screen, like the Mac backend does
                   for its interstitials.
  nav.recents      double-click Home on a Home-button phone, the swipe-and-hold
                   gesture on the rest (told apart by the screen aspect ratio).
  no `tree`, no `raw`, no `apps.current`: the phone does not offer them.

Never pairs, never mounts, never types a passcode: the daemon mounts the
developer image (a reversible, per-boot step) and everything else is a
`phone-harness ios ...` command the user runs on purpose.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import config
from .transport import Backend, Unsupported

TMP = Path(tempfile.gettempdir()) / "phone-harness"

# Lock-screen phrases, lower-cased. Fallback only — a structural signal does
# not exist here. Add the phrase, not a guess, when a new one is observed.
_LOCKED_MARKERS = ("press home to open", "swipe up to open", "enter passcode",
                   "touch id or enter passcode", "face id or enter passcode",
                   "unlock iphone", "iphone unavailable")

AWAKE_HINT = ("no CoreDevice session is running. Plug the iPhone in and run "
              "`phone-harness ios awake` (it opens the USB tunnel that every "
              "action needs), then retry.")


# --- talking to the daemon ---------------------------------------------------

def _state():
    """The daemon's state file if its process is alive, else None."""
    from .coredevice_daemon import read_state
    st = read_state()
    if not st or not isinstance(st.get("pid"), int):
        return None
    try:
        if sys.platform == "win32":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, st["pid"])
            if not h:
                return None
            ctypes.windll.kernel32.CloseHandle(h)
        else:
            os.kill(st["pid"], 0)
    except OSError:
        return None
    return st


def _connect(st, timeout):
    ep = st.get("endpoint") or {}
    if ep.get("kind") == "tcp":
        s = socket.create_connection(("127.0.0.1", int(ep["port"])), timeout=timeout)
    else:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(ep.get("path") or str(config.run_dir() / "coredevice.sock"))
    return s


def request(op, timeout=30.0, **kw):
    """One round trip to the daemon. Raises RuntimeError with the user-facing
    message when there is no session, Unsupported when the daemon says so."""
    st = _state()
    if st is None or not st.get("ready"):
        if st and st.get("phase") == "failed":
            raise RuntimeError(f"the last `phone-harness ios awake` failed: {st.get('error')}")
        raise RuntimeError(AWAKE_HINT)
    try:
        s = _connect(st, timeout)
    except OSError as e:
        raise RuntimeError(f"{AWAKE_HINT} (socket: {e})") from None
    try:
        s.sendall((json.dumps({"op": op, **kw}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    if not buf:
        raise RuntimeError("the CoreDevice session closed without answering; "
                           "run `phone-harness ios awake` again")
    reply = json.loads(buf)
    if reply.get("ok"):
        return reply.get("result")
    if reply.get("kind") == "Unsupported":
        raise Unsupported(reply.get("error"))
    raise RuntimeError(reply.get("error") or "CoreDevice request failed")


# --- the backend -------------------------------------------------------------

class CoreDevice(Backend):
    name = "coredevice"

    def __init__(self, serial=None):
        self.serial = serial
        self._gate_at = 0.0
        self._gate_state = None

    # --- screen ---------------------------------------------------------

    def _bounds_from(self, st):
        return {"x": 0, "y": 0, "w": int(st.get("w") or 0), "h": int(st.get("h") or 0),
                "id": st.get("udid")}

    def _screen_bounds(self):
        st = _state()
        return self._bounds_from(st) if st and st.get("ready") else None

    def _screen_require(self):
        b = self._screen_bounds()
        if b is None:
            raise RuntimeError(AWAKE_HINT)
        return b

    def _screen_capture(self, path=None):
        TMP.mkdir(exist_ok=True)
        path = str(path or TMP / "coredevice.png")
        r = request("capture", path=path)
        return r["path"], {"x": 0, "y": 0, "w": r["w"], "h": r["h"],
                           "id": (_state() or {}).get("udid")}

    def _screen_text(self, min_confidence=0.3):
        from . import ocr
        path, win = self._screen_capture()
        return [dict(o, source="pixels") for o in ocr.recognize(path, win)
                if o["confidence"] >= min_confidence]

    _screen_text_pixels = _screen_text

    # --- input ----------------------------------------------------------

    def _gate(self):
        """Refuse to drive a locked phone. Read off the screen, cached 2s.
        The phone auto-locks on its own idle timeout regardless of the open
        stream; unlocking is the user's, so this only reports."""
        if time.time() - self._gate_at < 2.0:
            state = self._gate_state
        else:
            state = self._session_state()
            self._gate_at, self._gate_state = time.time(), state
        if state == "locked":
            raise RuntimeError(
                "The iPhone is locked. Unlock it on the phone, then retry — I won't "
                "enter a passcode.")
        if state != "ready":
            raise RuntimeError(AWAKE_HINT)

    def _input_tap(self, x, y):
        self._gate()
        request("tap", x=x, y=y)

    def _input_press(self, x, y, duration=0.8):
        self._gate()
        request("press", x=x, y=y, duration=duration)

    def _input_drag(self, x1, y1, x2, y2, duration=0.35, steps=14):
        self._gate()
        request("drag", x1=x1, y1=y1, x2=x2, y2=y2, duration=duration, steps=steps)

    def _input_scroll(self, x, y, dy, dx=0, steps=6):
        self._gate()
        request("scroll", x=x, y=y, dy=dy, dx=dx, steps=steps)

    def _input_keys(self, combo):
        self._gate()
        request("keys", combo=combo)

    def _input_text(self, s, delay=0.03, keystrokes=False):
        self._gate()
        request("text", s=s, delay=delay, keystrokes=keystrokes,
                timeout=max(30.0, 0.2 * len(s) + 10))

    # --- navigation -----------------------------------------------------

    def _nav_home(self):
        request("home")
        time.sleep(0.6)

    def _nav_recents(self):
        self._gate()
        request("recents")
        time.sleep(0.6)

    # No _nav_back: iOS has no system Back button.

    def _apps_launch(self, name):
        self._gate()
        bid = request("launch", name=name, timeout=60)
        time.sleep(0.8)
        return bid

    def _apps_list(self, include_system=False):
        return request("apps", include_system=include_system, timeout=60)

    # No _apps_current: the foreground app is not exposed.

    # --- session --------------------------------------------------------

    def _session_state(self):
        """'ready' | 'locked' | 'not-running'."""
        st = _state()
        if st is None or not st.get("ready"):
            return "not-running"
        from . import ocr
        try:
            path, win = self._screen_capture()
            texts = " ".join(o["text"] for o in ocr.recognize(path, win)).lower()
        except RuntimeError:
            return "not-running"
        return "locked" if any(m in texts for m in _LOCKED_MARKERS) else "ready"

    def _session_detail(self):
        st = _state()
        if st is None:
            return "no session daemon"
        if st.get("phase") == "failed":
            return f"awake failed: {st.get('error')}"
        return f"phase {st.get('phase')}"

    def _session_require(self):
        state = self._session_state()
        if state == "ready":
            return self._screen_bounds()
        if state == "locked":
            raise RuntimeError(
                "The iPhone is locked. Please unlock it on the phone, then retry — "
                "I will not enter a passcode.")
        st = _state()
        if st and st.get("phase") == "failed":
            raise RuntimeError(f"The last `phone-harness ios awake` failed: {st.get('error')}")
        raise RuntimeError(AWAKE_HINT)

    def _session_refocus(self):
        return None                    # nothing on this computer to focus

    # --- interruption ---------------------------------------------------

    def _focus_probe(self):
        return (True,)                 # the user's screen is never touched

    def _focus_diff(self, before, after):
        return {"raised": False, "stole_focus": False}


# --- CLI (phone-harness ios ...) --------------------------------------------

CLI_USAGE = """Usage:
  phone-harness ios                       what is plugged in, trust, Developer Mode, session
  phone-harness ios awake [--bg] [--serial UDID]
        open the USB session every action needs: mounts the developer image if
        the phone dropped it (it does on every reboot), then holds the tunnel
        and screen stream. Ends with rest or Ctrl-C. --bg detaches.
  phone-harness ios rest                  end the session
  phone-harness ios pair [--serial UDID]  trust this computer (approve on the phone; once)
  phone-harness ios reveal                make the Developer Mode switch visible in Settings
  phone-harness ios mount [--remount]     mount the developer image now (awake does this itself)
"""


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def _arg(args, flag):
    if flag in args:
        i = args.index(flag)
        return args[i + 1] if i + 1 < len(args) else None
    return None


def _serial(args):
    return _arg(args, "--serial") or os.environ.get("PHONE_HARNESS_IOS_SERIAL") \
        or config.devices_of("coredevice").get("primary")


def _remember(info):
    if not info.get("udid"):
        return
    reg = config.devices_of("coredevice")
    reg["phones"][info["udid"]] = {"name": info.get("name"), "model": info.get("model"),
                                   "ios": info.get("ios"),
                                   "last_seen": time.strftime("%Y-%m-%dT%H:%M:%S")}
    reg["primary"] = reg["primary"] or info["udid"]
    config.save_devices_of("coredevice", reg)


def _daemon_pid():
    st = _state()
    return st["pid"] if st else None


def _stop_daemon(pid, wait=45.0):
    """Ask the daemon to shut down over its socket, which lets it stop the
    phone's stream cleanly (an unstopped stream wedges the next session).
    Fall back to a signal, and on Windows to taskkill, if it does not answer."""
    st = _state()
    if st and st.get("ready"):
        try:
            _connect(st, 3).close()          # reachable?
            request("stop", timeout=5)
        except (RuntimeError, OSError, Unsupported):
            pass
    deadline = time.time() + wait
    said = False
    while time.time() < deadline:
        if _daemon_pid() is None:
            return True
        if not said and time.time() > deadline - wait + 3:
            print("waiting for the phone to end its screen stream...", flush=True)
            said = True
        time.sleep(0.25)
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        return _daemon_pid() is None
    for _ in range(40):
        if _daemon_pid() is None:
            return True
        time.sleep(0.25)
    return False


def _wait_ready(pid, timeout=300):
    """Follow the daemon's phases until ready or failed. Prints each phase.
    Only this daemon's state counts: a stale file from an earlier run is
    ignored until the new process has overwritten it."""
    from .coredevice_daemon import read_state
    last = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = read_state() or {}
        if st.get("pid") != pid:
            time.sleep(0.1)
            continue
        phase = st.get("phase")
        if phase != last and phase:
            notes = {"probing": "checking the phone over USB",
                     "mounting": "mounting the developer image (downloads it the first time)",
                     "connecting": "opening the USB tunnel",
                     "recovering": "display service stalled; remounting the developer image",
                     "clearing": "ending a stream session an earlier run left behind",
                     "streaming": "starting the screen stream that authorises input",
                     "ready": "ready"}
            print(f"  {phase}: {notes.get(phase, '')}".rstrip(": "), flush=True)
            last = phase
        if st.get("ready"):
            return True
        if phase == "failed":
            print(f"awake failed: {st.get('error')}")
            return False
        time.sleep(0.25)
    print("awake timed out before the session was ready; see `phone-harness ios`")
    return False


def cli(args):
    cmd = args[0] if args else None
    from . import coredevice_daemon as D

    if cmd is None:
        try:
            info = _run(D.probe(_serial(args)))
        except ImportError as e:
            print(f"pymobiledevice3 is not installed: pip install 'phone-harness[iphone]' ({e})")
            return 1
        reg = config.devices_of("coredevice")
        print(f"remembered: {', '.join(reg['phones']) or 'none'}   primary: {reg['primary'] or 'none'}")
        print(f"usb devices: {', '.join(info['devices']) or 'none'}")
        if info["udid"]:
            print(f"phone: {info.get('name') or '?'} ({info.get('model')}, iOS {info.get('ios')})  "
                  f"trusted: {info['paired']}  developer mode: {info['developer_mode']}  "
                  f"developer image mounted: {info['ddi_mounted']}")
        if info["error"]:
            print(f"problem: {info['error']}")
        st = _state()
        if st:
            print(f"session: {st.get('phase')} (pid {st['pid']}) — `phone-harness ios rest` to end")
        else:
            print("session: off — `phone-harness ios awake` to start")
        return 0

    if cmd == "awake":
        try:
            import pymobiledevice3  # noqa: F401
        except ImportError:
            print("pymobiledevice3 is not installed: pip install 'phone-harness[iphone]' "
                  "(Python 3.13 or newer)")
            return 1
        if _daemon_pid():
            print(f"already awake (pid {_daemon_pid()}); `phone-harness ios rest` to end")
            return 0
        serial = _serial(args)
        argv = [sys.executable, "-m", "phone_harness.coredevice_daemon"] + \
               (["--serial", serial] if serial else [])
        D.paths()["state"].parent.mkdir(parents=True, exist_ok=True)
        D.paths()["state"].unlink(missing_ok=True)          # never trust a stale run
        with open(D.paths()["log"], "ab") as logf:
            child = subprocess.Popen(argv, stdout=logf, stderr=logf,
                                     start_new_session=(sys.platform != "win32"),
                                     creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                                    | getattr(subprocess, "DETACHED_PROCESS", 0))
                                     if sys.platform == "win32" else 0)
        print(f"awake: starting the session (pid {child.pid}); log at {D.paths()['log']}")
        ok = _wait_ready(child.pid)
        if not ok:
            return 1
        st = _state() or {}
        _remember(st)
        print(f"awake: {st.get('name')} ({st.get('model')}, iOS {st.get('ios')}), "
              f"screen {st.get('w')}x{st.get('h')} px")
        if "--bg" in args:
            print("running in the background; `phone-harness ios rest` to end")
            return 0
        print("Ctrl-C to end")
        try:
            while child.poll() is None:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            if child.poll() is None:
                _stop_daemon(child.pid)
                if child.poll() is None:
                    child.kill()
        print("session ended")
        return 0

    if cmd == "rest":
        pid = _daemon_pid()
        if not pid:
            print("no session running")
            return 0
        print("session ended" if _stop_daemon(pid) else f"pid {pid} is still stopping")
        return 0

    if cmd == "pair":
        serial = _arg(args, "--serial")

        async def pair():
            from pymobiledevice3.lockdown import create_using_usbmux
            print("On the phone: unlock it, then tap Trust and enter the passcode "
                  "there when asked.", flush=True)
            ld = await create_using_usbmux(serial=serial, autopair=True, pair_timeout=120)
            try:
                vals = await ld.get_value()
                return {"udid": vals.get("UniqueDeviceID"), "name": vals.get("DeviceName"),
                        "model": vals.get("ProductType"), "ios": vals.get("ProductVersion")}
            finally:
                await ld.close()
        try:
            info = _run(pair())
        except Exception as e:
            print(f"pairing failed: {type(e).__name__}: {e}")
            return 1
        _remember(info)
        print(f"trusted: {info['name']} ({info['model']}, iOS {info['ios']}) — remembered as primary")
        return 0

    if cmd == "reveal":
        async def reveal():
            from pymobiledevice3.lockdown import create_using_usbmux
            from pymobiledevice3.services.amfi import AmfiService
            ld = await create_using_usbmux(serial=_serial(args), autopair=False)
            try:
                await AmfiService(ld).reveal_developer_mode_option_in_ui()
            finally:
                await ld.close()
        try:
            _run(reveal())
        except Exception as e:
            print(f"reveal failed: {type(e).__name__}: {e}")
            return 1
        print("done. On the phone: close and reopen Settings, then Privacy & Security > "
              "Developer Mode. Turning it on restarts the phone; nothing here does that for you.")
        return 0

    if cmd == "mount":
        try:
            info = _run(D.probe(_serial(args)))
            if info["error"]:
                print(info["error"]); return 1
            did = _run(D.mount_ddi(info["udid"], remount="--remount" in args))
        except Exception as e:
            print(f"mount failed: {type(e).__name__}: {e}")
            return 1
        print("developer image mounted" if did else "developer image was already mounted")
        return 0

    print(CLI_USAGE)
    return 2
