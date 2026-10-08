"""`phone-harness cloud` against a fake API and a fake adb. No network, no credits.

    python -m unittest discover tests
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")

FAKE_ADB = r'''#!/bin/sh
# Locked until `shell unlock CODE`; state is a file next to this script.
DIR="$(dirname "$0")"
echo "$@" >> "$DIR/adb.log"
[ "$1" = "-s" ] && shift 2
case "$1" in
  connect)    touch "$DIR/connected"; echo "connected to $2" ;;
  disconnect) rm -f "$DIR/connected" "$DIR/unlocked"; echo "disconnected $2" ;;
  shell)
    [ -f "$DIR/connected" ] || { echo "adb: device not found" >&2; exit 1; }
    if [ "$2" = "unlock" ]; then
      [ "$3" = "ph_code" ] && { touch "$DIR/unlocked"; echo unlocked; } || echo "wrong code"
    elif [ -f "$DIR/unlocked" ]; then shift; sh -c "$*"
    else echo locked; fi ;;
esac
'''


class FakeCloud(BaseHTTPRequestHandler):
    sessions, profile, polls, posts = {}, {}, {}, []
    valid, token_polls, refreshes, revoked = set(), 0, 0, []
    closing_reads = 0
    seen_headers = []
    forbid = False
    control_url = None            # set: an iPhone's control grant points here
    available = None              # set: GET /me includes `available`
    fail = None                   # set: POST /sessions returns this instead

    def _oauth(self, path, form):
        c = FakeCloud
        if path == "/oauth/device_authorization":
            assert form["client_id"] == "cli-client" and "offline_access" in form["scope"]
            return self._send(200, {"device_code": "dev-1", "user_code": "BCDF-GHJK",
                                    "verification_uri": "https://accounts.example/device",
                                    "expires_in": 600, "interval": 0})
        if path == "/oauth/token/revoke":
            c.revoked.append(form["token"])
            c.valid.discard(form["token"])
            return self._send(200, {})
        if form["grant_type"] == "refresh_token":
            if form["refresh_token"] != "rt-1":
                return self._send(400, {"error": "invalid_grant"})
            c.refreshes += 1
            token = f"at-refreshed-{c.refreshes}"
        else:
            c.token_polls += 1
            if c.token_polls < 2:                       # the user has not clicked yet
                return self._send(400, {"error": "authorization_pending"})
            token = "at-1"
        c.valid.add(token)
        return self._send(200, {"access_token": token, "refresh_token": "rt-1",
                                "expires_in": 86400})

    def log_message(self, *a):
        pass

    def _send(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self):
        c = FakeCloud
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        path, m = self.path.split("?")[0], self.command
        if path.startswith("/oauth/"):
            return self._oauth(path, dict(urllib.parse.parse_qsl(raw.decode())))
        c.seen_headers.append(dict(self.headers))
        if self.headers.get("Authorization", "")[7:] not in c.valid:
            return self._send(401, {"error": "unauthorized"})
        body = json.loads(raw) if raw else {}
        if (m, path) == ("GET", "/me") and c.forbid:
            return self._send(403, {"error": "beta access required", "code": "beta_access_required"})
        if (m, path) == ("GET", "/me"):
            if c.closing_reads:
                c.closing_reads -= 1
                if not c.closing_reads:
                    c.sessions.pop(c.profile["session"], None)
                    c.profile.update(state="stored", session=None)
            me = {"uid": "u1", "email": "a@b.c", "balance_cents": 425,
                  "price_cents_per_minute": 5, "can_rent": True,
                  "active_session_count": len(c.sessions),
                  "session_limit": 3, "profile": c.profile,
                  "default": {"platform": "android", "kind": "emulator"}}
            if c.available is not None:
                me["available"] = c.available
            return self._send(200, me)
        if (m, path) == ("POST", "/sessions"):
            c.posts.append(body)
            if c.fail:
                status, payload, extra = c.fail
                if extra:
                    self.send_response(status)
                    raw = json.dumps(payload).encode()
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    for k, v in extra.items():
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                return self._send(status, payload)
            if "provider" in body:
                return self._send(400, {"error": "provider was replaced by platform and kind"})
            if ("platform" in body) ^ ("kind" in body):
                return self._send(400, {"error": "platform and kind go together"})
            iphone = body.get("platform") == "ios" and body.get("kind") == "device"
            if body.get("profile_id") and c.profile["state"] == "running":
                return self._send(409, {"error": "held", "code": "profile_running",
                                        "session": c.profile["session"]})
            if iphone and isinstance(c.available, list):
                phones = [p for p in c.available
                          if p.get("platform") == "ios" and p.get("kind") == "device"]
                if not phones:
                    return self._send(400, {"error": "no phone of that platform and kind "
                                            "available to this account"})
                chosen = body.get("device_id")
                match = [p for p in phones if p.get("id") == chosen] if chosen else phones[:1]
                if chosen and not match:
                    return self._send(400, {"error": "no grant for that phone"})
                phone = match[0]
                if phone.get("state") == "unavailable":
                    return self._send(409, {"error": "not on its host", "code": "device_unavailable"})
                if phone.get("state") == "running":
                    return self._send(409, {"error": "held", "code": "profile_running",
                                            "session": phone.get("session") or "liveiphone"})
            sid = f"sid{len(c.posts):03d}"
            c.sessions[sid] = {"id": sid, "state": "provisioning",
                               "platform": "ios" if iphone else "android",
                               "kind": "device" if iphone else "emulator",
                               "device": "iPhone" if iphone else "Android emulator",
                               "billing_mode": "disabled" if iphone else "metered",
                               # Present on an iPhone too; the CLI must not treat that as a save.
                               "profile": "prof-1" if iphone else body.get("profile_id"),
                               "phone": body.get("device_id"),
                               "expires_at": time.time() + body["timeout_seconds"],
                               "watch_url": "https://watch.example/secret"}
            if body.get("profile_id"):
                c.profile.update(state="running", session=sid)
            return self._send(202, c.sessions[sid])
        if (m, path) == ("GET", "/sessions"):
            return self._send(200, list(c.sessions.values()))
        sid = path.rsplit("/", 1)[-1]
        if path.startswith("/sessions/") and sid in c.sessions:
            if m == "GET":
                c.polls[sid] = c.polls.get(sid, 0) + 1
                if c.polls[sid] >= 2 and c.sessions[sid]["state"] == "provisioning":
                    c.sessions[sid].update(state="ready", startup={"startup": "exact"})
                    if c.sessions[sid].get("platform") == "ios":
                        c.sessions[sid]["control"] = {
                            "url": c.control_url or "http://127.0.0.1:1", "token": "ct_1",
                            "expires_at": c.sessions[sid]["expires_at"]}
                    else:
                        c.sessions[sid]["adb"] = {"host": "live.example", "port": 22220, "code": "ph_code"}
                return self._send(200, c.sessions[sid])
            if m == "DELETE":
                if c.sessions.pop(sid).get("profile"):
                    c.profile.update(state="stored", session=None, saved_at=time.time())
                return self._send(200, {"released": True})
        return self._send(404, {"error": "not found"})

    do_GET = do_POST = do_DELETE = _handle


class FakeControl(BaseHTTPRequestHandler):
    """What a phone driven by ops over HTTPS answers: /ops, /op, /frame.png."""
    calls = []
    PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                        "0000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")

    def log_message(self, *a):
        pass

    def _send(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self):
        import base64
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else {}
        if self.headers.get("Authorization") != "Bearer ct_1":
            return self._send(404, {"error": "not found"})
        if self.path == "/ops":
            # nav.back is advertised so the client actually POSTs it; the op
            # answer below is the unsupported 400 the live iPhone returns.
            return self._send(200, {"ops": ["screen.capture", "screen.bounds", "input.tap",
                                            "nav.home", "nav.back"]})
        if self.path == "/op":
            FakeControl.calls.append(body)
            op = body["op"]
            if op == "screen.capture":
                return self._send(200, {"result": {"png_b64": base64.b64encode(self.PNG).decode(),
                                                   "bounds": {"x": 0, "y": 0, "w": 750, "h": 1334, "id": "udid1"}}})
            if op == "screen.bounds":
                return self._send(200, {"result": {"x": 0, "y": 0, "w": 750, "h": 1334, "id": "udid1"}})
            if op == "input.tap":
                return self._send(200, {"result": True})
            if op == "nav.back":
                return self._send(400, {"error": "iOS has no back", "unsupported": True})
            return self._send(400, {"error": f"cannot {op}", "unsupported": True})
        return self._send(404, {"error": "not found"})

    do_GET = do_POST = _handle


class CloudCli(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        adb = Path(self.home.name) / "adb"
        adb.write_text(FAKE_ADB)
        adb.chmod(adb.stat().st_mode | stat.S_IEXEC)
        FakeCloud.sessions, FakeCloud.polls, FakeCloud.posts = {}, {}, []
        FakeCloud.valid, FakeCloud.token_polls = set(), 0
        FakeCloud.refreshes, FakeCloud.revoked, FakeCloud.closing_reads = 0, [], 0
        FakeCloud.forbid = False
        FakeCloud.control_url = None
        FakeCloud.available = None
        FakeCloud.fail = None
        FakeCloud.profile = {"id": "prof-1", "state": "stored", "session": None,
                             "saved_at": time.time() - 7200}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeCloud)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.env = {**os.environ, "PYTHONPATH": SRC, "PHONE_HARNESS_HOME": self.home.name,
                    "PHONE_HARNESS_ADB": str(adb), "PHONE_HARNESS_TELEMETRY": "0",
                    "PHONE_HARNESS_CLOUD_API": f"http://127.0.0.1:{self.server.server_port}",
                    "PHONE_HARNESS_CLOUD_OAUTH_ISSUER": f"http://127.0.0.1:{self.server.server_port}",
                    "PHONE_HARNESS_CLOUD_OAUTH_CLIENT_ID": "cli-client"}
        for name in ("PHONE_HARNESS_API_KEY", "ANDROID_SERIAL", "PHONE_HARNESS_PLATFORM"):
            self.env.pop(name, None)

    def run_cli(self, *args, stdin=None, env=None):
        return subprocess.run([sys.executable, "-m", "phone_harness.run", *args], input=stdin,
                              capture_output=True, text=True, env={**self.env, **(env or {})})

    def login(self):
        r = self.run_cli("cloud", "login", "--no-browser")
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def adb_log(self):
        return (Path(self.home.name) / "adb.log").read_text()

    def test_needs_login(self):
        r = self.run_cli("cloud", "start")
        self.assertEqual(r.returncode, 1)
        self.assertIn("phone-harness cloud login", r.stderr)

    def auth(self):
        return json.loads((Path(self.home.name) / "config" / "auth.json").read_text())

    def test_login_is_a_device_flow_and_the_tokens_stay_private(self):
        r = self.login()
        self.assertIn("BCDF-GHJK", r.stdout)
        self.assertIn("https://accounts.example/device", r.stdout)
        self.assertIn("Signed in as a@b.c", r.stdout)
        self.assertNotIn("at-1", r.stdout)
        self.assertEqual(self.auth()["access_token"], "at-1")
        path = Path(self.home.name) / "config" / "auth.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_an_expired_or_refused_token_is_refreshed_silently(self):
        self.login()
        record = {**self.auth(), "expires_at": time.time() - 5}
        (Path(self.home.name) / "config" / "auth.json").write_text(json.dumps(record))
        self.assertIn("a@b.c", self.run_cli("cloud", "whoami").stdout)
        self.assertEqual(self.auth()["access_token"], "at-refreshed-1")
        FakeCloud.valid.clear()                          # refused before its expiry
        self.assertIn("a@b.c", self.run_cli("cloud", "whoami").stdout)
        self.assertEqual(FakeCloud.refreshes, 2)

    def test_logout_revokes_and_forgets(self):
        self.login()
        self.assertEqual(self.run_cli("cloud", "logout").returncode, 0)
        self.assertEqual(FakeCloud.revoked, ["rt-1", "at-1"])
        self.assertIn("cloud login", self.run_cli("cloud", "whoami").stderr)

    def test_a_dotenv_in_the_agent_workspace_fills_in_unset_variables(self):
        FakeCloud.valid.add("pck_ci")
        ws = Path(self.home.name) / "workspace"
        ws.mkdir()
        (ws / ".env").write_text("# per-machine overrides\n"
                                 "PHONE_HARNESS_API_KEY=pck_ci\n"
                                 "PHONE_HARNESS_CLOUD_API='http://127.0.0.1:1'\n")   # loses to the real env
        r = self.run_cli("cloud", "whoami", env={"PH_AGENT_WORKSPACE": str(ws)})
        self.assertIn("a@b.c", r.stdout, r.stderr)

    def test_a_gate_header_rides_along_on_every_request(self):
        FakeCloud.valid.add("pck_ci")
        FakeCloud.seen_headers = []
        r = self.run_cli("cloud", "whoami", env={"PHONE_HARNESS_API_KEY": "pck_ci",
                                                  "PHONE_HARNESS_CLOUD_GATE_HEADER": "X-Gate-Authorization: Bearer gate-1"})
        self.assertIn("a@b.c", r.stdout)
        self.assertIn("Bearer gate-1", [h.get("X-Gate-Authorization") for h in FakeCloud.seen_headers])
        self.assertNotIn("gate-1", r.stdout)

    def test_an_api_key_in_the_environment_is_used_for_ci(self):
        FakeCloud.valid.add("pck_ci")
        r = self.run_cli("cloud", "whoami", env={"PHONE_HARNESS_API_KEY": "pck_ci"})
        self.assertIn("a@b.c", r.stdout)

    def test_start_uses_the_saved_phone_and_connects(self):
        self.login()
        r = self.run_cli("cloud", "start", "--minutes", "5")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(FakeCloud.posts, [{"timeout_seconds": 300, "profile_id": "prof-1"}])
        self.assertIn("Resumed exactly", r.stdout)
        self.assertNotIn("ph_code", r.stdout)
        self.assertNotIn("watch.example", r.stdout)
        log = self.adb_log()
        self.assertIn("connect live.example:22220", log)
        self.assertIn("shell unlock ph_code", log)

        again = self.run_cli("cloud", "start")            # idempotent: no second phone
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(len(FakeCloud.posts), 1)

        ls = self.run_cli("cloud", "ls")
        self.assertIn("* sid001", ls.stdout)

    def test_start_opens_the_live_view_unless_told_not_to(self):
        self.login()
        opened = Path(self.home.name) / "opened-urls"
        # a fake browser: `webbrowser` honours $BROWSER, so point it at a script that records the url
        fake = Path(self.home.name) / "browser"
        fake.write_text(f'#!/bin/sh\necho "$1" >> "{opened}"\n')
        fake.chmod(0o755)
        r = self.run_cli("cloud", "start", env={"BROWSER": str(fake)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("opened in your browser", r.stdout)
        self.assertEqual(opened.read_text().strip(), "https://watch.example/secret")
        self.assertNotIn("watch.example", r.stdout)            # the url itself is never printed
        again = self.run_cli("cloud", "start", env={"BROWSER": str(fake)})   # reattach: no second tab
        self.assertEqual(len(opened.read_text().split()), 1, again.stdout)
        self.run_cli("cloud", "stop")
        r = self.run_cli("cloud", "start", "--no-watch", env={"BROWSER": str(fake)})
        self.assertIn("phone-harness cloud watch", r.stdout)
        self.assertEqual(len(opened.read_text().split()), 1)
        self.run_cli("cloud", "stop")
        r = self.run_cli("cloud", "start", env={"BROWSER": str(fake), "PHONE_HARNESS_CLOUD_WATCH": "0"})
        self.assertIn("phone-harness cloud watch", r.stdout)
        self.assertEqual(len(opened.read_text().split()), 1)

    def test_open_is_the_dashboard_not_the_watch_link(self):
        self.login()
        r = self.run_cli("cloud", "open", "--print")
        self.assertEqual(r.stdout.strip(), "https://phone-harness.com/dashboard")
        self.assertNotIn("watch", r.stdout)

    def test_the_uninvited_are_pointed_at_the_waitlist_with_a_source(self):
        r = self.run_cli("cloud")                                # not signed in
        self.assertIn("phone-harness.com/cloud?source=cli", r.stdout)
        FakeCloud.forbid = True
        r = self.run_cli("cloud", "login", "--no-browser")       # signed in at Clerk, not invited
        self.assertEqual(r.returncode, 1)
        self.assertIn("isn't enabled for phones yet", r.stderr)
        self.assertIn("?source=cli", r.stderr)
        FakeCloud.forbid = False
        r = self.login()                                         # invited: no waitlist talk after sign-in
        self.assertNotIn("account yet", r.stdout.split("Signed in as")[1])

    def test_status_says_saving_while_the_session_closes(self):
        self.login()
        self.run_cli("cloud", "start")
        sid = json.loads((Path(self.home.name) / "state" / "cloud.json").read_text())["session"]["sid"]
        FakeCloud.sessions[sid]["state"] = "closing"
        FakeCloud.profile.update(state="running", session=sid)
        r = self.run_cli("cloud")
        self.assertIn("phone       saving", r.stdout)
        self.assertIn("closing — saving the phone", r.stdout)
        self.assertNotIn("running in session", r.stdout)

    def test_a_closing_temporary_android_stays_plain_closing(self):
        """A temp session has no profile, so closing it must not look like a save
        and must not relabel the stored phone."""
        self.login()
        self.assertEqual(self.run_cli("cloud", "start", "--temp").returncode, 0)
        sid = json.loads((Path(self.home.name) / "state" / "cloud.json").read_text())["session"]["sid"]
        self.assertFalse(FakeCloud.sessions[sid].get("profile"))
        FakeCloud.sessions[sid]["state"] = "closing"
        r = self.run_cli("cloud")
        self.assertIn("· closing ·", r.stdout)
        self.assertNotIn("saving", r.stdout)
        self.assertIn("phone       stored", r.stdout)
        self.assertIn("temporary", r.stdout)

    def test_a_closing_iphone_is_not_a_save(self):
        """An iPhone session can name a profile; closing it still is not a save."""
        self.login()
        started = self.run_cli("cloud", "start", "--iphone", "--no-watch",
                               env={"PHONE_HARNESS_ADB": "/nonexistent/adb"})
        self.assertEqual(started.returncode, 0, started.stderr)
        sid = json.loads((Path(self.home.name) / "state" / "cloud.json").read_text())["session"]["sid"]
        self.assertEqual(FakeCloud.sessions[sid].get("profile"), "prof-1")
        FakeCloud.sessions[sid]["state"] = "closing"
        r = self.run_cli("cloud")
        self.assertIn("· closing ·", r.stdout)
        self.assertIn("your iPhone", r.stdout)
        self.assertNotIn("saving", r.stdout)
        self.assertIn("phone       stored", r.stdout)

    def test_temp_phone_and_minutes_cap(self):
        self.login()
        self.assertEqual(self.run_cli("cloud", "start", "--temp").returncode, 0)
        self.assertEqual(FakeCloud.posts, [{"timeout_seconds": 900}])
        over = self.run_cli("cloud", "start", "--minutes", "999")
        self.assertEqual(over.returncode, 1)
        self.assertIn("between 1 and 30", over.stderr)

    def test_attaches_when_the_phone_already_runs_elsewhere(self):
        self.login()
        FakeCloud.sessions["dash01"] = {
            "id": "dash01", "state": "ready", "profile": "prof-1",
            "expires_at": time.time() + 600,
            "adb": {"host": "live.example", "port": 22220, "code": "ph_code"}}
        FakeCloud.profile.update(state="running", session="dash01")
        r = self.run_cli("cloud", "start")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already running", r.stdout)
        self.assertEqual(FakeCloud.posts, [])

    def test_helpers_drive_the_rented_phone_and_relock_is_healed(self):
        self.login()
        self.run_cli("cloud", "start")
        home = self.home.name
        script = ("import os\n"
                  "from phone_harness import transport\n"
                  "p = transport.connect()\n"
                  "print(p.name, p._sh('echo hello').strip())\n"
                  f"os.unlink({home!r} + '/unlocked')\n"            # relocked mid-script
                  "print(p._sh('echo again').strip())\n"
                  f"os.unlink({home!r} + '/connected')\n"           # dropped mid-script
                  "print(p._sh('echo back').strip())\n")
        r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                           env=self.env)
        self.assertEqual(r.stdout.split(), ["android", "hello", "again", "back"], r.stderr)
        (Path(home) / "unlocked").unlink()                # and between two runs
        r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                           env=self.env)
        self.assertEqual(r.stdout.split()[:2], ["android", "hello"], r.stderr)

    def test_a_control_session_needs_no_adb_and_the_helpers_drive_it_over_https(self):
        control = ThreadingHTTPServer(("127.0.0.1", 0), FakeControl)
        threading.Thread(target=control.serve_forever, daemon=True).start()
        self.addCleanup(control.server_close)
        self.addCleanup(control.shutdown)
        FakeCloud.control_url = f"http://127.0.0.1:{control.server_port}"
        FakeControl.calls = []
        self.login()
        r = self.run_cli("cloud", "start", "--iphone", "--no-watch", env={"PHONE_HARNESS_ADB": "/nonexistent/adb"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(FakeCloud.posts[0]["platform"], "ios")
        self.assertEqual(FakeCloud.posts[0]["kind"], "device")
        self.assertEqual(FakeCloud.posts[0]["timeout_seconds"], 900)
        self.assertNotIn("provider", FakeCloud.posts[0])
        self.assertNotIn("profile_id", FakeCloud.posts[0])
        self.assertNotIn("device_id", FakeCloud.posts[0])
        self.assertIn("Starting your iPhone", r.stdout)
        self.assertIn("control", r.stdout)
        self.assertNotIn("adb", r.stdout)
        self.assertFalse((Path(self.home.name) / "adb.log").exists())     # never touched
        status = self.run_cli("cloud")
        self.assertIn("your iPhone", status.stdout)
        self.assertIn("drive it over HTTPS", status.stdout)
        self.assertNotIn("ct_1", status.stdout + r.stdout)                # the grant stays private

        script = ("from phone_harness import transport\n"
                  "from phone_harness.helpers import tap, Unsupported\n"
                  "p = transport.connect()\n"
                  "print(p.name)\n"
                  "tap(10, 20)\n"
                  "path, win = p.send('screen.capture')\n"
                  "print(open(path, 'rb').read()[:4] == b'\\x89PNG', win['w'])\n"
                  "try:\n"
                  "    p.send('nav.back')\n"
                  "except Unsupported:\n"
                  "    print('unsupported')\n"
                  "print(p.supports('tree'), p.supports('apps.current'))\n")
        r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=self.env)
        self.assertEqual(r.stdout.split(), ["remote", "True", "750", "unsupported", "False", "False"], r.stderr)
        self.assertEqual([c["op"] for c in FakeControl.calls][:2], ["input.tap", "screen.capture"])
        self.assertIn("nav.back", [c["op"] for c in FakeControl.calls])

        stop = self.run_cli("cloud", "stop")
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertNotIn("saved", stop.stdout)                            # an iPhone keeps its data
        self.assertIn("none attached", self.run_cli("cloud").stdout)

    def test_a_session_saved_by_the_android_cli_still_attaches(self):
        """main writes host/port/code and no link. An upgrade keeps that phone."""
        state = Path(self.home.name) / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "cloud.json").write_text(json.dumps({"session": {
            "sid": "sid009", "host": "live.example", "port": 22220, "code": "ph_code",
            "expires_at": time.time() + 600}}))
        r = subprocess.run([sys.executable, "-c", "from phone_harness import cloud; print(cloud.attached())"],
                           capture_output=True, text=True, env=self.env)
        self.assertIn("'kind': 'adb'", r.stdout)
        self.assertIn("live.example", r.stdout)
        self.assertNotEqual(r.stdout.strip(), "None", r.stderr)

    def test_start_waits_out_a_phone_that_is_still_closing(self):
        self.login()
        FakeCloud.sessions["old001"] = {"id": "old001", "state": "closing", "profile": "prof-1",
                                       "expires_at": time.time() + 600}
        FakeCloud.profile.update(state="running", session="old001")
        FakeCloud.closing_reads = 2                       # then it is saved
        r = self.run_cli("cloud", "start")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("still saving", r.stdout)
        self.assertEqual(FakeCloud.posts, [{"timeout_seconds": 900, "profile_id": "prof-1"}])

    def test_stop_returns_at_once_and_detaches(self):
        self.login()
        self.run_cli("cloud", "start")
        started = time.monotonic()
        r = self.run_cli("cloud", "stop")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(time.monotonic() - started, 2.5)             # no polling for the save
        self.assertIn("billing stopped", r.stdout)
        self.assertIn("being saved", r.stdout)
        self.assertEqual(FakeCloud.sessions, {})
        state = json.loads((Path(self.home.name) / "state" / "cloud.json").read_text())
        self.assertNotIn("session", state)
        self.assertIn("none attached", self.run_cli("cloud").stdout)

    def test_stop_wait_watches_the_save(self):
        self.login()
        self.run_cli("cloud", "start")
        r = self.run_cli("cloud", "stop", "--wait")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("stored", r.stdout)

    def test_a_temporary_phone_has_nothing_to_save(self):
        self.login()
        self.run_cli("cloud", "start", "--temp")
        r = self.run_cli("cloud", "stop")
        self.assertNotIn("saved", r.stdout)

    def test_iphone_flags_and_create_errors(self):
        self.login()
        both = self.run_cli("cloud", "start", "--iphone", "--temp")
        self.assertNotEqual(both.returncode, 0)
        self.assertIn("--temp", both.stderr)
        bare = self.run_cli("cloud", "start", "--device", "iph-a")
        self.assertNotEqual(bare.returncode, 0)
        self.assertIn("--iphone", bare.stderr)
        self.assertEqual(FakeCloud.posts, [])

        FakeCloud.available = [
            {"platform": "ios", "kind": "device", "id": "iph-a", "label": "SE", "state": "available"},
            {"platform": "ios", "kind": "device", "id": "iph-b", "label": "Pro", "state": "unavailable"},
        ]
        many = self.run_cli("cloud", "start", "--iphone", "--no-watch")
        self.assertNotEqual(many.returncode, 0)
        self.assertIn("iph-a", many.stderr)
        self.assertIn("Pro", many.stderr)
        self.assertIn("--device", many.stderr)
        self.assertEqual(FakeCloud.posts, [])

        missing = self.run_cli("cloud", "start", "--iphone", "--device", "nope", "--no-watch")
        self.assertIn("nope", missing.stderr)
        self.assertIn("granted", missing.stderr)
        self.assertEqual(FakeCloud.posts, [])

        down = self.run_cli("cloud", "start", "--iphone", "--device", "iph-b", "--no-watch")
        self.assertNotEqual(down.returncode, 0)
        self.assertIn("not on its host", down.stderr)

        FakeCloud.available = [
            {"platform": "ios", "kind": "device", "id": "iph-a", "label": "SE",
             "state": "running", "session": "live1"}]
        held = self.run_cli("cloud", "start", "--iphone", "--no-watch")
        self.assertNotEqual(held.returncode, 0)
        self.assertIn("cloud use live1", held.stderr)
        self.assertIn("cloud stop live1", held.stderr)

        FakeCloud.available = [
            {"platform": "android", "kind": "emulator", "label": "Android emulator", "temporary": True}]
        before = len(FakeCloud.posts)
        none = self.run_cli("cloud", "start", "--iphone", "--no-watch")
        self.assertIn("no iPhone", none.stderr)
        self.assertEqual(len(FakeCloud.posts), before)

        FakeCloud.available = None
        FakeCloud.fail = (400, {"error": "no phone of that platform and kind available to this account"}, None)
        hinted = self.run_cli("cloud", "start", "--iphone", "--no-watch")
        self.assertIn("no iPhone available", hinted.stderr)
        FakeCloud.fail = (409, {"error": "limit", "code": "session_limit",
                                "active_session_count": 2, "session_limit": 2}, None)
        limited = self.run_cli("cloud", "start", "--iphone", "--no-watch")
        self.assertIn("2 of 2", limited.stderr)
        FakeCloud.fail = (503, {"error": "busy", "code": "phones_busy"}, {"Retry-After": "12"})
        busy = self.run_cli("cloud", "start", "--iphone", "--no-watch")
        self.assertIn("Try again in 12s", busy.stderr)
        FakeCloud.fail = (403, {"error": "blocked", "code": "account_blocked"}, None)
        blocked = self.run_cli("cloud", "start", "--iphone", "--no-watch")
        self.assertIn("Rentals are blocked", blocked.stderr)
        self.assertNotIn("isn't enabled", blocked.stderr)
        FakeCloud.fail = None

        FakeCloud.available = [
            {"platform": "ios", "kind": "device", "id": "iph-a", "label": "SE", "state": "available"}]
        r = self.run_cli("cloud", "start", "--iphone", "--timeout", "2m", "--no-watch")
        self.assertEqual(r.returncode, 0, r.stderr)
        sent = [p for p in FakeCloud.posts if p.get("platform") == "ios"][-1]
        self.assertEqual(sent["timeout_seconds"], 120)
        self.assertNotIn("device_id", sent)
        named = self.run_cli("cloud", "start", "--iphone", "--device", "iph-a", "--timeout", "45s", "--no-watch")
        # already attached to the only iPhone, so this reuses and does not post again
        self.assertEqual(named.returncode, 0, named.stderr)
        self.assertIn("reusing", named.stdout)
        self.run_cli("cloud", "stop")
        posted = self.run_cli("cloud", "start", "--iphone", "--device", "iph-a", "--timeout", "45", "--no-watch")
        self.assertEqual(posted.returncode, 0, posted.stderr)
        self.assertEqual(FakeCloud.posts[-1]["device_id"], "iph-a")
        self.assertEqual(FakeCloud.posts[-1]["timeout_seconds"], 45)
        self.run_cli("cloud", "stop")
        over = self.run_cli("cloud", "start", "--iphone", "--timeout", "90m", "--no-watch")
        self.assertNotEqual(over.returncode, 0)
        self.assertIn("30 minutes", over.stderr)
        clash = self.run_cli("cloud", "start", "--minutes", "5", "--timeout", "90")
        self.assertIn("not both", clash.stderr)

    def test_android_timeout_keeps_the_profile_body(self):
        self.login()
        r = self.run_cli("cloud", "start", "--timeout", "120s", "--no-watch")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(FakeCloud.posts, [{"timeout_seconds": 120, "profile_id": "prof-1"}])

    def test_an_iphone_and_an_android_can_run_together(self):
        self.login()
        first = self.run_cli("cloud", "start", "--minutes", "5", "--no-watch")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_cli("cloud", "start", "--iphone", "--no-watch")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("stays up", second.stdout)
        self.assertEqual(FakeCloud.posts[1]["platform"], "ios")
        self.assertEqual(FakeCloud.posts[1]["kind"], "device")
        self.assertEqual(len(FakeCloud.sessions), 2)
        back = self.run_cli("cloud", "start", "--no-watch")
        self.assertEqual(back.returncode, 0, back.stderr)
        self.assertIn("already running", back.stdout)
        self.assertEqual(len(FakeCloud.posts), 2)          # attached to the Android; no third phone
        self.assertIn("your phone", back.stdout)
        self.assertNotIn("your iPhone", back.stdout)


class ControlBase(unittest.TestCase):
    def test_rewrites_the_control_url_only_when_asked(self):
        from phone_harness.remote import Remote
        os.environ.pop("PHONE_HARNESS_CLOUD_CONTROL_BASE", None)
        self.assertEqual(Remote("https://public.example/phones/abc", "t").url,
                         "https://public.example/phones/abc")
        os.environ["PHONE_HARNESS_CLOUD_CONTROL_BASE"] = "http://127.0.0.1:9"
        try:
            self.assertEqual(Remote("https://public.example/phones/abc", "t").url,
                             "http://127.0.0.1:9/phones/abc")
        finally:
            os.environ.pop("PHONE_HARNESS_CLOUD_CONTROL_BASE", None)


if __name__ == "__main__":
    unittest.main()
