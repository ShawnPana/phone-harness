"""A phone with a keyboard: its keys go through the keyboard, from ops and from
the live preview; everything else still goes to the daemon."""
import json
import os
import socketserver
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

UDID = "00008110-TEST"


class FakeKeyboard(socketserver.StreamRequestHandler):
    calls = []
    connected = True

    def handle(self):
        req = json.loads(self.rfile.readline())
        FakeKeyboard.calls.append(req)
        if req.get("token") != "kbd-token":
            out = {"ok": False, "error": "bad token"}
        elif req["op"] == "send" and not FakeKeyboard.connected:
            out = {"ok": False, "error": "the iPhone is not connected to the keyboard"}
        else:
            out = {"ok": True, "connected": FakeKeyboard.connected}
        self.wfile.write((json.dumps(out) + "\n").encode())


class HostKeyboardTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        os.environ["PHONE_HARNESS_HOME"] = self.home.name
        from phone_harness import host
        self.hostmod = host
        self.kbd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeKeyboard)
        threading.Thread(target=self.kbd.serve_forever, daemon=True).start()
        token = Path(self.home.name) / "kbd.token"
        token.write_text("kbd-token\n")
        cfg = host.load_config()
        cfg["assignments"] = {"prof-1": UDID}
        cfg["keyboards"] = {UDID: {"addr": f"127.0.0.1:{self.kbd.server_address[1]}", "token_file": str(token)}}
        host.save_config(cfg)
        FakeKeyboard.calls, FakeKeyboard.connected = [], True

        self.daemon_calls = []
        test = self

        def fake_request(phone, op, timeout=30.0, **kw):
            test.daemon_calls.append((op, kw))
            return None
        self._patches = [(host.Phone, "request", host.Phone.request),
                         (host.Phone, "session_state", host.Phone.session_state)]
        host.Phone.request = fake_request
        host.Phone.session_state = lambda phone: "ready"
        self.phone = host.Phone(UDID)

    def tearDown(self):
        for cls, name, fn in self._patches:
            setattr(cls, name, fn)
        self.kbd.shutdown()
        self.kbd.server_close()
        os.environ.pop("PHONE_HARNESS_HOME", None)
        self.home.cleanup()

    def sent(self):
        return [c["reports"] for c in FakeKeyboard.calls if c["op"] == "send"]

    def test_text_goes_through_the_keyboard_as_key_states(self):
        self.assertTrue(self.phone.op("input.text", {"s": "Ab!"}))
        # A: shift+a; b; !: shift+1; each followed by all keys up.
        self.assertEqual(self.sent(), [[[4, 225], [], [5], [], [30, 225], []]])
        self.assertEqual(self.daemon_calls, [])

    def test_key_combos_go_through_the_keyboard(self):
        self.phone.op("input.keys", {"combo": "cmd+a"})
        self.assertEqual(self.sent(), [[[4, 227], []]])

    def test_text_no_key_can_type_is_pasted_by_the_daemon(self):
        self.phone.op("input.text", {"s": "héllo 🙂"})
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.daemon_calls[0][0], "text")

    def test_touches_still_go_to_the_daemon(self):
        self.phone.op("input.tap", {"x": 10, "y": 20})
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.daemon_calls, [("tap", {"x": 10, "y": 20})])

    def test_a_disconnected_keyboard_fails_loudly(self):
        FakeKeyboard.connected = False
        with self.assertRaises(self.hostmod.HostError) as e:
            self.phone.op("input.text", {"s": "secret"})
        self.assertEqual(e.exception.status, 503)
        self.assertIn("not connected", str(e.exception))
        self.assertEqual(self.daemon_calls, [])          # never a silent second path

    def test_without_a_keyboard_keys_go_to_the_daemon(self):
        cfg = self.hostmod.load_config()
        cfg["keyboards"] = {}
        self.hostmod.save_config(cfg)
        self.phone.op("input.text", {"s": "abc"})
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.daemon_calls[0][0], "text")

    def test_the_preview_keys_go_through_the_keyboard(self):
        host = self.hostmod.Host(self.hostmod.load_config())
        host.phones[UDID] = self.phone
        host.leases["l1"] = {"udid": UDID, "expires_at": time.time() + 60}
        token, _ = host.mint("l1", "viewer", 60)
        view, _ = host.mint("l1", "viewer", 60, mode="view")
        self.hostmod.Handler.host = host
        self.hostmod.Handler.origin = "http://test"
        srv = ThreadingHTTPServer(("127.0.0.1", 0), self.hostmod.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

        def post(tok, body):
            req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/iphone/v/{tok}/key",
                                         data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code

        self.assertEqual(post(token, {"usages": [225, 4]}), 200)
        self.assertEqual(post(token, {"usages": []}), 200)
        self.assertEqual(self.sent(), [[[4, 225]], [[]]])
        self.assertEqual(post(token, {"usages": ["x"]}), 400)
        self.assertEqual(post(view, {"usages": [4]}), 403)       # a watch link cannot type
        self.assertEqual(len(self.sent()), 2)


if __name__ == "__main__":
    unittest.main()
