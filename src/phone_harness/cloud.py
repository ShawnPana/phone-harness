"""Phone Harness Cloud: rent a phone, then drive it like any other.

This module decides which phone exists and who pays for it. It never drives
one. `cloud start` picks an entry from the account catalog (`GET /me` →
`available`) and the service returns a session. The session carries one link:
an Android phone is adb at host:port (`adb connect` and `unlock` live here),
and a phone with no shell carries a control URL that remote.py drives over
HTTPS. Which link it is, not the platform name, is what the rest of the CLI
branches on.

  auth.json    (config dir, 0600)  this machine's sign-in: OAuth tokens from
                                   `cloud login`, refreshed here as they expire
  cloud.json   (state dir)         the attached session and the cached profile id

Signing in is the browser's job (OAuth device flow); nobody pastes a key.
PHONE_HARNESS_API_KEY, if set, is used instead: that is for CI, where there
is no one to click Approve.

Stdlib only. API reference: https://phone-harness.com/docs
"""
import argparse
import json
import math
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
from . import telemetry
from . import term

SAVE_WAIT = 90        # seconds to let a stopped profile phone finish closing and saving
READY_WAIT = 240      # seconds to wait for `ready` before giving the phone back
LOW_TIME = 120        # warn the script when the session has less than this left
# Named, because the sign-in host's CDN refuses urllib's default signature.
USER_AGENT = "phone-harness-cli"


class CloudPhoneError(RuntimeError):
    """A cloud phone was attached and cannot be driven.

    Callers must surface this. Returning None would look like no cloud phone
    was ever attached, and the next line of code would drive a different one.
    """


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
    """The cloud state file, or {} when this machine has never had one.

    A file that exists but cannot be read is an error. Treating it as empty
    would drop the attached session and let a script drive some other phone.
    """
    path = _state_path()
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise CloudPhoneError(
            f"the cloud state file {path} is unreadable ({e}). "
            "This did not fall back to another phone.") from None
    try:
        data = json.loads(text)
    except ValueError as e:
        raise CloudPhoneError(
            f"the cloud state file {path} is unreadable ({e}). "
            "This did not fall back to another phone.") from None
    if not isinstance(data, dict):
        raise CloudPhoneError(
            f"the cloud state file {path} is unreadable (not an object). "
            "This did not fall back to another phone.")
    return data


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


def _me(token=None):
    """GET /me: the signed-in account. Its uid is what this install's
    telemetry is linked to."""
    me = _api("GET", "/me", token=token)
    telemetry.link_user(me.get("uid"))
    return me


def _request(method, path, body, token, headers, timeout):
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json",
         "User-Agent": USER_AGENT, **telemetry.cloud_headers(), **(headers or {})}
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
    if e.status == 403 and e.code == "account_blocked":
        return f"Rentals are blocked on this account ({e})."
    if e.status in (402, 403) and (
            e.code in ("insufficient_balance", "balance") or "balance_cents" in e.body
            or "credit" in str(e).lower() or "balance" in str(e).lower()):
        cents = e.body.get("balance_cents")
        have = f" (${cents / 100:.2f})" if isinstance(cents, int) else ""
        return f"Not enough credit{have}. Add credit at {_dashboard()}"
    if e.status == 403:
        # beta_access_required keeps the waitlist line the older CLI printed.
        return (f"Your account isn't enabled for phones yet ({e}). "
                f"{_waitlist_line()}")
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
        return ("That phone is granted to this account but not on its host right now. "
                "Try again once it is connected.")
    return f"{e} (HTTP {e.status})"


def _retry_after(e):
    raw = e.headers.get("Retry-After") if getattr(e, "headers", None) else None
    if raw is None:
        raw = e.body.get("retry_after_seconds")
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


def _known_sessions(state):
    """sid -> record, including the single attached session older files store."""
    sessions = dict(state.get("sessions") or {})
    current = state.get("session")
    if isinstance(current, dict) and current.get("sid") and current["sid"] not in sessions:
        sessions[current["sid"]] = current
    return sessions


def _find_recorded(state, pin):
    sessions = _known_sessions(state)
    if pin in sessions:
        return sessions[pin]
    hits = [s for sid, s in sessions.items() if sid.startswith(pin)]
    return hits[0] if len(hits) == 1 else None


def _expired(sess):
    return bool(sess and sess.get("expires_at") and time.time() >= sess["expires_at"])


def _pin_hits(sid, pin):
    return bool(sid and pin and (sid == pin or str(sid).startswith(pin)))


def _quote_arg(name):
    if name and all(c.isalnum() or c in "_.:+@-" for c in name):
        return name
    return '"' + str(name).replace('"', "") + '"'


def _start_target(rec):
    """The catalog name a later `cloud start` uses for this same phone.

    None means a bare `cloud start` (the unnamed choice). A label or id is
    what the catalog command accepts; `ios` / `android` only when that is
    all we still know.
    """
    rec = rec or {}
    sel = rec.get("selector") or ""
    if sel == "unnamed":
        return None
    label = str(rec.get("label") or "").strip()
    if label:
        return _quote_arg(label)
    device = str(rec.get("device_id") or "").strip()
    if device:
        return _quote_arg(device)
    if sel.startswith("id:") and sel[3:].strip():
        return _quote_arg(sel[3:].strip())
    if sel.startswith("label:") and sel[6:].strip():
        return _quote_arg(sel[6:].strip())
    if sel == "temporary":
        return None
    platform = rec.get("platform")
    if platform in ("ios", "android"):
        return platform
    return None


def restart_command(rec):
    """`cloud start` of the same phone, for a longer duration than the one that ended."""
    target = _start_target(rec)
    if target:
        return f"phone-harness cloud start {target} --for <longer>"
    return "phone-harness cloud start --for <longer>"


def _ended_record(rec, cause, at=None):
    rec = rec or {}
    return {
        "sid": rec.get("sid"),
        "platform": rec.get("platform"),
        "label": rec.get("label"),
        "device_id": rec.get("device_id"),
        "selector": rec.get("selector"),
        "expires_at": rec.get("expires_at"),
        "cause": cause,
        "at": time.time() if at is None else at,
    }


def ended_message(rec):
    """What a script should show, and the one next command.

    Expiry is named `expired`. Anything else (the service lost the session,
    the state file says it is gone) is named `failed` and `unreachable`.
    The CLI does not rent a replacement itself.
    """
    rec = rec or {}
    cause = "expired" if rec.get("cause") == "expired" or _expired(rec) else "unreachable"
    sid = rec.get("sid") or "unknown"
    if cause == "expired":
        lead = f"session {sid} expired"
    else:
        lead = f"session {sid} failed: the cloud phone is unreachable"
    cmd = restart_command(rec)
    return (f"{lead}. This script did not switch to another phone. "
            f"Start the same phone again for longer and continue the task: `{cmd}`. "
            "Replace <longer> with a duration longer than the session that just ended "
            "(`90s`, `15m`, or a number of minutes). "
            "The CLI does not rent a phone by itself.")


# Sids whose ended-session message this process has already shown. The copy
# on disk is removed at that moment so the next command does not print it
# again. Remembering it here is what keeps the rest of this process from
# treating the dead phone as "no phone" and driving a different one.
_reported_dead = {}
_reported_fallback = None


def _reported_for(pin):
    for sid, rec in _reported_dead.items():
        if _pin_hits(sid, pin):
            return rec
    return None


def _forget_stored_dead(sid):
    """Drop the stored dead-session note for `sid`.

    That is the `gone` mark and the saved session itself (the attached one
    and the `--session` copy). Other remembered sessions stay. No network.
    """
    state = _load_state()
    changed = False
    gone = state.get("gone")
    if isinstance(gone, dict) and (not sid or gone.get("sid") == sid):
        state.pop("gone", None)
        changed = True
    sessions = dict(state.get("sessions") or {})
    current = state.get("session") if isinstance(state.get("session"), dict) else None

    def hits(rec, key=None):
        if sid and (key == sid or (isinstance(rec, dict) and rec.get("sid") == sid)):
            return True
        return not sid and _expired(rec)

    drop = []
    for key, rec in sessions.items():
        if hits(rec, key):
            drop.append(key)
    if current and hits(current, current.get("sid")) and current.get("sid") not in drop:
        drop.append(current.get("sid"))
    for target in drop:
        rec = sessions.get(target)
        if rec is None and current and current.get("sid") == target:
            rec = current
        linked = _as_linked(rec)
        if linked and linked["link"]["kind"] == "adb":
            try:
                from .android import _run
                _run("disconnect", serial_of(linked), timeout=10, check=False)
            except Exception:
                pass
        if target in sessions:
            sessions.pop(target, None)
            changed = True
        if current and current.get("sid") == target:
            state.pop("session", None)
            current = None
            changed = True
    if sessions:
        state["sessions"] = sessions
    elif "sessions" in state:
        state.pop("sessions", None)
        changed = True
    if changed:
        _save_state(state)


def report_ended(rec):
    """The ended-session message has been shown for this record.

    Forget the stored copy so `phone-harness cloud` and later commands do not
    print it again. This process still fails if it targets that sid.
    """
    global _reported_fallback
    rec = dict(rec or {})
    sid = rec.get("sid")
    if sid:
        _reported_dead[sid] = rec
    if not os.environ.get("PHONE_HARNESS_SESSION"):
        _reported_fallback = rec
    _forget_stored_dead(sid)
    return rec


def attached(kind=None):
    """The session the helpers should drive, or None; with `kind`, only a
    session reached that way.

    `PHONE_HARNESS_SESSION` (or `--session`, which sets it) pins one session
    for this process, so parallel agents do not share the state file's
    fallback. With neither, the fallback is the attached session a person at
    a terminal last started or `cloud use`d. A pin that is not on disk is
    fetched once.

    None means no cloud phone was attached. A pin whose session has expired
    raises `session <sid> expired` instead of returning None, and an
    unreadable state file raises too: both used to look like "no cloud phone"
    and the next call drove a different device. Raising reports that dead
    session, and the stored note is cleared so a later command does not repeat
    it. An unpinned session whose clock has run out still returns None;
    `lost()` tells `connect` to raise.
    """
    state = _load_state()
    pin = os.environ.get("PHONE_HARNESS_SESSION")
    if pin:
        seen = _reported_for(pin)
        if seen:
            raise CloudPhoneError(ended_message(seen))
        raw = _find_recorded(state, pin)
        sess = _as_linked(raw)
        if sess is None:
            gone = state.get("gone")
            if isinstance(gone, dict) and _pin_hits(gone.get("sid"), pin):
                report_ended(gone)
                raise CloudPhoneError(ended_message(gone))
            sess = _pull_pinned(pin)
        if _expired(sess):
            rec = _ended_record(sess, "expired")
            report_ended(rec)
            raise CloudPhoneError(ended_message(rec))
    else:
        sess = _as_linked(state.get("session"))
    if sess is None:
        return None
    if kind not in (None, sess["link"]["kind"]):
        return None
    if _expired(sess):
        return None
    telemetry.note_session(sess["sid"], sess.get("platform"))
    return sess


def serial_of(sess):
    """The adb serial of an adb-linked session."""
    return f"{sess['link']['host']}:{sess['link']['port']}"


def _describe_link(sess):
    if sess["link"]["kind"] == "control":
        return "no shell on this phone; the helpers drive it over HTTPS"
    return serial_of(sess)


def _link_kind(session):
    """adb or control, from a saved record or from the API session. None when
    the session has not said yet (still provisioning, no block)."""
    if not isinstance(session, dict):
        return None
    link = session.get("link")
    if isinstance(link, dict) and link.get("kind") in LINK_KINDS:
        return link["kind"]
    if (session.get("control") or {}).get("url"):
        return "control"
    if (session.get("adb") or {}).get("host") or session.get("host"):
        return "adb"
    return None


def _keeps_state(session):
    """Whether stopping saves the phone. A control link keeps its data on the
    phone, even when the session also names a profile. An adb link with a
    profile is written to storage after the session ends."""
    if _link_kind(session) == "control":
        return False
    return bool(session.get("profile"))


def _iphone_words(label):
    """'your iPhone', plus a label only when the label is not itself 'iPhone'.

    The catalog name for the one phone on an account is often just iPhone, and
    'your iPhone, iPhone' says the same thing twice."""
    text = " ".join(str(label).split()) if label else ""
    if text.lower() in {"", "iphone", "your iphone"}:
        return "your iPhone"
    return f"your iPhone, {text}"


def _kind_of_phone(session, label=None):
    """Wording for a session, from its link. A control link is an iPhone."""
    if _link_kind(session) == "control":
        if label is None:
            label = session.get("label")
        return _iphone_words(label)
    if session.get("profile") or session.get("profile_id"):
        return "your phone"
    return "temporary"


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
        print(term.note(
            f"phone-harness: the cloud phone expires in {int(left)}s — run "
            "`phone-harness cloud stop` now to keep its running state",
            sys.stderr), file=sys.stderr)
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


def _session_record(session, selector=None, label=None):
    phone = session.get("phone")
    return {
        "sid": session["id"], "platform": session.get("platform"),
        "kind": session.get("kind"),
        "device_id": session.get("device_id") or (phone if isinstance(phone, str) else None),
        "label": label or session.get("label"),
        "profile": session.get("profile"), "expires_at": session.get("expires_at"),
        "watch_url": session.get("watch_url"), "selector": selector,
        "link": _link(session),
    }


def _store_session(record, make_attached=True):
    """Remember a session. `make_attached` updates the terminal fallback.
    Every remembered session stays addressable by `--session`, so a later
    start does not forget the previous phone. Storing one clears a previous
    "gone" mark: the user has a phone again."""
    _reported_dead.clear()
    global _reported_fallback
    _reported_fallback = None
    telemetry.note_session(record["sid"], record.get("platform"))
    state = _load_state()
    sessions = _known_sessions(state)
    sessions[record["sid"]] = record
    state["sessions"] = sessions
    if make_attached or not isinstance(state.get("session"), dict):
        state["session"] = record
    if make_attached and record.get("profile"):
        state["profile_id"] = record["profile"]
    state.pop("gone", None)
    _save_state(state)
    if record["link"]["kind"] == "adb":
        ensure_connected(record, quiet=True)
    return record


def _attach(session, profile_id=None, selector=None, label=None):
    """Remember a ready session and connect to it."""
    record = _session_record(session, selector=selector, label=label)
    if profile_id and not record.get("profile"):
        record["profile"] = profile_id
    return _store_session(record, make_attached=True)


def _pull_pinned(pin):
    """A pinned sid that this machine has not remembered yet. Raises when it
    does not name one running session, so a helper does not fall through to
    the phone on the desk."""
    try:
        sessions = _api("GET", "/sessions")
    except CloudError as e:
        raise RuntimeError(f"could not resolve --session {pin}: {_explain(e)}") from None
    except OSError as e:
        raise RuntimeError(f"could not resolve --session {pin}: {e}") from None
    hits = [s for s in sessions if s.get("id") == pin] or [s for s in sessions if str(s.get("id", "")).startswith(pin)]
    if len(hits) != 1:
        raise RuntimeError(f"{pin!r} matches {len(hits)} running sessions"
                           + (f": {', '.join(s['id'] for s in hits)}" if hits else ""))
    session = hits[0]
    if session.get("state") != "ready":
        session = _wait_ready(session["id"])
    return _store_session(_session_record(session), make_attached=False)


def _detach(sid=None, gone=False):
    """Forget a remembered session.

    `gone=True` means the service lost the attached one (it failed, or it
    expired) rather than the user ending it. That session is remembered
    under "gone" so the helpers refuse to run instead of driving the phone
    on this machine. A plain detach (the user's `cloud stop`) clears that
    too. Other remembered sessions stay addressable by `--session`.
    """
    state = _load_state()
    sessions = _known_sessions(state)
    current = state.get("session") if isinstance(state.get("session"), dict) else None
    if sid is None:
        targeted = _as_linked(current)
        sids = [targeted["sid"]] if targeted and targeted.get("sid") else []
    else:
        sids = [sid]
    removed_attached = None
    for target in sids:
        rec = _as_linked(sessions.get(target))
        if rec is None and current and current.get("sid") == target:
            rec = _as_linked(current)
        if rec and rec["link"]["kind"] == "adb":
            try:
                from .android import _run
                _run("disconnect", serial_of(rec), timeout=10, check=False)
            except Exception:
                pass
        sessions.pop(target, None)
        if current and current.get("sid") == target:
            if gone:
                removed_attached = rec or current
            state.pop("session", None)
            current = None
    if sessions:
        state["sessions"] = sessions
    else:
        state.pop("sessions", None)
    if gone and removed_attached:
        cause = "expired" if _expired(removed_attached) else "unreachable"
        state["gone"] = _ended_record(removed_attached, cause)
    if not gone:
        state.pop("gone", None)
    _save_state(state)


def lost():
    """A session that was attached and is no longer usable, or None.

    Either the service said it was gone, or its expiry passed while it was
    still attached. `cause` is `expired` or `unreachable`. No network. An
    unreadable state file raises; it is not "no session".

    Once that message has been reported, the stored note is gone. This
    process still sees a session it already reported, so a second call in the
    same script fails instead of switching phones.
    """
    state = _load_state()
    gone = state.get("gone")
    if isinstance(gone, dict) and gone.get("sid"):
        return gone
    sess = state.get("session") if isinstance(state.get("session"), dict) else None
    if _expired(sess):
        return _ended_record(sess, "expired", at=sess.get("expires_at"))
    if _reported_fallback is not None and not os.environ.get("PHONE_HARNESS_SESSION"):
        return _reported_fallback
    return None


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
    """How long the session has left, for a person reading it.

    Minutes round up. `cloud start --for 6` still says 6 min after the phone
    spends part of that first minute booting; flooring made it say 5. Under
    two minutes the exact seconds stay.
    """
    if not expires_at:
        return "?"
    remaining = expires_at - time.time()
    if remaining <= 0:
        return "expired"
    if remaining < 120:
        return f"{int(remaining)}s"
    return f"{math.ceil(remaining / 60)} min"


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
            sys.exit("No cloud phone attached. Pass --session SID, or `phone-harness cloud ls`.")
        return sess["sid"]
    sessions = _api("GET", "/sessions")
    hits = ([s for s in sessions if s["id"] == given]
            or [s for s in sessions if s["id"].startswith(given)])
    if len(hits) != 1:
        sys.exit(f"{given!r} matches {len(hits)} running sessions"
                 + (f": {', '.join(s['id'] for s in hits)}" if hits else ""))
    telemetry.note_session(hits[0]["id"], hits[0].get("platform"))
    return hits[0]["id"]


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
            print(term.note(f"  … {line}"))
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
          f"{term.note(_waitlist_line())}\n")
    if open_browser:
        try:
            webbrowser.open(start.get("verification_uri_complete") or start["verification_uri"])
        except Exception:
            pass
    print(term.note("Waiting for you to approve in the browser…"), flush=True)
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
        me = _me(token=tok["access_token"])
    except CloudError as e:
        sys.exit(f"Signed in, but the Phone Harness API refused the sign-in: {_explain(e)}")
    _store_tokens(tok, email=me.get("email"))
    state = _load_state()
    state["profile_id"] = (me.get("profile") or {}).get("id")
    _save_state(state)
    print(f"{term.mark()} Signed in as {me.get('email') or me.get('uid')}")
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
    telemetry.unlink_user()
    print("Signed out.")
    if attached():
        print("A cloud phone is still running; sign in again to stop it.")
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

def _catalog(me):
    available = me.get("available") if isinstance(me, dict) else None
    return list(available) if isinstance(available, list) else []


def _unnamed(me):
    """What the service gives a start that names nothing: its own rule
    (`GET /me` → `unnamed`), a temporary Android. Never a preference."""
    rule = me.get("unnamed") if isinstance(me, dict) else None
    if isinstance(rule, dict) and rule.get("platform"):
        return {**rule, "temporary": bool(rule.get("temporary", True))}
    return {"platform": "android", "kind": "emulator", "temporary": True}


def _entry_selector(entry):
    """What a later `cloud start` of the same phone matches against."""
    if entry is None:
        return "unnamed"
    if entry.get("id"):
        return "id:" + entry["id"]
    if entry.get("temporary"):
        return "temporary"
    return "label:" + (entry.get("label") or "")


def _match_catalog(catalog, query):
    """Catalog entries named by id, label, or `ios`/`android` when only one
    entry has that platform. Exact, case-insensitive. No partial matches."""
    q = query.strip().lower()
    by_id = [e for e in catalog if str(e.get("id") or "").lower() == q]
    if by_id:
        return by_id
    by_label = [e for e in catalog if str(e.get("label") or "").lower() == q]
    if by_label:
        return by_label
    if q in ("ios", "android"):
        return [e for e in catalog if e.get("platform") == q]
    return []


def _format_catalog(catalog):
    if not catalog:
        return "  (nothing in this account's catalog)"
    lines = []
    for e in catalog:
        note = "temporary" if e.get("temporary") else ""
        lines.append(f"  {e.get('label') or 'phone':<18} {e.get('platform') or '':<8} "
                     f"{e.get('id') or '-':<14} {e.get('state') or '-':<12} {note}".rstrip())
    return "\n".join(lines)


def _create_body(entry, timeout):
    """The POST /sessions body for one catalog entry. A bare start (entry is
    None) names nothing: the service applies GET /me → default. This is the
    only place the CLI looks at platform, and only to choose device_id or
    profile_id."""
    body = {"timeout_seconds": timeout}
    if entry is None:
        return body
    body["platform"] = entry.get("platform")
    body["kind"] = entry.get("kind")
    if entry.get("temporary"):
        return body
    if entry.get("platform") == "ios":
        if entry.get("id"):
            body["device_id"] = entry["id"]
    elif entry.get("id"):
        body["profile_id"] = entry["id"]
    return body


def _needs_adb(entry, me):
    """An emulator link is adb. A device link is not. A bare start gets
    what the service gives an unnamed start."""
    chosen = entry if entry is not None else _unnamed(me)
    return chosen.get("kind") != "device"


def _starting_line(entry, me):
    shown = entry if entry is not None else _unnamed(me)
    if shown and shown.get("platform") == "ios":
        return "Starting " + _iphone_words(shown.get("label") or shown.get("id") or "") + "…"
    if shown and shown.get("temporary"):
        return "Starting a temporary phone…"
    if shown and shown.get("label"):
        return f"Starting {shown['label']}…"
    return "Starting your phone…"


def _parse_for(text):
    """Seconds. A bare number is minutes. `90s` is seconds. `15m` / `15min`
    is minutes."""
    t = str(text).strip().lower()
    as_minutes = True
    if t.endswith("min"):
        t = t[:-3]
    elif t.endswith("m"):
        t = t[:-1]
    elif t.endswith("s"):
        t, as_minutes = t[:-1], False
    n = _int(t, "--for")
    return n * 60 if as_minutes else n


def _already_running(session):
    sid = (session or {}).get("id") or (session or {}).get("session") or "SID"
    sys.exit(f"That phone is already running (session {sid}). "
             f"Attach with `--session {sid}`.")


def _saved_holder(entry, me):
    """The profile id this start will occupy, when it is the saved Android.
    None for a temporary phone, an iPhone, or a bare start whose default is
    not that profile."""
    shown = entry if entry is not None else _unnamed(me)
    profile = (me.get("profile") or {}).get("id")
    if not shown or not profile or shown.get("temporary") or shown.get("platform") == "ios":
        return None
    if shown.get("id") == profile:
        return profile
    return None


def _wait_out_save(profile_id):
    """The saved Android can be `saving` for a few seconds after stop. Wait
    that out. If a ready session already holds it, say so and exit. -> None
    when the phone is free to start."""
    deadline = time.monotonic() + SAVE_WAIT
    told = False
    while True:
        me = _me()
        profile = me.get("profile") or {}
        if profile.get("id") != profile_id:
            return
        state = profile.get("state")
        live = None
        if state == "running" and profile.get("session"):
            try:
                live = _api("GET", f"/sessions/{profile['session']}")
            except CloudError:
                live = None
            if live and live.get("state") in ("provisioning", "ready"):
                _already_running(live)
        if state not in ("saving", "running"):
            return
        if live and live.get("state") not in ("closing", None):
            return
        if time.monotonic() >= deadline:
            sys.exit("Your phone is still shutting down from its last session. "
                     "Try again in a few seconds.")
        if not told:
            print(term.note("Your phone is still saving from its last session…"))
            told = True
        time.sleep(3)


def _reusable(selector):
    """A session this machine already started for the same catalog choice,
    still provisioning or ready. None when this start should create one."""
    try:
        state = _load_state()
    except CloudPhoneError:
        raise
    except Exception:
        return None
    seen = set()
    for rec in _known_sessions(state).values():
        rec = _as_linked(rec)
        if not rec or rec.get("sid") in seen or rec.get("selector") != selector:
            continue
        seen.add(rec["sid"])
        try:
            live = _api("GET", f"/sessions/{rec['sid']}")
        except CloudError:
            continue
        if live.get("state") in ("provisioning", "ready"):
            return live, rec
    return None


def _reject_removed(args):
    gone = {
        "--iphone": "name the phone instead: `cloud start ios` when that is unambiguous, or an id or label from `cloud ls`",
        "--device": "pass the catalog id: `cloud start ID`",
        "--temp": "start the temporary entry: `cloud start` with its label (see `cloud ls`)",
        "--timeout": "pass the length with `--for` (`90s`, `15m`, or a number of minutes)",
    }
    for flag, hint in gone.items():
        if flag in args:
            sys.exit(f"{flag} was removed. {hint}.")


def _start(args):
    args = list(args)
    _reject_removed(args)
    watch = not _flag(args, "--no-watch")
    for_opt = _option(args, "--for")
    minutes_opt = _option(args, "--minutes", "-m")
    if any(a.startswith("-") for a in args):
        sys.exit(CLI_USAGE)
    if len(args) > 1:
        sys.exit(CLI_USAGE)
    # No name: this machine's own preference (`config set cloud.phone ...`),
    # else whatever the service gives an unnamed start (a temporary Android).
    configured = str(config.get("cloud.phone") or "").strip()
    phone = args[0] if args else (configured or None)
    if for_opt and minutes_opt:
        sys.exit("Pass --for or --minutes, not both.")
    cap = int(config.get("cloud.max_minutes"))
    if for_opt:
        timeout = _parse_for(for_opt)
    elif minutes_opt:
        timeout = _int(minutes_opt, "--minutes") * 60
    else:
        timeout = int(config.get("cloud.minutes")) * 60
    if timeout < 1 or timeout > cap * 60:
        sys.exit(f"Duration must be between 1 and {cap} minutes "
                 f"(raise the cap with `phone-harness config set cloud.max_minutes N`).")

    me = _me()
    catalog = _catalog(me)
    entry = None
    if phone:
        matches = _match_catalog(catalog, phone)
        if not matches:
            where = (" (from `config set cloud.phone`; clear it with `config unset cloud.phone`)"
                     if not args else "")
            sys.exit(f"No phone named {phone!r} is in this account's catalog{where}.\n"
                     f"{_format_catalog(catalog)}")
        if len(matches) > 1:
            print(_format_catalog(matches))
            if not sys.stdin.isatty():
                sys.exit(f"{phone!r} matches {len(matches)} phones. "
                         "Pass a catalog id or label.")
            choice = input("Id or label: ").strip()
            narrowed = _match_catalog(matches, choice)
            if len(narrowed) != 1:
                sys.exit(f"{choice!r} matches {len(narrowed)} phones.")
            matches = narrowed
        entry = matches[0]

    if _needs_adb(entry, me):
        _adb_present()

    selector = _entry_selector(entry)
    reused = _reusable(selector)
    if reused:
        live, rec = reused
        print(term.note(f"Already attached to session {live['id']}; reusing it."))
        return _report(_wait_ready(live["id"]), selector=selector,
                       label=rec.get("label"), watch=False)

    holder = _saved_holder(entry, me)
    if holder:
        _wait_out_save(holder)

    # Another session may keep running. Say so, and how to aim at it again.
    try:
        previous = _as_linked(_load_state().get("session"))
    except CloudPhoneError:
        raise
    except Exception:
        previous = None
    if previous and previous.get("selector") != selector:
        try:
            still = _api("GET", f"/sessions/{previous['sid']}")
        except CloudError:
            still = None
        if still and still.get("state") in ("provisioning", "ready"):
            print(term.note(
                f"Session {previous['sid']} stays up. `--session {previous['sid']}` targets it."))

    body = _create_body(entry, timeout)
    label = (entry or {}).get("label")
    request_key = uuid.uuid4().hex
    print(_starting_line(entry, me))
    try:
        created = _api("POST", "/sessions", body, headers={"Idempotency-Key": request_key})
    except CloudError as e:
        if e.code == "profile_running":
            _already_running({"id": e.body.get("session")})
        if e.code == "device_unavailable":
            sys.exit(_explain(e))
        if e.status == 400:
            sys.exit(f"{_explain(e)}\n{_format_catalog(catalog)}")
        raise
    except OSError:
        # The request may have been admitted even though the answer was lost.
        created = _api("GET", f"/sessions/requests/{request_key}")
        if created.get("cleanup_complete") or not created.get("id"):
            raise
    return _report(_wait_ready(created["id"]), selector=selector, label=label, watch=watch)




def _report(session, profile_id=None, watch=True, selector=None, label=None):
    attached_now = _attach(session, profile_id, selector=selector, label=label)
    how = (session.get("startup") or {}).get("startup")
    note = {"exact": " Resumed exactly where you left it.",
            "rebooted": " Rebooted from saved storage: apps and logins kept, the screen is not."}
    words = _kind_of_phone({**session, "link": attached_now["link"]}, label or attached_now.get("label"))
    ready_note = note.get(how, "")
    print(f"{term.mark()} ready.{term.note(ready_note) if ready_note else ''}")
    print(f"  session  {session['id']}  ({words})")
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
            telemetry.note_session(sid, before.get("platform"))
            held_profile = held_profile or _keeps_state(before)
            _api("DELETE", f"/sessions/{sid}")
            print(f"{term.mark()} Session {sid} ended.")
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
        print(term.note("  Your phone is being saved (about 30s); `phone-harness cloud phone` shows when."))
        return 0
    print(term.note("  Saving your phone…"), end="", flush=True)
    deadline = time.monotonic() + SAVE_WAIT
    state = "saving"
    while time.monotonic() < deadline:
        time.sleep(3)
        state = (_me().get("profile") or {}).get("state")
        if state not in ("saving", "running"):
            break
    print(f" {state}.")
    return 0


# --- looking -----------------------------------------------------------------

def _status(args):
    if not _bearer(required=False):
        print(term.err("Not signed in. Run: phone-harness cloud login", sys.stdout))
        print(term.note(_waitlist_line()))
        return 1
    me = _me()
    if os.environ.get("PHONE_HARNESS_CLOUD_API"):
        print(f"api         {_target('API')}")
    print(f"signed in   {me.get('email') or me.get('uid')}")
    print(f"credit      {_credit(me)}")
    profile = me.get("profile") or {}
    try:
        sess = attached()
    except CloudPhoneError as e:
        print(term.err(str(e), sys.stdout))
        return 1
    live = None
    if sess:
        try:
            live = _api("GET", f"/sessions/{sess['sid']}")
        except CloudError:
            live = None
    # Just after `stop`, the API still says the profile is `running` while its
    # session is `closing`; the truth for the user is that it is being saved.
    # A control link keeps its data on the phone, so closing it is not a save.
    if live and live.get("state") == "closing" and _keeps_state(live):
        profile = {**profile, "state": "saving"}
    print(f"phone       {_describe_profile(profile)}")
    if not sess:
        lost_rec = lost()
        if lost_rec:
            print(term.err(ended_message(lost_rec), sys.stdout))
            # Shown once. The next status must not print it again.
            if lost_rec.get("sid") not in _reported_dead:
                report_ended(lost_rec)
            return 1
        running = me.get("active_session_count") or 0
        print("session     none attached" + (f" ({running} running: `phone-harness cloud ls`)"
                                              if running else " — `phone-harness cloud start`"))
        return 0
    if live is None:
        cause = "expired" if _expired(sess) else "unreachable"
        rec = _ended_record(sess, cause)
        print(term.err(ended_message(rec), sys.stdout))
        report_ended(rec)
        return 1
    state = ("closing — saving the phone"
             if live["state"] == "closing" and _keeps_state(live)
             else live["state"])
    label = sess.get("label")
    print(f"session     {live['id']} · {state} · {_left(live.get('expires_at'))} left"
          f" · {_kind_of_phone({**live, 'link': sess['link']}, label)}")
    print(f"{sess['link']['kind']:<12}{_describe_link(sess)}")
    return 0


def _text_id(value):
    """An id string, or the id inside a small object. Display text is not an id."""
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, dict):
        for key in ("id", "device_id", "profile_id"):
            found = _text_id(value.get(key))
            if found:
                return found
    return None


def _id_set(mapping, keys):
    found = set()
    if not isinstance(mapping, dict):
        return found
    for key in keys:
        text = _text_id(mapping.get(key))
        if text:
            found.add(text)
    return found


# `phone` used to be a saved-phone id. The live API also puts a display name
# there ("iPhone", "Android emulator"); those are not catalog ids.
_PHONE_DISPLAY_NAMES = {"iphone", "android", "android emulator", "your iphone", "your phone"}


def _session_ids(session):
    """device ids and profile ids this session holds.

    `profile` is the older name for `profile_id`. A string `phone` counts only
    when it is an id, not the display name."""
    devices = _id_set(session, ("device_id",))
    profiles = _id_set(session, ("profile_id", "profile"))
    phone = _text_id(session.get("phone"))
    if phone and phone.lower() not in _PHONE_DISPLAY_NAMES and phone not in devices:
        profiles.add(phone)
    return devices, profiles


def _session_on_entry(entry, session):
    """True when this live session is that catalog phone.

    Match by device_id or profile_id (a catalog row keeps that id on `id`,
    `device_id`, or `profile_id`). A temporary row, which names neither, is
    the session that also names neither. Anything else is not this row.
    An iPhone session does not carry the catalog id: `device` is the display
    word "iPhone". That case is decided in `_inline_running`."""
    devices, profiles = _session_ids(session)
    entry_ids = _id_set(entry, ("id", "device_id", "profile_id"))
    if (devices | profiles) & entry_ids:
        return True
    if entry.get("temporary") and not entry_ids and not devices and not profiles:
        if _link_kind(session) == "control" or session.get("platform") == "ios":
            return False
        platform = entry.get("platform")
        return not platform or session.get("platform") in (None, platform)
    return False


def _inline_running(entries, rows, sessions, used, blob, mine):
    """Put one unmatched live session on the one catalog phone that is running.

    A cloud iPhone session does not echo the catalog id. It carries
    `platform`, `kind`, `device` set to the word "iPhone", and a `profile`
    like `iphone-…` that matches no catalog row. Any session whose
    device_id or profile matched no row is unmatched, even when those fields
    are set. When exactly one entry of that platform and kind is `running`,
    and exactly one unmatched session has that same platform and kind, the
    session is that row.
    """
    for entry, row in zip(entries, rows):
        if row.get("session") or entry.get("state") != "running":
            continue
        platform, kind = entry.get("platform"), entry.get("kind")
        if not platform or not kind:
            continue
        running = [e for e in entries
                   if e.get("platform") == platform and e.get("kind") == kind
                   and e.get("state") == "running"]
        if len(running) != 1:
            continue
        candidates = [s for s in sessions
                      if s.get("id") not in used
                      and s.get("platform") == platform
                      and s.get("kind") == kind]
        if len(candidates) != 1:
            continue
        match = candidates[0]
        used.add(match["id"])
        row["session"] = blob(match)
        row["targeted"] = match.get("id") == mine


def _ls_rows(me, sessions):
    """One row per catalog entry, with a live session inlined when it is that
    phone. Sessions that match nothing still appear, after the catalog."""
    catalog = _catalog(me)
    mine = (attached() or {}).get("sid")
    used = set()

    def session_blob(s):
        return {"id": s.get("id"), "state": s.get("state"),
                "left": _left(s.get("expires_at")), "expires_at": s.get("expires_at")}

    rows = []
    for entry in catalog:
        match = None
        for s in sessions:
            if s.get("id") in used:
                continue
            if _session_on_entry(entry, s):
                match = s
                used.add(s["id"])
                break
        rows.append({
            "label": entry.get("label") or "",
            "platform": entry.get("platform"),
            "id": entry.get("id") or entry.get("device_id") or entry.get("profile_id"),
            "state": entry.get("state"),
            "temporary": bool(entry.get("temporary")),
            "session": session_blob(match) if match else None,
            "targeted": bool(match and match.get("id") == mine),
        })
    _inline_running(catalog, rows, sessions, used, session_blob, mine)
    for s in sessions:
        if s.get("id") in used:
            continue
        named, _profiles = _session_ids(s)
        rows.append({
            "label": s.get("device") or s.get("label") or "session",
            "platform": s.get("platform"),
            "id": None,
            "state": s.get("state"),
            "temporary": not named and not _profiles and _link_kind(s) != "control",
            "session": session_blob(s),
            "targeted": s.get("id") == mine,
            "unmatched": True,
        })
    return rows


def _format_ls(rows):
    """Text table for `cloud ls`. Widths follow the header and the cells.

    A long catalog id stays inside ID; the next column starts after it.
    The last column is not padded, and trailing space is stripped, matching
    the previous line shape when every value is short.
    """
    header = ["", "LABEL", "PLATFORM", "ID", "STATE", "NOTE", "SESSION", "LEFT"]
    body = []
    for row in rows:
        sess = row.get("session") or {}
        body.append([
            "*" if row.get("targeted") else "",
            str(row.get("label") or ""),
            str(row.get("platform") or ""),
            str(row.get("id") or "-"),
            str(row.get("state") or "-"),
            "temporary" if row.get("temporary") else "",
            str(sess.get("id") or "-"),
            str(sess.get("left") or ""),
        ])
    if not body:
        return ""
    widths = [max(len(header[i]), *(len(cells[i]) for cells in body)) for i in range(len(header))]
    widths[0] = max(widths[0], 1)

    def render(cells):
        padded = [cells[i].ljust(widths[i]) for i in range(len(cells) - 1)]
        padded.append(cells[-1])
        return " ".join(padded).rstrip()

    return "\n".join([render(header), *(render(cells) for cells in body)])


def _ls(args):
    as_json = _flag(args, "--json")
    if args:
        sys.exit(CLI_USAGE)
    me = _me()
    sessions = _api("GET", "/sessions")
    rows = _ls_rows(me, sessions)
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No phones in this account's catalog.")
        return 0
    print(_format_ls(rows))
    return 0


def _show(args):
    as_json = _flag(args, "--json")
    session = _api("GET", f"/sessions/{_resolve_sid(args[0] if args else None)}")
    if as_json:
        print(json.dumps(session, indent=2))
        return 0
    adb = session.pop("adb", None) or {}
    control = session.pop("control", None) or {}
    session.pop("watch_url", None)          # a credential; `cloud watch` opens it
    for k, v in session.items():
        if v not in (None, "", {}):
            print(f"{k:<16} {json.dumps(v) if isinstance(v, (dict, list)) else v}")
    if adb.get("host"):
        print(f"{'adb':<16} {adb['host']}:{adb['port']}")
    if control.get("url"):
        print(f"{'control':<16} {control['url']}")
    return 0


def _use(args):
    if len(args) != 1:
        sys.exit("Usage: phone-harness cloud use SID")
    session = _wait_ready(_resolve_sid(args[0]))
    sess = _attach(session)
    print(f"{term.mark()} attached to {session['id']}; {_describe_link(sess)}")
    print(term.note("Helpers with no --session and no PHONE_HARNESS_SESSION follow this one."))
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
    `watch` is for looking; this is for taking the controls. An optional SID
    checks that the session exists; the page is still the account dashboard."""
    if args and args[0] != "--print":
        _resolve_sid(args[0])
        args = args[1:]
    url = _dashboard()
    if _flag(args, "--print") or not _open_browser(url):
        print(url)
    else:
        print(f"opened {url} — sign in there to control the phone")
    return 0


def _whoami(args):
    me = _me()
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
            sys.exit("This wipes the saved Android's stored state — its apps, logins and "
                     "data — and cannot be undone. The CLI has no iPhone reset; that is "
                     "the operator's.\nRun: phone-harness cloud phone reset --yes")
        _api("POST", "/me/profile/reset")
        print("Wiped the saved Android's stored state. The next start of that phone begins fresh.")
        return 0
    profile = _me().get("profile") or {}
    print(json.dumps(profile, indent=2) if _flag(args, "--json") else _describe_profile(profile))
    return 0


def _keys(args):
    if args[:1] == ["create"]:
        minted = _api("POST", "/me/keys", {"label": " ".join(args[1:]) or None})
        print(minted["key"])
        print(term.note("Shown once; store it now.", sys.stderr), file=sys.stderr)
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
  phone-harness cloud [--session SID]          who is signed in, what is attached
  phone-harness cloud login [--no-browser]     sign in through the browser
  phone-harness cloud logout | whoami
  phone-harness cloud ls [--json]              the account catalog, with live sessions inlined
                                               (* = the session this process targets)
  phone-harness cloud start [PHONE] [--for DURATION] [--no-watch]
                                               PHONE is a catalog id or label, or ios/android
                                               when only one entry has that platform. No PHONE
                                               starts cloud.phone (config set cloud.phone NAME),
                                               else a temporary Android, the service's rule for an
                                               unnamed start. --for is 90s, 15m, or
                                               minutes (default 15, cap cloud.max_minutes).
                                               --minutes N is the same thing, deprecated.
                                               Opens the live view (cloud.watch)
  phone-harness cloud stop [SID|--all] [--wait]
                                               end it. An adb phone with a saved profile is saved;
                                               a control link (an iPhone) keeps its own data
                                               (--wait watches an Android save finish)
  phone-harness cloud show [SID] [--json]      one session in full
  phone-harness cloud use SID                  point the terminal fallback at a running session
  phone-harness cloud watch [SID] [--print]    the read-only live view (no sign-in; shareable)
  phone-harness cloud open [SID] [--print]     the dashboard: control the phone yourself (sign-in)
  phone-harness cloud phone [reset --yes]      the saved Android. reset --yes wipes its stored state
  phone-harness cloud keys [create [LABEL] | revoke HASH]   API keys, for CI
  phone-harness cloud history [-n NUM] [--json]
Every command takes --session SID, or PHONE_HARNESS_SESSION, and so do helper
scripts: that pins one phone for this process. With neither, helpers follow the
attached session (the last start, or cloud use). SID may be a unique prefix.
PHONE_HARNESS_CLOUD_API, _OAUTH_ISSUER and _OAUTH_CLIENT_ID point the CLI
at a Phone Harness instance of your own; unset, it is the public cloud.
They may live in a .env file at the repo root or in the agent workspace (never committed).
"""

_COMMANDS = {"login": _login, "logout": _logout, "whoami": _whoami, "start": _start,
             "stop": _stop, "ls": _ls, "show": _show, "use": _use, "watch": _watch, "open": _open,
             "phone": _phone, "keys": _keys, "history": _history}


def _cloud_parsers():
    """One argparse parser per subcommand, for --help and unknown flags.

    The command functions still read the original words. This layer only
    rejects a flag they do not have, and prints help for `-h` / `--help`.
    """
    also = "Also accepts --session SID, or PHONE_HARNESS_SESSION, to pin one phone."

    def make(name, usage, description):
        return term.Parser(
            prog=f"phone-harness cloud {name}",
            usage=usage,
            description=description + "\n\n" + also,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )

    parsers = {}
    login = make("login", "phone-harness cloud login [--no-browser]",
                 "Sign in through the browser.")
    login.add_argument("--no-browser", action="store_true",
                       help="print the link and code without opening a browser")
    parsers["login"] = login

    parsers["logout"] = make("logout", "phone-harness cloud logout",
                             "Sign out on this machine.")

    whoami = make("whoami", "phone-harness cloud whoami [--json]",
                  "Show the signed-in account.")
    whoami.add_argument("--json", action="store_true", help="print the account record as JSON")
    parsers["whoami"] = whoami

    start = make(
        "start",
        "phone-harness cloud start [PHONE] [--for DURATION] [--minutes N] [--no-watch]",
        "PHONE is a catalog id or label, or ios/android when only one entry has that "
        "platform. With no PHONE, start cloud.phone if it is set, otherwise a temporary "
        "Android. --for is 90s, 15m, or a number of minutes (default 15, cap "
        "cloud.max_minutes). --minutes N is the same length. Opens the live view.")
    start.add_argument("--for", metavar="DURATION",
                       help="how long to keep the phone (90s, 15m, or minutes)")
    start.add_argument("--minutes", "-m", metavar="N",
                       help="length in minutes; same as --for")
    start.add_argument("--no-watch", action="store_true", help="do not open the live view")
    # Accepted so the command can print the removal message itself.
    for flag in ("--iphone", "--device", "--temp", "--timeout"):
        start.add_argument(flag, action="store_true", help=argparse.SUPPRESS)
    parsers["start"] = start

    stop = make("stop", "phone-harness cloud stop [SID] [--all] [--wait]",
                "End a session. An adb phone with a saved profile is saved; a control "
                "link keeps its own data. --wait watches an Android save finish.")
    stop.add_argument("--all", action="store_true", help="end every running session")
    stop.add_argument("--wait", action="store_true", help="wait until an Android save finishes")
    parsers["stop"] = stop

    ls = make("ls", "phone-harness cloud ls [--json]",
              "The account catalog, with a live session inlined on its phone. "
              "* marks the session this process targets.")
    ls.add_argument("--json", action="store_true", help="print the rows as JSON")
    parsers["ls"] = ls

    show = make("show", "phone-harness cloud show [SID] [--json]", "One session in full.")
    show.add_argument("--json", action="store_true", help="print the session as JSON")
    parsers["show"] = show

    parsers["use"] = make("use", "phone-harness cloud use SID",
                          "Point the terminal fallback at a running session. "
                          "SID may be a unique prefix.")

    watch = make("watch", "phone-harness cloud watch [SID] [--print]",
                 "The read-only live view. It needs no sign-in, and the link is shareable.")
    watch.add_argument("--print", action="store_true", help="print the link instead of opening it")
    parsers["watch"] = watch

    open_ = make("open", "phone-harness cloud open [SID] [--print]",
                 "The dashboard, where you control the phone yourself.")
    open_.add_argument("--print", action="store_true", help="print the link instead of opening it")
    parsers["open"] = open_

    phone = make("phone", "phone-harness cloud phone [reset --yes] [--json]",
                 "The saved Android phone. reset --yes wipes its stored state. "
                 "The CLI has no iPhone reset.")
    phone.add_argument("--json", action="store_true", help="print the profile as JSON")
    phone.add_argument("--yes", action="store_true", help="confirm phone reset")
    parsers["phone"] = phone

    keys = make("keys", "phone-harness cloud keys [create [LABEL] | revoke HASH] [--json]",
                "API keys, for CI. create mints a key and prints it once. "
                "revoke takes a key hash or a unique prefix of one.")
    keys.add_argument("--json", action="store_true", help="print the keys as JSON")
    parsers["keys"] = keys

    history = make("history", "phone-harness cloud history [-n NUM] [--json]",
                   "Finished sessions.")
    history.add_argument("-n", "--limit", metavar="NUM",
                         help="how many sessions to show (default 20)")
    history.add_argument("--json", action="store_true", help="print the sessions as JSON")
    parsers["history"] = history
    return parsers


def cli(args):
    args = list(args)
    try:
        return _dispatch(args)
    except SystemExit as e:
        # Command errors raise SystemExit(message), which the interpreter would
        # print and turn into exit code 1. Print it here so a terminal can
        # color it, and so the process exit code stays 1.
        if isinstance(e.code, str):
            print(term.err(e.code), file=sys.stderr)
            return 1
        raise


def _dispatch(args):
    pinned = _option(args, "--session")
    if pinned:
        os.environ["PHONE_HARNESS_SESSION"] = pinned
    if not args:
        return _status(args)
    if args[0] in ("-h", "--help"):
        print(CLI_USAGE)
        return 0
    fn = _COMMANDS.get(args[0])
    if fn is None:
        print(CLI_USAGE)
        return 2
    term.reject_unknown_flags(_cloud_parsers()[args[0]], args[1:])
    try:
        return fn(args[1:])
    except CloudError as e:
        print(term.err(_explain(e)), file=sys.stderr)
        return 1
    except OSError as e:
        print(term.err(f"Could not reach {_target('API')}: {e}"), file=sys.stderr)
        return 1
