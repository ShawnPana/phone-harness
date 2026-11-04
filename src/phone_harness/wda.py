"""WebDriverAgent HTTP client — no Appium dependency.

Talks to a WebDriverAgent instance running on a USB-connected iPad,
reachable through an iproxy tunnel (default localhost:8100).

WDA is Apple's XCUITest-based automation server. It runs on the iPad,
installed once via Xcode (Product > Test on WebDriverAgentRunner target).
iproxy forwards localhost:8100 to the iPad's port 8100 over USB.

Reference: https://github.com/appium/WebDriverAgent
"""

import json
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin

# ---- Configuration ------------------------------------------------------

WDA_URL = "http://localhost:8100"
DEFAULT_TIMEOUT = 30          # seconds for HTTP requests
SESSION_TIMEOUT = 60          # how long to wait for WDA to be reachable
RETRY_COUNT = 3
RETRY_DELAY = 1.0             # seconds between retries


def set_url(url):
    """Override the WDA base URL. Set from PHONE_HARNESS_WDA_URL env var."""
    global WDA_URL
    WDA_URL = url


# ---- HTTP helpers -------------------------------------------------------


def _request(method, path, data=None, timeout=DEFAULT_TIMEOUT):
    """Make an HTTP request to WDA. Returns parsed JSON or raises.

    Connection refused → WDA or iproxy not running.
    404 on session ops → session expired (caller recreates).
    """
    url = urljoin(WDA_URL, path)
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"} if body else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # WDA returns error details in the body
        try:
            detail = json.loads(e.read().decode("utf-8"))
        except Exception:
            detail = {}
        raise RuntimeError(
            f"WDA {method} {path} failed: HTTP {e.code} — "
            f"{detail.get('value', {}).get('error', str(e))}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"WDA unreachable at {WDA_URL} ({e.reason}). "
            f"Is iproxy running? Run: iproxy 8100 8100"
        ) from e


def _retry(fn, *args, **kwargs):
    """Retry with backoff."""
    for attempt in range(RETRY_COUNT):
        try:
            return fn(*args, **kwargs)
        except RuntimeError as e:
            if attempt == RETRY_COUNT - 1:
                raise
            time.sleep(RETRY_DELAY * (attempt + 1))


# ---- Public API ---------------------------------------------------------


class WDAClient:
    """Stateful client. Holds one WDA session at a time."""

    def __init__(self, url=None):
        if url:
            set_url(url)
        self._session_id = None
        self._session_data = None

    # -- Connection -------------------------------------------------------

    def is_ready(self):
        """True if WDA is running and a device is attached."""
        try:
            status = _retry(_request, "GET", "/status", timeout=5)
            return status.get("value", {}).get("state") == "success"
        except RuntimeError:
            return False

    def wait_ready(self, timeout=SESSION_TIMEOUT):
        """Block until WDA is reachable, or raise."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_ready():
                return True
            time.sleep(1)
        raise RuntimeError(
            f"WDA not reachable after {timeout}s. "
            f"Check: iPad connected? iproxy running? WDA built to device?"
        )

    def create_session(self, bundle_id=None):
        """Create a WDA session. Optionally target a specific app.

        Returns the session id.
        """
        capabilities = {}
        if bundle_id:
            capabilities["bundleId"] = bundle_id

        resp = _request("POST", "/session", data={
            "capabilities": {
                "alwaysMatch": capabilities or {},
            }
        })
        self._session_id = resp.get("sessionId")
        self._session_data = resp.get("value", {})
        if not self._session_id:
            raise RuntimeError(f"WDA session creation returned no sessionId: {resp}")
        return self._session_id

    @property
    def session_id(self):
        if not self._session_id:
            self.create_session()
        return self._session_id

    def _session_path(self, subpath):
        """/session/:id/<subpath>"""
        return f"/session/{self.session_id}/{subpath}"

    # -- Touch ------------------------------------------------------------

    def tap(self, x, y):
        """Single tap at iPad-native coordinates."""
        return _request("POST", self._session_path(f"wda/tap/{x},{y}"))

    def double_tap(self, x, y):
        return _request("POST", self._session_path(f"wda/doubleTap/{x},{y}"))

    def long_press(self, x, y, duration=0.8):
        """Press and hold at (x, y) for `duration` seconds."""
        return _request("POST", self._session_path("wda/pressAndHold"), data={
            "x": x, "y": y, "duration": duration,
        })

    def drag(self, x1, y1, x2, y2, duration=0.35):
        """Swipe/drag from (x1,y1) to (x2,y2)."""
        return _request("POST",
                        self._session_path("wda/dragfromtoforduration"),
                        data={
                            "fromX": x1, "fromY": y1,
                            "toX": x2, "toY": y2,
                            "duration": duration,
                        })

    def scroll(self, x, y, dx, dy, steps=6):
        """Content-space scroll at (x, y) by (dx, dy).

        TODO: This is NOT a native WDA endpoint. WDA has /wda/scroll which
        scrolls an element. For a generic scroll at screen coordinates we
        need to either:
        - Use a drag gesture with the right parameters
        - Or find the scrollable element at (x,y) and call /wda/scroll on it

        For now, falls through to a drag — which is a momentum flick,
        not a precise scroll. Match Android's scroll() behavior if possible.
        """
        # WDA coordinates are flipped for drags in some versions.
        # Measure first on a real iPad.
        return self.drag(x, y, x + dx, y + dy, duration=0.3)

    # -- Keyboard ---------------------------------------------------------

    def type_text(self, text):
        """Type text via WDA keyboard. Goes through iOS autocorrect.

        For paste-like behavior (no autocorrect), we'd need a custom WDA
        endpoint that sets UIPasteboard.general.string, or a helper that
        splits on word boundaries.

        TODO: Measure autocorrect behavior on real iPad. If it rewrites
        words like iPhone Mirroring keystrokes does, add a paste path.
        """
        # WDA keys endpoint can send a string of characters.
        # Each character becomes a key event.
        return _request("POST", self._session_path("wda/keys"), data={
            "keys": [text],
        })

    def press_key(self, key):
        """Press a single key. Valid: 'enter', 'backspace', 'tab', etc."""
        return _request("POST", self._session_path("wda/keys"), data={
            "keys": [f"\\uE007" if key == "enter" else key],
        })

    # -- Navigation -------------------------------------------------------

    def home(self):
        """Press the Home button (or swipe up on Face ID iPads)."""
        return _request("POST", self._session_path("wda/homeButton"))

    def app_switcher(self):
        """Open the app switcher."""
        return _request("POST", self._session_path("wda/appSwitcher"))

    # -- Apps -------------------------------------------------------------

    def launch_app(self, bundle_id):
        """Launch an app by bundle identifier.

        TODO: Build a name-to-bundle-id map for common apps.
        For now caller passes the bundle id directly.
        """
        return _request("POST", self._session_path("wda/appLauncher"), data={
            "bundleId": bundle_id,
        })

    def current_bundle_id(self):
        """The foreground app's bundle id, if WDA exposes it."""
        resp = _request("GET", self._session_path(""))
        return resp.get("value", {}).get("bundleId")

    # -- Screenshot (fallback) --------------------------------------------

    def screenshot(self):
        """Take a screenshot via WDA. Returns base64 PNG.

        This is a FALLBACK. The primary screenshot path is via QuickTime
        window capture (faster, no re-compression). Use this only when
        QuickTime capture is not available.
        """
        resp = _request("GET", self._session_path("screenshot"))
        return resp.get("value", {}).get("png")


# ---- Singleton ----------------------------------------------------------

# Default client instance. Multiple agents sharing one iPad use the same
# WDA session (WDA is single-session, so this is correct).
_default_client = None


def client():
    """Get or create the default WDA client."""
    global _default_client
    if _default_client is None:
        _default_client = WDAClient()
    return _default_client