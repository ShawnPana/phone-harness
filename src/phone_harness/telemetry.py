"""Best-effort, opt-out telemetry for phone-harness.

One `cli_event` per CLI invocation, sent to PostHog from a detached helper
process so the CLI never blocks on the network.

Off: `phone-harness config set telemetry false`, or PHONE_HARNESS_TELEMETRY=0
for one call. The install id lives in the state dir; see config.py.
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
