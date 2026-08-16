"""Android backend: the op vocabulary over adb.

Everything here is `adb shell` — no agent app on the phone, no Appium, no
window to find. adb reaches the device directly, which is what makes three
things simply better than over iPhone Mirroring:

- Nothing needs focus. Captures and input never care what the Mac is doing,
  so focus.probe/diff always report "nothing disturbed" and session.refocus
  is a no-op.
- Coordinates are device pixels and screen.bounds is the whole display, so a
  text box's center is already a valid tap target — no scaling.
- The device has a real accessibility tree (uiautomator), so screen.text is
  exact where iOS has to recognise glyphs. Pixel OCR stays available as
  screen.text_pixels for what a tree cannot see: games, canvas, WebViews
  without accessibility.

Connecting is the phone's own developer path — Settings > Developer options
> USB debugging, plug in, tap Allow — or Wireless debugging + `adb pair`.
`ANDROID_SERIAL` picks a device when several are attached; adb honours it.
An emulator is indistinguishable from a phone here, which is how this was
developed.
"""
import os
import re
import shlex
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

from .transport import Backend, Unsupported

ADB = os.environ.get("PHONE_HARNESS_ADB", "adb")

# input.keys names -> Android keycodes. Modifier chords are not a thing
# `input keyevent` can express, so combos raise rather than half-work.
_KEYS = {
    "enter": "ENTER", "return": "ENTER", "tab": "TAB", "space": "SPACE",
    "delete": "DEL", "backspace": "DEL", "escape": "ESCAPE", "esc": "ESCAPE",
    "home": "HOME", "back": "BACK", "recents": "APP_SWITCH", "menu": "MENU",
    "up": "DPAD_UP", "down": "DPAD_DOWN", "left": "DPAD_LEFT",
    "right": "DPAD_RIGHT", "power": "POWER", "volumeup": "VOLUME_UP",
    "volumedown": "VOLUME_DOWN", "search": "SEARCH",
}


class Android(Backend):
    name = "android"

    def __init__(self, serial=None):
        if serial:
            os.environ["ANDROID_SERIAL"] = serial
        self._bounds = None

    # --- adb plumbing -------------------------------------------------------

    def _adb(self, *args, binary=False, timeout=60):
        r = subprocess.run([ADB, *args], capture_output=True, timeout=timeout)
        if r.returncode != 0:
            err = (r.stderr or r.stdout).decode(errors="replace").strip()
            raise RuntimeError(f"adb {' '.join(args)} failed: {err}")
        return r.stdout if binary else r.stdout.decode(errors="replace")

    def _sh(self, cmd, timeout=60):
        return self._adb("shell", cmd, timeout=timeout)

    def _devices(self):
        """[(serial, state)] as `adb devices` sees them."""
        out = self._adb("devices")
        rows = [l.split("\t") for l in out.splitlines()[1:] if "\t" in l]
        want = os.environ.get("ANDROID_SERIAL")
        return [(s, st) for s, st in rows if not want or s == want]

    # --- screen -------------------------------------------------------------

    def _screen_bounds(self):
        if self._bounds is None:
            try:
                out = self._sh("wm size")
            except RuntimeError:
                return None
            m = (re.search(r"Override size:\s*(\d+)x(\d+)", out)
                 or re.search(r"Physical size:\s*(\d+)x(\d+)", out))
            if not m:
                return None
            serial = next((s for s, st in self._devices() if st == "device"),
                          None)
            self._bounds = {"x": 0, "y": 0, "w": int(m.group(1)),
                            "h": int(m.group(2)), "id": serial}
        return self._bounds

    def _screen_require(self):
        b = self._screen_bounds()
        if b is None:
            return self._session_require()
        return b

    def _screen_capture(self, path=None):
        png = self._adb("exec-out", "screencap", "-p", binary=True)
        if path is None:
            fd, path = tempfile.mkstemp(prefix="phone-android-", suffix=".png")
            os.close(fd)
        with open(path, "wb") as f:
            f.write(png)
        return path, self._screen_require()

    def _screen_text(self, min_confidence=0.3):
        """From the accessibility tree: exact strings, exact boxes."""
        out = []
        for n in self._tree():
            s = n["text"] or n["desc"]
            if s:
                out.append({"text": s, "confidence": 1.0, "source": "tree",
                            "x": n["x"], "y": n["y"], "w": n["w"], "h": n["h"]})
        return out

    def _screen_text_pixels(self, min_confidence=0.3):
        from . import ocr as _vision
        path, win = self._screen_capture()
        return [dict(o, source="pixels")
                for o in _vision.recognize(path, win)
                if o["confidence"] >= min_confidence]

    # --- input --------------------------------------------------------------

    def _input_tap(self, x, y):
        self._sh(f"input tap {int(x)} {int(y)}")

    def _input_press(self, x, y, duration=0.8):
        x, y = int(x), int(y)
        self._sh(f"input swipe {x} {y} {x} {y} {int(duration * 1000)}")

    def _input_drag(self, x1, y1, x2, y2, duration=0.35, steps=14):
        self._sh(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} "
                 f"{int(duration * 1000)}")

    def _input_scroll(self, x, y, dy, steps=6):
        """A finger drag standing in for a wheel: +dy moves content up the
        way wheel-up does (revealing what is above), so the finger travels
        +dy pixels downward. Slow enough not to fling."""
        self._sh(f"input swipe {int(x)} {int(y)} {int(x)} {int(y + dy)} "
                 f"{max(150, int(steps) * 50)}")

    def _input_keys(self, combo):
        parts = [p.strip().lower() for p in combo.split("+")]
        if len(parts) > 1:
            raise Unsupported(f"android cannot press chords ({combo!r}); "
                              "adb keyevents are single keys")
        k = parts[0]
        code = _KEYS.get(k) or (k.upper() if len(k) == 1 and k.isalnum()
                                else None)
        if code is None:
            raise Unsupported(f"android has no key named {combo!r}")
        self._sh(f"input keyevent KEYCODE_{code}")

    def _input_text(self, s, delay=0.03):
        """`input text` takes one ASCII token; newlines and backspaces become
        keyevents, spaces become %s, and the rest is shell-quoted."""
        for i, line in enumerate(s.split("\n")):
            if i:
                self._sh("input keyevent KEYCODE_ENTER")
            for j, chunk in enumerate(line.split("\b")):
                if j:
                    self._sh("input keyevent KEYCODE_DEL")
                if chunk:
                    self._sh("input text " + shlex.quote(chunk.replace(" ", "%s")))

    # --- navigation ---------------------------------------------------------

    def _nav_home(self):
        self._sh("input keyevent KEYCODE_HOME")
        time.sleep(0.5)

    def _nav_back(self):
        self._sh("input keyevent KEYCODE_BACK")
        time.sleep(0.3)

    def _nav_recents(self):
        self._sh("input keyevent KEYCODE_APP_SWITCH")
        time.sleep(0.5)

    # --- apps ---------------------------------------------------------------

    def _apps_launch(self, name):
        """A package id, or a name matched against installed package ids
        ('chrome' -> com.android.chrome). Returns the package launched."""
        pkg = name if "." in name else None
        if pkg is None:
            pkgs = self._apps_list(include_system=True)
            hits = [p for p in pkgs if name.lower() in p.lower()]
            if not hits:
                raise RuntimeError(f"no installed app matches {name!r}")
            # shortest match is the least-qualified, e.g. com.android.chrome
            # over com.android.chrome.helper
            pkg = sorted(hits, key=len)[0]
        out = self._sh("cmd package resolve-activity --brief "
                       f"-c android.intent.category.LAUNCHER {shlex.quote(pkg)}")
        comp = next((l.strip() for l in reversed(out.splitlines())
                     if "/" in l), None)
        if comp is None:
            raise RuntimeError(f"{pkg} has no launchable activity")
        self._sh(f"am start -W -n {shlex.quote(comp)}")
        return pkg

    def _apps_current(self):
        out = self._sh("dumpsys activity activities")
        m = (re.search(r"topResumedActivity=ActivityRecord\{[^}]*?\bu\d+ ([\w.]+)/", out)
             or re.search(r"mResumedActivity: ActivityRecord\{[^}]*?\bu\d+ ([\w.]+)/", out))
        return m.group(1) if m else None

    def _apps_list(self, include_system=False):
        out = self._sh("pm list packages" + ("" if include_system else " -3"))
        return sorted(l[len("package:"):].strip()
                      for l in out.splitlines() if l.startswith("package:"))

    # --- session ------------------------------------------------------------

    def _session_state(self):
        """'ready' | 'unauthorized' | 'offline' | 'no-device' | 'no-adb'."""
        try:
            devs = self._devices()
        except (RuntimeError, FileNotFoundError):
            return "no-adb"
        states = [st for _, st in devs]
        if "device" in states:
            return "ready"
        if "unauthorized" in states:
            return "unauthorized"
        if states:
            return "offline"
        return "no-device"

    def _session_detail(self):
        try:
            return self._adb("devices", "-l").strip()
        except Exception as e:
            return str(e)

    def _session_require(self):
        """Bounds if a device is ready, else raise telling the USER what to do
        — plugging in, tapping Allow, and pairing are all physical."""
        state = self._session_state()
        if state == "ready":
            b = self._screen_bounds()
            if b is not None:
                return b
            raise RuntimeError("an Android device is attached but `wm size` "
                               "gave no screen size; is it still booting?")
        if state == "no-adb":
            raise RuntimeError(
                "adb isn't available. Install Android platform-tools "
                "(brew install android-platform-tools) or set "
                "PHONE_HARNESS_ADB to the adb binary, then retry.")
        if state == "unauthorized":
            raise RuntimeError(
                "The Android phone is attached but hasn't authorised this "
                "computer. Unlock it and tap Allow on the 'Allow USB "
                "debugging?' prompt (tick 'Always allow'), then retry.")
        if state == "offline":
            raise RuntimeError(
                "adb sees the Android device but it is offline. Unplug and "
                "replug it (or re-pair over Wireless debugging), then retry.")
        raise RuntimeError(
            "No Android device is connected. On the phone: Settings > About "
            "phone > tap Build number 7 times, then Settings > Developer "
            "options > USB debugging, plug it in and tap Allow — or use "
            "Wireless debugging and `adb pair`. Then retry. "
            f"({self._session_detail()})")

    def _session_refocus(self):
        return None                    # adb needs nothing in front

    # --- interruption -------------------------------------------------------

    def _focus_probe(self):
        return (True,)                 # frontmost: adb is never occluded

    def _focus_diff(self, before, after):
        return {"raised": False, "stole_focus": False}

    # --- tree / raw ---------------------------------------------------------

    def _tree(self):
        """[{text, desc, id, class, clickable, x, y, w, h}] from
        `uiautomator dump`, retried briefly: it refuses while the UI is
        settling ("could not get idle state")."""
        last = None
        for _ in range(4):
            try:
                raw = self._adb("exec-out", "uiautomator", "dump", "/dev/tty",
                                binary=True, timeout=30)
                xml = raw[raw.find(b"<?xml"):]
                xml = xml[:xml.rfind(b">") + 1]
                root = ET.fromstring(xml)
                break
            except (RuntimeError, ET.ParseError) as e:
                last = e
                time.sleep(0.5)
        else:
            raise RuntimeError(f"uiautomator dump failed: {last}")
        nodes = []
        for el in root.iter("node"):
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", el.get("bounds", ""))
            if not m:
                continue
            x1, y1, x2, y2 = map(int, m.groups())
            nodes.append({
                "text": el.get("text", ""), "desc": el.get("content-desc", ""),
                "id": el.get("resource-id", ""),
                "class": el.get("class", ""),
                "clickable": el.get("clickable") == "true",
                "x": (x1 + x2) // 2, "y": (y1 + y2) // 2,
                "w": x2 - x1, "h": y2 - y1,
            })
        return nodes

    def _raw(self, cmd, binary=False, timeout=60):
        """`adb shell <cmd>` — the escape hatch."""
        return self._adb("shell", cmd, binary=binary, timeout=timeout)
