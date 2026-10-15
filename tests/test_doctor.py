"""`phone-harness doctor` follows connect() when a cloud iPhone is attached.

No platform argument checks that control link over HTTPS. `doctor android`
and PHONE_HARNESS_PLATFORM still check the local phone, which looks for adb.
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")


class Control(BaseHTTPRequestHandler):
    """The attached cloud iPhone: GET /ops, then the three screen ops."""
    requests = []

    def log_message(self, *args):
        pass

    def _send(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw) if raw else {}

    def _handle(self):
        body = self._body()
        Control.requests.append((self.command, self.path, body))
        if self.headers.get("Authorization") != "Bearer ct_test":
            return self._send(401, {"error": "unauthorized"})
        if self.command == "GET" and self.path == "/ops":
            return self._send(200, {"ops": ["screen.text", "screen.capture", "screen.bounds"]})
        if self.command == "POST" and self.path == "/op":
            op = body.get("op")
            if op == "screen.text":
                return self._send(200, {"result": [
                    {"text": "Settings", "confidence": 1, "source": "pixels",
                     "x": 10, "y": 20, "w": 30, "h": 40}]})
            if op == "screen.capture":
                return self._send(200, {"result": {
                    "png_b64": base64.b64encode(b"png-bytes").decode("ascii"),
                    "bounds": {"x": 0, "y": 0, "w": 750, "h": 1334, "id": "udid1"}}})
            if op == "screen.bounds":
                return self._send(200, {"result": {
                    "x": 0, "y": 0, "w": 750, "h": 1334, "id": "udid1"}})
        return self._send(404, {"error": "not found"})

    do_GET = do_POST = _handle


class DoctorCloudIPhone(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.ws = tempfile.TemporaryDirectory()
        self.addCleanup(self.ws.cleanup)
        Control.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Control)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        config = Path(self.home.name) / "config"
        config.mkdir()
        (config / "config.json").write_text(json.dumps(
            {"version": 1, "platform": "android"}))
        record = {
            "sid": "iph-1",
            "platform": "ios",
            "label": "iPhone",
            "expires_at": time.time() + 3600,
            "link": {"kind": "control", "url": self.url, "token": "ct_test"},
        }
        state = Path(self.home.name) / "state"
        state.mkdir()
        (state / "cloud.json").write_text(json.dumps(
            {"version": 1, "session": record, "sessions": {"iph-1": record}}))
        self.env = os.environ.copy()
        self.env.update({
            "PYTHONPATH": SRC + os.pathsep + self.env.get("PYTHONPATH", ""),
            "PHONE_HARNESS_HOME": self.home.name,
            "PH_AGENT_WORKSPACE": self.ws.name,
            "PHONE_HARNESS_TELEMETRY": "0",
            "PHONE_HARNESS_NO_UPDATE_CHECK": "1",
            "PHONE_HARNESS_ADB": str(Path(self.home.name) / "missing-adb"),
        })
        for name in ("PHONE_HARNESS_PLATFORM", "PHONE_HARNESS_SESSION", "BROWSER"):
            self.env.pop(name, None)

    def run_doctor(self, *args, env=None):
        merged = dict(self.env)
        if env:
            merged.update(env)
        return subprocess.run(
            [sys.executable, "-m", "phone_harness.run", *args],
            capture_output=True, text=True, env=merged)

    def test_no_platform_checks_the_attached_cloud_iphone(self):
        for args in (["doctor"], ["--doctor"]):
            with self.subTest(args=args):
                Control.requests.clear()
                got = self.run_doctor(*args)
                self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
                self.assertIn("phone-harness doctor — cloud iphone", got.stdout)
                self.assertIn("(default is android)", got.stdout)
                for label in ("control URL reachable", "screen.text",
                              "screen.capture", "screen.bounds"):
                    self.assertIn(f"[PASS] {label}", got.stdout)
                self.assertIn(f"[PASS] control URL reachable ({self.url})", got.stdout)
                self.assertIn("all clear", got.stdout)
                self.assertNotIn("adb", got.stdout.lower())
                self.assertNotIn("adb", got.stderr.lower())
                posted = [body.get("op") for method, path, body in Control.requests
                          if method == "POST" and path == "/op"]
                self.assertEqual(posted, ["screen.text", "screen.capture", "screen.bounds"])

    def test_explicit_platform_still_checks_the_local_phone(self):
        cases = (
            ("argument", ["doctor", "android"], {}),
            ("env", ["doctor"], {"PHONE_HARNESS_PLATFORM": "android"}),
            ("flag", ["--doctor", "android"], {}),
        )
        for name, args, extra in cases:
            with self.subTest(name=name):
                Control.requests.clear()
                got = self.run_doctor(*args, env=extra)
                self.assertNotEqual(got.returncode, 0, got.stdout + got.stderr)
                self.assertIn("phone-harness doctor — android", got.stdout)
                self.assertIn("[FAIL] adb found", got.stdout)
                self.assertNotIn("all clear", got.stdout)
                self.assertNotIn("cloud iphone", got.stdout)
                self.assertNotIn("control URL", got.stdout)
                self.assertNotIn("screen.text", got.stdout)
                self.assertEqual(Control.requests, [])
