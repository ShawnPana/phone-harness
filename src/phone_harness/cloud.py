"""Cloud backend: the op vocabulary over a phone-cloud service.

A phone-cloud service (github: phone-cloud) rents a phone in a
datacenter and speaks this package's op vocabulary over HTTPS. CloudPhone
relays send() to it, so everything above the seam — helpers, agent skills,
OCR loops — drives a phone that isn't in the room. It knows nothing about
Appium or any device vendor; the service URL is the entire coupling.

Two ops are answered client-side rather than relayed:

  screen.capture       the wire carries PNG bytes; the local contract is a
                       file path, so the bytes land in a temp file here.
  screen.text_pixels   Vision OCR runs where Vision exists — this Mac —
                       over the capture. The server's screen.text comes
                       from the device's accessibility tree instead, so
                       the two sources finally mean different things on
                       the same phone.

Sessions are metered per-minute, which shapes the lifecycle: connect("cloud")
attaches to the session named by PHONE_CLOUD_SESSION rather than renting a
fresh phone, because helpers.py connects at import time and every CLI
invocation would otherwise open a new bill. Create one session with
`phone-harness cloud up`, export what its banner tells you, and every script
after that shares the phone until `phone-harness cloud down`.
"""
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .transport import Backend, Unsupported


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A configured service must not forward account credentials or an APK
        # to a redirect destination, including for existing JSON operations.
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
MAX_APK_BYTES = 256 * 1024 * 1024


class RequestError(RuntimeError):
    """Service rejection, retaining structured details for explicit recovery."""
    def __init__(self, message, *, status=None, detail=None):
        super().__init__(message)
        self.status = status
        self.detail = detail if isinstance(detail, dict) else {}


class CreateError(RuntimeError):
    """Creation/attachment was not confirmed. Never infer no phone was created."""
    def __init__(self, message, *, request_key=None, session_id=None, status=None):
        super().__init__(message)
        self.request_key, self.session_id, self.status = request_key, session_id, status


class InstallError(RuntimeError):
    """An install failure; outcome='unknown' must not be blindly retried."""
    def __init__(self, message, *, status=None, code=None, outcome=None):
        super().__init__(message)
        self.status, self.code, self.outcome = status, code, outcome


class _UploadBody:
    def __init__(self, source, size):
        self.source, self.remaining = source, size

    def read(self, size):
        if self.remaining == 0:
            return b""
        block = self.source.read(min(size, self.remaining))
        if not block:
            raise OSError("APK changed during upload")
        self.remaining -= len(block)
        return block


def _service_url(base):
    """Only literal loopback may carry account credentials without TLS."""
    try:
        target = urllib.parse.urlsplit(base)
        port = target.port
        host = target.hostname
        if (target.scheme not in ("http", "https") or not host
                or target.username is not None or target.password is not None
                or target.query or target.fragment or "\\" in base
                or any(ord(c) <= 32 or ord(c) == 127 for c in base)
                or port == 0):
            raise ValueError()
        if target.scheme == "http" and not ipaddress.ip_address(host).is_loopback:
            raise ValueError()
    except (TypeError, ValueError):
        raise RuntimeError("phone-cloud: use HTTPS for the service URL; HTTP is allowed only for a literal loopback address, without credentials, query or fragment") from None
    return base.rstrip("/")


def _install_apk(base, token, session_id, path, timeout=600):
    """Install into one existing session. Never create or retry a session."""
    if not token:
        raise InstallError("phone-cloud: set PHONE_CLOUD_TOKEN to your account API key")
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session_id):
        raise InstallError("phone-cloud: invalid session ID")
    try:
        base = _service_url(base)
    except RuntimeError as error:
        raise InstallError(str(error)) from None
    path = Path(path)
    if path.suffix.lower() != ".apk":
        raise InstallError("phone-cloud: supply a single .apk file; AAB/split sets are unsupported")
    try:
        source = open(path, "rb", opener=lambda name, flags:
                      os.open(name, flags | getattr(os, "O_NONBLOCK", 0)))
    except OSError:
        raise InstallError("phone-cloud: cannot open the APK file") from None
    with source:
        size = os.fstat(source.fileno()).st_size
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode) or not 4 <= size <= MAX_APK_BYTES:
            raise InstallError("phone-cloud: APK must be a regular file from 4 bytes through 256 MiB")
        if source.read(4) != b"PK\x03\x04":
            raise InstallError("phone-cloud: file is not an APK ZIP container")
        source.seek(0)
        digest, hashed = hashlib.sha256(), 0
        for block in iter(lambda: source.read(64 * 1024), b""):
            hashed += len(block)
            if hashed > size:
                raise InstallError("phone-cloud: APK changed while calculating its checksum")
            digest.update(block)
        if hashed != size:
            raise InstallError("phone-cloud: APK changed while calculating its checksum")
        checksum = digest.hexdigest()
        source.seek(0)
        request = urllib.request.Request(base.rstrip('/') + f"/sessions/{session_id}/apk", method="POST",
            data=_UploadBody(source, size), headers={"Authorization": "Bearer " + token,
                "Content-Type": "application/vnd.android.package-archive",
                "Content-Length": str(size), "X-APK-SHA256": checksum})
        try:
            with _OPENER.open(request, timeout=timeout) as response:
                raw = response.read(65537)
                if response.status != 200 or len(raw) > 65536:
                    raise ValueError("unexpected install response")
                receipt = json.loads(raw)
                if (not isinstance(receipt, dict) or receipt.get("installed") is not True
                        or receipt.get("bytes") != size or receipt.get("sha256") != checksum):
                    raise ValueError("installation receipt did not match uploaded bytes")
                return receipt
        except urllib.error.HTTPError as error:
            status = error.code
            try:
                raw = error.read(65537)
                detail = json.loads(raw) if len(raw) <= 65536 else {}
                if not isinstance(detail, dict):
                    detail = {}
            except (OSError, ValueError):
                detail = {}
            finally:
                error.close()
            unknown = status >= 500 or detail.get("outcome") == "unknown"
            message = str(detail.get("error") or f"installation refused (HTTP {status})")[:400]
            if unknown:
                message += "; outcome is unknown — inspect the phone before retrying"
            raise InstallError("phone-cloud: " + message, status=status,
                               code=detail.get("code"), outcome="unknown" if unknown else None) from None
        except (OSError, urllib.error.URLError, ValueError):
            raise InstallError("phone-cloud: installation outcome is unknown — inspect the phone before retrying",
                               outcome="unknown") from None


def _request(base, token, method, path, body=None, timeout=180, *, request_key=None):
    if request_key is not None:
        _valid_request_key(request_key)
        if method != "POST" or path != "/sessions":
            raise ValueError("request keys are supported only for POST /sessions")
    base = _service_url(base)
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if request_key is not None:
        req.add_header("Idempotency-Key", request_key)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            raw = e.read(65537)
            err = json.loads(raw) if len(raw) <= 65536 else {}
            if not isinstance(err, dict):
                err = {}
        except Exception:
            err = {"error": f"HTTP {e.code}"}
        finally:
            e.close()
        if err.get("unsupported"):
            raise Unsupported(err.get("error", "unsupported")) from None
        raise RequestError(
            f"phone-cloud: {err.get('error', e.code)}", status=e.code, detail=err) from None
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"phone-cloud: cannot reach {base} ({e.reason}) — is the "
            "service up? Start one, or set PHONE_CLOUD_URL.") from None


def _service():
    base = os.environ.get("PHONE_CLOUD_URL", "http://127.0.0.1:8722")
    return base.rstrip("/"), os.environ.get("PHONE_CLOUD_TOKEN")


def _wait_ready(base, token, info, timeout=900):
    """Poll a session out of "provisioning". The service answers create
    immediately — real hardware takes minutes to arrive — so readiness is
    a poll, not a long-held response. Info from a pre-state server has no
    "state" and passes straight through."""
    deadline = time.time() + timeout
    while info.get("state") == "provisioning":
        if time.time() > deadline:
            # Best-effort release: an abandoned session would otherwise go
            # ready later and bill until the reaper notices it idle.
            try:
                _request(base, token, "DELETE", f"/sessions/{info['id']}")
            except RuntimeError:
                pass
            raise RuntimeError("phone-cloud: timed out waiting for the "
                               "phone to provision")
        time.sleep(3)
        info = _request(base, token, "GET", f"/sessions/{info['id']}")
    if info.get("state") == "error":
        raise RuntimeError(
            f"phone-cloud: {info.get('error', 'provisioning failed')}")
    if info.get("state") not in (None, "ready"):
        raise RuntimeError(f"phone-cloud: session is {info['state']}; it is not ready for use")
    return info


def _valid_request_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", value):
        raise ValueError("phone-cloud: request key must contain 16–128 ASCII letters, digits, underscores or hyphens")
    return value


def _supports_create_requests(base, token):
    account = _request(base, token, "GET", "/me")
    return isinstance(account, dict) and account.get("session_create_idempotency") == "v1"


def creation_receipt(request_key, *, base=None, token=None):
    """Read your exact create receipt. Never allocate, attach, touch or release."""
    _valid_request_key(request_key)
    default_base, default_token = _service()
    base, token = base or default_base, token or default_token
    if not _supports_create_requests(base, token):
        raise RuntimeError("phone-cloud: this service does not support recoverable creation")
    receipt = _request(base, token, "GET", "/sessions/requests/" + request_key)
    if (not isinstance(receipt, dict) or receipt.get("request_key") != request_key
            or not isinstance(receipt.get("id"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", receipt["id"])):
        raise RuntimeError("phone-cloud: invalid create receipt; allocation remains unresolved")
    return receipt


def _create(base, token, body, *, request_key=None, on_request=None):
    if request_key is not None:
        _valid_request_key(request_key)
    supported = _supports_create_requests(base, token)
    if request_key is not None and not supported:
        raise RuntimeError("phone-cloud: this service does not support recoverable creation; no phone was requested")
    if supported:
        request_key = request_key or str(uuid.uuid4())
    if on_request:
        on_request(request_key)  # Record/print identity before sending the POST.
    info = None
    try:
        info = _request(base, token, "POST", "/sessions", body,
                        timeout=60, request_key=request_key)
        if (not isinstance(info, dict)
                or not isinstance(info.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", info["id"])
                or (request_key is not None and info.get("request_key") != request_key)):
            raise RuntimeError("phone-cloud: invalid create response")
        return _wait_ready(base, token, info)
    except (RuntimeError, OSError, ValueError) as error:
        sid = info.get("id") if isinstance(info, dict) and info.get("request_key") == request_key else None
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sid):
            sid = None
        recovery = (f"; inspect creation receipt {request_key} before requesting a replacement"
                    if request_key else "; legacy service: inspect your sessions before requesting a replacement")
        raise CreateError(str(error) + recovery, request_key=request_key,
                          session_id=sid, status=getattr(error, "status", None)) from None


class CloudPhone(Backend):
    name = "cloud-phone"

    def __init__(self, base=None, token=None, session_id=None,
                 provider=None, caps=None, request_key=None):
        default_base, default_token = _service()
        self.base = (base or default_base).rstrip("/")
        self.token = token or default_token
        # Attach before create: an exported PHONE_CLOUD_SESSION means "this
        # phone", and silently renting a second one would be a second bill.
        session_id = session_id or os.environ.get("PHONE_CLOUD_SESSION")
        self.request_key = request_key
        if session_id and request_key is not None:
            raise ValueError("phone-cloud: choose an existing session or a creation request key, not both")
        if session_id:
            if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session_id):
                raise RuntimeError("phone-cloud: invalid session ID")
            # Attaching mid-provision (cloud up still running) waits too.
            info = _wait_ready(self.base, self.token,
                               self._http("GET", f"/sessions/{session_id}"))
        else:
            body = {}
            if provider:
                body["provider"] = provider
            if caps:
                body["caps"] = caps
            info = _create(self.base, self.token, body, request_key=request_key,
                           on_request=lambda key: setattr(self, "request_key", key))
        self.session_id = info["id"]
        self.watch_url = info.get("watch_url")
        self.device = info.get("device")
        self._server_ops = set(info.get("ops") or [])

    # --- wire ------------------------------------------------------------

    def _http(self, method, path, body=None, timeout=180):
        return _request(self.base, self.token, method, path, body, timeout)

    def _rpc(self, op, **kw):
        return self._http("POST", f"/sessions/{self.session_id}/op",
                          {"op": op, "kw": kw})["result"]

    # --- the seam --------------------------------------------------------

    def send(self, op, **kw):
        if op == "screen.capture":
            return self._capture(kw.get("path"))
        if op == "screen.text_pixels":
            if sys.platform != "darwin":
                raise Unsupported("pixel OCR requires macOS Vision; use screen.text for the accessibility tree")
            return self._text_pixels(**kw)
        return self._rpc(op, **kw)

    def supports(self, op):
        if op == "screen.text_pixels":
            return sys.platform == "darwin"
        return op in self._server_ops or op == "screen.capture"

    # --- client-side ops -------------------------------------------------

    def _capture(self, path=None):
        r = self._rpc("screen.capture")
        if path is None:
            fd, path = tempfile.mkstemp(prefix="phone-cloud-", suffix=".png")
            os.close(fd)
        with open(path, "wb") as f:
            f.write(base64.b64decode(r["png_b64"]))
        return path, r["bounds"]

    def _text_pixels(self, min_confidence=0.3):
        from . import ocr as _vision
        path, win = self._capture()
        return [dict(o, source="pixels")
                for o in _vision.recognize(path, win)
                if o["confidence"] >= min_confidence]

    # --- lifecycle -------------------------------------------------------

    def install_apk(self, path, timeout=600):
        return _install_apk(self.base, self.token, self.session_id, path, timeout)

    def release(self):
        return self._http("DELETE", f"/sessions/{self.session_id}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


# --- CLI (phone-harness cloud ...) -------------------------------------------

CLI_USAGE = """Usage:
  phone-harness cloud up [provider] [--request-key KEY]   rent/recover one phone
  phone-harness cloud receipt KEY    inspect your create receipt without allocating
  phone-harness cloud ls              list live sessions
  phone-harness cloud down <id|all>   release a session — the meter runs
  phone-harness cloud install <id> <apk>  upload/install into an existing phone
"""


def _banner(info):
    print(f"cloud phone ready: {info.get('device')} "
          f"(session {info['id']}, provider {info.get('provider')})")
    if info.get("watch_url"):
        print(f"  watch    {info['watch_url']}")
    print(f"  attach   export PHONE_CLOUD_SESSION={info['id']} "
          "PHONE_HARNESS_PLATFORM=cloud")
    print(f"  release  phone-harness cloud down {info['id']}"
          "   # metered per-minute until you do")


def cli(args):
    base, token = _service()
    cmd = args[0] if args else None

    if cmd == "up":
        import argparse
        parser = argparse.ArgumentParser(prog="phone-harness cloud up")
        parser.add_argument("provider", nargs="?")
        parser.add_argument("--request-key")
        try:
            options = parser.parse_args(args[1:])
            body = {"provider": options.provider} if options.provider else {}
            def announce(key):
                print(f"creation request: {key}" if key else
                      "legacy service: recoverable creation unavailable", flush=True)
            _banner(_create(base, token, body, request_key=options.request_key, on_request=announce))
            return 0
        except (RuntimeError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 1

    if cmd == "receipt" and len(args) == 2:
        try:
            print(json.dumps(creation_receipt(args[1], base=base, token=token), indent=2))
            return 0
        except (RuntimeError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 1

    if cmd == "ls":
        sessions = _request(base, token, "GET", "/sessions")
        if not sessions:
            print("no live sessions")
            return 0
        for s in sessions:
            state = s.get("state", "ready")
            print(f"{s['id']}  {s['provider']:<10} {state:<12}"
                  f" {s.get('device') or '?':<28}"
                  f" age {s['age_s']}s  idle {s['idle_s']}s")
        return 0

    if cmd == "install" and len(args) == 3:
        try:
            receipt = _install_apk(base, token, args[1], args[2])
        except InstallError as error:
            print(str(error), file=sys.stderr)
            return 1
        print(f"{args[1]}  installed {receipt['bytes']} bytes  sha256={receipt['sha256']}")
        return 0

    if cmd == "down" and len(args) > 1:
        ids = ([s["id"] for s in _request(base, token, "GET", "/sessions")]
               if args[1] == "all" else [args[1]])
        if not ids:
            print("no live sessions")
        for sid in ids:
            r = _request(base, token, "DELETE", f"/sessions/{sid}")
            if r.get('cleanup_pending') or r.get('state') == 'closing':
                result = 'closing — billing stopped; cleanup is still pending'
            else:
                result = 'released' if r.get('released') else 'not found'
            print(f"{sid}  {result}")
        return 0

    print(CLI_USAGE, end="")
    return 0 if cmd in (None, "-h", "--help") else 2
