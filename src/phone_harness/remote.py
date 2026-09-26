"""A cloud phone driven by ops over HTTPS, not by ADB.

Some cloud phones have no shell to connect to: a real iPhone behind a Mac is
one. Its session carries a `control` grant instead of an `adb` block: a URL
and a token. Every op in the vocabulary becomes one POST to `<url>/op`, and
the answer is the op's value. Screenshots arrive as PNG bytes and are written
where a local backend would have written them, so helpers.py never learns
where the phone is.

What the far side cannot do comes back as `unsupported`, which is raised as
Unsupported here, exactly as a local backend would.
"""
import base64
import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from .transport import Backend, OP_NAMES, Unsupported

TMP = Path(tempfile.gettempdir()) / "phone-harness"
USER_AGENT = "phone-harness-cli"


class Remote(Backend):
    name = "remote"

    def __init__(self, url, token, expires_at=None):
        self.url = url.rstrip("/")
        self.token = token
        self.expires_at = expires_at
        self._ops = None

    # --- the wire -----------------------------------------------------------

    def _call(self, method, path, body=None, timeout=60):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}", "User-Agent": USER_AGENT}
        if data:
            headers["Content-Type"] = "application/json"
        # The same gate the API sits behind on a private instance (cloud.py).
        proxy = os.environ.get("PHONE_HARNESS_CLOUD_PROXY_TOKEN")
        if proxy:
            headers["X-Exedev-Authorization"] = f"Bearer {proxy}"
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if r.headers.get("Content-Type", "").startswith("application/json") else raw
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read())
            except ValueError:
                err = {}
            message = err.get("error") or f"the cloud phone answered {e.code}"
            if err.get("unsupported"):
                raise Unsupported(message) from None
            if e.code == 404:
                raise RuntimeError("the cloud phone session is gone or its grant expired; "
                                   "`phone-harness cloud` shows whether it is still running") from None
            raise RuntimeError(message) from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"could not reach the cloud phone: {e.reason}") from None

    def _op(self, op, timeout=60, **kw):
        return self._call("POST", "/op", {"op": op, "kw": kw}, timeout=timeout)["result"]

    # --- capabilities -------------------------------------------------------

    def _known(self):
        if self._ops is None:
            try:
                self._ops = set(self._call("GET", "/ops").get("ops") or [])
            except RuntimeError:
                self._ops = set(op for op in OP_NAMES if op not in ("tree", "raw"))
        return self._ops

    def supports(self, op):
        # What the far side offers, plus the local bookkeeping ops (focus,
        # refocus) that mean nothing for a phone nobody's screen shows.
        return op in self._known() or getattr(self, self._slot(op), None) is not None

    def send(self, op, **kw):
        if not self.supports(op):
            raise Unsupported(f"this cloud phone cannot {op!r}")
        fn = getattr(self, self._slot(op), None)
        if fn is not None:
            return fn(**kw)
        return self._op(op, **kw)

    # --- the ops whose shape is local -----------------------------------------

    def _screen_capture(self, path=None):
        r = self._op("screen.capture", timeout=60)
        TMP.mkdir(exist_ok=True)
        path = Path(path or TMP / "remote.png")
        path.write_bytes(base64.b64decode(r["png_b64"]))
        return str(path), r["bounds"]

    def _input_text(self, s, delay=0.03, keystrokes=False):
        return self._op("input.text", timeout=max(60, 0.2 * len(s) + 30), s=s, delay=delay, keystrokes=keystrokes)

    def _apps_launch(self, name, fresh=False):
        return self._op("apps.launch", timeout=90, name=name, fresh=fresh)

    def _apps_list(self, include_system=False):
        return self._op("apps.list", timeout=90, include_system=include_system)

    def _session_refocus(self):
        return None

    def _session_detail(self):
        return f"cloud phone at {self.url}"

    def _focus_probe(self):
        return (True,)

    def _focus_diff(self, before, after):
        return {"raised": False, "stole_focus": False}
