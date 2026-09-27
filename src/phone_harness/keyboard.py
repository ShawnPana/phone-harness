"""A phone's hardware keyboard: a small Bluetooth keyboard next to the phone.

Apple's own Apple Account sheets drop keystrokes and paste that arrive over the
developer connection; they take a real keyboard. So a phone on the host can
have a keyboard of its own: a Raspberry Pi that the phone has paired with as a
Bluetooth keyboard (deploy/pi-keyboard). When a phone has one, every key the
host sends it goes through that keyboard, so typing works on every screen.

The keyboard is deliberately dumb. It knows one thing: send this sequence of
key states, each state being the set of HID usages held down. All knowledge of
text, key names and layouts lives here:

  text 'Ab'   -> [{shift, a}], [], [{b}], []        (US layout)
  keys 'cmd+a'-> [{cmd, a}], []
  a report    -> [{...}]   as the live preview sends it, held set and all

Wire: one JSON line per TCP connection, with the keyboard's token.
  {"token", "op": "send", "reports": [[usage, ...], ...]}
  {"token", "op": "status"}        -> {"connected": bool, "pairing_prompt": bool, "discoverable": bool}
  {"token", "op": "passkey", "code": "123456"}
  {"token", "op": "pairing", "on": bool}
Reply: {"ok": true, ...} or {"ok": false, "error": "..."}.
"""
import json
import socket
from pathlib import Path


class KeyboardError(RuntimeError):
    pass


def text_reports(text):
    """Key states that type `text` on a US keyboard, or None when some
    character has no key (emoji, other scripts): that text is pasted instead."""
    from pymobiledevice3.remote.core_device.hid_service import ASCII_TO_HID
    from .coredevice_daemon import SHIFT
    if any(c not in ASCII_TO_HID for c in text):
        return None
    reports = []
    for c in text:
        usage, shifted = ASCII_TO_HID[c]
        reports += [sorted({usage} | ({SHIFT} if shifted else set())), []]
    return reports


def combo_reports(combo):
    from .coredevice_daemon import key_usages
    return [sorted(key_usages(combo)), []]


class Keyboard:
    """The keyboard of one phone, reached at host:port with a token."""

    def __init__(self, addr, token_file):
        host, _, port = addr.rpartition(":")
        self.addr = (host, int(port))
        self.token_file = Path(token_file).expanduser()

    @classmethod
    def from_config(cls, entry):
        return cls(entry["addr"], entry["token_file"])

    def _call(self, req, timeout=60):
        req = {**req, "token": self.token_file.read_text().strip()}
        try:
            with socket.create_connection(self.addr, timeout=timeout) as s:
                s.sendall((json.dumps(req) + "\n").encode())
                line = s.makefile().readline()
        except OSError as e:
            raise KeyboardError(f"the phone's keyboard did not answer ({e.strerror or e})") from None
        try:
            reply = json.loads(line)
        except ValueError:
            raise KeyboardError("the phone's keyboard gave no answer") from None
        if not reply.get("ok"):
            raise KeyboardError(f"the phone's keyboard refused: {reply.get('error')}")
        return reply

    def send(self, reports):
        # ~12 ms per state on the keyboard; allow for long text.
        self._call({"op": "send", "reports": reports}, timeout=30 + 0.03 * len(reports))

    def status(self):
        return self._call({"op": "status"}, timeout=10)

    def passkey(self, code):
        self._call({"op": "passkey", "code": str(code)}, timeout=10)

    def pairing(self, on):
        return self._call({"op": "pairing", "on": bool(on)}, timeout=10)
