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
SUPPORT = str(Path(__file__).resolve().parent / "support")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
if SUPPORT not in sys.path:
    sys.path.insert(0, SUPPORT)
import browser_recorder
browser_recorder.install()

from phone_harness import telemetry  # noqa: E402

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


def _fake_catalog(c):
    """The account catalog. Tests that leave `available` unset get the saved
    Android plus a temporary phone, which is what a normal account holds."""
    if c.available is not None:
        return c.available
    return [
        {"platform": "android", "kind": "emulator", "id": c.profile["id"],
         "label": "Saved phone", "state": c.profile.get("state") or "stored"},
        {"platform": "android", "kind": "emulator", "temporary": True,
         "label": "Temporary phone"},
    ]


UNNAMED = {"platform": "android", "kind": "emulator", "temporary": True}


def _fake_resolve(c, body):
    """What the service makes of a create body. `posts` keeps the raw body.
    A body that names nothing gets the service's rule: a temporary Android,
    never anyone's saved phone."""
    if "platform" in body or "kind" in body:
        return dict(body)
    return {**body, "platform": "android", "kind": "emulator"}


class FakeCloud(BaseHTTPRequestHandler):
    sessions, profile, polls, posts, keys = {}, {}, {}, [], []
    requests = []                 # (method, path, headers with lowercase names)
    valid, token_polls, refreshes, revoked = set(), 0, 0, []
    closing_reads = 0
    forbid = False
    control_url = None            # set: an iPhone's control grant points here
    available = None              # set: replaces the synthesized catalog
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
        c.requests.append((m, path, {k.lower(): v for k, v in self.headers.items()}))
        if path.startswith("/oauth/"):
            return self._oauth(path, dict(urllib.parse.parse_qsl(raw.decode())))
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
                  "unnamed": dict(UNNAMED),
                  "available": _fake_catalog(c)}
            return self._send(200, me)
        if (m, path) == ("POST", "/sessions"):
            c.keys.append(self.headers.get("Idempotency-Key"))
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
            resolved = _fake_resolve(c, body)
            iphone = resolved.get("platform") == "ios" and resolved.get("kind") == "device"
            if resolved.get("profile_id") and c.profile["state"] == "running":
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
                               "platform": "ios" if iphone else (resolved.get("platform") or "android"),
                               "kind": "device" if iphone else (resolved.get("kind") or "emulator"),
                               "device": "iPhone" if iphone else "Android emulator",
                               "billing_mode": "disabled" if iphone else "metered",
                               "profile": resolved.get("profile_id"),
                               "device_id": resolved.get("device_id"),
                               "phone": resolved.get("device_id"),
                               "expires_at": time.time() + body["timeout_seconds"],
                               "watch_url": "https://watch.example/secret"}
            if resolved.get("profile_id"):
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
    requests = []             # (method, path, headers with lowercase names)
    launch_error = None       # set: apps.launch returns this JSON body with HTTP 400
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
        FakeControl.requests.append((self.command, self.path,
                                     {k.lower(): v for k, v in self.headers.items()}))
        if self.headers.get("Authorization") != "Bearer ct_1":
            return self._send(404, {"error": "not found"})
        if self.path == "/ops":
            # nav.back is advertised so the client actually POSTs it; the op
            # answer below is the unsupported 400 the live iPhone returns.
            return self._send(200, {"ops": ["screen.capture", "screen.bounds", "input.tap",
                                            "nav.home", "nav.back", "apps.launch"]})
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
            if op == "apps.launch" and FakeControl.launch_error:
                return self._send(400, FakeControl.launch_error)
            return self._send(400, {"error": f"cannot {op}", "unsupported": True})
        return self._send(404, {"error": "not found"})

    do_GET = do_POST = _handle


class FakePostHog(BaseHTTPRequestHandler):
    """PostHog's capture endpoint: every event the CLI's detached sender posts."""
    events = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        FakePostHog.events.append(json.loads(self.rfile.read(n)))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


# What each coding agent sets; a test that checks the client must not inherit one.
AGENT_MARKERS = [m.partition("=")[0] for m, _ in telemetry._AGENT_ENV_MARKERS] + ["PH_CLIENT"]


class CloudCli(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        adb = Path(self.home.name) / "adb"
        adb.write_text(FAKE_ADB)
        adb.chmod(adb.stat().st_mode | stat.S_IEXEC)
        FakeCloud.sessions, FakeCloud.polls, FakeCloud.posts, FakeCloud.keys = {}, {}, [], []
        FakeCloud.requests, FakeControl.requests = [], []
        FakeCloud.valid, FakeCloud.token_polls = set(), 0
        FakeCloud.refreshes, FakeCloud.revoked, FakeCloud.closing_reads = 0, [], 0
        FakeCloud.forbid = False
        FakeCloud.control_url = None
        FakeCloud.available = None
        FakeCloud.fail = None
        FakeControl.launch_error = None
        FakeCloud.profile = {"id": "prof-1", "state": "stored", "session": None,
                             "saved_at": time.time() - 7200}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeCloud)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.env = {**os.environ, "PYTHONPATH": SUPPORT + os.pathsep + SRC,
                    "PHONE_HARNESS_HOME": self.home.name,
                    "PHONE_HARNESS_ADB": str(adb), "PHONE_HARNESS_TELEMETRY": "0",
                    "PHONE_HARNESS_NO_UPDATE_CHECK": "1",
                    "PHONE_HARNESS_CLOUD_API": f"http://127.0.0.1:{self.server.server_port}",
                    "PHONE_HARNESS_CLOUD_OAUTH_ISSUER": f"http://127.0.0.1:{self.server.server_port}",
                    "PHONE_HARNESS_CLOUD_OAUTH_CLIENT_ID": "cli-client"}
        for name in ("PHONE_HARNESS_API_KEY", "ANDROID_SERIAL", "PHONE_HARNESS_PLATFORM",
                     "PHONE_HARNESS_SESSION", "BROWSER"):
            self.env.pop(name, None)
        browser_recorder.recorder.reset()

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

    def test_an_api_key_in_the_environment_is_used_for_ci(self):
        FakeCloud.valid.add("pck_ci")
        r = self.run_cli("cloud", "whoami", env={"PHONE_HARNESS_API_KEY": "pck_ci"})
        self.assertIn("a@b.c", r.stdout)

    def test_start_uses_the_saved_phone_and_connects(self):
        self.login()
        r = self.run_cli("cloud", "start", "Saved phone", "--minutes", "5")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(FakeCloud.posts, [{"timeout_seconds": 300, "platform": "android",
                                            "kind": "emulator", "profile_id": "prof-1"}])
        self.assertRegex(FakeCloud.keys[0] or "", r"^[A-Za-z0-9_-]{16,128}$")
        self.assertIn("Resumed exactly", r.stdout)
        self.assertNotIn("ph_code", r.stdout)
        self.assertNotIn("watch.example", r.stdout)
        log = self.adb_log()
        self.assertIn("connect live.example:22220", log)
        self.assertIn("shell unlock ph_code", log)

        again = self.run_cli("cloud", "start", "Saved phone")            # same catalog choice: no second phone
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("reusing", again.stdout)
        self.assertEqual(len(FakeCloud.posts), 1)

        ls = self.run_cli("cloud", "ls")
        line = next(l for l in ls.stdout.splitlines() if "sid001" in l)
        self.assertTrue(line.startswith("*"), ls.stdout)
        self.assertIn("Saved phone", line)

    def test_start_opens_the_live_view_unless_told_not_to(self):
        self.login()
        r = self.run_cli("cloud", "start")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("opened in your browser", r.stdout)
        self.assertEqual(browser_recorder.recorder.urls(), ["https://watch.example/secret"])
        self.assertNotIn("watch.example", r.stdout)            # the url itself is never printed
        again = self.run_cli("cloud", "start")                 # reattach: no second tab
        self.assertEqual(browser_recorder.recorder.urls(), ["https://watch.example/secret"], again.stdout)
        self.run_cli("cloud", "stop")
        r = self.run_cli("cloud", "start", "--no-watch")
        self.assertIn("phone-harness cloud watch", r.stdout)
        self.assertEqual(browser_recorder.recorder.urls(), ["https://watch.example/secret"])
        self.run_cli("cloud", "stop")
        r = self.run_cli("cloud", "start", env={"PHONE_HARNESS_CLOUD_WATCH": "0"})
        self.assertIn("phone-harness cloud watch", r.stdout)
        self.assertEqual(browser_recorder.recorder.urls(), ["https://watch.example/secret"])

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
        self.run_cli("cloud", "start", "Saved phone")
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
        self.assertEqual(self.run_cli("cloud", "start", "Temporary phone").returncode, 0)
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
        FakeCloud.available = [
            {"platform": "ios", "kind": "device", "id": "iph-1", "label": "My iPhone SE",
             "state": "available"}]
        started = self.run_cli("cloud", "start", "ios", "--no-watch",
                               env={"PHONE_HARNESS_ADB": "/nonexistent/adb"})
        self.assertEqual(started.returncode, 0, started.stderr)
        sid = json.loads((Path(self.home.name) / "state" / "cloud.json").read_text())["session"]["sid"]
        # A control session may name a profile; closing it is still not a save.
        FakeCloud.sessions[sid]["profile"] = "prof-1"
        FakeCloud.sessions[sid]["state"] = "closing"
        r = self.run_cli("cloud")
        self.assertIn("· closing ·", r.stdout)
        self.assertIn("your iPhone", r.stdout)
        self.assertNotIn("saving", r.stdout)
        self.assertIn("phone       stored", r.stdout)

    def test_temp_phone_and_minutes_cap(self):
        self.login()
        self.assertEqual(self.run_cli("cloud", "start", "Temporary phone").returncode, 0)
        self.assertEqual(FakeCloud.posts, [{"timeout_seconds": 900, "platform": "android",
                                            "kind": "emulator"}])
        self.assertNotIn("profile_id", FakeCloud.posts[0])
        self.assertNotIn("device_id", FakeCloud.posts[0])
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
        r = self.run_cli("cloud", "start", "Saved phone")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already running", r.stderr)
        self.assertIn("--session", r.stderr)
        self.assertIn("dash01", r.stderr)
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
        FakeCloud.available = [
            {"platform": "ios", "kind": "device", "id": "iph-1", "label": "My iPhone SE",
             "state": "available"}]
        r = self.run_cli("cloud", "start", "ios", "--no-watch", env={"PHONE_HARNESS_ADB": "/nonexistent/adb"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(FakeCloud.posts[0]["platform"], "ios")
        self.assertEqual(FakeCloud.posts[0]["kind"], "device")
        self.assertEqual(FakeCloud.posts[0]["device_id"], "iph-1")
        self.assertEqual(FakeCloud.posts[0]["timeout_seconds"], 900)
        self.assertNotIn("provider", FakeCloud.posts[0])
        self.assertNotIn("profile_id", FakeCloud.posts[0])
        self.assertIn("Starting your iPhone", r.stdout)
        self.assertIn("your iPhone, My iPhone SE", r.stdout)
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

    def _expire_attached(self):
        path = Path(self.home.name) / "state" / "cloud.json"
        state = json.loads(path.read_text())
        sid = state["session"]["sid"]
        when = time.time() - 30
        state["session"]["expires_at"] = when
        for rec in (state.get("sessions") or {}).values():
            if isinstance(rec, dict) and rec.get("sid") == sid:
                rec["expires_at"] = when
        path.write_text(json.dumps(state))
        return sid

    def _connect_script(self, also_attached=False):
        lines = ["from phone_harness import cloud, transport"]
        if also_attached:
            lines += ["try:",
                      "    print('ATTACHED', cloud.attached())",
                      "except RuntimeError as e:",
                      "    print('REFUSED', e)"]
        lines += ["try:",
                  "    transport.connect()",
                  "    print('RAN')",
                  "except RuntimeError as e:",
                  "    print('CONNECT', e)"]
        return "\n".join(lines) + "\n"

    def test_no_cloud_state_file_still_means_no_cloud_phone(self):
        r = subprocess.run([sys.executable, "-c",
                            "from phone_harness import cloud\n"
                            "print('ATTACHED', cloud.attached())\n"
                            "print('LOST', cloud.lost())\n"],
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ATTACHED None", r.stdout)
        self.assertIn("LOST None", r.stdout)

    def test_an_unreadable_cloud_state_file_does_not_fall_back(self):
        state = Path(self.home.name) / "state"
        state.mkdir(parents=True)
        (state / "cloud.json").write_text("{not json")
        r = subprocess.run([sys.executable, "-c", self._connect_script(also_attached=True)],
                           capture_output=True, text=True, env=self.env)
        self.assertIn("REFUSED", r.stdout, r.stderr)
        self.assertIn("unreadable", r.stdout)
        self.assertIn("CONNECT", r.stdout)
        self.assertNotIn("RAN", r.stdout)
        self.assertNotIn("expired", r.stdout)

    def test_an_expired_session_pin_does_not_fall_back_to_a_local_phone(self):
        self.login()
        started = self.run_cli("cloud", "start", "Saved phone", "--no-watch")
        self.assertEqual(started.returncode, 0, started.stderr)
        sid = self._expire_attached()
        r = subprocess.run([sys.executable, "-c", self._connect_script(also_attached=True)],
                           capture_output=True, text=True,
                           env={**self.env, "PHONE_HARNESS_SESSION": sid})
        self.assertIn("REFUSED", r.stdout, r.stderr)
        self.assertIn(f"session {sid} expired", r.stdout)
        self.assertIn('cloud start "Saved phone"', r.stdout)
        self.assertIn("--for", r.stdout)
        self.assertIn("<longer>", r.stdout)
        self.assertNotIn("RAN", r.stdout)
        self.assertNotIn("unreachable", r.stdout)
        # Reported once. A later status does not repeat it. A new process that
        # pins the same sid no longer has the stored note.
        status = self.run_cli("cloud")
        self.assertNotIn("expired", status.stdout)
        self.assertNotIn("did not switch", status.stdout)
        self.assertIn("none attached", status.stdout)

    def test_an_expired_attached_session_does_not_fall_back(self):
        self.login()
        started = self.run_cli("cloud", "start", "Saved phone", "--no-watch")
        self.assertEqual(started.returncode, 0, started.stderr)
        sid = self._expire_attached()
        r = subprocess.run([sys.executable, "-c", self._connect_script(also_attached=True)],
                           capture_output=True, text=True, env=self.env)
        self.assertIn("ATTACHED None", r.stdout, r.stderr)
        self.assertIn("CONNECT", r.stdout)
        self.assertIn(f"session {sid} expired", r.stdout)
        self.assertIn('cloud start "Saved phone"', r.stdout)
        self.assertIn("--for", r.stdout)
        self.assertNotIn("RAN", r.stdout)
        status = self.run_cli("cloud")
        self.assertNotIn("expired", status.stdout)
        self.assertNotIn("did not switch", status.stdout)
        self.assertIn("none attached", status.stdout)
        again = subprocess.run([sys.executable, "-c", self._connect_script()],
                               capture_output=True, text=True, env=self.env)
        self.assertNotIn("expired", again.stdout)
        self.assertNotIn("did not switch", again.stdout)
        self.assertIn("RAN", again.stdout)

    def test_an_expired_session_keeps_failing_for_the_rest_of_that_process(self):
        """Clearing the stored note must not let the same script switch phones."""
        self.login()
        started = self.run_cli("cloud", "start", "Saved phone", "--no-watch")
        self.assertEqual(started.returncode, 0, started.stderr)
        sid = self._expire_attached()
        script = (
            "from phone_harness import transport\n"
            "for i in range(2):\n"
            "    try:\n"
            "        transport.connect()\n"
            "        print('RAN', i)\n"
            "    except RuntimeError as e:\n"
            "        print('CONNECT', i, e)\n"
        )
        r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=self.env)
        self.assertIn("CONNECT 0", r.stdout, r.stderr)
        self.assertIn("CONNECT 1", r.stdout)
        self.assertIn(f"session {sid} expired", r.stdout)
        self.assertNotIn("RAN", r.stdout)

    def test_status_shows_an_expired_session_once(self):
        self.login()
        started = self.run_cli("cloud", "start", "Saved phone", "--no-watch")
        self.assertEqual(started.returncode, 0, started.stderr)
        sid = self._expire_attached()
        FakeCloud.sessions[sid]["expires_at"] = time.time() - 30
        first = self.run_cli("cloud")
        self.assertNotEqual(first.returncode, 0, first.stdout)
        self.assertIn(f"session {sid} expired", first.stdout)
        self.assertIn("did not switch", first.stdout)
        second = self.run_cli("cloud")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotIn("expired", second.stdout)
        self.assertNotIn("did not switch", second.stdout)
        self.assertIn("none attached", second.stdout)
        # The service still has that dead session. A script that targets it
        # fails with the same message instead of driving another phone.
        pinned = subprocess.run(
            [sys.executable, "-c", self._connect_script()],
            capture_output=True, text=True,
            env={**self.env, "PHONE_HARNESS_SESSION": sid})
        self.assertIn(f"session {sid} expired", pinned.stdout, pinned.stderr)
        self.assertNotIn("RAN", pinned.stdout)

    def test_a_stored_dead_session_fails_a_script_then_is_cleared(self):
        state = Path(self.home.name) / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "cloud.json").write_text(json.dumps({"gone": {
            "sid": "dead1", "cause": "expired", "label": "Saved phone",
            "selector": "label:Saved phone"}}))
        r = subprocess.run([sys.executable, "-c", self._connect_script()],
                           capture_output=True, text=True, env=self.env)
        self.assertIn("session dead1 expired", r.stdout, r.stderr)
        self.assertIn("did not switch", r.stdout)
        self.assertNotIn("RAN", r.stdout)
        again = subprocess.run([sys.executable, "-c", self._connect_script()],
                               capture_output=True, text=True, env=self.env)
        self.assertNotIn("expired", again.stdout)
        self.assertNotIn("did not switch", again.stdout)
        self.assertIn("RAN", again.stdout)

    def test_a_session_that_dies_does_not_fall_back_to_a_local_phone(self):
        self.login()
        started = self.run_cli("cloud", "start", "Saved phone", "--no-watch")
        self.assertEqual(started.returncode, 0, started.stderr)
        FakeCloud.sessions.clear()
        status = self.run_cli("cloud")
        self.assertIn("unreachable", status.stdout, status.stderr)
        self.assertIn("failed", status.stdout)
        self.assertIn('cloud start "Saved phone"', status.stdout)
        self.assertIn("--for", status.stdout)
        self.assertNotIn("expired", status.stdout)
        # Status showed the dead session once, which clears the stored note.
        again = self.run_cli("cloud")
        self.assertNotIn("unreachable", again.stdout)
        self.assertNotIn("did not switch", again.stdout)
        self.assertIn("none attached", again.stdout)
        self.assertEqual(again.returncode, 0, again.stderr)
        cleared = subprocess.run(
            [sys.executable, "-c", "from phone_harness import cloud; print(cloud.lost())"],
            capture_output=True, text=True, env=self.env)
        self.assertEqual(cleared.stdout.strip(), "None", cleared.stderr)
        stopped = self.run_cli("cloud", "stop", "--all")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)

    def test_start_waits_out_a_phone_that_is_still_closing(self):
        self.login()
        FakeCloud.sessions["old001"] = {"id": "old001", "state": "closing", "profile": "prof-1",
                                       "expires_at": time.time() + 600}
        FakeCloud.profile.update(state="running", session="old001")
        # One read builds the catalog; the wait then sees the phone still closing.
        FakeCloud.closing_reads = 3
        r = self.run_cli("cloud", "start", "Saved phone")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("still saving", r.stdout)
        self.assertEqual(FakeCloud.posts, [{"timeout_seconds": 900, "platform": "android",
                                            "kind": "emulator", "profile_id": "prof-1"}])

    def test_stop_returns_at_once_and_detaches(self):
        self.login()
        self.run_cli("cloud", "start", "Saved phone")
        started = time.monotonic()
        r = self.run_cli("cloud", "stop")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(time.monotonic() - started, 2.5)             # no polling for the save
        self.assertIn("ended.", r.stdout)
        self.assertIn("being saved", r.stdout)
        self.assertEqual(FakeCloud.sessions, {})
        state = json.loads((Path(self.home.name) / "state" / "cloud.json").read_text())
        self.assertNotIn("session", state)
        self.assertIn("none attached", self.run_cli("cloud").stdout)

    def test_stop_wait_watches_the_save(self):
        self.login()
        self.run_cli("cloud", "start", "Saved phone")
        r = self.run_cli("cloud", "stop", "--wait")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("stored", r.stdout)

    def test_a_temporary_phone_has_nothing_to_save(self):
        self.login()
        self.run_cli("cloud", "start", "Temporary phone")
        r = self.run_cli("cloud", "stop")
        self.assertNotIn("saved", r.stdout)

    def test_a_bare_start_names_nothing_and_gets_the_service_s_unnamed_phone(self):
        self.login()
        r = self.run_cli("cloud", "start", "--no-watch")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Starting a temporary phone", r.stdout)
        self.assertNotIn("Saved phone", r.stdout)
        self.assertEqual(FakeCloud.posts, [{"timeout_seconds": 900}])       # no platform, no kind, no profile
        self.assertEqual(FakeCloud.sessions[next(iter(FakeCloud.sessions))].get("profile"), None)

    def test_this_machine_may_prefer_a_phone_for_bare_starts(self):
        self.login()
        self.assertEqual(self.run_cli("config", "set", "cloud.phone", "Saved phone").returncode, 0)
        r = self.run_cli("cloud", "start", "--no-watch")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Starting Saved phone", r.stdout)
        self.assertEqual(FakeCloud.posts[-1], {"timeout_seconds": 900, "platform": "android",
                                               "kind": "emulator", "profile_id": "prof-1"})
        # a stale preference says where it came from
        self.run_cli("cloud", "stop")
        self.assertEqual(self.run_cli("config", "set", "cloud.phone", "Old phone").returncode, 0)
        gone = self.run_cli("cloud", "start", "--no-watch")
        self.assertNotEqual(gone.returncode, 0)
        self.assertIn("cloud.phone", gone.stderr)
        self.assertIn("Saved phone", gone.stderr)

    def test_removed_flags_are_not_phone_names(self):
        self.login()
        for flag in ("--iphone", "--device", "--temp", "--timeout"):
            r = self.run_cli("cloud", "start", flag)
            self.assertNotEqual(r.returncode, 0, flag)
            self.assertIn(flag, r.stderr)
        self.assertEqual(FakeCloud.posts, [])

    def test_catalog_start_is_exact_and_an_ambiguous_name_stops(self):
        self.login()
        FakeCloud.available = [
            {"platform": "ios", "kind": "device", "id": "iph-a", "label": "SE", "state": "available"},
            {"platform": "ios", "kind": "device", "id": "iph-b", "label": "Pro", "state": "unavailable"},
        ]
        many = self.run_cli("cloud", "start", "ios", "--no-watch")
        self.assertNotEqual(many.returncode, 0)
        shown = many.stdout + many.stderr
        self.assertIn("SE", shown)
        self.assertIn("Pro", shown)
        self.assertIn("iph-a", shown)
        self.assertEqual(FakeCloud.posts, [])

        missing = self.run_cli("cloud", "start", "nope", "--no-watch")
        self.assertIn("nope", missing.stderr)
        self.assertIn("catalog", missing.stderr)
        self.assertEqual(FakeCloud.posts, [])

        down = self.run_cli("cloud", "start", "iph-b", "--no-watch",
                            env={"PHONE_HARNESS_ADB": "/nonexistent/adb"})
        self.assertNotEqual(down.returncode, 0)
        self.assertIn("not on its host", down.stderr)

        FakeCloud.available = [
            {"platform": "ios", "kind": "device", "id": "iph-a", "label": "SE",
             "state": "running", "session": "live1"}]
        held = self.run_cli("cloud", "start", "SE", "--no-watch",
                            env={"PHONE_HARNESS_ADB": "/nonexistent/adb"})
        self.assertNotEqual(held.returncode, 0)
        self.assertIn("--session", held.stderr)
        self.assertIn("live1", held.stderr)
        self.assertNotIn("cloud use", held.stderr)

        FakeCloud.available = [
            {"platform": "android", "kind": "emulator", "label": "Temporary phone", "temporary": True}]
        before = len(FakeCloud.posts)
        none = self.run_cli("cloud", "start", "ios", "--no-watch")
        self.assertIn("catalog", none.stderr)
        self.assertEqual(len(FakeCloud.posts), before)

    def test_create_errors_name_the_next_step(self):
        self.login()
        FakeCloud.fail = (400, {"error": "no phone of that platform and kind available to this account"}, None)
        hinted = self.run_cli("cloud", "start", "Saved phone", "--no-watch")
        self.assertIn("no phone of that platform and kind", hinted.stderr)
        self.assertIn("Saved phone", hinted.stderr)
        FakeCloud.fail = (409, {"error": "limit", "code": "session_limit",
                                "active_session_count": 2, "session_limit": 2}, None)
        limited = self.run_cli("cloud", "start", "--no-watch")
        self.assertIn("2 of 2", limited.stderr)
        FakeCloud.fail = (503, {"error": "busy", "code": "phones_busy"}, {"Retry-After": "12"})
        busy = self.run_cli("cloud", "start", "--no-watch")
        self.assertIn("Try again in 12s", busy.stderr)
        FakeCloud.fail = (403, {"error": "blocked", "code": "account_blocked"}, None)
        blocked = self.run_cli("cloud", "start", "--no-watch")
        self.assertIn("Rentals are blocked", blocked.stderr)
        self.assertNotIn("isn't enabled", blocked.stderr)
        FakeCloud.fail = (403, {"error": "not enough credit", "balance_cents": 0}, None)
        broke = self.run_cli("cloud", "start", "--no-watch")
        self.assertIn("Not enough credit", broke.stderr)
        self.assertNotIn("isn't enabled", broke.stderr)

    def test_one_iphone_starts_by_platform_or_id_and_for_sets_the_length(self):
        self.login()
        FakeCloud.available = [
            {"platform": "ios", "kind": "device", "id": "iph-a", "label": "SE", "state": "available"}]
        r = self.run_cli("cloud", "start", "ios", "--for", "2m", "--no-watch",
                         env={"PHONE_HARNESS_ADB": "/nonexistent/adb"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(FakeCloud.posts[-1]["timeout_seconds"], 120)
        self.assertEqual(FakeCloud.posts[-1]["device_id"], "iph-a")
        self.assertEqual(FakeCloud.posts[-1]["platform"], "ios")
        self.assertEqual(FakeCloud.posts[-1]["kind"], "device")
        named = self.run_cli("cloud", "start", "iph-a", "--for", "45s", "--no-watch",
                             env={"PHONE_HARNESS_ADB": "/nonexistent/adb"})
        self.assertEqual(named.returncode, 0, named.stderr)
        self.assertIn("reusing", named.stdout)
        self.assertEqual(len(FakeCloud.posts), 1)
        self.run_cli("cloud", "stop")
        posted = self.run_cli("cloud", "start", "SE", "--for", "45s", "--no-watch",
                              env={"PHONE_HARNESS_ADB": "/nonexistent/adb"})
        self.assertEqual(posted.returncode, 0, posted.stderr)
        self.assertEqual(FakeCloud.posts[-1]["device_id"], "iph-a")
        self.assertEqual(FakeCloud.posts[-1]["timeout_seconds"], 45)
        over = self.run_cli("cloud", "start", "--for", "90m", "--no-watch")
        self.assertNotEqual(over.returncode, 0)
        self.assertIn("between 1 and 30", over.stderr)
        clash = self.run_cli("cloud", "start", "--minutes", "5", "--for", "90s")
        self.assertIn("not both", clash.stderr)

    def test_for_accepts_seconds_minutes_and_a_bare_number(self):
        self.login()
        for text, seconds in (("90s", 90), ("15m", 900), ("2", 120), ("15min", 900)):
            if FakeCloud.sessions:
                stopped = self.run_cli("cloud", "stop")
                self.assertEqual(stopped.returncode, 0, stopped.stderr)
            r = self.run_cli("cloud", "start", "Temporary phone", "--for", text, "--no-watch")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(FakeCloud.posts[-1]["timeout_seconds"], seconds, text)
            self.assertNotIn("profile_id", FakeCloud.posts[-1])

    def test_android_timeout_keeps_the_profile_body(self):
        self.login()
        r = self.run_cli("cloud", "start", "Saved phone", "--for", "120s", "--no-watch")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(FakeCloud.posts, [{"timeout_seconds": 120, "platform": "android",
                                            "kind": "emulator", "profile_id": "prof-1"}])

    def test_an_iphone_and_an_android_can_run_together(self):
        self.login()
        FakeCloud.available = [
            {"platform": "android", "kind": "emulator", "id": "prof-1", "label": "Saved phone",
             "state": "stored"},
            {"platform": "ios", "kind": "device", "id": "iph-1", "label": "My iPhone SE",
             "state": "available"},
        ]
        first = self.run_cli("cloud", "start", "Saved phone", "--minutes", "5", "--no-watch")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(FakeCloud.posts[0], {"timeout_seconds": 300, "platform": "android",
                                              "kind": "emulator", "profile_id": "prof-1"})
        second = self.run_cli("cloud", "start", "ios", "--no-watch",
                              env={"PHONE_HARNESS_ADB": "/nonexistent/adb"})
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("stays up", second.stdout)
        self.assertEqual(FakeCloud.posts[1]["platform"], "ios")
        self.assertEqual(FakeCloud.posts[1]["kind"], "device")
        self.assertEqual(FakeCloud.posts[1]["device_id"], "iph-1")
        self.assertEqual(len(FakeCloud.sessions), 2)
        back = self.run_cli("cloud", "start", "Saved phone", "--no-watch")
        self.assertEqual(back.returncode, 0, back.stderr)
        self.assertIn("reusing", back.stdout)
        self.assertEqual(len(FakeCloud.posts), 2)
        self.assertIn("your phone", back.stdout)
        self.assertNotIn("your iPhone", back.stdout)

    def test_ls_merges_the_catalog_with_a_session_that_matches_nothing(self):
        self.login()
        FakeCloud.available = [
            {"platform": "android", "kind": "emulator", "id": "prof-1", "label": "Saved phone",
             "state": "stored"},
            {"platform": "ios", "kind": "device", "id": "iph-1", "label": "My iPhone SE",
             "state": "available"},
            {"platform": "android", "kind": "emulator", "temporary": True, "label": "Temporary phone"},
        ]
        self.assertEqual(self.run_cli("cloud", "start", "Saved phone", "--no-watch").returncode, 0)
        FakeCloud.sessions["orphan"] = {
            "id": "orphan", "state": "ready", "platform": "android", "profile": "prof-other",
            "expires_at": time.time() + 600,
            "adb": {"host": "live.example", "port": 22220, "code": "ph_code"}}
        raw = self.run_cli("cloud", "ls", "--json")
        self.assertEqual(raw.returncode, 0, raw.stderr)
        rows = json.loads(raw.stdout)
        self.assertEqual([row["label"] for row in rows[:3]],
                         ["Saved phone", "My iPhone SE", "Temporary phone"])
        self.assertNotIn("default", rows[0])          # the service keeps no preference, so neither does ls
        self.assertFalse(rows[0]["temporary"])
        self.assertEqual(rows[0]["id"], "prof-1")
        self.assertEqual(rows[0]["platform"], "android")
        self.assertEqual(rows[0]["state"], "stored")
        self.assertEqual(rows[0]["session"]["id"], "sid001")
        self.assertTrue(rows[0]["targeted"])
        self.assertIsNone(rows[1]["session"])
        self.assertFalse(rows[1]["targeted"])
        self.assertTrue(rows[-1]["unmatched"])
        self.assertEqual(rows[-1]["session"]["id"], "orphan")
        text = self.run_cli("cloud", "ls")
        self.assertIn("orphan", text.stdout)
        self.assertTrue(any(line.startswith("*") and "sid001" in line
                            for line in text.stdout.splitlines()))

    def test_ls_inlines_a_live_session_on_its_catalog_row(self):
        """device_id, profile_id, and a temporary session each share their row.

        A live iPhone used to print a second `iPhone ios - ready <sid>` line
        because the list matches `profile` and a display name, not device_id
        or profile_id. A session that matches nothing is still its own row,
        including a control link that names no device."""
        self.login()
        FakeCloud.available = [
            {"platform": "ios", "kind": "device", "device_id": "iph-1", "label": "iPhone",
             "state": "running"},
            {"platform": "android", "kind": "emulator", "id": "prof-1", "label": "Saved phone",
             "state": "running"},
            {"platform": "android", "kind": "emulator", "temporary": True, "label": "Temporary phone"},
        ]
        soon = time.time() + 7 * 60
        control = {"url": "http://127.0.0.1:1", "token": "ct_1"}
        FakeCloud.sessions["liveios"] = {
            "id": "liveios", "state": "ready", "platform": "ios", "kind": "device",
            "device": "iPhone", "label": "iPhone", "device_id": "iph-1",
            "profile_id": None, "profile": None, "phone": None,
            "expires_at": soon, "control": control}
        FakeCloud.sessions["liveand"] = {
            "id": "liveand", "state": "ready", "platform": "android", "kind": "emulator",
            "device": "Android emulator", "profile_id": "prof-1", "profile": None,
            "device_id": None, "phone": None, "expires_at": soon,
            "adb": {"host": "live.example", "port": 22220, "code": "ph_code"}}
        FakeCloud.sessions["livetemp"] = {
            "id": "livetemp", "state": "ready", "platform": "android", "kind": "emulator",
            "device": "Android emulator", "profile_id": None, "profile": None,
            "device_id": None, "phone": None, "expires_at": soon,
            "adb": {"host": "live.example", "port": 22221, "code": "ph_code"}}
        FakeCloud.sessions["orphan"] = {
            "id": "orphan", "state": "ready", "platform": "ios", "kind": "device",
            "device": "iPhone", "device_id": "iph-other", "profile_id": None,
            "expires_at": soon, "control": control}
        FakeCloud.sessions["noid"] = {
            "id": "noid", "state": "ready", "platform": "ios", "device": "iPhone",
            "profile_id": None, "profile": None, "device_id": None, "phone": "iPhone",
            "expires_at": soon, "control": control}
        raw = self.run_cli("cloud", "ls", "--json")
        self.assertEqual(raw.returncode, 0, raw.stderr)
        rows = json.loads(raw.stdout)
        self.assertEqual([row["session"]["id"] for row in rows],
                         ["liveios", "liveand", "livetemp", "orphan", "noid"])
        self.assertEqual(rows[0]["id"], "iph-1")
        self.assertEqual(rows[0]["label"], "iPhone")
        self.assertFalse(rows[0].get("unmatched"))
        self.assertEqual(rows[1]["id"], "prof-1")
        self.assertTrue(rows[2]["temporary"])
        self.assertEqual([row["session"]["id"] for row in rows if row.get("unmatched")],
                         ["orphan", "noid"])
        text = self.run_cli("cloud", "ls").stdout
        for sid in ("liveios", "liveand", "livetemp", "orphan", "noid"):
            self.assertEqual(text.count(sid), 1, text)
        iphone = next(line for line in text.splitlines() if "liveios" in line)
        self.assertIn("iPhone", iphone)
        self.assertIn("iph-1", iphone)
        self.assertIn("Saved phone", next(line for line in text.splitlines() if "liveand" in line))
        self.assertIn("Temporary", next(line for line in text.splitlines() if "livetemp" in line))

    def test_ls_inlines_a_running_iphone_that_does_not_echo_its_id(self):
        """Live shape: the session's `device` is the word iPhone, not the catalog id.

        GET /me `available` marks that phone `state: running`. GET /sessions
        does not return `device_id`. One running ios device and one ios
        session with no id share the catalog row.
        """
        self.login()
        FakeCloud.available = [
            {"platform": "ios", "kind": "device", "id": "iph-e2eeaed7e6",
             "label": "iPhone", "state": "running"},
            {"platform": "android", "kind": "emulator", "id": "prof-1",
             "label": "Saved phone", "state": "stored"},
            {"platform": "android", "kind": "emulator", "temporary": True,
             "label": "Temporary phone"},
        ]
        soon = time.time() + 117
        FakeCloud.sessions["74f35c02595f"] = {
            "id": "74f35c02595f", "state": "ready", "platform": "ios", "kind": "device",
            "device": "iPhone", "phone": None, "profile": None, "billing_mode": "disabled",
            "expires_at": soon,
            "control": {"url": "http://127.0.0.1:1", "token": "ct_1"}}
        raw = self.run_cli("cloud", "ls", "--json")
        self.assertEqual(raw.returncode, 0, raw.stderr)
        rows = json.loads(raw.stdout)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["id"], "iph-e2eeaed7e6")
        self.assertEqual(rows[0]["state"], "running")
        self.assertEqual(rows[0]["session"]["id"], "74f35c02595f")
        self.assertFalse(rows[0].get("unmatched"))
        text = self.run_cli("cloud", "ls").stdout
        self.assertEqual(text.count("74f35c02595f"), 1, text)
        self.assertEqual(sum(1 for line in text.splitlines() if "iPhone" in line), 1, text)

    def test_ls_inlines_the_live_iphone_profile_that_matches_no_catalog_id(self):
        """Captured while a cloud iPhone was running.

        The session's `profile` is `iphone-…`, which is not the catalog id
        `iph-…`. It is still that one running ios device.
        """
        self.login()
        FakeCloud.available = [
            {"platform": "android", "kind": "emulator",
             "id": "prof-948b7471ed7575fc17236906", "label": "Saved phone",
             "state": "stored"},
            {"platform": "android", "kind": "emulator", "temporary": True,
             "label": "Temporary phone"},
            {"platform": "ios", "kind": "device", "id": "iph-e2eeaed7e6",
             "label": "iPhone", "state": "running"},
        ]
        FakeCloud.sessions["61712b99d488"] = {
            "id": "61712b99d488", "state": "ready", "platform": "ios", "kind": "device",
            "device": "iPhone", "phone": None, "profile": "iphone-99d901c34490fbe8bc4b0402",
            "screen": {"w": 750, "h": 1334}, "expires_at": 1791693728.89,
            "timeout_seconds": 120,
            "control": {"url": "http://127.0.0.1:1", "token": "ct_1"}}
        raw = self.run_cli("cloud", "ls", "--json")
        self.assertEqual(raw.returncode, 0, raw.stderr)
        rows = json.loads(raw.stdout)
        self.assertEqual([row.get("unmatched") for row in rows], [None, None, None])
        iphone = rows[2]
        self.assertEqual(iphone["id"], "iph-e2eeaed7e6")
        self.assertEqual(iphone["state"], "running")
        self.assertEqual(iphone["session"]["id"], "61712b99d488")
        text = self.run_cli("cloud", "ls").stdout
        self.assertEqual(text.count("61712b99d488"), 1, text)
        line = next(line for line in text.splitlines() if "61712b99d488" in line)
        self.assertIn("iph-e2eeaed7e6", line)
        self.assertIn("iPhone", line)

    def test_ls_does_not_guess_when_two_iphones_are_running(self):
        self.login()
        FakeCloud.available = [
            {"platform": "ios", "kind": "device", "id": "iph-a", "label": "iPhone",
             "state": "running"},
            {"platform": "ios", "kind": "device", "id": "iph-b", "label": "Other",
             "state": "running"},
        ]
        FakeCloud.sessions["solo"] = {
            "id": "solo", "state": "ready", "platform": "ios", "kind": "device",
            "device": "iPhone", "phone": None, "profile": None,
            "expires_at": time.time() + 600,
            "control": {"url": "http://127.0.0.1:1", "token": "ct_1"}}
        rows = json.loads(self.run_cli("cloud", "ls", "--json").stdout)
        self.assertIsNone(rows[0]["session"])
        self.assertIsNone(rows[1]["session"])
        self.assertEqual(rows[2]["session"]["id"], "solo")
        self.assertTrue(rows[2]["unmatched"])

    def test_start_does_not_repeat_an_iphone_label(self):
        self.login()
        FakeCloud.available = [
            {"platform": "ios", "kind": "device", "id": "iph-1", "label": "iPhone",
             "state": "available"}]
        r = self.run_cli("cloud", "start", "iPhone", "--no-watch",
                         env={"PHONE_HARNESS_ADB": "/nonexistent/adb"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("iPhone, iPhone", r.stdout)
        self.assertRegex(r.stdout, r"Starting your iPhone(?!,)")
        self.assertRegex(r.stdout, r"\(your iPhone\)")

    def test_shown_expiry_rounds_up_to_the_requested_minutes(self):
        from phone_harness.cloud import _left
        self.assertEqual(_left(time.time() + 6 * 60 - 50), "6 min")
        self.assertEqual(_left(time.time() + 5 * 60), "5 min")
        self.assertTrue(_left(time.time() + 90).endswith("s"))
        self.login()
        r = self.run_cli("cloud", "start", "Temporary phone", "--for", "6", "--no-watch")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(FakeCloud.posts[-1]["timeout_seconds"], 360)
        self.assertIn("expires  in 6 min", r.stdout)

    def test_a_control_error_prints_its_type_once(self):
        from phone_harness.remote import _plain_error
        self.assertEqual(
            _plain_error("RuntimeError: no installed app matches 'Notes'", RuntimeError),
            "no installed app matches 'Notes'")
        self.assertEqual(
            _plain_error("RuntimeError: RuntimeError: no installed app matches 'Notes'", RuntimeError),
            "no installed app matches 'Notes'")
        self.assertEqual(
            _plain_error("CloudPhoneError: session s expired", RuntimeError),
            "session s expired")
        self.assertEqual(
            _plain_error("RuntimeError: CloudPhoneError: session s expired", RuntimeError),
            "session s expired")
        self.assertEqual(
            _plain_error(
                "phone_harness.cloud.CloudPhoneError: RuntimeError: session s expired",
                RuntimeError),
            "session s expired")
        from phone_harness.cloud import CloudPhoneError
        self.assertEqual(
            _plain_error("CloudPhoneError: RuntimeError: session s expired", CloudPhoneError),
            "session s expired")
        control = ThreadingHTTPServer(("127.0.0.1", 0), FakeControl)
        threading.Thread(target=control.serve_forever, daemon=True).start()
        self.addCleanup(control.server_close)
        self.addCleanup(control.shutdown)
        FakeCloud.control_url = f"http://127.0.0.1:{control.server_port}"
        FakeControl.launch_error = {"error": "RuntimeError: no installed app matches 'Notes'"}
        self.login()
        FakeCloud.available = [
            {"platform": "ios", "kind": "device", "id": "iph-1", "label": "iPhone",
             "state": "available"}]
        started = self.run_cli("cloud", "start", "ios", "--no-watch",
                               env={"PHONE_HARNESS_ADB": "/nonexistent/adb"})
        self.assertEqual(started.returncode, 0, started.stderr)
        script = "from phone_harness.helpers import open_app\nopen_app('Notes')\n"
        r = self.run_cli(stdin=script)
        self.assertNotEqual(r.returncode, 0)
        last = [line for line in r.stderr.splitlines() if line.strip()][-1]
        self.assertEqual(last, "RuntimeError: no installed app matches 'Notes'")
        self.assertNotIn("RuntimeError: RuntimeError:", r.stderr)
        FakeControl.launch_error = {
            "error": "phone_harness.cloud.CloudPhoneError: session dead1 expired"}
        again = self.run_cli(stdin=script)
        self.assertNotEqual(again.returncode, 0)
        cloud_last = [line for line in again.stderr.splitlines() if line.strip()][-1]
        self.assertEqual(
            cloud_last, "phone_harness.cloud.CloudPhoneError: session dead1 expired")
        self.assertNotIn("RuntimeError: phone_harness.cloud.CloudPhoneError:", again.stderr)
        self.assertNotIn("CloudPhoneError: phone_harness.cloud.CloudPhoneError:", again.stderr)
        self.assertNotIn("CloudPhoneError: RuntimeError:", again.stderr)

    def test_a_session_pin_targets_one_phone_for_commands_and_helpers(self):
        self.login()
        FakeCloud.available = [
            {"platform": "android", "kind": "emulator", "id": "prof-1", "label": "Saved phone",
             "state": "stored"},
            {"platform": "ios", "kind": "device", "id": "iph-1", "label": "My iPhone SE",
             "state": "available"},
        ]
        self.assertEqual(self.run_cli("cloud", "start", "--no-watch").returncode, 0)
        self.assertEqual(self.run_cli("cloud", "start", "ios", "--no-watch",
                                      env={"PHONE_HARNESS_ADB": "/nonexistent/adb"}).returncode, 0)
        state = json.loads((Path(self.home.name) / "state" / "cloud.json").read_text())
        android_sid = next(s["sid"] for s in state["sessions"].values() if s["link"]["kind"] == "adb")
        ios_sid = next(s["sid"] for s in state["sessions"].values() if s["link"]["kind"] == "control")

        def starred(result, sid):
            line = next(l for l in result.stdout.splitlines() if sid in l)
            return line.startswith("*")

        plain = self.run_cli("cloud", "ls")
        self.assertTrue(starred(plain, ios_sid), plain.stdout)
        self.assertFalse(starred(plain, android_sid), plain.stdout)
        pinned = self.run_cli("cloud", "ls", "--session", android_sid)
        self.assertTrue(starred(pinned, android_sid), pinned.stdout)
        self.assertFalse(starred(pinned, ios_sid), pinned.stdout)
        via_env = self.run_cli("cloud", "ls", env={"PHONE_HARNESS_SESSION": android_sid})
        self.assertTrue(starred(via_env, android_sid), via_env.stdout)

        script = "from phone_harness import transport\nprint(transport.connect().name)\n"
        remote = self.run_cli("--session", ios_sid, stdin=script)
        self.assertEqual(remote.stdout.strip(), "remote", remote.stderr)
        android = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                 env={**self.env, "PHONE_HARNESS_SESSION": android_sid})
        self.assertEqual(android.stdout.strip(), "android", android.stderr)

        stopped = self.run_cli("cloud", "stop", "--session", android_sid)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertIn(android_sid, stopped.stdout)
        self.assertNotIn(android_sid, FakeCloud.sessions)
        self.assertIn(ios_sid, FakeCloud.sessions)
        self.assertIn("your iPhone", self.run_cli("cloud").stdout)

    # --- telemetry -------------------------------------------------------------

    def posthog(self, client="test-agent"):
        """Telemetry on, posting to a fake PostHog; the client is PH_CLIENT alone."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakePostHog)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        FakePostHog.events = []
        for name in AGENT_MARKERS:
            self.env.pop(name, None)
        self.env.update({"PHONE_HARNESS_TELEMETRY": "1", "PH_CLIENT": client,
                         "PH_POSTHOG_HOST": f"http://127.0.0.1:{server.server_port}"})

    def events(self, name, count=1, timeout=10.0):
        """The `name` events, once `count` have arrived (the sender is a detached
        process), or whatever arrived by the deadline."""
        deadline = time.monotonic() + timeout
        while True:
            got = [e for e in FakePostHog.events if e["event"] == name]
            if len(got) >= count or time.monotonic() >= deadline:
                return got
            time.sleep(0.05)

    def cli_event(self, count, **match):
        """The properties of the one cli_event that matches, once `count` have
        arrived. Each run's sender is its own process, so arrival order is not
        run order."""
        hits = [e["properties"] for e in self.events("cli_event", count)
                if all(e["properties"].get(k) == v for k, v in match.items())]
        self.assertEqual(len(hits), 1, (match, FakePostHog.events))
        return hits[0]

    def telemetry_state(self):
        path = Path(self.home.name) / "state" / "telemetry.json"
        return json.loads(path.read_text()) if path.exists() else None

    def test_a_leading_session_pin_still_keeps_cloud_output_out_of_telemetry(self):
        self.posthog()
        self.login()
        self.assertEqual(self.run_cli("cloud", "start", "--no-watch").returncode, 0)
        [sid] = FakeCloud.sessions
        shown = self.run_cli("--session", sid, "cloud", "show", "--json")
        self.assertIn("ph_code", shown.stdout)                    # the unlock code, on screen
        events = self.events("cli_event", count=3)
        self.assertEqual([e["properties"]["command"] for e in events], ["cloud"] * 3)
        self.assertEqual([e["properties"]["output"] for e in events], [None] * 3)
        blob = json.dumps(FakePostHog.events)
        for secret in ("ph_code", "watch.example"):
            self.assertNotIn(secret, blob)

    def test_signing_in_links_this_install_to_the_account_once(self):
        self.posthog()
        self.login()
        install = self.telemetry_state()["install_id"]
        [identify] = self.events("$identify")
        self.assertEqual(identify["distinct_id"], "u1")
        self.assertEqual(identify["properties"]["$anon_distinct_id"], install)
        self.assertEqual(identify["properties"]["client"], "test-agent")
        [login] = self.events("cli_event")
        self.assertEqual(login["distinct_id"], install)          # still one id per install
        props = login["properties"]
        self.assertEqual((props["command"], props["subcommand"], props["user_id"], props["channel"]),
                         ("cloud", "login", "u1", "cli"))

        self.run_cli("cloud", "whoami")                           # GET /me again: no second link
        self.assertEqual(self.cli_event(2, subcommand="whoami")["user_id"], "u1")
        time.sleep(0.5)
        self.assertEqual(len(self.events("$identify")), 1)

        self.run_cli("cloud", "logout")
        self.assertIsNone(self.cli_event(3, subcommand="logout")["user_id"])
        self.assertNotIn("user_id", self.telemetry_state())
        self.login()                                              # a new sign-in links again
        self.assertEqual(len(self.events("$identify", count=2)), 2)
        self.assertEqual(self.telemetry_state()["install_id"], install)
        blob = json.dumps(FakePostHog.events)
        for secret in ("at-1", "rt-1", "dev-1", "BCDF-GHJK"):
            self.assertNotIn(secret, blob)

    def test_cloud_runs_carry_the_account_and_the_session_they_drove(self):
        self.posthog()
        self.login()
        r = self.run_cli("cloud", "start", "Saved phone", "--for", "5m", "--no-watch")
        self.assertEqual(r.returncode, 0, r.stderr)
        [sid] = FakeCloud.sessions
        start = self.cli_event(2, subcommand="start")
        self.assertEqual((start["subcommand"], start["session_id"], start["platform"], start["user_id"]),
                         ("start", sid, "android", "u1"))
        self.assertEqual(start["argv"], ["cloud", "start", "Saved phone", "--for", "5m", "--no-watch"])
        self.assertIsNone(start["output"])

        script = "# task: say hi\nprint(6 * 7)\n"
        r = self.run_cli(stdin=script)
        self.assertEqual(r.returncode, 0, r.stderr)
        ran = self.cli_event(3, command="script")
        self.assertEqual((ran["command"], ran["phone"], ran["platform"], ran["session_id"], ran["user_id"]),
                         ("script", "android", "android", sid, "u1"))
        self.assertEqual((ran["task"], ran["task_intent"], ran["output"]), (script, "say hi", "42\n"))

        # `--session` before `cloud` is still a cloud command, whose output (the
        # unlock code, the watch link) is never sent.
        shown = self.run_cli("--session", sid, "cloud", "show", "--json")
        self.assertIn("ph_code", shown.stdout)
        show = self.cli_event(4, subcommand="show")
        self.assertEqual((show["command"], show["subcommand"], show["session_id"], show["argv"]),
                         ("cloud", "show", sid, ["cloud", "show", "--json"]))
        self.assertIsNone(show["output"])
        blob = json.dumps(FakePostHog.events)
        for secret in ("ph_code", "watch.example", "at-1", "rt-1"):
            self.assertNotIn(secret, blob)

    def test_a_script_pinned_with_session_sends_its_task_like_any_script(self):
        self.posthog()
        self.login()
        self.assertEqual(self.run_cli("cloud", "start", "--no-watch").returncode, 0)
        [sid] = FakeCloud.sessions
        script = "# task: say hi\n# step: through the pin\nprint(6 * 7)\n"
        r = self.run_cli("--session", sid, stdin=script)
        self.assertEqual(r.returncode, 0, r.stderr)
        ran = self.cli_event(3, command="script")
        self.assertEqual((ran["task"], ran["task_intent"], ran["step_intent"], ran["output"]),
                         (script, "say hi", "through the pin", "42\n"))
        self.assertEqual((ran["session_id"], ran["argv"]), (sid, None))

    def test_cloud_calls_name_the_version_and_client_only_while_telemetry_is_on(self):
        control = ThreadingHTTPServer(("127.0.0.1", 0), FakeControl)
        threading.Thread(target=control.serve_forever, daemon=True).start()
        self.addCleanup(control.server_close)
        self.addCleanup(control.shutdown)
        FakeCloud.control_url = f"http://127.0.0.1:{control.server_port}"
        FakeControl.calls = []
        FakeCloud.available = [{"platform": "ios", "kind": "device", "id": "iph-1",
                                "label": "My iPhone SE", "state": "available"}]
        no_adb = {"PHONE_HARNESS_ADB": "/nonexistent/adb"}
        self.posthog(client="hermes")

        # Off, as for a Hermes run that did not opt in: the calls are what they
        # were, nothing is posted, and nothing is linked.
        off = {**no_adb, "PHONE_HARNESS_TELEMETRY": "0"}
        self.assertEqual(self.run_cli("cloud", "login", "--no-browser", env=off).returncode, 0)
        r = self.run_cli("cloud", "start", "ios", "--no-watch", env=off)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_cli(stdin="tap(10, 20)\n", env=off)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(FakeControl.requests)
        for _, path, headers in FakeCloud.requests + FakeControl.requests:
            self.assertNotIn("x-agent-client", headers, path)
            self.assertNotIn("x-phone-harness-version", headers, path)
        time.sleep(0.5)
        self.assertEqual(FakePostHog.events, [])
        self.assertIsNone(self.telemetry_state())

        # On: every call to the API and to the phone's control URL names both.
        FakeCloud.requests, FakeControl.requests = [], []
        r = self.run_cli(stdin="tap(10, 20)\n", env=no_adb)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.run_cli("cloud", "ls", env=no_adb).returncode, 0)
        api = [(p, h) for _, p, h in FakeCloud.requests if not p.startswith("/oauth/")]
        ops = [(p, h) for _, p, h in FakeControl.requests]
        self.assertTrue(api and ops)
        for path, headers in api + ops:
            self.assertEqual(headers.get("x-agent-client"), "hermes", path)
            self.assertEqual(headers.get("x-phone-harness-version"), telemetry._version() or "unknown", path)
        [sid] = FakeCloud.sessions
        ran = self.cli_event(2, command="script")
        self.assertEqual((ran["phone"], ran["platform"], ran["session_id"], ran["client"]),
                         ("remote", "ios", sid, "hermes"))
        self.assertEqual(ran["steps"][0]["helper"], "tap")
        self.assertNotIn("ct_1", json.dumps(FakePostHog.events))

    def test_phone_reset_describes_the_saved_android_only(self):
        self.login()
        refused = self.run_cli("cloud", "phone", "reset")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("saved Android", refused.stderr)
        self.assertIn("no iPhone reset", refused.stderr)
        help_text = self.run_cli("cloud", "nope")
        self.assertIn("wipes its stored state", help_text.stdout)

    def test_minutes_alias_still_sets_the_length(self):
        self.login()
        r = self.run_cli("cloud", "start", "Temporary phone", "-m", "2", "--no-watch")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(FakeCloud.posts[-1]["timeout_seconds"], 120)

    def test_ls_columns_fit_a_long_catalog_id(self):
        self.login()
        long_id = "prof-948b7471ed7575fc17236906"
        FakeCloud.available = [
            {"platform": "android", "kind": "emulator", "id": long_id,
             "label": "Saved phone", "state": "stored"},
            {"platform": "android", "kind": "emulator", "temporary": True,
             "label": "Temporary phone"},
        ]
        text = self.run_cli("cloud", "ls")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertNotIn("\x1b", text.stdout)
        header, saved, temporary = [ln for ln in text.stdout.splitlines() if ln.strip()]
        id_at = header.index("ID")
        state_at = header.index("STATE")
        self.assertEqual(saved.index(long_id), id_at)
        self.assertEqual(state_at, id_at + len(long_id) + 1)
        self.assertTrue(saved[state_at:].startswith("stored"))
        self.assertEqual(temporary.index("android"), header.index("PLATFORM"))
        self.assertIn("temporary", temporary[header.index("NOTE"):])


if __name__ == "__main__":
    unittest.main()
