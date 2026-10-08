"""Phone Harness Cloud: rent a phone, then drive it like any other.

This module decides which phone exists and who pays for it. It never drives
one. An Android phone is a device at host:port: once `cloud start` has run
`adb connect` and `unlock`, android.py and every helper work on it unchanged.
An iPhone (`cloud start --iphone`) has no shell; its session carries a control
URL and remote.py drives it over HTTPS. The ADB endpoint and unlock code stay
in here; nobody copies them.

  auth.json    (config dir, 0600)  this machine's sign-in: OAuth tokens from
                                   `cloud login`, refreshed here as they expire
  cloud.json   (state dir)         the attached session and the cached profile id

Signing in is the browser's job (OAuth device flow); nobody pastes a key.
PHONE_HARNESS_API_KEY, if set, is used instead: that is for CI, where there
is no one to click Approve.

Stdlib only. API reference: https://phone-harness.com/docs
"""
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser

from . import config

SAVE_WAIT = 90        # seconds to let a stopped profile phone finish closing and saving
READY_WAIT = 240      # seconds to wait for `ready` before giving the phone back
LOW_TIME = 120        # warn the script when the session has less than this left
# Named, because the sign-in host's CDN refuses urllib's default signature.
USER_AGENT = "phone-harness-cli"


class CloudError(RuntimeError):
    def __init__(self, status, body, headers=None):
        self.status = status
        self.body = body if isinstance(body, dict) else {}
        self.code = self.body.get("code")
        self.headers = headers
        super().__init__(self.body.get("error") or f"HTTP {status}")


# Where the cloud is. Like gh, vercel and gcloud, the CLI names itself to the
# sign-in system with a public OAuth client id: it identifies the app and
# proves nothing, so it is safe here. PHONE_HARNESS_CLOUD_API and friends
# override these for tests and for a Phone Harness instance of your own.
API = "https://api.phone-harness.com"
DASHBOARD = "https://phone-harness.com/dashboard"
OAUTH_ISSUER = "https://clerk.phone-harness.com"
OAUTH_CLIENT_ID = "Fu2QHJcGewhL7uKh"           # the "phone-harness CLI" OAuth app
# Someone who finds the cloud here and has no account is the strongest signal
# there is. Send them to the site — which decides whether that means a signup
# form or a waitlist, so this text never goes stale — and say where they came
# from so the signal is not lost.
SIGNUP = "https://phone-harness.com/cloud?source=cli"
waitlist_shown = False                          # read by run.py for telemetry (a flag, no PII)


def _target(key):
    return os.environ.get(f"PHONE_HARNESS_CLOUD_{key}") or globals()[key]


def _dashboard():
    return _target("DASHBOARD").rstrip("/")


def _waitlist_line():
    global waitlist_shown
    waitlist_shown = True
    return f"No Phone Harness Cloud account yet? Get one at {SIGNUP}"


# --- files -------------------------------------------------------------------

def _auth_path():
    return config.config_dir() / "auth.json"


def _state_path():
    return config.state_dir() / "cloud.json"


def _load_state():
    return config._read(_state_path(), {})


def _save_state(state):
    config._write(_state_path(), state)
    _private(_state_path())           # it holds the phone's unlock code


def _private(path):
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _save_auth(record):
    path = _auth_path()
    config._write(path, record)
    _private(path)


def _oauth():
    return _target("OAUTH_ISSUER").rstrip("/"), _target("OAUTH_CLIENT_ID")


def _form_post(url, fields):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
                                 headers={"Accept": "application/json",
                                          "User-Agent": USER_AGENT}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except ValueError:
            return e.code, {"error": f"HTTP {e.code}"}


def _store_tokens(tok, email=None):
    old = config._read(_auth_path(), {})
    _save_auth({"access_token": tok["access_token"],
                # a refresh may or may not rotate the refresh token
                "refresh_token": tok.get("refresh_token") or old.get("refresh_token"),
                "expires_at": time.time() + int(tok.get("expires_in") or 3600),
                "email": email or old.get("email")})
    return tok["access_token"]


def _refresh(record):
    """-> a fresh access token, or None when the sign-in is no longer good."""
    if not record.get("refresh_token"):
        return None
    issuer, client_id = _oauth()
    status, tok = _form_post(f"{issuer}/oauth/token", {
        "grant_type": "refresh_token", "client_id": client_id,
        "refresh_token": record["refresh_token"]})
    if status != 200 or not tok.get("access_token"):
        return None
    return _store_tokens(tok)


def _bearer(required=True, force_refresh=False):
    if os.environ.get("PHONE_HARNESS_API_KEY"):
        return os.environ["PHONE_HARNESS_API_KEY"]
    record = config._read(_auth_path(), {})
    token = record.get("access_token")
    if token and (force_refresh or time.time() > (record.get("expires_at") or 0) - 60):
        token = _refresh(record)
    if not token and required:
        sys.exit("Not signed in. Run: phone-harness cloud login")
    return token


# --- http --------------------------------------------------------------------

def gate_headers():
    """A header a private instance's front gate wants on every request, from
    PHONE_HARNESS_CLOUD_GATE_HEADER="Name: value". Unset for the public cloud."""
    name, sep, value = os.environ.get("PHONE_HARNESS_CLOUD_GATE_HEADER", "").partition(":")
    return {name.strip(): value.strip()} if sep and name.strip() else {}


def _api(method, path, body=None, token=None, headers=None, timeout=40):
    """-> parsed JSON. CloudError for an HTTP error, OSError if unreachable."""
    try:
        return _request(method, path, body, token or _bearer(), headers, timeout)
    except CloudError as e:
        # The access token can be refused before its stated expiry (clock
        # skew, a revoked grant): one refresh, one retry, then it is a 401.
        fresh = None if token or e.status != 401 else _bearer(required=False, force_refresh=True)
        if not fresh:
            raise
        return _request(method, path, body, fresh, headers, timeout)


def _request(method, path, body, token, headers, timeout):
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json",
         "User-Agent": USER_AGENT, **(headers or {})}
    h.update(gate_headers())
    data = None
    if body is not None:
        h["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(_target("API").rstrip("/") + path,
                                 data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {"error": raw.decode(errors="replace")[:200]}
        raise CloudError(e.code, parsed, e.headers) from None
    return json.loads(raw) if raw.strip() else {}


def _explain(e):
    """A CloudError, as the next thing the user should do."""
    if e.status == 401:
        return "This machine is signed out. Run: phone-harness cloud login"
    if e.status == 403:
        # beta_access_required keeps the waitlist line the older CLI printed.
        # account_blocked is a signed-in account that may not rent.
        if e.code == "account_blocked":
            return f"Rentals are blocked on this account ({e})."
        return (f"Your account isn't enabled for phones yet ({e}). "
                f"{_waitlist_line()}")
    if e.status == 402 or "balance_cents" in e.body:
        cents = e.body.get("balance_cents")
        have = f" (${cents / 100:.2f})" if isinstance(cents, int) else ""
        return f"Not enough credit{have}. Add credit at {_dashboard()}"
    if "available_session_slots" in e.body:
        return (f"Session limit reached ({e.body.get('active_session_count')} of "
                f"{e.body.get('session_limit')}). `phone-harness cloud ls` shows them.")
    if e.code == "session_limit":
        count, limit = e.body.get("active_session_count"), e.body.get("session_limit")
        extra = f" ({count} of {limit})" if count is not None and limit is not None else ""
        return f"Session limit reached{extra}. `phone-harness cloud ls` shows them."
    if e.code == "phones_busy":
        retry = _retry_after(e)
        when = f" Try again in {retry}s." if retry else " Try again shortly."
        return f"Every phone is busy right now.{when}"
    if e.code == "device_unavailable":
        return ("That iPhone is granted to this account but not on its host right now. "
                "Try again once it is connected.")
    return f"{e} (HTTP {e.status})"


def _retry_after(e):
    raw = e.headers.get("Retry-After") if getattr(e, "headers", None) else None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


# --- the attached session ----------------------------------------------------

# A saved session reaches its phone through exactly one link:
#   {"kind": "adb", "host", "port", "code"}   an Android: adb connect + unlock
#   {"kind": "control", "url", "token"}       no shell (a real iPhone): ops over HTTPS
LINK_KINDS = ("adb", "control")


def _as_linked(sess):
    """A session record the helpers can drive, or None.

    Current files store `link`. A file written by the Android-only CLI stores
    host/port/code on the session itself; that is an adb link, so an upgrade
    mid-session keeps driving the phone that is already running.
    """
    if not isinstance(sess, dict):
        return None
    link = sess.get("link")
    if isinstance(link, dict) and link.get("kind") in LINK_KINDS:
        return sess
    if sess.get("host") and sess.get("code"):
        return {**sess, "link": {"kind": "adb", "host": sess["host"],
                                 "port": sess.get("port"), "code": sess["code"]}}
    return None


def attached(kind=None):
    """The session the helpers should drive, or None; with `kind`, only a
    session reached that way. Read by transport.py and android.py on every
    run, so: no network, and never raises."""
    try:
        sess = _as_linked(_load_state().get("session"))
    except Exception:
        return None
    if sess is None:
        return None
    if kind not in (None, sess["link"]["kind"]):
        return None
    if sess.get("expires_at") and time.time() >= sess["expires_at"]:
        return None
    return sess


def serial_of(sess):
    """The adb serial of an adb-linked session."""
    return f"{sess['link']['host']}:{sess['link']['port']}"


def _describe_link(sess):
    if sess["link"]["kind"] == "control":
        return "no shell on this phone; the helpers drive it over HTTPS"
    return serial_of(sess)


def _is_ios(session):
    """An iPhone session. Keyed on `platform`, which is what the API reports.
    A session with no platform is the Android shape this CLI has always stored."""
    return (session or {}).get("platform") == "ios"


def _keeps_state(session):
    """Whether stopping saves the phone. An Android profile phone is written to
    storage after its session ends. An iPhone keeps everything on the phone,
    even when the session also names a profile."""
    return bool(session.get("profile")) and not _is_ios(session)


def _kind_of_phone(session):
    if _is_ios(session):
        return "your iPhone"
    return "your phone" if session.get("profile") else "temporary"


def _phone_column(session):
    """The PHONE cell in `cloud ls`. Android wording matches the older CLI."""
    if _is_ios(session):
        return "iPhone"
    return "yours" if session.get("profile") else "temporary"


def _adb_present():
    from .android import _adb_bin
    if not shutil.which(_adb_bin()):
        sys.exit("adb is not installed, and it is how the helpers reach the phone. Install "
                 "Android platform-tools first (macOS: brew install android-platform-tools; "
                 "`phone-harness --doctor android` names it for this OS).")


def ensure_connected(sess, quiet=False):
    """adb connect + unlock, skipped when the link is already up. Every new
    adb connection starts locked, so this runs once per process and again
    after a drop. -> the adb serial."""
    from .android import _run
    serial = serial_of(sess)
    probe = _run("-s", serial, "shell", "echo", "ph-ok", timeout=15, check=False)
    if "ph-ok" not in probe:
        # A link that went `offline` still counts as connected to adb, and
        # `connect` would answer "already connected": drop it first.
        _run("disconnect", serial, timeout=10, check=False)
        out = _run("connect", serial, timeout=20, check=False)
        if "connected" not in out or "cannot" in out or "failed" in out:
            raise RuntimeError(f"could not reach the cloud phone at {serial}: {out.strip()} "
                               "— `phone-harness cloud` shows whether it is still running")
        _run("-s", serial, "shell", "unlock", sess["link"]["code"], timeout=20, check=False)
        probe = _run("-s", serial, "shell", "echo", "ph-ok", timeout=15, check=False)
        if "ph-ok" not in probe:
            raise RuntimeError(f"the cloud phone at {serial} stayed locked; its unlock code "
                               "may have been reset — run `phone-harness cloud use "
                               f"{sess.get('sid', 'SID')}` to fetch the current one")
    left = (sess.get("expires_at") or 0) - time.time()
    if not quiet and 0 < left < LOW_TIME:
        print(f"phone-harness: the cloud phone expires in {int(left)}s — run "
              "`phone-harness cloud stop` now to keep its running state", file=sys.stderr)
    return serial


def _link(session):
    """How the helpers reach a ready session (see LINK_KINDS)."""
    control = session.get("control") or {}
    if control.get("url") and control.get("token"):
        return {"kind": "control", "url": control["url"], "token": control["token"]}
    _adb_present()
    adb = session.get("adb") or {}
    if not adb.get("host") or not adb.get("code"):
        # Only after someone turned ADB off for this session; ready phones have it.
        adb = _api("POST", f"/sessions/{session['id']}/adb")
    return {"kind": "adb", "host": adb["host"], "port": adb["port"], "code": adb["code"]}


def _attach(session, profile_id=None):
    """Remember a ready session and connect to it."""
    state = _load_state()
    phone = session.get("phone")
    state["session"] = {
        "sid": session["id"], "platform": session.get("platform"),
        "kind": session.get("kind"),
        "device_id": session.get("device_id") or (phone if isinstance(phone, str) else None),
        "profile": session.get("profile"), "expires_at": session.get("expires_at"),
        "watch_url": session.get("watch_url"), "link": _link(session),
    }
    if profile_id:
        state["profile_id"] = profile_id
    _save_state(state)
    if state["session"]["link"]["kind"] == "adb":
        ensure_connected(state["session"], quiet=True)
    return state["session"]


def _detach(sid=None):
    state = _load_state()
    sess = state.get("session")
    sess = _as_linked(sess)
    if sess and (sid is None or sess.get("sid") == sid):
        if sess["link"]["kind"] == "adb":
            try:
                from .android import _run
                _run("disconnect", serial_of(sess), timeout=10, check=False)
            except Exception:
                pass
        state.pop("session", None)
        _save_state(state)


# --- small things ------------------------------------------------------------

def _flag(args, name):
    if name in args:
        args.remove(name)
        return True
    return False


def _option(args, *names):
    for name in names:
        if name in args:
            i = args.index(name)
            if i + 1 >= len(args):
                sys.exit(f"{name} needs a value")
            value = args[i + 1]
            del args[i:i + 2]
            return value
    return None


def _int(text, what):
    try:
        return int(text)
    except (TypeError, ValueError):
        sys.exit(f"{what} must be a number, not {text!r}")


def _money(cents):
    return f"${(cents or 0) / 100:.2f}"


def _left(expires_at):
    if not expires_at:
        return "?"
    s = int(expires_at - time.time())
    return "expired" if s <= 0 else f"{s // 60} min" if s >= 120 else f"{s}s"


def _ago(ts):
    if not ts:
        return "never"
    s = int(time.time() - ts)
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "min")):
        if s >= size:
            n = s // size
            return f"{n} {unit}{'s' if n != 1 and unit != 'min' else ''} ago"
    return "just now"


def _resolve_sid(given):
    """A session id, a unique prefix of one, or (None) the attached session."""
    if given is None:
        sess = attached()
        if not sess:
            sys.exit("No cloud phone attached. `phone-harness cloud ls` lists running ones.")
        return sess["sid"]
    ids = [s["id"] for s in _api("GET", "/sessions")]
    hits = [i for i in ids if i == given] or [i for i in ids if i.startswith(given)]
    if len(hits) != 1:
        sys.exit(f"{given!r} matches {len(hits)} running sessions"
                 + (f": {', '.join(hits)}" if hits else ""))
    return hits[0]


def _wait_ready(sid):
    deadline = time.monotonic() + READY_WAIT
    shown = None
    while True:
        session = _api("GET", f"/sessions/{sid}")
        if session["state"] == "ready":
            return session
        if session["state"] in ("error", "closing"):
            sys.exit(f"The phone did not start: {session.get('error') or session['state']}")
        if time.monotonic() >= deadline:
            try:
                _api("DELETE", f"/sessions/{sid}")
            except (CloudError, OSError):
                pass
            sys.exit(f"Gave up after {READY_WAIT}s waiting for the phone; asked the service "
                     f"to end session {sid}.")
        prog = session.get("progress") or {}
        line = prog.get("phase") or "provisioning"
        if prog.get("queue_position"):
            line += f", position {prog['queue_position']} in the queue"
        if line != shown:
            print(f"  … {line}")
            shown = line
        time.sleep(2)


# --- login -------------------------------------------------------------------

def _login(args):
    """OAuth 2.0 Device Authorization Grant (RFC 8628): works the same on a
    laptop and over SSH, because the approval happens in any browser."""
    open_browser = not _flag(args, "--no-browser")
    issuer, client_id = _oauth()
    status, start = _form_post(f"{issuer}/oauth/device_authorization", {
        "client_id": client_id, "scope": "openid profile email offline_access"})
    if status != 200 or "device_code" not in start:
        sys.exit("Could not start sign-in: "
                 f"{start.get('error_description') or start.get('error') or start.get('title') or status}")
    print(f"To sign in, open:  {start['verification_uri']}\n"
          f"and enter code:    {start['user_code']}\n\n"
          f"{_waitlist_line()}\n")
    if open_browser:
        try:
            webbrowser.open(start.get("verification_uri_complete") or start["verification_uri"])
        except Exception:
            pass
    print("Waiting for you to approve in the browser…", flush=True)
    interval = int(start.get("interval", 5))
    deadline = time.monotonic() + int(start.get("expires_in") or 600)
    tok = None
    while time.monotonic() < deadline:
        time.sleep(interval)
        status, tok = _form_post(f"{issuer}/oauth/token", {
            "client_id": client_id, "device_code": start["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        if status == 200 and tok.get("access_token"):
            break
        err, tok = tok.get("error"), None
        if err == "slow_down":
            interval += 5
        elif err != "authorization_pending":
            sys.exit("Sign-in was denied." if err == "access_denied"
                     else f"Sign-in failed: {err}")
    if not tok:
        sys.exit("Sign-in timed out. Run `phone-harness cloud login` again when you can "
                 f"sign in. No account yet? {SIGNUP}")
    try:
        me = _api("GET", "/me", token=tok["access_token"])
    except CloudError as e:
        sys.exit(f"Signed in, but the Phone Harness API refused the sign-in: {_explain(e)}")
    _store_tokens(tok, email=me.get("email"))
    state = _load_state()
    state["profile_id"] = (me.get("profile") or {}).get("id")
    _save_state(state)
    print(f"✓ Signed in as {me.get('email') or me.get('uid')}")
    _print_account(me)
    print("\nStart your phone with: phone-harness cloud start")
    return 0


def _logout(args):
    record = config._read(_auth_path(), {})
    if not record.get("access_token"):
        print("Not signed in.")
        return 0
    issuer, client_id = _oauth()
    for kind in ("refresh_token", "access_token"):      # best effort; the file goes either way
        if record.get(kind):
            try:
                _form_post(f"{issuer}/oauth/token/revoke", {
                    "client_id": client_id, "token": record[kind], "token_type_hint": kind})
            except OSError:
                pass
    _auth_path().unlink(missing_ok=True)
    print("Signed out.")
    if attached():
        print("A cloud phone is still running and billing; sign in again to stop it.")
    return 0


def _credit(me):
    rate = me.get("price_cents_per_minute") or 0
    minutes = f" (about {me.get('balance_cents', 0) // rate} min)" if rate else ""
    return f"{_money(me.get('balance_cents'))}{minutes}"


def _print_account(me):
    print(f"  credit   {_credit(me)}")
    print(f"  phone    {_describe_profile(me.get('profile') or {})}")
    if not me.get("can_rent", True):
        print(f"  access   not enabled yet: {me.get('rent_blocked_reason') or 'rentals are closed'}")


def _describe_profile(profile):
    state = profile.get("state") or "empty"
    if state == "stored":
        return f"stored, saved {_ago(profile.get('saved_at'))}"
    if state == "running":
        return f"running in session {profile.get('session')}"
    if state == "saving":
        return "saving (a session just ended)"
    return "empty — the first `cloud start` sets it up"


# --- start / stop ------------------------------------------------------------

def _wait_for_profile():
    """-> the profile id once the saved phone is free to start, or
    {"id", "session"} when a live session already holds it. A phone that was
    just stopped is `running` (its session `closing`) and then `saving` for a
    few seconds; starting it again should simply wait that out."""
    deadline = time.monotonic() + SAVE_WAIT
    told = False
    while True:
        profile = _api("GET", "/me").get("profile") or {}
        state = profile.get("state")
        if state == "running" and profile.get("session"):
            try:
                live = _api("GET", f"/sessions/{profile['session']}")
            except CloudError:
                live = None
            if live and live["state"] in ("provisioning", "ready"):
                print(f"Your phone is already running (session {live['id']}). Attaching to it.")
                return {"id": profile.get("id"), "session": _wait_ready(live["id"])}
        elif state != "saving":
            return profile.get("id")
        if time.monotonic() >= deadline:
            sys.exit("Your phone is still shutting down from its last session. "
                     "Try again in a few seconds.")
        if not told:
            print("Your phone is still saving from its last session…")
            told = True
        time.sleep(3)



def _parse_timeout(text):
    """Seconds. A bare number or `90s` is seconds; `15m` / `15min` is minutes."""
    t = str(text).strip().lower()
    mult = 1
    if t.endswith("min"):
        t, mult = t[:-3], 60
    elif t.endswith("m"):
        t, mult = t[:-1], 60
    elif t.endswith("s"):
        t, mult = t[:-1], 1
    return _int(t, "--timeout") * mult


def _device_of(session):
    phone = session.get("phone")
    return session.get("device_id") or (phone if isinstance(phone, str) else None)


def _same_target(live, iphone, device_id):
    """True when `live` is the phone this start asked for."""
    if iphone != _is_ios(live):
        return False
    if iphone and device_id:
        have = _device_of(live)
        if have and have != device_id:
            return False
    return True


def _granted_iphones(me):
    """iPhones on `GET /me` `available`, or None when the server has no such list."""
    available = me.get("available") if isinstance(me, dict) else None
    if not isinstance(available, list):
        return None
    return [p for p in available
            if p.get("platform") == "ios" and p.get("kind") == "device"]


def _format_iphones(phones):
    lines = []
    for p in phones:
        label = p.get("label") or "iPhone"
        state = p.get("state") or ""
        lines.append(f"  {p.get('id') or '?'}  {label}  {state}".rstrip())
    return "\n".join(lines)


def _choose_device(device_id):
    """The device_id to send, or None when the account has a single iPhone.

    Several iPhones and no --device: list them and, on a terminal, ask.
    """
    phones = _granted_iphones(_api("GET", "/me"))
    if phones is None:
        return device_id
    if device_id:
        if not any(p.get("id") == device_id for p in phones):
            granted = _format_iphones(phones) or "  (none)"
            sys.exit(f"No iPhone with id {device_id!r} is granted to this account.\n"
                     f"Granted:\n{granted}")
        return device_id
    if not phones:
        sys.exit("This account has no iPhone. `phone-harness cloud start` "
                 "rents the Android phone.")
    if len(phones) == 1:
        return None
    listing = _format_iphones(phones)
    if not sys.stdin.isatty():
        sys.exit("This account has more than one iPhone. Re-run with "
                 "`phone-harness cloud start --iphone --device ID`:\n" + listing)
    print("This account has more than one iPhone:\n" + listing)
    choice = input("Id: ").strip()
    if not any(p.get("id") == choice for p in phones):
        sys.exit(f"{choice!r} is not one of those ids.")
    return choice


def _ios_create_error(e):
    """A create failure that is specific to asking for an iPhone, or None."""
    if e.code == "profile_running":
        sid = e.body.get("session") or "SID"
        return (f"That iPhone is already running (session {sid}). "
                f"Attach with `phone-harness cloud use {sid}`, or "
                f"`phone-harness cloud stop {sid}`.")
    if e.code == "device_unavailable":
        return _explain(e)
    if e.status == 400:
        text = str(e).lower()
        if "grant" in text:
            return f"No grant for that iPhone ({e})."
        if "no phone" in text or "not available" in text:
            return ("This account has no iPhone available. "
                    "`phone-harness cloud start` rents the Android phone.")
    return None


def _start(args):
    temp = _flag(args, "--temp")
    iphone = _flag(args, "--iphone")
    watch = not _flag(args, "--no-watch")
    minutes_opt = _option(args, "--minutes", "-m")
    timeout_opt = _option(args, "--timeout")
    device_id = _option(args, "--device")
    if args:
        sys.exit(CLI_USAGE)
    if temp and iphone:
        sys.exit("--iphone starts the account's iPhone; it cannot be combined with --temp.")
    if device_id and not iphone:
        sys.exit("--device names one of the account's iPhones. Use it with --iphone.")
    if minutes_opt and timeout_opt:
        sys.exit("Pass --minutes or --timeout, not both.")
    cap = int(config.get("cloud.max_minutes"))
    if timeout_opt:
        timeout = _parse_timeout(timeout_opt)
        if timeout < 1 or timeout > cap * 60:
            sys.exit(f"--timeout must be between 1 second and {cap} minutes "
                     f"(raise the cap with `phone-harness config set cloud.max_minutes N`).")
    else:
        minutes = _int(minutes_opt, "--minutes") if minutes_opt else int(config.get("cloud.minutes"))
        if not 1 <= minutes <= cap:
            sys.exit(f"--minutes must be between 1 and {cap} "
                     f"(raise the cap with `phone-harness config set cloud.max_minutes N`).")
        timeout = minutes * 60
    if not iphone:
        _adb_present()

    current = attached()
    live = None
    if current:
        try:
            live = _api("GET", f"/sessions/{current['sid']}")
        except CloudError:
            live = None
        if live and live["state"] in ("provisioning", "ready") and _same_target(live, iphone, device_id):
            print(f"Already attached to session {live['id']}; reusing it.")
            return _report(_wait_ready(live["id"]), watch=False)   # its view is already open
        if not (live and live["state"] in ("provisioning", "ready")):
            _detach()
        elif iphone != _is_ios(live):
            # Android and an iPhone may run at once. Leave the other session
            # billing on the server; the helpers follow the one just started.
            print(f"Session {live['id']} stays up. `phone-harness cloud use {live['id']}` "
                  "switches the helpers back to it.")

    body = {"timeout_seconds": timeout}
    profile_id = None
    if iphone:
        # platform and kind together. `provider` is rejected by the API.
        # device_id is required only when the account holds more than one iPhone.
        chosen = _choose_device(device_id)
        body["platform"] = "ios"
        body["kind"] = "device"
        if chosen:
            body["device_id"] = chosen
    elif not temp:
        profile_id = _wait_for_profile()
        if isinstance(profile_id, dict):                  # it is already up: use it
            return _report(profile_id["session"], profile_id["id"], watch)
        if profile_id:
            body["profile_id"] = profile_id

    request_key = uuid.uuid4().hex
    print("Starting your iPhone…" if iphone else
          "Starting a temporary phone…" if "profile_id" not in body else "Starting your phone…")
    try:
        created = _api("POST", "/sessions", body, headers={"Idempotency-Key": request_key})
    except CloudError as e:
        if iphone:
            message = _ios_create_error(e)
            if message:
                sys.exit(message)
        if e.code == "profile_running":
            sys.exit("Your phone is still shutting down from its last session. "
                     "Try again in a few seconds.")
        raise
    except OSError:
        # The request may have been admitted even though the answer was lost.
        created = _api("GET", f"/sessions/requests/{request_key}")
        if created.get("cleanup_complete") or not created.get("id"):
            raise
    return _report(_wait_ready(created["id"]), profile_id, watch)


def _report(session, profile_id=None, watch=True):
    attached_now = _attach(session, profile_id)
    how = (session.get("startup") or {}).get("startup")
    note = {"exact": " Resumed exactly where you left it.",
            "rebooted": " Rebooted from saved storage: apps and logins kept, the screen is not."}
    print(f"✓ ready.{note.get(how, '')}")
    print(f"  session  {session['id']}  ({_kind_of_phone(session)})")
    link = attached_now["link"]["kind"]
    print(f"  {link:<8} {_describe_link(attached_now)}" + (" (connected, unlocked)" if link == "adb" else ""))
    print(f"  expires  in {_left(session.get('expires_at'))} — `phone-harness cloud stop` "
          "before then" + (" to keep the running state" if _keeps_state(session) else ""))
    # The user asked for a phone; show it to them. Fails quietly where there
    # is no browser (an SSH box, CI), and the command is one line away.
    if watch and session.get("watch_url") and _open_browser(session["watch_url"]):
        print("  watch    opened in your browser")
    else:
        print("  watch    phone-harness cloud watch")
    return 0


def _open_browser(url):
    if not config.get("cloud.watch"):
        return False
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def _stop(args):
    everything = _flag(args, "--all")
    watch_save = _flag(args, "--wait")
    if everything:
        sids = [s["id"] for s in _api("GET", "/sessions")]
        if not sids:
            print("Nothing is running.")
            _detach()
            return 0
    else:
        sids = [_resolve_sid(args[0] if args else None)]
    held_profile = False
    for sid in sids:
        try:
            before = _api("GET", f"/sessions/{sid}")
            held_profile = held_profile or _keeps_state(before)
            _api("DELETE", f"/sessions/{sid}")
            print(f"✓ Session {sid} ended; billing stopped.")
        except CloudError as e:
            if e.status != 404:
                raise
            print(f"Session {sid} was already gone.")
        _detach(sid)
    if not held_profile:
        return 0
    # Billing ended with the DELETE. The save runs on the service for another
    # half minute and needs nothing from here, so waiting on it is opt-in;
    # `cloud start` waits out a save that is still going.
    if not watch_save:
        print("  Your phone is being saved (about 30s); `phone-harness cloud phone` shows when.")
        return 0
    print("  Saving your phone…", end="", flush=True)
    deadline = time.monotonic() + SAVE_WAIT
    state = "saving"
    while time.monotonic() < deadline:
        time.sleep(3)
        state = (_api("GET", "/me").get("profile") or {}).get("state")
        if state not in ("saving", "running"):
            break
    print(f" {state}.")
    return 0


# --- looking -----------------------------------------------------------------

def _status(args):
    if not _bearer(required=False):
        print("Not signed in. Run: phone-harness cloud login")
        print(_waitlist_line())
        return 1
    me = _api("GET", "/me")
    if os.environ.get("PHONE_HARNESS_CLOUD_API"):
        print(f"api         {_target('API')}")
    print(f"signed in   {me.get('email') or me.get('uid')}")
    print(f"credit      {_credit(me)}")
    profile = me.get("profile") or {}
    sess = attached()
    live = None
    if sess:
        try:
            live = _api("GET", f"/sessions/{sess['sid']}")
        except CloudError:
            live = None
    # Just after `stop`, the API still says the profile is `running` while its
    # session is `closing`; the truth for the user is that it is being saved.
    if live and live.get("state") == "closing" and live.get("profile") and not _is_ios(live):
        profile = {**profile, "state": "saving"}
    print(f"phone       {_describe_profile(profile)}")
    if not sess:
        running = me.get("active_session_count") or 0
        print("session     none attached" + (f" ({running} running: `phone-harness cloud ls`)"
                                              if running else " — `phone-harness cloud start`"))
        return 0
    if live is None:
        print(f"session     {sess['sid']} is gone; detaching")
        _detach()
        return 0
    state = ("closing — saving the phone" if live["state"] == "closing" and not _is_ios(live)
             else live["state"])
    print(f"session     {live['id']} · {state} · {_left(live.get('expires_at'))} left"
          f" · {_kind_of_phone(live)}")
    print(f"{sess['link']['kind']:<12}{_describe_link(sess)}")
    return 0


def _ls(args):
    as_json = _flag(args, "--json")
    limit = _option(args, "-n", "--limit")
    sessions = _api("GET", "/sessions")
    if limit:
        sessions = sessions[:_int(limit, "-n")]
    if as_json:
        print(json.dumps(sessions, indent=2))
        return 0
    if not sessions:
        print("Nothing is running.")
        return 0
    mine = (attached() or {}).get("sid")
    print(f"  {'SESSION':<14} {'STATE':<13} {'PHONE':<10} {'LEFT':<8} ADB")
    for s in sessions:
        adb = s.get("adb") or {}
        print(f"{'*' if s['id'] == mine else ' '} {s['id']:<14} {s['state']:<13} "
              f"{_phone_column(s):<10} "
              f"{_left(s.get('expires_at')):<8} "
              f"{adb.get('host', '-')}{':' + str(adb['port']) if adb.get('port') else ''}")
    return 0


def _show(args):
    as_json = _flag(args, "--json")
    session = _api("GET", f"/sessions/{_resolve_sid(args[0] if args else None)}")
    if as_json:
        print(json.dumps(session, indent=2))
        return 0
    adb = session.pop("adb", None) or {}
    session.pop("watch_url", None)          # a credential; `cloud watch` opens it
    for k, v in session.items():
        if v not in (None, "", {}):
            print(f"{k:<16} {json.dumps(v) if isinstance(v, (dict, list)) else v}")
    if adb.get("host"):
        print(f"{'adb':<16} {adb['host']}:{adb['port']}")
    return 0


def _use(args):
    if len(args) != 1:
        sys.exit("Usage: phone-harness cloud use SID")
    session = _wait_ready(_resolve_sid(args[0]))
    sess = _attach(session)
    print(f"✓ attached to {session['id']}; {_describe_link(sess)}")
    return 0


def _watch(args):
    show = _flag(args, "--print")
    url = _api("GET", f"/sessions/{_resolve_sid(args[0] if args else None)}").get("watch_url")
    if not url:
        sys.exit("This session has no live view.")
    if show or not webbrowser.open(url):
        print(url)
    return 0


def _open(args):
    """The dashboard: the interactive viewer, behind the user's own sign-in.
    `watch` is for looking; this is for taking the controls."""
    url = _dashboard()
    if _flag(args, "--print") or not _open_browser(url):
        print(url)
    else:
        print(f"opened {url} — sign in there to control the phone")
    return 0


def _whoami(args):
    me = _api("GET", "/me")
    if _flag(args, "--json"):
        print(json.dumps(me, indent=2))
        return 0
    print(f"{me.get('email') or me.get('uid')}")
    _print_account(me)
    print(f"  sessions {me.get('active_session_count', 0)} of {me.get('session_limit', '?')} running")
    return 0


def _phone(args):
    if args[:1] == ["reset"]:
        if "--yes" not in args:
            sys.exit("This forgets your saved phone — its apps, logins and data — and cannot "
                     "be undone.\nRun: phone-harness cloud phone reset --yes")
        _api("POST", "/me/profile/reset")
        print("Your saved phone was reset. The next `cloud start` begins fresh.")
        return 0
    profile = _api("GET", "/me").get("profile") or {}
    print(json.dumps(profile, indent=2) if _flag(args, "--json") else _describe_profile(profile))
    return 0


def _keys(args):
    if args[:1] == ["create"]:
        minted = _api("POST", "/me/keys", {"label": " ".join(args[1:]) or None})
        print(minted["key"])
        print("Shown once; store it now.", file=sys.stderr)
        return 0
    if args[:1] == ["revoke"] and len(args) == 2:
        keys = [k for k in _api("GET", "/me/keys") if k["key_hash"].startswith(args[1])]
        if len(keys) != 1:
            sys.exit(f"{args[1]!r} matches {len(keys)} keys")
        _api("POST", "/me/keys/revoke", {"key_hash": keys[0]["key_hash"]})
        print(f"Revoked {keys[0].get('hint')}")
        return 0
    keys = _api("GET", "/me/keys")
    if _flag(args, "--json"):
        print(json.dumps(keys, indent=2))
        return 0
    print(f"{'HASH':<14} {'KEY':<16} {'CREATED':<14} LABEL")
    for k in keys:
        print(f"{k['key_hash'][:12]:<14} "
              f"{k.get('hint') or '':<16} {_ago(k.get('created')):<14} {k.get('label') or ''}")
    return 0


def _history(args):
    as_json = _flag(args, "--json")
    limit = _int(_option(args, "-n", "--limit") or 20, "-n")
    items, cursor = [], None
    while len(items) < limit:
        q = {"limit": min(100, limit - len(items)), **({"cursor": cursor} if cursor else {})}
        page = _api("GET", "/history/page?" + urllib.parse.urlencode(q))
        items += page.get("items") or []
        cursor = page.get("next_cursor")
        if not cursor:
            break
    if as_json:
        print(json.dumps(items, indent=2))
        return 0
    if not items:
        print("No finished sessions yet.")
        return 0
    print(f"{'SESSION':<14} {'ENDED':<14} {'MIN':>4} {'COST':>7}  REASON")
    for h in items:
        print(f"{h.get('sid', ''):<14} {_ago(h.get('ended')):<14} "
              f"{h.get('usage_minutes', 0):>4} {_money(h.get('cost_cents')):>7}  "
              f"{h.get('end_reason') or ''}")
    return 0


# --- CLI (phone-harness cloud ...) -------------------------------------------

CLI_USAGE = """Usage:
  phone-harness cloud                          who is signed in, what is attached
  phone-harness cloud login [--no-browser]     sign in through the browser
  phone-harness cloud logout | whoami
  phone-harness cloud start [--temp|--iphone] [--device ID] [--minutes N|--timeout DURATION] [--no-watch]
                                               start your saved Android (or a throwaway one, or an
                                               iPhone: --iphone, and --device when the account has
                                               several). Duration is minutes, or --timeout 90s / 15m.
                                               Default 15 minutes (config: cloud.minutes, cap
                                               cloud.max_minutes). Opens the live view (cloud.watch)
  phone-harness cloud stop [SID|--all] [--wait]
                                               end it; your phone is saved for next time
                                               (--wait watches the save finish)
  phone-harness cloud ls [-n NUM]              running sessions (* = attached)
  phone-harness cloud show [SID]               one session in full
  phone-harness cloud use SID                  attach the helpers to another running session
  phone-harness cloud watch [SID] [--print]    the read-only live view (no sign-in; shareable)
  phone-harness cloud open [--print]           the dashboard: control the phone yourself (sign-in)
  phone-harness cloud phone [reset --yes]      your saved phone
  phone-harness cloud keys [create [LABEL] | revoke HASH]   API keys, for CI
  phone-harness cloud history [-n NUM]
ls, show, whoami, phone, keys and history take --json. SID may be a unique prefix.
PHONE_HARNESS_CLOUD_API, _OAUTH_ISSUER, _OAUTH_CLIENT_ID and _GATE_HEADER point the CLI
at a Phone Harness instance of your own (or a gated one); unset, it is the public cloud.
They may live in a .env file at the repo root or in the agent workspace (never committed).
"""

_COMMANDS = {"login": _login, "logout": _logout, "whoami": _whoami, "start": _start,
             "stop": _stop, "ls": _ls, "show": _show, "use": _use, "watch": _watch, "open": _open,
             "phone": _phone, "keys": _keys, "history": _history}


def cli(args):
    args = list(args)
    fn = _status if not args else _COMMANDS.get(args[0])
    if fn is None:
        print(CLI_USAGE)
        return 2
    try:
        return fn(args[1:])
    except CloudError as e:
        print(_explain(e), file=sys.stderr)
        return 1
    except OSError as e:
        print(f"Could not reach {_target('API')}: {e}", file=sys.stderr)
        return 1
