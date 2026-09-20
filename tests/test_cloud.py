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
        if (m, path) == ("GET", "/me"):
            if c.closing_reads:
                c.closing_reads -= 1
                if not c.closing_reads:
                    c.sessions.pop(c.profile["session"], None)
                    c.profile.update(state="stored", session=None)
            return self._send(200, {"uid": "u1", "email": "a@b.c", "balance_cents": 425,
                                    "price_cents_per_minute": 5, "can_rent": True,
                                    "active_session_count": len(c.sessions),
                                    "session_limit": 3, "profile": c.profile})
        if (m, path) == ("POST", "/sessions"):
            c.posts.append(body)
            if body.get("profile_id") and c.profile["state"] == "running":
                return self._send(409, {"error": "held", "code": "profile_running",
                                        "session": c.profile["session"]})
            sid = f"sid{len(c.posts):03d}"
            c.sessions[sid] = {"id": sid, "state": "provisioning",
                               "profile": body.get("profile_id"),
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
                if c.polls[sid] >= 2:
                    c.sessions[sid].update(state="ready", startup={"startup": "exact"}, adb={
                        "host": "live.example", "port": 22220, "code": "ph_code"})
                return self._send(200, c.sessions[sid])
            if m == "DELETE":
                if c.sessions.pop(sid).get("profile"):
                    c.profile.update(state="stored", session=None, saved_at=time.time())
                return self._send(200, {"released": True})
        return self._send(404, {"error": "not found"})

    do_GET = do_POST = do_DELETE = _handle


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

    def test_a_proxy_token_rides_along_as_the_gate_header(self):
        FakeCloud.valid.add("pck_ci")
        FakeCloud.seen_headers = []
        r = self.run_cli("cloud", "whoami", env={"PHONE_HARNESS_API_KEY": "pck_ci",
                                                  "PHONE_HARNESS_CLOUD_PROXY_TOKEN": "gate-1"})
        self.assertIn("a@b.c", r.stdout)
        self.assertIn("Bearer gate-1", [h.get("X-Exedev-Authorization") for h in FakeCloud.seen_headers])
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

if __name__ == "__main__":
    unittest.main()
