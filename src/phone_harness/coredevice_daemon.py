"""The session companion for the CoreDevice backend: one long-lived process
that holds everything expensive open — the USB tunnel, the media stream that
keeps HID authenticated, the HID and screenshot services — and answers
one-line JSON requests on a local socket.

Why a daemon at all: the phone silently drops HID reports unless a video
stream session is open (pymobiledevice3's "auth gate"), and bringing the
tunnel and stream up costs seconds. phone-harness executes a fresh script per
invocation, so the expensive state has to outlive the script. This is the
`android awake` idea with the transport inside it.

Runs as `python -m phone_harness.coredevice_daemon [--serial UDID]`; the
`phone-harness ios awake` command spawns it. Nothing here logs screen
contents, keystrokes, or clipboard text.

Recovery this process does on its own, because it is cheap and reversible:
  - mount the developer disk image when the phone dropped it (every reboot);
  - remount it when the display service stops answering, which un-wedges the
    phone's media daemon after an unclean previous session.
Nothing else: no pairing, no Developer Mode changes, no passcodes.
"""
import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
import uuid
from pathlib import Path

from . import config

log = logging.getLogger("phone-harness.coredevice")

TOUCH_MAX = 65535
STREAM_TIMEOUT = 12
CONNECT_TIMEOUT = 8

# HID keyboard usages for the names helpers.press() takes ("return", "cmd+1").
KEY_NAMES = {
    "return": 40, "enter": 40, "escape": 41, "esc": 41, "delete": 42,
    "backspace": 42, "tab": 43, "space": 44, "forwarddelete": 76,
    "right": 79, "left": 80, "down": 81, "up": 82, "home": 74, "end": 77,
    "pageup": 75, "pagedown": 78, "capslock": 57,
    **{f"f{i}": 57 + i for i in range(1, 13)},
}
MODIFIERS = {"ctrl": 224, "control": 224, "shift": 225, "alt": 226,
             "option": 226, "opt": 226, "cmd": 227, "command": 227, "meta": 227,
             "super": 227}
CMD, SHIFT = 227, 225


# --- where the socket and state live --------------------------------------

def paths():
    run = config.run_dir()
    return {"state": run / "coredevice.json", "sock": run / "coredevice.sock",
            "port": run / "coredevice.port", "log": run / "coredevice.log",
            "pid": run / "coredevice.pid"}


def read_state():
    try:
        return json.loads(paths()["state"].read_text())
    except (OSError, ValueError):
        return None


def _write_state(data):
    p = paths()["state"]
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, p)


def png_size(data):
    """(w, h) from a PNG's IHDR chunk; no image library needed."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def key_usages(combo):
    """'cmd+shift+3' -> {227, 225, 32}. Single characters map through the
    ASCII table; a shifted character adds Shift itself ('A' == shift+a)."""
    from pymobiledevice3.remote.core_device.hid_service import ASCII_TO_HID
    parts = [p for p in combo.split("+") if p]
    if combo == "+" or "++" in combo:       # a literal plus key, e.g. 'cmd++'
        parts.append("+")
    usages = set()
    for i, part in enumerate(parts):
        name = part.lower()
        if name in MODIFIERS:
            usages.add(MODIFIERS[name])
        elif name in KEY_NAMES:
            usages.add(KEY_NAMES[name])
        elif len(part) == 1 and part in ASCII_TO_HID:
            usage, shifted = ASCII_TO_HID[part]
            usages.add(usage)
            if shifted:
                usages.add(SHIFT)
        else:
            raise ValueError(f"unknown key {part!r} in {combo!r}")
    return usages


def to_touch(x, y, w, h):
    """Screenshot pixels -> the phone's 0..65535 digitizer space."""
    fx = min(1.0, max(0.0, x / max(1, w - 1)))
    fy = min(1.0, max(0.0, y / max(1, h - 1)))
    return round(fx * TOUCH_MAX), round(fy * TOUCH_MAX)


# --- lockdown-side checks (USB, no tunnel) ---------------------------------

async def probe(serial=None):
    """What the doctor and `awake` need to know before opening a tunnel.
    Every field is filled as far as the ladder got; `error` names the rung
    that failed. Never pairs, never mounts."""
    out = {"usbmuxd": False, "devices": [], "udid": None, "paired": None,
           "name": None, "model": None, "ios": None, "developer_mode": None,
           "ddi_mounted": None, "error": None}
    from pymobiledevice3 import exceptions as E
    from pymobiledevice3.usbmux import list_devices
    try:
        devices = [d for d in await list_devices() if d.is_usb]
    except (E.ConnectionFailedToUsbmuxdError, OSError) as e:
        out["error"] = f"usbmuxd: {e}"
        return out
    out["usbmuxd"] = True
    out["devices"] = [d.serial for d in devices]
    if serial:
        devices = [d for d in devices if d.matches_udid(serial)]
    if not devices:
        out["error"] = "no-device"
        return out
    if len(devices) > 1 and not serial:
        out["error"] = "several-devices"
        return out
    out["udid"] = devices[0].serial
    from pymobiledevice3.lockdown import create_using_usbmux
    try:
        ld = await create_using_usbmux(serial=out["udid"], autopair=False)
    except (E.NotPairedError, E.PairingError) as e:
        out["paired"] = False
        out["error"] = f"not-paired: {type(e).__name__}"
        return out
    except E.PasswordRequiredError:
        out["paired"] = False
        out["error"] = "locked: unlock the phone and retry"
        return out
    out["paired"] = True
    try:
        vals = await ld.get_value()
        out["name"] = vals.get("DeviceName")
        out["model"] = vals.get("ProductType")
        out["ios"] = vals.get("ProductVersion")
        from pymobiledevice3.services.mobile_image_mounter import PersonalizedImageMounter
        m = PersonalizedImageMounter(lockdown=ld)
        try:
            out["developer_mode"] = await m.query_developer_mode_status()
            out["ddi_mounted"] = await m.is_image_mounted("Personalized")
        finally:
            await m.close()
    except Exception as e:                      # a rung, not a crash
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        await ld.close()
    return out


def ios_at_least(version, major, minor=0):
    try:
        parts = [int(p) for p in str(version).split(".")[:2]] + [0]
        return (parts[0], parts[1]) >= (major, minor)
    except ValueError:
        return False


async def mount_ddi(udid, remount=False):
    """Mount the personalized developer disk image, downloading it on first
    use. remount=True unmounts first — the recovery for a display service that
    stopped answering."""
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.mobile_image_mounter import (
        PersonalizedImageMounter, auto_mount_personalized)
    ld = await create_using_usbmux(serial=udid, autopair=False)
    try:
        m = PersonalizedImageMounter(lockdown=ld)
        try:
            if await m.is_image_mounted("Personalized"):
                if not remount:
                    return False
                await m.umount()
        finally:
            await m.close()
        await auto_mount_personalized(ld)
        return True
    finally:
        await ld.close()


# --- the session -----------------------------------------------------------

class Session:
    def __init__(self, udid, info):
        self.udid = udid
        self.info = info                # name/model/ios from probe()
        self.rsd = None
        self.tunnel = None
        self.display = None
        self.transport = None
        self.stream_id = None
        self.drain = None
        self.hid = None
        self.keyboard = None
        self.indigo = None
        self.apps_cache = None
        self.w = self.h = 0
        self.contact = None
        self.lock = asyncio.Lock()
        self.dead = False
        self.started = time.time()

    # -- lifecycle --

    async def open(self, phase):
        from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
        from pymobiledevice3.remote.core_device.hid_service import UniversalHIDServiceService

        phase("connecting")
        self.tunnel = UserspaceRsdTunnel(serial=self.udid, autopair=False,
                                         remotepairing_fallback=False)
        self.rsd = await self.tunnel.aopen()

        # A session an earlier run never stopped (cable pulled, process
        # killed) keeps the phone's screen-sharing indicator on. Clear it
        # before starting ours, so plugging in and running awake is the fix.
        try:
            leftover = await self._stream_sessions()
        except Exception as e:
            leftover = []
            log.warning("could not read stream sessions: %s", type(e).__name__)
        if leftover:
            phase("clearing")
            log.info("stopping %d leftover stream session(s)", len(leftover))
            await self._stop_sessions(leftover, why="leftover")

        # The display service is the part that wedges after an unclean
        # session: connect times out, or the stream request is dropped. One
        # remount of the developer image, one retry, then give up loudly.
        for attempt in (1, 2):
            try:
                await self._open_stream(phase)
                break
            except (asyncio.TimeoutError, OSError, asyncio.IncompleteReadError) as e:
                await self._close_stream()
                if attempt == 2:
                    raise RuntimeError(
                        f"the phone's display service is not answering ({type(e).__name__}), "
                        "even after remounting the developer image. Reboot the iPhone and "
                        "run `phone-harness ios awake` again.") from None
                phase("recovering")
                log.warning("display service unresponsive (%s); remounting the developer image",
                            type(e).__name__)
                await mount_ddi(self.udid, remount=True)
                await asyncio.sleep(1.0)

        self.hid = UniversalHIDServiceService(self.rsd)
        await self.hid.connect()
        img = await self._capture_bytes()
        self.w, self.h = png_size(img)
        phase("ready")

    async def _open_stream(self, phase):
        from pymobiledevice3.remote.core_device.display_service import DisplayService
        from pymobiledevice3.remote.core_device.screen_stream import open_media_receiver
        # One connection per request: the support query and the stream do not
        # share a channel (the phone drops a stream request on a reused one).
        probe = DisplayService(self.rsd)
        await asyncio.wait_for(probe.connect(), CONNECT_TIMEOUT)
        try:
            support = await asyncio.wait_for(probe.get_media_support_info(), CONNECT_TIMEOUT)
        finally:
            with contextlib.suppress(Exception):
                await probe.close()
        features = int(support.get("supportedFeatures", 0) or 0)
        if not features:
            raise RuntimeError(
                f"this iPhone ({self.info.get('model')}, iOS {self.info.get('ios')}) "
                "reports no screen-streaming features from its display service. "
                "iOS 27 is the earliest version seen to work; older phones cannot be "
                "driven this way.")
        phase("streaming")
        self.display = DisplayService(self.rsd)
        await asyncio.wait_for(self.display.connect(), CONNECT_TIMEOUT)
        self.transport, receiver_ip = open_media_receiver(self.display, (1 << 20,))
        answer = await asyncio.wait_for(self.display.start_video_stream(
            receiver_ip=receiver_ip, receiver_port=self.transport.port,
            sender_ip=self.rsd.service.address[0], display_id=1), STREAM_TIMEOUT)
        sid = answer["connection"]["options"]["avcMediaStreamOptionClientSessionID"]["uuid"]
        self.stream_id = sid if isinstance(sid, uuid.UUID) else uuid.UUID(str(sid))
        await asyncio.sleep(0.3)                # backboardd re-matches HID surfaces
        self.drain = asyncio.create_task(self._drain())

    async def _display(self):
        from pymobiledevice3.remote.core_device.display_service import DisplayService
        svc = DisplayService(self.rsd)
        await asyncio.wait_for(svc.connect(), CONNECT_TIMEOUT)
        return svc

    async def _stream_sessions(self):
        """Session ids the phone's media server currently reports."""
        svc = await self._display()
        try:
            status = await asyncio.wait_for(svc.get_media_stream_server_status(), 6)
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(svc.close(), 1)
        ids = []
        for sess in (status or {}).get("sessions") or []:
            try:
                raw = sess["connection"]["options"]["avcMediaStreamOptionClientSessionID"]["uuid"]
                ids.append(raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw)))
            except (KeyError, TypeError, ValueError):
                continue
        return ids

    async def _stop_sessions(self, ids, wait=25.0, why="ours", send_stop=True):
        """End stream sessions the phone still reports, then wait for its
        media server to confirm. A per-session stop is only accepted on the
        connection that started the stream; from any other connection the
        server wants a `stopAll` request, which ends every session at once
        (observed on iOS 27). While a session lingers the phone shows its
        screen-sharing indicator and blocks the camera, so this waits for
        the server's word and remounts the developer image as a last resort,
        which restarts the media daemon."""
        if send_stop and ids:
            svc = await self._display()
            try:
                r = await asyncio.wait_for(svc.invoke(
                    "com.apple.coredevice.feature.stopmediastream", {"stopAll": True},
                    action_identifier="com.apple.coredevice.action.mediastreamstop"), 6)
                log.info("stopAll (%s) -> %s", why, json.dumps(r, default=str)[:120])
            except Exception as e:
                log.warning("stopAll (%s): %s", why, type(e).__name__)
            finally:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(svc.close(), 1)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                left = await self._stream_sessions()
            except Exception as e:
                log.warning("stream status: %s", type(e).__name__)
                return False
            if not left:
                log.info("stream sessions ended (%s)", why)
                return True
            await asyncio.sleep(0.5)
        log.error("the phone still reports %d stream session(s) after %.0fs; remounting the "
                  "developer image to clear them", len(left), wait)
        try:
            await mount_ddi(self.udid, remount=True)
        except Exception as e:
            log.error("remount failed: %s", type(e).__name__)
        return False

    async def _stop_stream_verified(self):
        """Stop our stream on the connection that started it: the phone ends
        the session at once and drops that channel (the library reports
        {"stopped": True}). A stop from any other connection is refused, and
        an unstopped session lingers ~20s (RTCP timeout) with the phone's
        screen-sharing indicator on and its camera blocked."""
        if self.stream_id is None or self.rsd is None:
            return
        stopped = False
        if self.display is not None:
            try:
                r = await asyncio.wait_for(self.display.stop_media_stream(self.stream_id), 6)
                stopped = bool((r or {}).get("stopped")) or r == {}
                log.info("stream %s stop -> %s", self.stream_id, json.dumps(r, default=str)[:80])
            except Exception as e:
                log.warning("stream %s stop on its own connection: %s", self.stream_id, type(e).__name__)
        await self._stop_sessions([self.stream_id], wait=(4.0 if stopped else 8.0),
                                  why="ours", send_stop=not stopped)

    async def _close_stream(self):
        if self.drain is not None:
            self.drain.cancel()
            with contextlib.suppress(BaseException):
                await self.drain
            self.drain = None
        await self._stop_stream_verified()
        if self.display is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.display.close(), 1)
        if self.transport is not None:
            with contextlib.suppress(Exception):
                self.transport.close()
        self.display = self.transport = self.stream_id = None

    async def _drain(self):
        try:
            while True:
                await self.transport.recv()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("media stream ended")
        self.dead = True

    async def close(self):
        async def quiet(coro, t=2):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(coro, t)
        if self.hid is not None:
            from pymobiledevice3.remote.core_device.hid_service import TOUCHSCREEN_STATE_RELEASE
            if self.contact is not None:
                await quiet(self.hid.send_touchscreen(TOUCHSCREEN_STATE_RELEASE, *self.contact), 1)
            if self.keyboard is not None:
                await quiet(self.hid.send_keyboard(self.keyboard, ()), 1)
        # Tell the phone the stream is over. Skipping this is what wedges the
        # display service for the next session.
        await self._close_stream()
        for svc in (self.indigo, self.hid):
            if svc is not None:
                await quiet(svc.close(), 1)
        if self.tunnel is not None:
            await quiet(self.tunnel.aclose(), 5)

    # -- helpers --

    async def _capture_bytes(self):
        """A fresh screenshot-service connection per capture: reusing one
        stops answering after a few requests (observed on iOS 27), while a
        connection per shot costs ~0.1s and was solid in testing."""
        from pymobiledevice3.remote.core_device.screen_capture_service import ScreenCaptureService
        svc = ScreenCaptureService(self.rsd)
        await asyncio.wait_for(svc.connect(), CONNECT_TIMEOUT)
        try:
            r = await asyncio.wait_for(svc.capture_screenshot(), 10)
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(svc.close(), 1)
        return r["image"]

    async def _touch(self, state, x, y):
        await self.hid.send_touchscreen(state, x, y)

    async def _keys(self, usages):
        """Send one full-bitmap keyboard report, modifiers in a report of
        their own first so a letter is never processed before its Shift."""
        if self.keyboard is None:
            self.keyboard = await self.hid.create_keyboard_service()
        mods = {u for u in usages if 224 <= u <= 231}
        if mods and mods != set(usages):
            await self.hid.send_keyboard(self.keyboard, mods)
            await asyncio.sleep(0.005)
        await self.hid.send_keyboard(self.keyboard, set(usages))

    async def _chord(self, usages, hold=0.05):
        await self._keys(usages)
        await asyncio.sleep(hold)
        await self._keys(set())

    async def _home_press(self, hold=0.06):
        from pymobiledevice3.remote.core_device.hid_service import (
            IndigoHIDService, HID_BUTTON_STATE_DOWN, HID_BUTTON_STATE_UP)
        if self.indigo is None:
            self.indigo = IndigoHIDService(self.rsd)
            await self.indigo.connect()
        await self.indigo.send_button(0x0C, 0x40, HID_BUTTON_STATE_DOWN)
        try:
            await asyncio.sleep(hold)
        finally:
            await self.indigo.send_button(0x0C, 0x40, HID_BUTTON_STATE_UP)

    async def _glide(self, p1, p2, duration, steps, hold_end=0.0):
        """Finger down at p1, interpolate to p2 over `duration`, optionally
        rest, lift. All points in screenshot pixels."""
        from pymobiledevice3.remote.core_device.hid_service import (
            TOUCHSCREEN_STATE_CONTACT, TOUCHSCREEN_STATE_RELEASE)
        steps = max(1, int(steps))
        a, b = to_touch(*p1, self.w, self.h), to_touch(*p2, self.w, self.h)
        pos = a
        try:
            await self._touch(TOUCHSCREEN_STATE_CONTACT, *a)
            self.contact = a
            for i in range(1, steps + 1):
                await asyncio.sleep(duration / steps)
                pos = (round(a[0] + (b[0] - a[0]) * i / steps),
                       round(a[1] + (b[1] - a[1]) * i / steps))
                await self._touch(TOUCHSCREEN_STATE_CONTACT, *pos)
                self.contact = pos
            if hold_end:
                await asyncio.sleep(hold_end)
        finally:
            self.contact = None
            await self._touch(TOUCHSCREEN_STATE_RELEASE, *pos)

    # -- ops --

    async def op_ping(self, **_):
        return {"udid": self.udid, "w": self.w, "h": self.h, "pid": os.getpid(),
                "uptime": round(time.time() - self.started, 1), **self.info}

    async def op_capture(self, path, **_):
        img = await self._capture_bytes()
        self.w, self.h = png_size(img)          # rotation changes it
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".part")
        tmp.write_bytes(img)
        os.replace(tmp, p)
        return {"path": str(p), "w": self.w, "h": self.h}

    async def op_tap(self, x, y, **_):
        from pymobiledevice3.remote.core_device.hid_service import (
            TOUCHSCREEN_STATE_CONTACT, TOUCHSCREEN_STATE_RELEASE)
        t = to_touch(x, y, self.w, self.h)
        await self._touch(TOUCHSCREEN_STATE_CONTACT, *t)
        self.contact = t
        try:
            await asyncio.sleep(0.06)
        finally:
            self.contact = None
            await self._touch(TOUCHSCREEN_STATE_RELEASE, *t)
        return None

    async def op_press(self, x, y, duration=0.8, **_):
        from pymobiledevice3.remote.core_device.hid_service import (
            TOUCHSCREEN_STATE_CONTACT, TOUCHSCREEN_STATE_RELEASE)
        t = to_touch(x, y, self.w, self.h)
        await self._touch(TOUCHSCREEN_STATE_CONTACT, *t)
        self.contact = t
        try:
            end = time.monotonic() + float(duration)
            while time.monotonic() < end:       # keep the sample fresh
                await asyncio.sleep(min(0.1, max(0.0, end - time.monotonic())))
                await self._touch(TOUCHSCREEN_STATE_CONTACT, *t)
        finally:
            self.contact = None
            await self._touch(TOUCHSCREEN_STATE_RELEASE, *t)
        return None

    async def op_drag(self, x1, y1, x2, y2, duration=0.35, steps=14, **_):
        await self._glide((x1, y1), (x2, y2), float(duration), steps)
        return None

    async def op_scroll(self, x, y, dy, dx=0, steps=6, **_):
        # helpers hand over the finger delta; a slow glide with a rest at the
        # end moves content without a momentum fling.
        start = (x - dx / 2, y - dy / 2)
        end = (x + dx / 2, y + dy / 2)
        await self._glide(start, end, 0.28, max(8, int(steps)), hold_end=0.15)
        return None

    async def op_keys(self, combo, **_):
        await self._chord(key_usages(combo))
        return None

    async def op_text(self, s, delay=0.03, keystrokes=False, **_):
        from pymobiledevice3.remote.core_device.hid_service import ASCII_TO_HID
        if keystrokes:
            missing = sorted({c for c in s if c not in ASCII_TO_HID})
            if missing:
                raise ValueError(f"keystrokes cannot type {missing!r}; use the paste path")
            for c in s:
                usage, shifted = ASCII_TO_HID[c]
                await self._chord({usage} | ({SHIFT} if shifted else set()), hold=0.02)
                await asyncio.sleep(float(delay))
            return None
        from pymobiledevice3.remote.core_device.pasteboard_service import PasteboardService
        svc = PasteboardService(self.rsd)
        await asyncio.wait_for(svc.connect(), CONNECT_TIMEOUT)
        try:
            reply = await asyncio.wait_for(svc.set_text(s), 10)
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(svc.close(), 1)
        if not isinstance(reply, dict) or reply.get("error"):
            raise RuntimeError("the phone did not accept the pasteboard contents")
        await asyncio.sleep(0.15)
        await self._keys({CMD})
        await self._keys({CMD, ASCII_TO_HID["v"][0]})   # iOS pastes on Cmd+V
        await asyncio.sleep(0.05)
        await self._keys({CMD})
        await self._keys(set())
        return None

    async def op_home(self, **_):
        await self._home_press()
        return None

    async def op_recents(self, **_):
        if self.h and self.w and self.h / self.w < 2.0:
            # 16:9 screen = a phone with a Home button: double-click it
            await self._home_press()
            await asyncio.sleep(0.12)
            await self._home_press()
        else:                                    # swipe up from the bar and pause
            await self._glide((self.w / 2, self.h * 0.995), (self.w / 2, self.h * 0.55),
                              0.25, 10, hold_end=0.45)
        return None

    async def _app_service(self):
        """A fresh connection each time: CoreDevice request/reply services
        (apps, pasteboard, screenshots) stop answering when reused after a
        pause; only the HID report channel stays open for the session."""
        from pymobiledevice3.remote.core_device.app_service import AppServiceService
        svc = AppServiceService(self.rsd)
        await asyncio.wait_for(svc.connect(), CONNECT_TIMEOUT)
        return svc

    async def _app_list(self, refresh=False):
        if self.apps_cache is None or refresh:
            svc = await self._app_service()
            try:
                self.apps_cache = await asyncio.wait_for(svc.list_apps(), 30)
            finally:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(svc.close(), 1)
        return self.apps_cache

    async def op_apps(self, include_system=False, raw=False, **_):
        apps = await self._app_list()
        if raw:
            return [{k: (v if isinstance(v, (str, int, float, bool)) or v is None else str(v))
                     for k, v in a.items()} for a in apps]
        out = []
        for a in apps:
            bid = a.get("bundleIdentifier")
            if not bid:
                continue
            # "system": Apple's own non-removable apps and hidden/internal ones.
            # App Store, Weather and friends are removable, so they count as apps.
            system = bool(a.get("isInternal")) or bool(a.get("isHidden")) or (
                bool(a.get("isFirstParty")) and not a.get("isRemovable"))
            if include_system or not system:
                out.append(bid)
        return sorted(set(out))

    async def op_launch(self, name, **_):
        q = name.lower()
        apps = await self._app_list()
        def pick(items):
            exact = [a for a in items if a.get("bundleIdentifier", "").lower() == q
                     or str(a.get("name", "")).lower() == q]
            loose = [a for a in items if q in a.get("bundleIdentifier", "").lower()
                     or q in str(a.get("name", "")).lower()]
            return (exact or loose)
        hits = pick(apps) or pick(await self._app_list(refresh=True))
        if not hits:
            raise RuntimeError(f"no installed app matches {name!r}")
        bid = hits[0]["bundleIdentifier"]
        t0 = time.monotonic()
        svc = await self._app_service()
        t1 = time.monotonic()
        try:
            await asyncio.wait_for(svc.launch_application(bid, kill_existing=False), 20)
            log.info("launch %s: connect %.2fs, launch %.2fs", bid, t1 - t0, time.monotonic() - t1)
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(svc.close(), 1)
        return bid

    async def op_stop(self, **_):
        self.dead = True
        return None

    async def handle(self, req):
        op = req.get("op")
        fn = getattr(self, f"op_{op}", None)
        if fn is None:
            return {"ok": False, "kind": "Unsupported", "error": f"daemon has no op {op!r}"}
        kw = {k: v for k, v in req.items() if k != "op"}
        try:
            async with self.lock:
                result = await fn(**kw)
            return {"ok": True, "result": result}
        except (OSError, asyncio.IncompleteReadError, ConnectionError) as e:
            if self.drain is None or self.drain.done() or self.tunnel is None or self.tunnel.rsd is None:
                self.dead = True
                return {"ok": False, "kind": "RuntimeError",
                        "error": f"lost the phone ({type(e).__name__}); run `phone-harness ios awake` again"}
            return {"ok": False, "kind": type(e).__name__,
                    "error": f"{type(e).__name__}: the phone did not answer; retry once, and if it "
                             "keeps happening run `phone-harness ios rest` then `awake`"}
        except Exception as e:
            return {"ok": False, "kind": type(e).__name__, "error": f"{type(e).__name__}: {e}"[:400]}


# --- serving ---------------------------------------------------------------

async def _serve(session, stop):
    async def handler(reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            if not line:
                return
            req = json.loads(line)
            reply = await session.handle(req)
        except Exception as e:
            reply = {"ok": False, "kind": type(e).__name__, "error": str(e)[:200]}
        try:
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
        if session.dead:
            stop.set()

    p = paths()
    if sys.platform == "win32":
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        p["port"].write_text(str(port))
        endpoint = {"kind": "tcp", "port": port}
    else:
        p["sock"].unlink(missing_ok=True)
        server = await asyncio.start_unix_server(handler, path=str(p["sock"]))
        os.chmod(p["sock"], 0o600)
        endpoint = {"kind": "unix", "path": str(p["sock"])}
    return server, endpoint


async def run(serial):
    p = paths()
    p["state"].parent.mkdir(parents=True, exist_ok=True)
    state = {"pid": os.getpid(), "phase": "probing", "ready": False, "error": None,
             "started": time.time()}

    def phase(name, **extra):
        state.update(phase=name, ready=(name == "ready"), **extra)
        _write_state(state)
        log.info("phase: %s", name)

    phase("probing")
    info = await probe(serial)
    if info["error"]:
        hints = {
            "no-device": "no iPhone is connected by USB. Plug it in and unlock it.",
            "several-devices": "several iPhones are connected; pass --serial UDID.",
        }
        msg = hints.get(info["error"], info["error"])
        if str(info["error"]).startswith("not-paired"):
            msg = "this computer is not trusted by the phone yet: run `phone-harness ios pair`."
        if str(info["error"]).startswith("usbmuxd"):
            msg = ("cannot reach the USB service. " +
                   ("Install usbmuxd (`apt install usbmuxd`) and replug the phone."
                    if sys.platform.startswith("linux") else
                    "Install iTunes or the Apple Devices app so the Apple Mobile Device service runs."
                    if sys.platform == "win32" else "Is the phone plugged in?"))
        raise RuntimeError(msg)
    if not ios_at_least(info["ios"], 17, 4):
        raise RuntimeError(f"iOS {info['ios']} is too old: the USB tunnel needs 17.4 and screen "
                           "streaming has only been seen working on iOS 27.")
    if info["developer_mode"] is False:
        raise RuntimeError("Developer Mode is off. On the phone: Settings > Privacy & Security > "
                           "Developer Mode (run `phone-harness ios reveal` first if it is not listed).")
    state.update({k: info[k] for k in ("udid", "name", "model", "ios")})
    if not info["ddi_mounted"]:
        phase("mounting")
        await mount_ddi(info["udid"])

    session = Session(info["udid"], {k: info[k] for k in ("name", "model", "ios")})
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    server = None
    try:
        await session.open(phase)
        server, endpoint = await _serve(session, stop)
        state.update(endpoint=endpoint, w=session.w, h=session.h)
        phase("ready")
        while not stop.is_set() and not session.dead:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), 1.0)
    finally:
        phase("stopping")
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), 2)
        await session.close()
        for key in ("state", "sock", "port", "pid"):
            with contextlib.suppress(OSError):
                p[key].unlink()


def main(argv=None):
    ap = argparse.ArgumentParser(description="phone-harness CoreDevice session daemon")
    ap.add_argument("--serial", default=None)
    ap.add_argument("--log", default=None, help="log file (default: the state dir)")
    args = ap.parse_args(argv)
    p = paths()
    p["log"].parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=args.log or str(p["log"]), level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("pymobiledevice3").setLevel(logging.WARNING)
    p["pid"].write_text(str(os.getpid()))
    try:
        asyncio.run(run(args.serial))
        return 0
    except Exception as e:
        log.error("daemon failed: %s", e)
        try:
            st = read_state() or {}
            st.update(pid=os.getpid(), phase="failed", ready=False, error=str(e)[:500])
            _write_state(st)
        except OSError:
            pass
        print(f"awake failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
