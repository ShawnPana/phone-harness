"""Cloud backend: the op vocabulary over a phone-cloud service.

A phone-cloud service (github: phone-cloud) rents a real iPhone in a
datacenter and speaks this package's op vocabulary over HTTP. CloudPhone
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
import json
import os
import tempfile
import urllib.error
import urllib.request

from .transport import Backend, Unsupported


def _request(base, token, method, path, body=None, timeout=180):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode())
        except Exception:
            err = {"error": f"HTTP {e.code}"}
        if err.get("unsupported"):
            raise Unsupported(err.get("error", "unsupported")) from None
        raise RuntimeError(
            f"phone-cloud: {err.get('error', e.code)}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"phone-cloud: cannot reach {base} ({e.reason}) — is the "
            "service up? Start one, or set PHONE_CLOUD_URL.") from None


def _service():
    base = os.environ.get("PHONE_CLOUD_URL", "http://127.0.0.1:8722")
    return base.rstrip("/"), os.environ.get("PHONE_CLOUD_TOKEN")


class CloudPhone(Backend):
    name = "cloud-phone"

    def __init__(self, base=None, token=None, session_id=None,
                 provider=None, caps=None):
        default_base, default_token = _service()
        self.base = (base or default_base).rstrip("/")
        self.token = token or default_token
        # Attach before create: an exported PHONE_CLOUD_SESSION means "this
        # phone", and silently renting a second one would be a second bill.
        session_id = session_id or os.environ.get("PHONE_CLOUD_SESSION")
        if session_id:
            info = self._http("GET", f"/sessions/{session_id}")
        else:
            body = {}
            if provider:
                body["provider"] = provider
            if caps:
                body["caps"] = caps
            # Creation waits far longer than any op: real hardware can queue
            # for minutes before the provider hands a device over.
            info = self._http("POST", "/sessions", body, timeout=640)
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
            return self._text_pixels(**kw)
        return self._rpc(op, **kw)

    def supports(self, op):
        return op in self._server_ops or op in ("screen.capture",
                                                "screen.text_pixels")

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

    def release(self):
        self._http("DELETE", f"/sessions/{self.session_id}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


# --- CLI (phone-harness cloud ...) -------------------------------------------

CLI_USAGE = """Usage:
  phone-harness cloud up [provider]   rent a phone (provider: service default)
  phone-harness cloud ls              list live sessions
  phone-harness cloud down <id|all>   release a session — the meter runs
"""


def _banner(info):
    print(f"cloud iPhone ready: {info.get('device')} "
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
        body = {"provider": args[1]} if len(args) > 1 else {}
        print("requesting a device (real hardware can queue for minutes)...",
              flush=True)
        _banner(_request(base, token, "POST", "/sessions", body, timeout=640))
        return 0

    if cmd == "ls":
        sessions = _request(base, token, "GET", "/sessions")
        if not sessions:
            print("no live sessions")
            return 0
        for s in sessions:
            print(f"{s['id']}  {s['provider']:<10} {s.get('device') or '?':<28}"
                  f" age {s['age_s']}s  idle {s['idle_s']}s")
        return 0

    if cmd == "down" and len(args) > 1:
        ids = ([s["id"] for s in _request(base, token, "GET", "/sessions")]
               if args[1] == "all" else [args[1]])
        if not ids:
            print("no live sessions")
        for sid in ids:
            r = _request(base, token, "DELETE", f"/sessions/{sid}")
            print(f"{sid}  {'released' if r.get('released') else 'not found'}")
        return 0

    print(CLI_USAGE, end="")
    return 0 if cmd in (None, "-h", "--help") else 2
