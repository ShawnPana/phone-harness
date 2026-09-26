"""`phone-harness host`: this Mac's iPhones, offered to Phone Harness Cloud.

One HTTP service in front of one CoreDevice daemon per plugged-in iPhone. It
speaks the worker contract phone-cloud already uses for shlut (reserve, poll,
frame, op, viewer, release, profiles, capacity), so the API needs no new
transport to drive a real iPhone. Two things differ from a container worker
and both follow from the phone being physical: a lease never boots anything
(the daemon is always up) and a release never wipes anything.

A phone belongs to one account: `assignments` maps the account's profile id to
a UDID. That map is the whole access gate; the API asks `GET /v1/profiles/<id>`
and a 404 means "no iPhone for this account".

Reachability is somebody else's job. The service binds 127.0.0.1; a reverse
tunnel or an ingress puts it where the API and the customer can see it. The
customer-facing paths (`/iphone/v/<token>/…` for the live mirror,
`/iphone/c/<token>/…` for ops) carry their own per-lease tokens, so they can
sit on a public origin while the worker paths stay behind the bearer token.

Files (config dir): host.json {token, port, assignments{profile_id: udid}}.
Files (state dir):  host/<udid>/ is PHONE_HARNESS_HOME for that phone's daemon;
                    host-leases.json survives a restart so the API can adopt.
"""
import argparse
import base64
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config
from .coredevice import _LOCKED_MARKERS

DEFAULT_PORT = 8730
DAEMON_START_TIMEOUT = 240
SUPERVISE_EVERY = 10
FRAME_MAX = 8 * 1024 * 1024
KIND_ABSENT = "shlut-expired-reservation-v1"

# Public op -> (daemon op, needs the phone unlocked). Argument names are the
# vocabulary's own (transport.py); the daemon takes the same names.
OPS = {
    "screen.capture": ("capture", False),
    "screen.text": (None, False),          # OCR here, on the capture
    "screen.text_pixels": (None, False),
    "screen.bounds": (None, False),
    "screen.require": (None, False),
    "input.tap": ("tap", True),
    "input.press": ("press", True),
    "input.drag": ("drag", True),
    "input.scroll": ("scroll", True),
    "input.keys": ("keys", True),
    "input.text": ("text", True),
    "nav.home": ("home", False),
    "nav.recents": ("recents", True),
    "apps.launch": ("launch", True),
    "apps.list": ("apps", False),
    "clipboard.read": ("clipboard", False),
    "session.state": (None, False),
    "session.require": (None, False),
}


class HostError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status, self.extra = status, extra


# --- configuration -----------------------------------------------------------

def _config_path():
    return config.config_dir() / "host.json"


def load_config():
    try:
        cfg = json.loads(_config_path().read_text())
    except (OSError, ValueError):
        cfg = {}
    cfg.setdefault("assignments", {})
    cfg.setdefault("port", DEFAULT_PORT)
    return cfg


def save_config(cfg):
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def ensure_token(cfg):
    if not cfg.get("token"):
        cfg["token"] = "phk_" + secrets.token_hex(24)
        save_config(cfg)
    return cfg["token"]


# --- one phone: its daemon and its socket -------------------------------------

class Phone:
    """A CoreDevice daemon for one UDID, kept alive by this process.

    The daemon gets its own PHONE_HARNESS_HOME so several phones never share a
    state file or a socket. Ops are one JSON line each on the daemon's socket,
    exactly what `phone-harness ios` does on a single-phone Mac.
    """

    def __init__(self, udid):
        self.udid = udid
        self.home = config.state_dir() / "host" / udid
        self.run = self.home / "state" / "run"
        self.lock = threading.Lock()
        self.proc = None
        self.started_at = 0.0
        self.failures = 0
        self._gate_at, self._gate_state = 0.0, None

    # daemon lifecycle
    def state(self):
        try:
            st = json.loads((self.run / "coredevice.json").read_text())
        except (OSError, ValueError):
            return None
        pid = st.get("pid")
        if not isinstance(pid, int):
            return None
        try:
            os.kill(pid, 0)
        except OSError:
            return None
        return st

    def phase(self):
        st = self.state()
        if st is None:
            return "booting" if time.time() - self.started_at < DAEMON_START_TIMEOUT else "error"
        if st.get("ready"):
            return "ready"
        if st.get("phase") == "failed":
            return "error"
        return "booting"

    def ensure_daemon(self):
        """Start the daemon when none is alive. Idempotent; called by the
        supervisor every few seconds and by any op that finds it down."""
        if self.state() is not None:
            return
        if self.proc is not None and self.proc.poll() is None:
            return                                  # starting, not yet ready
        backoff = min(60, 5 * self.failures)
        if time.time() - self.started_at < backoff:
            return
        self.run.mkdir(parents=True, exist_ok=True)
        (self.run / "coredevice.json").unlink(missing_ok=True)
        env = dict(os.environ, PHONE_HARNESS_HOME=str(self.home))
        argv = [sys.executable, "-m", "phone_harness.coredevice_daemon",
                "--serial", self.udid, "--connection", "usb", "--mirror"]
        log = open(self.run / "coredevice.log", "ab")
        self.proc = subprocess.Popen(argv, stdout=log, stderr=log, env=env, start_new_session=True)
        self.started_at = time.time()
        self.failures += 1

    def note_alive(self):
        if self.phase() == "ready":
            self.failures = 0

    # the socket
    def request(self, op, timeout=30.0, **kw):
        st = self.state()
        if st is None or not st.get("ready"):
            raise HostError(503, "the iPhone session is not up; the host is restarting it")
        ep = st.get("endpoint") or {}
        try:
            if ep.get("kind") == "tcp":
                s = socket.create_connection(("127.0.0.1", int(ep["port"])), timeout=timeout)
            else:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(timeout)
                s.connect(ep.get("path") or str(self.run / "coredevice.sock"))
        except OSError as e:
            raise HostError(503, f"the iPhone session did not answer ({e})")
        try:
            s.sendall((json.dumps({"op": op, **kw}) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        finally:
            s.close()
        if not buf:
            raise HostError(503, "the iPhone session closed without answering")
        reply = json.loads(buf)
        if reply.get("ok"):
            return reply.get("result")
        if reply.get("kind") == "Unsupported":
            raise HostError(400, reply.get("error") or "unsupported", unsupported=True)
        raise HostError(502, reply.get("error") or "the iPhone did not answer")

    # ops
    def bounds(self):
        st = self.state() or {}
        return {"x": 0, "y": 0, "w": int(st.get("w") or 0), "h": int(st.get("h") or 0), "id": self.udid}

    def capture(self):
        """-> (png bytes, bounds)."""
        path = self.run / "frame.png"
        with self.lock:
            r = self.request("capture", path=str(path))
        data = Path(r["path"]).read_bytes()
        return data, {"x": 0, "y": 0, "w": r["w"], "h": r["h"], "id": self.udid}

    def text(self, min_confidence=0.3):
        from . import ocr
        path = self.run / "frame.png"
        with self.lock:
            r = self.request("capture", path=str(path))
        win = {"x": 0, "y": 0, "w": r["w"], "h": r["h"], "id": self.udid}
        return [dict(o, source="pixels") for o in ocr.recognize(r["path"], win)
                if o["confidence"] >= min_confidence]

    def session_state(self):
        """'ready' | 'locked' | 'not-running', read off the screen, cached 2 s.
        Unlocking is the owner's; this only reports."""
        if time.time() - self._gate_at < 2.0:
            return self._gate_state
        try:
            texts = " ".join(o["text"] for o in self.text(0.0)).lower()
            state = "locked" if any(m in texts for m in _LOCKED_MARKERS) else "ready"
        except HostError:
            state = "not-running"
        self._gate_at, self._gate_state = time.time(), state
        return state

    def _gate(self):
        state = self.session_state()
        if state == "locked":
            raise HostError(409, "The iPhone is locked. Unlock it on the phone, then retry; "
                                 "the host never enters a passcode.", code="locked")
        if state != "ready":
            raise HostError(503, "the iPhone session is not up")

    def op(self, name, kw):
        """One op from the public vocabulary; the value the API and the CLI see."""
        if name not in OPS:
            raise HostError(400, f"this phone cannot {name!r}", unsupported=True)
        daemon_op, gated = OPS[name]
        if name == "screen.capture":
            data, bounds = self.capture()
            return {"png_b64": base64.b64encode(data).decode(), "bounds": bounds}
        if name in ("screen.text", "screen.text_pixels"):
            return self.text(float(kw.get("min_confidence", 0.3)))
        if name == "screen.bounds":
            return self.bounds()
        if name in ("screen.require", "session.require"):
            self._gate()
            return self.bounds()
        if name == "session.state":
            return self.session_state()
        if gated:
            self._gate()
        timeout = 60 if name in ("apps.launch", "apps.list") else 30
        if name == "input.text":
            timeout = max(30.0, 0.2 * len(str(kw.get("s", ""))) + 10)
        with self.lock:
            result = self.request(daemon_op, timeout=timeout, **kw)
        if name in ("nav.home", "nav.recents"):
            time.sleep(0.6)
        elif name == "apps.launch":
            time.sleep(0.8)
        return result if result is not None else True

    def mirror_port(self):
        st = self.state() or {}
        url = st.get("mirror_url") or ""
        try:
            return int(urllib.parse.urlsplit(url).port)
        except (TypeError, ValueError):
            return None


# --- leases and tokens --------------------------------------------------------

class Host:
    def __init__(self, cfg):
        self.cfg = cfg
        self.token = ensure_token(cfg)
        self.phones = {}                     # udid -> Phone
        self.leases = {}                     # id -> {...}
        self.tokens = {}                     # token -> {lease, mode|control, expires_at}
        self.lock = threading.Lock()
        self.leases_path = config.state_dir() / "host-leases.json"
        self._load_leases()
        for udid in set(cfg["assignments"].values()):
            self.phones[udid] = Phone(udid)

    # persistence: leases outlive the process so the API can adopt after a restart
    def _load_leases(self):
        try:
            data = json.loads(self.leases_path.read_text())
        except (OSError, ValueError):
            return
        now = time.time()
        self.leases = {k: v for k, v in (data.get("leases") or {}).items() if v.get("expires_at", 0) > now}
        self.tokens = {k: v for k, v in (data.get("tokens") or {}).items()
                       if v.get("expires_at", 0) > now and v.get("lease") in self.leases}

    def _save_leases(self):
        self.leases_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.leases_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"leases": self.leases, "tokens": self.tokens}))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.leases_path)

    def supervise(self):
        while True:
            for phone in list(self.phones.values()):
                try:
                    phone.ensure_daemon()
                    phone.note_alive()
                except Exception:
                    pass
            with self.lock:
                now = time.time()
                expired = [k for k, v in self.leases.items() if v["expires_at"] <= now]
                for k in expired:
                    self._end_lease(k, expired=True)
                if expired:
                    self._save_leases()
            time.sleep(SUPERVISE_EVERY)

    def phone_of(self, udid):
        phone = self.phones.get(udid)
        if phone is None:
            phone = self.phones[udid] = Phone(udid)
            phone.ensure_daemon()
        return phone

    def profile_udid(self, profile_id):
        return self.cfg["assignments"].get(profile_id)

    def lease_of_udid(self, udid):
        for k, v in self.leases.items():
            if v["udid"] == udid:
                return k
        return None

    def reserve(self, body):
        pid, lease_id = body.get("profile_id"), body.get("id")
        if not isinstance(lease_id, str) or not lease_id:
            raise HostError(400, "id is required")
        if not isinstance(pid, str) or not pid:
            raise HostError(400, "profile_id is required: an iPhone is never a temporary phone")
        udid = self.profile_udid(pid)
        if udid is None:
            raise HostError(404, "no iPhone is assigned to this profile")
        expires_at = body.get("expires_at")
        if type(expires_at) not in (int, float) or expires_at <= time.time():
            raise HostError(400, "expires_at must be in the future")
        with self.lock:
            if lease_id in self.leases:
                raise HostError(409, "already exists")
            holder = self.lease_of_udid(udid)
            if holder is not None:
                raise HostError(409, f"this iPhone is held by lease {holder}")
            self.leases[lease_id] = {"id": lease_id, "udid": udid, "profile_id": pid,
                                     "client": body.get("client_scope"), "expires_at": float(expires_at),
                                     "created": time.time()}
            self._save_leases()
        self.phone_of(udid).ensure_daemon()
        return self.describe(lease_id)

    def lease(self, lease_id):
        lease = self.leases.get(lease_id)
        if lease is None:
            raise HostError(404, "no such phone")
        return lease

    def describe(self, lease_id):
        lease = self.lease(lease_id)
        phone = self.phone_of(lease["udid"])
        st = phone.state() or {}
        return {"id": lease_id, "owner": lease["client"], "state": phone.phase(),
                "ops": sorted(OPS), "startup": {"startup": "exact"},
                "device": st.get("model") or "iPhone", "expires_at": lease["expires_at"]}

    def _end_lease(self, lease_id, expired=False):
        lease = self.leases.pop(lease_id, None)
        if lease is None:
            return
        self.tokens = {k: v for k, v in self.tokens.items() if v.get("lease") != lease_id}
        self.ended = getattr(self, "ended", {})
        self.ended[lease_id] = {"expires_at": lease["expires_at"], "client": lease["client"],
                                "ended_at": time.time()}
        phone = self.phones.get(lease["udid"])
        if phone is not None:
            try:
                phone.op("nav.home", {})
            except HostError:
                pass

    def release(self, lease_id):
        with self.lock:
            self._end_lease(lease_id)
            self._save_leases()

    def mint(self, lease_id, kind, ttl, mode="control"):
        lease = self.lease(lease_id)
        expires_at = min(lease["expires_at"], time.time() + ttl)
        token = secrets.token_urlsafe(24)
        with self.lock:
            self.tokens[token] = {"lease": lease_id, "kind": kind, "mode": mode, "expires_at": expires_at}
            self._save_leases()
        return token, expires_at

    def token_phone(self, token, kind):
        grant = self.tokens.get(token)
        if not grant or grant["kind"] != kind or grant["expires_at"] <= time.time():
            raise HostError(404, "not found")
        lease = self.leases.get(grant["lease"])
        if lease is None:
            raise HostError(404, "not found")
        return self.phone_of(lease["udid"]), grant

    def absence(self, lease_id, client_scope, expires_at):
        gone = getattr(self, "ended", {}).get(lease_id)
        live = self.leases.get(lease_id)
        if live is not None:
            absent = False
        elif gone is not None and gone["client"] == client_scope and gone["expires_at"] == expires_at:
            absent = time.time() >= expires_at
        else:
            raise HostError(404, "unknown reservation")
        return {"kind": KIND_ABSENT, "id": lease_id, "client_scope": client_scope,
                "expires_at": expires_at, "observed_at": time.time(), "absent": absent}

    def profile(self, profile_id):
        udid = self.profile_udid(profile_id)
        if udid is None:
            raise HostError(404, "no iPhone is assigned to this profile")
        phone = self.phone_of(udid)
        st = phone.state() or {}
        return {"state": "running" if self.lease_of_udid(udid) else "stored", "exact": True,
                "saved_at": None, "bytes": None, "startup": {"startup": "exact"},
                "device": st.get("model") or "iPhone", "online": phone.phase() == "ready"}

    def capacity(self):
        ready = sum(1 for p in self.phones.values() if p.phase() == "ready")
        return {"phones": len(self.phones), "ready": ready, "leased": len(self.leases)}


# --- HTTP -----------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "phone-harness-host/1"
    protocol_version = "HTTP/1.1"
    host = None            # set on the class by serve()
    origin = None          # public origin for the viewer and control URLs

    def log_message(self, fmt, *args):
        pass

    # helpers
    def _json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _bytes(self, status, data, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            body = json.loads(raw)
        except ValueError:
            raise HostError(400, "body must be JSON")
        if not isinstance(body, dict):
            raise HostError(400, "body must be a JSON object")
        return body

    def _worker_auth(self):
        auth = self.headers.get("Authorization") or ""
        if not auth.startswith("Bearer ") or not secrets.compare_digest(auth[7:].strip(), self.host.token):
            raise HostError(401, "unauthorized")

    def _bearer(self):
        auth = self.headers.get("Authorization") or ""
        return auth[7:].strip() if auth.startswith("Bearer ") else ""

    # dispatch
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method):
        try:
            self._dispatch(method)
        except HostError as e:
            self._json(e.status, {"error": str(e), **e.extra})
        except Exception as e:                       # never leak a traceback to a client
            self._json(500, {"error": f"{type(e).__name__}: {e}"[:300]})

    def _dispatch(self, method):
        url = urllib.parse.urlsplit(self.path)
        path, query = url.path, dict(urllib.parse.parse_qsl(url.query))
        parts = [p for p in path.split("/") if p]
        host = self.host

        # customer-facing, token in the path
        if len(parts) >= 3 and parts[0] == "iphone" and parts[1] in ("v", "c"):
            kind = "viewer" if parts[1] == "v" else "control"
            phone, grant = host.token_phone(parts[2], kind)
            rest = "/" + "/".join(parts[3:])
            if kind == "control":
                return self._control(method, phone, rest)
            if method == "POST" and grant.get("mode") == "view":
                raise HostError(403, "this link is view-only")
            if rest == "/" and not path.endswith("/"):
                self.send_response(302)
                self.send_header("Location", path + "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._proxy_mirror(method, phone, rest + ("?" + url.query if url.query else ""))

        # worker contract, bearer host token
        self._worker_auth()
        if method == "GET" and path == "/capacity":
            return self._json(200, host.capacity())
        if method == "POST" and path == "/v1/disposable-phones":
            return self._json(201, host.reserve(self._body()))
        if len(parts) == 4 and parts[:2] == ["v1", "disposable-phones"] and parts[3] == "absence" and method == "GET":
            try:
                expires_at = float(query.get("expires_at", ""))
            except ValueError:
                raise HostError(400, "expires_at must be a number")
            return self._json(200, host.absence(parts[2], query.get("client_scope"), expires_at))
        if len(parts) >= 3 and parts[:2] == ["v1", "profiles"]:
            if len(parts) == 3 and method == "GET":
                return self._json(200, host.profile(parts[2]))
            if len(parts) == 4 and parts[3] == "reset" and method == "POST":
                host.profile(parts[2])
                raise HostError(409, "a real iPhone cannot be reset")
            raise HostError(404, "not found")
        if len(parts) >= 2 and parts[0] == "phones":
            lease_id = parts[1]
            sub = parts[2] if len(parts) > 2 else None
            if sub is None and method == "GET":
                return self._json(200, host.describe(lease_id))
            if sub is None and method == "DELETE":
                host.lease(lease_id)
                host.release(lease_id)
                return self._json(200, {"released": True})
            lease = host.lease(lease_id)
            phone = host.phone_of(lease["udid"])
            if sub == "frame.png" and method == "GET":
                data, _ = phone.capture()
                return self._bytes(200, data[:FRAME_MAX], "image/png")
            if sub == "op" and method == "POST":
                body = self._body()
                return self._json(200, {"result": phone.op(body.get("op"), body.get("kw") or {})})
            if sub == "viewer" and method == "POST":
                body = self._body()
                ttl = body.get("ttl_seconds")
                if type(ttl) is not int or ttl < 1:
                    raise HostError(400, "ttl_seconds must be a positive integer")
                mode = body.get("mode") or "control"
                token, expires_at = host.mint(lease_id, "viewer", ttl, mode)
                return self._json(200, {"url": f"{self.origin}/iphone/v/{token}/", "expires_at": expires_at,
                                        "mode": mode})
            if sub == "control" and method == "POST":
                lease = host.lease(lease_id)
                token, expires_at = host.mint(lease_id, "control", int(lease["expires_at"] - time.time()) + 1)
                return self._json(200, {"url": f"{self.origin}/iphone/c/{token}", "token": token,
                                        "expires_at": expires_at})
            raise HostError(404, "not found")            # /adb and everything else
        raise HostError(404, "not found")

    def _control(self, method, phone, rest):
        if method == "POST" and rest == "/op":
            body = self._body()
            return self._json(200, {"result": phone.op(body.get("op"), body.get("kw") or {})})
        if method == "GET" and rest == "/frame.png":
            data, _ = phone.capture()
            return self._bytes(200, data, "image/png")
        if method == "GET" and rest == "/ops":
            return self._json(200, {"ops": sorted(OPS), "bounds": phone.bounds(), "state": phone.phase()})
        raise HostError(404, "not found")

    def _proxy_mirror(self, method, phone, target):
        """Relay one request to the daemon's mirror server and stream the
        answer back. A raw byte relay, so the long-lived video response
        (`/stream.bin`) flows without buffering."""
        port = phone.mirror_port()
        if port is None:
            raise HostError(503, "the live mirror is not up yet")
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        try:
            up = socket.create_connection(("127.0.0.1", port), timeout=10)
        except OSError:
            raise HostError(503, "the live mirror did not answer")
        try:
            head = f"{method} {target} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n"
            for k in ("Content-Type", "Accept", "Range"):
                v = self.headers.get(k)
                if v:
                    head += f"{k}: {v}\r\n"
            head += f"Content-Length: {len(body)}\r\n\r\n"
            up.sendall(head.encode() + body)
            up.settimeout(None)
            self.close_connection = True
            while True:
                chunk = up.recv(65536)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
        finally:
            up.close()


def serve(cfg, port, origin):
    host = Host(cfg)
    Handler.host = host
    Handler.origin = origin.rstrip("/")
    threading.Thread(target=host.supervise, daemon=True, name="supervise").start()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    print(f"phone-harness host: {len(host.phones)} phone(s), http://127.0.0.1:{port}, origin {Handler.origin}",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


# --- CLI ------------------------------------------------------------------------

CLI_USAGE = """Usage:
  phone-harness host serve [--port N] [--origin URL]   run the host (foreground)
  phone-harness host token                              print the bearer token the API uses (made on first use)
  phone-harness host assign PROFILE_ID UDID             give an account's profile this iPhone
  phone-harness host unassign PROFILE_ID
  phone-harness host status                             assignments, daemons, leases
"""


def cli(args):
    ap = argparse.ArgumentParser(prog="phone-harness host", usage=CLI_USAGE, add_help=False)
    ap.add_argument("verb", nargs="?")
    ap.add_argument("rest", nargs="*")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--origin", default=None, help="public origin the viewer/control URLs live on")
    ap.add_argument("-h", "--help", action="store_true")
    ns = ap.parse_args(args)
    cfg = load_config()
    if ns.help or not ns.verb:
        print(CLI_USAGE)
        return 0
    if ns.verb == "token":
        print(ensure_token(cfg))
        return 0
    if ns.verb == "assign":
        if len(ns.rest) != 2:
            sys.exit("Usage: phone-harness host assign PROFILE_ID UDID")
        cfg["assignments"][ns.rest[0]] = ns.rest[1]
        save_config(cfg)
        print(f"{ns.rest[0]} -> {ns.rest[1]}")
        return 0
    if ns.verb == "unassign":
        if len(ns.rest) != 1:
            sys.exit("Usage: phone-harness host unassign PROFILE_ID")
        cfg["assignments"].pop(ns.rest[0], None)
        save_config(cfg)
        return 0
    if ns.verb == "status":
        print(f"config   {_config_path()}")
        print(f"port     {cfg.get('port')}")
        for pid, udid in cfg["assignments"].items():
            st = Phone(udid).state() or {}
            print(f"phone    {udid}  profile {pid}  {st.get('phase') or 'daemon not running'}")
        try:
            leases = json.loads((config.state_dir() / "host-leases.json").read_text()).get("leases", {})
        except (OSError, ValueError):
            leases = {}
        for k, v in leases.items():
            print(f"lease    {k}  {v['udid']}  expires in {int(v['expires_at'] - time.time())}s")
        return 0
    if ns.verb == "serve":
        port = ns.port or int(cfg.get("port") or DEFAULT_PORT)
        origin = ns.origin or cfg.get("origin") or f"http://127.0.0.1:{port}"
        if ns.port or ns.origin:
            cfg.update(port=port, origin=origin)
            save_config(cfg)
        serve(cfg, port, origin)
        return 0
    sys.exit(CLI_USAGE)
