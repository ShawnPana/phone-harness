"""Android backend: the op vocabulary over adb, and nothing else.

No agent app on the phone, no Appium, no window to find, no OCR: `screencap`
is the capture and `uiautomator dump` is the tree, so screen.text answers
exactly (source: "tree") where iOS has to recognise glyphs. adb reaches the
device directly, so focus.probe/diff report nothing disturbed and
session.refocus is a no-op. Coordinates are device pixels and screen.bounds
is the whole display: a text box's centre is already a tap target.

Connecting is the phone's own developer path, over USB or Wi-Fi:

    USB       Developer options > USB debugging, plug in, tap Allow.
    Wireless  Developer options > Wireless debugging > Pair device with
              pairing code, then:  phone-harness android pair IP:PORT CODE

The agent never picks a device. session.require — which every helper passes
through — resolves one: a USB phone if one is plugged in, else a live
wireless link (the saved primary first), else a paired phone found on the
LAN over mDNS and connected on the spot, else a message naming the physical
step that is missing. Paired phones are
remembered in ~/.config/phone-harness/android.json under a friendly name and
matched by hardware serial, so a phone whose Wi-Fi port changed overnight is
still "pixel-8". The first phone paired over Wi-Fi becomes the primary;
`phone-harness android use NAME` changes that; ANDROID_SERIAL overrides
everything.
"""
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from .transport import Backend, Unsupported

ADB = os.environ.get("PHONE_HARNESS_ADB", "adb")
CONFIG = Path(os.environ.get("PHONE_HARNESS_CONFIG_DIR",
                             Path.home() / ".config" / "phone-harness")) / "android.json"

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


# --- adb, without a device yet -----------------------------------------------

def _run(*args, binary=False, timeout=60, check=True):
    r = subprocess.run([ADB, *args], capture_output=True, timeout=timeout)
    if check and r.returncode != 0:
        err = (r.stderr or r.stdout).decode(errors="replace").strip()
        raise RuntimeError(f"adb {' '.join(args)} failed: {err}")
    return r.stdout if binary else r.stdout.decode(errors="replace")


def _attached():
    """[(adb_id, state, transport)] — transport is 'usb' or 'wifi'."""
    rows = [l.split() for l in _run("devices").splitlines()[1:] if l.strip()]
    return [(r[0], r[1], "wifi" if ":" in r[0] or r[0].startswith("adb-")
             else "usb") for r in rows if len(r) >= 2]


def _prop(adb_id, name):
    return _run("-s", adb_id, "shell", "getprop", name, timeout=15).strip()


def _mdns():
    """Paired phones announcing themselves on the LAN:
    [{serial, host, port, addr}] for _adb-tls-connect services. The service
    name carries the hardware serial: adb-<SERIAL>-<suffix>."""
    out = _run("mdns", "services", check=False)
    found = []
    for line in out.splitlines():
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) < 3 or "_adb-tls-connect" not in parts[1]:
            continue
        m = re.match(r"adb-(.+)-[^-]+$", parts[0])
        host, _, port = parts[2].rpartition(":")
        if m and host and port.isdigit():
            found.append({"serial": m.group(1), "host": host,
                          "port": int(port), "addr": parts[2]})
    return found


# --- the registry: phones we know, and which one is primary -----------------

def _load():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {"primary": None, "phones": {}}


def _store(cfg):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _remember(cfg, serial, model, host=None, name=None, primary=False):
    """Upsert a phone by serial. `primary=True` makes it the primary if there
    is none yet — passed by wireless pairing, not by a phone that merely
    showed up on USB: the primary is which phone to reach for over Wi-Fi."""
    phones = cfg.setdefault("phones", {})
    key = next((k for k, p in phones.items() if p.get("serial") == serial), None)
    if key is None:
        base = name or re.sub(r"[^a-z0-9]+", "-", (model or "android").lower()).strip("-")
        key, n = base, 2
        while key in phones:
            key, n = f"{base}-{n}", n + 1
        phones[key] = {"serial": serial, "model": model, "paired": _now()}
    p = phones[key]
    p["last_seen"] = _now()
    if host:
        p["last_host"] = host
    if primary and not cfg.get("primary"):
        cfg["primary"] = key
    _store(cfg)
    return key


class Android(Backend):
    name = "android"

    def __init__(self, serial=None):
        if serial:
            os.environ["ANDROID_SERIAL"] = serial
        self._bounds = None
        self._resolved = False
        self._gate_at = 0.0

    # --- adb plumbing -------------------------------------------------------

    def _adb(self, *args, binary=False, timeout=60):
        return _run(*args, binary=binary, timeout=timeout)

    def _sh(self, cmd, timeout=60):
        return self._adb("shell", cmd, timeout=timeout)

    # --- choosing the phone -------------------------------------------------

    def _resolve(self):
        """Pin ANDROID_SERIAL to one ready device, connecting a paired phone
        over Wi-Fi if nothing is attached. Returns the adb id or None.

        Order: the user's explicit ANDROID_SERIAL; then a USB phone (wired
        always wins — it is right there); then a live Wi-Fi link, the saved
        primary first; then mDNS — the primary if it is on the LAN, else the
        first known phone, else any paired phone."""
        if os.environ.get("ANDROID_SERIAL"):
            self._resolved = True
            return os.environ["ANDROID_SERIAL"]
        cfg = _load()
        primary = (cfg.get("phones") or {}).get(cfg.get("primary") or "", {})
        known = {p.get("serial") for p in (cfg.get("phones") or {}).values()}

        def pick(ready):
            usb = [r for r in ready if r[2] == "usb"]
            if usb:
                return usb[0][0]
            wifi = [r for r in ready if r[2] == "wifi"]
            if primary.get("serial"):
                for adb_id, _, _ in wifi:
                    try:
                        if _prop(adb_id, "ro.serialno") == primary["serial"]:
                            return adb_id
                    except RuntimeError:
                        pass
            return wifi[0][0] if wifi else None

        ready = [r for r in _attached() if r[1] == "device"]
        chosen = pick(ready)
        if chosen is None:
            wanted = [primary.get("serial")] + sorted(known - {primary.get("serial")})
            services = _mdns()
            services.sort(key=lambda s: (wanted.index(s["serial"])
                                         if s["serial"] in wanted else len(wanted)))
            for s in services:
                out = _run("connect", s["addr"], timeout=15, check=False)
                if "connected" in out and "cannot" not in out:
                    break
            ready = [r for r in _attached() if r[1] == "device"]
            chosen = pick(ready)
        if chosen is not None:
            os.environ["ANDROID_SERIAL"] = chosen
            self._resolved = True
            try:
                _remember(cfg, _prop(chosen, "ro.serialno"),
                          _prop(chosen, "ro.product.model"),
                          host=chosen.rpartition(":")[0] if ":" in chosen else None)
            except RuntimeError:
                pass
        return chosen

    # --- awake and unlocked? -----------------------------------------------

    def _screen_status(self):
        """(awake, locked) from two dumpsys reads, ~0.2s. adb happily taps a
        lock screen and dumps its PIN pad; nothing errors — so we ask."""
        awake = "Awake" in self._sh("dumpsys power | grep -m1 mWakefulness=")
        locked = "isKeyguardShowing=true" in self._sh(
            "dumpsys window | grep -m1 isKeyguardShowing")
        return awake, locked

    def _gate(self):
        """Refuse to drive a phone that is asleep or locked. Waking it is
        harmless and done here; unlocking is the user's — a PIN is never
        typed for them. Cached for 2s so a burst of taps checks once."""
        if time.time() - self._gate_at < 2.0:
            return
        awake, locked = self._screen_status()
        if not awake:
            self._sh("input keyevent KEYCODE_WAKEUP")
            time.sleep(0.5)
            awake, locked = self._screen_status()
        if locked:
            raise RuntimeError(
                "The Android phone is locked. Unlock it (PIN / fingerprint), "
                "then retry — I won't enter a PIN. It re-locks after its screen "
                "timeout; Settings > Developer options > Stay awake keeps it on "
                "while charging.")
        self._gate_at = time.time()

    # --- screen -------------------------------------------------------------

    def _screen_bounds(self):
        if self._bounds is None:
            if self._resolve() is None:
                return None
            try:
                out = self._sh("wm size")
            except RuntimeError:
                return None
            m = (re.search(r"Override size:\s*(\d+)x(\d+)", out)
                 or re.search(r"Physical size:\s*(\d+)x(\d+)", out))
            if not m:
                return None
            self._bounds = {"x": 0, "y": 0, "w": int(m.group(1)),
                            "h": int(m.group(2)),
                            "id": os.environ.get("ANDROID_SERIAL")}
        return self._bounds

    def _screen_require(self):
        b = self._screen_bounds()
        return b if b is not None else self._session_require()

    def _screen_capture(self, path=None):
        bounds = self._screen_require()
        png = self._adb("exec-out", "screencap", "-p", binary=True)
        if path is None:
            fd, path = tempfile.mkstemp(prefix="phone-android-", suffix=".png")
            os.close(fd)
        with open(path, "wb") as f:
            f.write(png)
        return path, bounds

    def _screen_text(self, min_confidence=0.3):
        """From the accessibility tree: exact strings, exact boxes."""
        out = []
        for n in self._tree():
            s = n["text"] or n["desc"]
            if s:
                out.append({"text": s, "confidence": 1.0, "source": "tree",
                            "x": n["x"], "y": n["y"], "w": n["w"], "h": n["h"]})
        return out

    # No _screen_text_pixels: the tree is the text source here, and a caller
    # that needs pixels can look at screen.capture. Keeps this backend free of
    # anything but adb.

    # --- input --------------------------------------------------------------

    def _input_tap(self, x, y):
        self._gate()
        self._sh(f"input tap {int(x)} {int(y)}")

    def _input_press(self, x, y, duration=0.8):
        self._gate()
        x, y = int(x), int(y)
        self._sh(f"input swipe {x} {y} {x} {y} {int(duration * 1000)}")

    def _input_drag(self, x1, y1, x2, y2, duration=0.35, steps=14):
        self._gate()
        self._sh(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} "
                 f"{int(duration * 1000)}")

    def _input_scroll(self, x, y, dy, steps=6):
        """A finger drag standing in for a wheel: +dy moves content up the
        way wheel-up does (revealing what is above), so the finger travels
        +dy pixels downward. Slow enough not to fling."""
        self._gate()
        self._sh(f"input swipe {int(x)} {int(y)} {int(x)} {int(y + dy)} "
                 f"{max(150, int(steps) * 50)}")

    def _input_keys(self, combo):
        self._gate()
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
        self._gate()
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
        self._gate()
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
        """'ready' | 'locked' | 'unauthorized' | 'offline' | 'no-device' | 'no-adb'."""
        try:
            if self._resolve() is not None:
                try:
                    self._gate()
                except RuntimeError as e:
                    if "locked" in str(e):
                        return "locked"
                    raise
                return "ready"
            states = [st for _, st, _ in _attached()]
        except (RuntimeError, FileNotFoundError):
            return "no-adb"
        if "unauthorized" in states:
            return "unauthorized"
        if states:
            return "offline"
        return "no-device"

    def _session_detail(self):
        try:
            return _run("devices", "-l").strip()
        except Exception as e:
            return str(e)

    def _session_require(self):
        """Bounds if a phone is ready, else raise telling the USER what to do
        — plugging in, tapping Allow, and pairing are all physical."""
        state = self._session_state()
        if state == "ready":
            b = self._screen_bounds()
            if b is not None:
                return b
            raise RuntimeError("an Android device is attached but `wm size` "
                               "gave no screen size; is it still booting?")
        if state == "locked":
            self._gate_at = 0.0
            self._gate()                       # raises with the unlock message
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
                "replug it (or toggle Wireless debugging off and on), then retry.")
        cfg = _load()
        prim = cfg.get("primary")
        known = (f" Your primary phone is '{prim}' — make sure Wireless "
                 "debugging is on and it is on this Wi-Fi; if it forgot this "
                 "Mac, pair again." if prim else "")
        raise RuntimeError(
            "No Android device is reachable." + known + " To connect one: "
            "USB — Settings > Developer options > USB debugging, plug in, tap "
            "Allow. Wi-Fi — Developer options > Wireless debugging > Pair "
            "device with pairing code, then `phone-harness android pair "
            "IP:PORT CODE`. Then retry.")

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
        self._screen_require()
        self._gate()
        last = None
        for i in range(5):
            try:
                raw = self._adb("exec-out", "uiautomator", "dump", "/dev/tty",
                                binary=True, timeout=30)
                xml = raw[raw.find(b"<?xml"):]
                xml = xml[:xml.rfind(b">") + 1]
                root = ET.fromstring(xml)
                break
            except (RuntimeError, ET.ParseError) as e:
                last = e
                time.sleep(0.5 * (i + 1))          # 0.5, 1, 1.5, 2s: transitions settle
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
        self._screen_require()
        return self._adb("shell", cmd, binary=binary, timeout=timeout)


# --- CLI (phone-harness android ...) ----------------------------------------

CLI_USAGE = """Usage:
  phone-harness android                          known phones, primary, what's live
  phone-harness android pair IP:PORT CODE [NAME] Wireless debugging: pair, connect, remember
  phone-harness android connect [NAME|IP:PORT]   connect a paired phone (default: primary, via mDNS)
  phone-harness android use NAME                 make NAME the primary
  phone-harness android forget NAME
"""


def _connect_serial(serial, timeout=10):
    """Find `serial` on the LAN and adb-connect it; -> adb id or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for s in _mdns():
            if s["serial"] == serial:
                out = _run("connect", s["addr"], timeout=15, check=False)
                if "connected" in out and "cannot" not in out:
                    return s["addr"]
        time.sleep(1)
    return None


def cli(args):
    cmd = args[0] if args else None
    cfg = _load()
    phones = cfg.get("phones") or {}

    if cmd is None:
        live = {a: (st, tr) for a, st, tr in _attached()}
        print(f"primary: {cfg.get('primary') or '(none)'}   config: {CONFIG}")
        for name, p in phones.items():
            mark = "*" if name == cfg.get("primary") else " "
            print(f" {mark} {name:<16} {p.get('model') or '?':<18} serial {p.get('serial')}"
                  f"   last {p.get('last_host') or '-'} @ {p.get('last_seen', '-')[:16]}")
        print("attached now:", ", ".join(f"{a} ({st}, {tr})" for a, (st, tr) in live.items())
              or "nothing")
        lan = _mdns()
        if lan:
            print("paired phones on this Wi-Fi:",
                  ", ".join(f"{s['serial']}@{s['addr']}" for s in lan))
        return 0

    if cmd == "pair":
        if len(args) < 3:
            print(CLI_USAGE); return 2
        addr, code = args[1], args[2]
        name = args[3] if len(args) > 3 else None
        out = _run("pair", addr, code, timeout=30, check=False)
        if "Successfully paired" not in out:
            print(out.strip() or "pairing failed"); return 1
        host = addr.rpartition(":")[0]
        # the connect port differs from the pairing port; find it via mDNS
        adb_id = None
        for _ in range(10):
            hit = next((s for s in _mdns() if s["host"] == host), None)
            if hit:
                o = _run("connect", hit["addr"], timeout=15, check=False)
                if "connected" in o and "cannot" not in o:
                    adb_id = hit["addr"]; break
            time.sleep(1)
        if adb_id is None:
            print("paired, but could not find the phone's connect port on this "
                  "network yet. Turn Wireless debugging off and on, then: "
                  "phone-harness android connect IP:PORT  (the port shown on "
                  "the Wireless debugging screen)")
            return 1
        serial, model = _prop(adb_id, "ro.serialno"), _prop(adb_id, "ro.product.model")
        key = _remember(cfg, serial, model, host=host, name=name, primary=True)
        cfg = _load()
        print(f"paired and connected: {model} as '{key}' ({adb_id})"
              + ("  — now the primary" if cfg.get("primary") == key else ""))
        return 0

    if cmd == "connect":
        target = args[1] if len(args) > 1 else cfg.get("primary")
        if not target:
            print("no primary phone yet — pair one first"); return 1
        if ":" in target:
            out = _run("connect", target, timeout=15, check=False); print(out.strip())
            return 0 if "connected" in out and "cannot" not in out else 1
        p = phones.get(target)
        if not p:
            print(f"unknown phone {target!r}; known: {', '.join(phones) or 'none'}"); return 1
        adb_id = _connect_serial(p["serial"])
        if adb_id is None:
            print(f"'{target}' isn't announcing on this Wi-Fi. Is Wireless "
                  "debugging on, and the phone on the same network?"); return 1
        _remember(cfg, p["serial"], p.get("model"), host=adb_id.rpartition(":")[0],
                  primary=True)
        print(f"connected: {target} ({adb_id})"); return 0

    if cmd == "use":
        if len(args) < 2 or args[1] not in phones:
            print(f"known phones: {', '.join(phones) or 'none'}"); return 2
        cfg["primary"] = args[1]; _store(cfg)
        print(f"primary: {args[1]}"); return 0

    if cmd == "forget":
        if len(args) < 2 or args[1] not in phones:
            print(f"known phones: {', '.join(phones) or 'none'}"); return 2
        phones.pop(args[1])
        if cfg.get("primary") == args[1]:
            cfg["primary"] = next(iter(phones), None)
        _store(cfg); print(f"forgot {args[1]}"); return 0

    print(CLI_USAGE)
    return 2
