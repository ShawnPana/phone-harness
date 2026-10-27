"""Best-effort, opt-out telemetry for phone-harness.

One `cli_event` per CLI invocation, sent to PostHog from a detached helper
process so the CLI never blocks on the network. The distinct id is the
random install id from the state dir (see config.py). Events do not carry
the email, access token, or API key.

After a cloud sign-in, one `$identify` sets `account_id` (the cloud uid)
on that same install id, so the install can be joined to the account.
The person stays the install: aliasing it to the account would merge two
people if a later sign-in on this machine used a different account. The
email is not sent. The same account is not identified twice.

Off: `phone-harness config set telemetry false`, or PHONE_HARNESS_TELEMETRY=0
for one call. Nothing is sent while telemetry is off, including `$identify`.
"""

from __future__ import annotations

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


def identify_account(account_id) -> None:
    """Link this install to a Phone Harness Cloud account id.

    No-op when telemetry is off, when `account_id` is blank, or when this
    install was already linked to that same account.
    """
    if not is_enabled():
        return
    if not isinstance(account_id, str) or not account_id.strip():
        return
    account_id = account_id.strip()
    try:
        if _config.install_account() == account_id:
            return
        payload = {
            "api_key": POSTHOG_KEY,
            "distinct_id": _config.install_id(),
            "event": "$identify",
            "properties": {
                "$process_person_profile": True,
                "$set": {"account_id": account_id},
            },
        }
        _send_detached(payload)
        _config.set_install_account(account_id)
    except Exception:
        return


def capture_cli_event(
    *,
    action: str,
    command: str,
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
                # 'iphone-mirroring' | 'android'
                "phone": phone,
                "client": os.environ.get("PH_CLIENT") or None,
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
