"""iOS Simulator backend — the SAME eyes and hands as a real iPhone.

The point of this backend is what it does NOT do: no simctl taps, no idb, no
accessibility tree. A booted simulator is a phone-shaped window on the Mac,
exactly like the iPhone Mirroring window, so it is driven by the identical
machinery — window capture + Vision OCR for eyes, HID-level events for hands.
An agent's behaviour measured here transfers to a real iPhone because the
control modality never changed; only the window did.

What actually differs, and is all this file contains:

  window   Simulator.app owns the window (one per booted device, titled with
           the device name) instead of com.apple.ScreenContinuity.
  hotkeys  Home is Cmd+Shift+H (Simulator's Device menu) instead of
           Mirroring's Cmd+1; the app switcher is a double Home press;
           Spotlight is the on-device swipe-down, not Cmd+3.
  session  There are no interstitials: a booted window is 'ready', anything
           else tells the caller how to boot one.

Select with PHONE_HARNESS_PLATFORM=sim. With several booted simulators,
PHONE_HARNESS_SIM_DEVICE="iPhone 17 Pro" picks the window by title substring.

Booting is the caller's job (it is environment setup, not phone control):
    xcrun simctl boot "iPhone 17 Pro" && open -a Simulator
"""
import os
import subprocess
import time

from .ios import IPhone, _sleep

BUNDLE_ID = "com.apple.iphonesimulator"
APP_NAME = "Simulator"
APP_PATH = "/Applications/Xcode.app/Contents/Developer/Applications/Simulator.app"


class Simulator(IPhone):
    name = "ios-simulator"

    def __init__(self):
        super().__init__()
        self.mirror.set_target(
            BUNDLE_ID, APP_NAME, APP_PATH,
            window_title=os.environ.get("PHONE_HARNESS_SIM_DEVICE"))

    # --- navigation: same ops, Simulator's accelerators -----------------

    def _nav_home(self):
        self.mirror.press("cmd+shift+h")
        _sleep(0.8)

    def _nav_recents(self):
        # iOS opens the app switcher on a double Home press.
        self.mirror.press("cmd+shift+h")
        time.sleep(0.15)
        self.mirror.press("cmd+shift+h")
        _sleep(0.8)

    def _apps_launch(self, name):
        """Spotlight via the on-device gesture: Home, swipe down, type."""
        self._nav_home()
        win = self.mirror.ensure_window()
        cx = win["x"] + win["w"] / 2
        cy = win["y"] + win["h"] * 0.4
        self.mirror.drag(cx, cy, cx, cy + win["h"] * 0.25, duration=0.25)
        _sleep(0.9)
        self.mirror.type_text(name, keystrokes=True)
        _sleep(1.2)
        self.mirror.press("return")
        return name

    # --- session: no interstitials, just booted-or-not ------------------

    def _session_state(self):
        if self.mirror.running_app() is None:
            return "not-running"
        return "ready" if self.mirror.find_window() else "no-window"

    def _session_detail(self):
        state = self._session_state()
        if state == "ready":
            return "simulator window found"
        return ("no booted simulator window — boot one with\n"
                "  xcrun simctl boot \"<device>\" && open -a Simulator")

    def _session_require(self):
        win = self.mirror.find_window()
        if win:
            return win
        raise RuntimeError(self._session_detail())

    def _session_refocus(self):
        # Nothing to clear: simulators have no iPhone-in-Use interstitials.
        return None


def booted_devices():
    """[(name, udid)] of currently booted simulators, via simctl."""
    out = subprocess.run(
        ["xcrun", "simctl", "list", "devices", "booted"],
        capture_output=True, text=True).stdout
    devices = []
    for line in out.splitlines():
        line = line.strip()
        if "(Booted)" in line:
            name = line.split(" (")[0]
            udid = line.split("(")[1].rstrip(")").strip()
            devices.append((name, udid))
    return devices
