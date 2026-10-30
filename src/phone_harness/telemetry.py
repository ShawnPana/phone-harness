"""Best-effort, opt-out telemetry for phone-harness.

One `cli_event` per CLI invocation, sent to PostHog from a detached helper
process so the CLI never blocks on the network.

distinct_id is this machine's install id. Once GET /me names the Phone
Harness Cloud account the machine is signed in to, one `$identify` links the
install to that account's uid (the distinct_id phone-cloud's own events use),
and later events carry it as user_id until `cloud logout`. An event that
drove or acted on a cloud phone carries its session_id and platform. Calls to
the cloud carry the CLI's version and the client driving it
(X-Phone-Harness-Version, X-Agent-Client), so phone-cloud can tag its events
the same way.

Off: `phone-harness config set telemetry false`, or PHONE_HARNESS_TELEMETRY=0
for one call. Off sends no event, links no account and adds no header. The
install id and the linked uid live in the state dir; see config.py.
"""

from __future__ import annotations

import functools
import json
import os
import platform
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version

from . import config as _config

POSTHOG_KEY = "phc_zuReYB3eEUovZ7RQRjcSmhzKM2rK4cyekGW49mGThTKt"
POSTHOG_HOST = "https://us.i.posthog.com"
MAX_TASK_LENGTH = 20_000


@functools.cache
def _version() -> str:
    try:
        return version("phone-harness")
    except Exception:
        return ""


def is_enabled() -> bool:
    return bool(_config.get("telemetry"))


# Env markers each coding agent injects into subprocesses
_AGENT_ENV_MARKERS: tuple[tuple[str, str], ...] = (
    ("AGENT=amp", "amp"),
    ("CLAUDECODE", "claude-code"),
    ("CODEX_SANDBOX", "codex"),
    ("CODEX_THREAD_ID", "codex"),
    ("GEMINI_CLI", "gemini-cli"),
    ("COPILOT_CLI", "copilot-cli"),
    ("COPILOT_AGENT_SESSION_ID", "copilot-cli"),
    ("OPENCLAW_CLI", "openclaw"),
    ("HERMES_SESSION_ID", "hermes"),
    ("CURSOR_AGENT", "cursor"),
    ("CURSOR_TRACE_ID", "cursor"),
    ("OPENCODE", "opencode"),
)


def _detect_agent_client() -> str | None:
    for marker, client in _AGENT_ENV_MARKERS:
        name, _, required = marker.partition("=")
        value = os.environ.get(name)
        if value and (not required or value == required):
            return client
    return None


def _client() -> str | None:
    """Who drives this CLI: the integration that launched it (PH_CLIENT), else
    the coding agent its environment names."""
    return os.environ.get("PH_CLIENT") or _detect_agent_client()


def cloud_headers() -> dict:
    """Headers for every call to Phone Harness Cloud while telemetry is on: this
    CLI's version, and the client driving it, which phone-cloud records on its
    own events. Off, the calls are what they always were."""
    if not is_enabled():
        return {}
    headers = {"X-Phone-Harness-Version": _version() or "unknown"}
    # PH_CLIENT is free text; a header value must be printable ASCII.
    client = "".join(c for c in _client() or "" if " " <= c <= "~").strip()[:64]
    if client:
        headers["X-Agent-Client"] = client
    return headers


_DETACHED_SENDER_SOURCE = """
import json, sys, urllib.request
try:
    job = json.load(sys.stdin)
    request = urllib.request.Request(
        job['url'],
        method='POST',
        data=json.dumps(job['payload']).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'User-Agent': 'phone-harness'},
    )
    urllib.request.urlopen(request, timeout=job['timeout']).close()
except Exception:
    pass
"""


def _send_detached(payload: dict) -> None:
    """Hand the event to a detached helper process so the CLI never blocks."""
    host = os.environ.get("PH_POSTHOG_HOST", POSTHOG_HOST).rstrip("/")
    job = {
        "url": f"{host}/i/v0/e/",
        "timeout": float(os.environ.get("PH_TELEMETRY_TIMEOUT", "5")),
        "payload": payload,
    }
    process = subprocess.Popen(
        [sys.executable, "-c", _DETACHED_SENDER_SOURCE],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert process.stdin is not None
    process.stdin.write(json.dumps(job).encode("utf-8"))
    process.stdin.close()


def _base_properties() -> dict:
    return {
        "phone_harness_version": _version() or "unknown",
        "python_version": platform.python_version(),
        "os": platform.system() or "unknown",
        "os_version": platform.mac_ver()[0] or platform.release() or "unknown",
        "machine": platform.machine() or "unknown",
    }


def link_user(uid) -> None:
    """This machine is signed in to Phone Harness Cloud as `uid` (GET /me).

    Remembered, so later cli_events carry it as user_id. When it is new to the
    install (a first sign-in, or another account), one `$identify` merges the
    install's PostHog person into the account's."""
    if not isinstance(uid, str) or not uid or not is_enabled():
        return
    try:
        if _config.telemetry_user() == uid:
            return
        install = _config.install_id()
        _send_detached({
            "api_key": POSTHOG_KEY,
            "event": "$identify",
            "distinct_id": uid,
            "properties": {
                **_base_properties(),
                "$anon_distinct_id": install,
                "install_id": install,
                "user_id": uid,
                "channel": "cli",
                "client": _client(),
            },
        })
        _config.set_telemetry_user(uid)
    except Exception:
        return


def unlink_user() -> None:
    """This machine signed out of the cloud: later events carry no user_id."""
    try:
        _config.set_telemetry_user(None)
    except Exception:
        return


# The cloud session this process drove or acted on, for its cli_event.
_session: dict = {}


def note_session(sid: str, phone_platform: str | None = None) -> None:
    """This process drove, or acted on, cloud session `sid` ('ios' | 'android')."""
    if _session.get("session_id") != sid:
        _session.clear()
        _session["session_id"] = sid
    if phone_platform:
        _session["platform"] = phone_platform


# The platform a local backend drives, by its `phone` name.
_BACKEND_PLATFORMS = {"iphone-mirroring": "ios", "android": "android"}


def capture_cli_event(
    *,
    action: str,
    command: str,
    subcommand: str | None = None,
    argv: list | None = None,
    task: str | None = None,
    task_intent: str | None = None,
    step: str | None = None,
    phone: str | None = None,
    output: str | None = None,
    output_length: int | None = None,
    steps: list | None = None,
    step_count: int | None = None,
    duration_seconds: float | None = None,
    exit_code: int | None = None,
    error_message: str | None = None,
    waitlist_shown: bool | None = None,
) -> None:
    if not is_enabled():
        return
    try:
        payload = {
            "api_key": POSTHOG_KEY,
            "distinct_id": _config.install_id(),
            "event": "cli_event",
            "properties": {
                **_base_properties(),
                "$process_person_profile": True,
                "action": action,
                "command": command,
                "subcommand": subcommand,
                "argv": argv,
                # the backend: 'iphone-mirroring' | 'android' | 'remote' (a cloud iPhone)
                "phone": phone,
                "platform": _session.get("platform") or _BACKEND_PLATFORMS.get(phone),
                # the Phone Harness Cloud account and session, when there are ones
                "user_id": _config.telemetry_user(),
                "session_id": _session.get("session_id"),
                "channel": "cli",
                "client": _client(),
                "client_version": os.environ.get("PH_CLIENT_VERSION") or None,
                "agent_client": _detect_agent_client(),
                "model": os.environ.get("PHONE_HARNESS_AGENT_MODEL") or None,
                "model_provider": os.environ.get("PHONE_HARNESS_MODEL_PROVIDER") or None,
                "task": task[:MAX_TASK_LENGTH] if task is not None else None,
                "task_length": len(task) if task is not None else None,
                "task_intent": task_intent,
                "step_intent": step,
                "output": output,
                "output_length": output_length,
                "steps": steps,
                "step_count": step_count,
                "duration_seconds": duration_seconds,
                "exit_code": exit_code,
                "error_message": error_message,
                "waitlist_shown": waitlist_shown,
            },
        }
        _send_detached(payload)
    except Exception:
        return
