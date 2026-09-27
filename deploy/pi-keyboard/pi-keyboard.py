#!/usr/bin/env python3
"""Bluetooth Classic HID keyboard for one farm iPhone ("Phone Harness Keyboard").

A plain keyboard: it sends the key states it is told to send, nothing more.
Text, key names and layouts are the host's business (phone_harness/keyboard.py).

Control: one JSON line per TCP connection on 127.0.0.1 (the host reaches it
through an SSH port forward, so nothing crosses the network in the clear), and
every request carries the token in /etc/pi-keyboard/token:
  {"token", "op": "send", "reports": [[usage, ...], ...]}   each: keys held down
  {"token", "op": "status"}
  {"token", "op": "passkey", "code": "123456"}             the code the phone shows
  {"token", "op": "pairing", "on": true|false}             discoverable or not
Reply: one JSON line {"ok": true, ...} or {"ok": false, "error": "..."}.
What is typed is never logged.

Discoverable only while pairing: on at start when nothing is paired, off once a
phone has paired, and on again through op=pairing.
"""
import hmac
import json
import os
import socket
import threading
import time

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

NAME = "Phone Harness Keyboard"
LISTEN_ADDR = os.environ.get("PIKBD_LISTEN", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("PIKBD_PORT", "7777"))
TOKEN = open(os.environ.get("PIKBD_TOKEN_FILE", "/etc/pi-keyboard/token")).read().strip()

PSM_CTRL, PSM_INTR = 17, 19
KEY_DELAY = 0.012            # between key states; iOS drops states faster than ~8 ms
MOD_FIRST, MOD_LAST = 0xE0, 0xE7   # HID usages of the eight modifier keys

# HID report descriptor: standard boot keyboard, report id 1.
REPORT_MAP = (
    "05010906a101850175019508050719e029e715002501810295017508810395057501"
    "05081901290591029501750391039506750815002565050719002965 8100c0"
).replace(" ", "")

SDP_RECORD = f"""<?xml version="1.0" encoding="UTF-8" ?>
<record>
  <attribute id="0x0001"><sequence><uuid value="0x1124" /></sequence></attribute>
  <attribute id="0x0004"><sequence>
    <sequence><uuid value="0x0100" /><uint16 value="0x0011" /></sequence>
    <sequence><uuid value="0x0011" /></sequence>
  </sequence></attribute>
  <attribute id="0x0005"><sequence><uuid value="0x1002" /></sequence></attribute>
  <attribute id="0x0006"><sequence><uint16 value="0x656e" /><uint16 value="0x006a" /><uint16 value="0x0100" /></sequence></attribute>
  <attribute id="0x0009"><sequence><sequence><uuid value="0x1124" /><uint16 value="0x0100" /></sequence></sequence></attribute>
  <attribute id="0x000d"><sequence><sequence>
    <sequence><uuid value="0x0100" /><uint16 value="0x0013" /></sequence>
    <sequence><uuid value="0x0011" /></sequence>
  </sequence></sequence></attribute>
  <attribute id="0x0100"><text value="{NAME}" /></attribute>
  <attribute id="0x0101"><text value="Keyboard" /></attribute>
  <attribute id="0x0102"><text value="Phone Harness" /></attribute>
  <attribute id="0x0200"><uint16 value="0x0100" /></attribute>
  <attribute id="0x0201"><uint16 value="0x0111" /></attribute>
  <attribute id="0x0202"><uint8 value="0x40" /></attribute>
  <attribute id="0x0203"><uint8 value="0x00" /></attribute>
  <attribute id="0x0204"><boolean value="false" /></attribute>
  <attribute id="0x0205"><boolean value="true" /></attribute>
  <attribute id="0x0206"><sequence><sequence>
    <uint8 value="0x22" /><text encoding="hex" value="{REPORT_MAP}" />
  </sequence></sequence></attribute>
  <attribute id="0x0207"><sequence><sequence><uint16 value="0x0409" /><uint16 value="0x0100" /></sequence></sequence></attribute>
  <attribute id="0x020b"><uint16 value="0x0100" /></attribute>
  <attribute id="0x020c"><uint16 value="0x0c80" /></attribute>
  <attribute id="0x020d"><boolean value="false" /></attribute>
  <attribute id="0x020e"><boolean value="true" /></attribute>
</record>"""


def log(msg):
    print(msg, flush=True)


def report_bytes(usages):
    """The boot-keyboard input report for this set of held keys."""
    mods = 0
    keys = []
    for u in usages:
        if MOD_FIRST <= u <= MOD_LAST:
            mods |= 1 << (u - MOD_FIRST)
        elif u:
            keys.append(u)
    if len(keys) > 6:
        raise ValueError("a keyboard report holds at most six keys")
    keys += [0] * (6 - len(keys))
    return bytes([0xA1, 0x01, mods, 0x00, *keys])


class Keyboard:
    """Accepts the phone's HID control and interrupt channels and sends reports."""

    def __init__(self):
        self.lock = threading.Lock()
        self.intr = None
        self.ctrl = None
        for psm, attr in ((PSM_CTRL, "ctrl"), (PSM_INTR, "intr")):
            s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((socket.BDADDR_ANY, psm))
            s.listen(1)
            threading.Thread(target=self._accept, args=(s, attr), daemon=True).start()

    def _accept(self, server, attr):
        while True:
            conn, addr = server.accept()
            log(f"hid {attr} connected from {addr[0]}")
            old = getattr(self, attr)
            setattr(self, attr, conn)
            if old:
                old.close()
            threading.Thread(target=self._watch, args=(conn, attr), daemon=True).start()

    def _watch(self, conn, attr):
        # Both channels: notice the phone going away. The control channel also
        # answers SET_PROTOCOL / SET_IDLE / SET_REPORT with "successful".
        try:
            while True:
                data = conn.recv(64)
                if not data:
                    break
                if attr == "ctrl" and data[0] >> 4 in (0x5, 0x7, 0x9):
                    conn.send(b"\x00")
        except OSError:
            pass
        if getattr(self, attr) is conn:
            setattr(self, attr, None)
        log(f"hid {attr} closed")

    @property
    def connected(self):
        return self.intr is not None

    def send(self, states):
        packets = [report_bytes(s) for s in states]
        with self.lock:
            if not self.connected:
                raise RuntimeError("the iPhone is not connected to the keyboard")
            for p in packets:
                self.intr.send(p)
                time.sleep(KEY_DELAY)


class Agent(dbus.service.Object):
    """Pairing agent with keyboard IO: the iPhone shows a code, the host enters it."""

    def __init__(self, bus, path, on_paired):
        super().__init__(bus, path)
        self.pending = None  # (reply, error) for an outstanding RequestPasskey
        self.on_paired = on_paired

    def supply(self, code):
        if not self.pending:
            raise RuntimeError("no pairing prompt is waiting for a code")
        reply, _ = self.pending
        self.pending = None
        GLib.idle_add(lambda: reply(dbus.UInt32(int(code))) and False)

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u",
                         async_callbacks=("reply", "error"))
    def RequestPasskey(self, device, reply, error):
        log(f"pairing: passkey requested by {device}; send op=passkey with the code the iPhone shows")
        self.pending = (reply, error)

    @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        log(f"pairing: display passkey for {device}")

    @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        log(f"pairing: confirmation accepted for {device}")

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        log(f"pairing: authorized {device}")

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        dev = dbus.Interface(dbus.SystemBus().get_object("org.bluez", device), "org.freedesktop.DBus.Properties")
        dev.Set("org.bluez.Device1", "Trusted", True)
        log(f"pairing: service {uuid} authorized, {device} trusted")
        self.on_paired()

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        raise dbus.exceptions.DBusException("org.bluez.Error.Rejected", "legacy PIN not supported")

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Cancel(self):
        log("pairing: cancelled")
        self.pending = None

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Release(self):
        pass


class Profile(dbus.service.Object):
    @dbus.service.method("org.bluez.Profile1", in_signature="", out_signature="")
    def Release(self):
        pass


class Adapter:
    def __init__(self, bus):
        self.props = dbus.Interface(bus.get_object("org.bluez", "/org/bluez/hci0"),
                                    "org.freedesktop.DBus.Properties")
        self.bus = bus

    def paired(self):
        om = dbus.Interface(self.bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager")
        return any(ifaces.get("org.bluez.Device1", {}).get("Paired")
                   for ifaces in om.GetManagedObjects().values())

    def discoverable(self, on=None):
        if on is not None:
            self.props.Set("org.bluez.Adapter1", "Discoverable", bool(on))
            log(f"discoverable {'on' if on else 'off'}")
        return bool(self.props.Get("org.bluez.Adapter1", "Discoverable"))


def setup_bluez(bus):
    mgr = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"), "org.bluez.ProfileManager1")
    Profile(bus, "/phoneharness/kbd")
    mgr.RegisterProfile("/phoneharness/kbd", "00001124-0000-1000-8000-00805f9b34fb", {
        "ServiceRecord": SDP_RECORD, "Role": "server",
        "RequireAuthentication": True, "RequireAuthorization": False,
    })
    adapter = Adapter(bus)
    agent = Agent(bus, "/phoneharness/agent", on_paired=lambda: adapter.discoverable(False))
    am = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"), "org.bluez.AgentManager1")
    am.RegisterAgent("/phoneharness/agent", "KeyboardOnly")
    am.RequestDefaultAgent("/phoneharness/agent")

    p = adapter.props
    p.Set("org.bluez.Adapter1", "Powered", True)
    p.Set("org.bluez.Adapter1", "Alias", NAME)
    p.Set("org.bluez.Adapter1", "Pairable", True)
    p.Set("org.bluez.Adapter1", "DiscoverableTimeout", dbus.UInt32(0))
    adapter.discoverable(not adapter.paired())
    return agent, adapter


def serve(kbd, agent, adapter):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_ADDR, LISTEN_PORT))
    srv.listen(4)
    log(f"control listening on {LISTEN_ADDR}:{LISTEN_PORT}")
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn, kbd, agent, adapter), daemon=True).start()


def handle(conn, kbd, agent, adapter):
    conn.settimeout(120)
    try:
        with conn, conn.makefile("rwb") as f:
            line = f.readline(1 << 20)
            try:
                req = json.loads(line)
                if not hmac.compare_digest(str(req.get("token", "")).encode(), TOKEN.encode()):
                    raise PermissionError("bad token")
                op = req.get("op")
                if op == "send":
                    states = req["reports"]
                    if not isinstance(states, list) or not all(
                            isinstance(s, list) and all(type(u) is int and 0 <= u <= 255 for u in s)
                            for s in states):
                        raise ValueError("reports must be lists of HID usages")
                    kbd.send(states)
                    out = {"ok": True}
                elif op == "passkey":
                    agent.supply(req["code"])
                    out = {"ok": True}
                elif op == "pairing":
                    out = {"ok": True, "discoverable": adapter.discoverable(bool(req["on"]))}
                elif op == "status":
                    out = {"ok": True, "connected": kbd.connected, "pairing_prompt": agent.pending is not None,
                           "discoverable": adapter.discoverable()}
                else:
                    raise ValueError("unknown op")
                log(f"control: op={op} ok")  # never what was typed
            except Exception as e:
                out = {"ok": False, "error": str(e) if not isinstance(e, KeyError) else f"missing {e}"}
                log(f"control: failed ({type(e).__name__})")
            f.write((json.dumps(out) + "\n").encode())
            f.flush()
    except OSError:
        pass


def main():
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    kbd = Keyboard()
    agent, adapter = setup_bluez(bus)
    threading.Thread(target=serve, args=(kbd, agent, adapter), daemon=True).start()
    log(f"keyboard {NAME!r} up")
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
