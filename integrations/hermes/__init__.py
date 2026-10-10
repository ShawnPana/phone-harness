"""phone-harness for Hermes Agent: one ``phone_exec`` tool, the same shape as Hermes's ``browser_exec``.

The model writes a Python script. The plugin pipes it on stdin to phone-harness, which runs it with
the phone helpers pre-imported against the user's phone, and hands stdout back as the tool result.
Each call is a fresh interpreter; the phone, the agent workspace and its agent_helpers.py persist.

Safety, in order of what it protects:
- A ``pre_tool_call`` hook sends any script that reaches past the phone (imports beyond a small set
  of data modules, file and eval builtins, dunder introspection, adb ``shell()``) to Hermes's
  approval prompt. It is a heuristic gate, not a sandbox.
- One script drives the phone at a time, across subagents and across Hermes processes.
- phone-harness telemetry is off unless the user turns it on in the plugin settings.
"""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

PLUGIN = "phone-harness"
DEFAULT_TIMEOUT_S, MIN_TIMEOUT_S, MAX_TIMEOUT_S = 300, 5, 1800
LOCK_WAIT_S = 120
OUTPUT_CAP, STDERR_CAP = 50_000, 4_000

_THREAD_LOCK = threading.Lock()   # subagents in this process take turns on the phone
_CTX = None                       # PluginContext: settings are read on every call, so changes apply at once

DESCRIPTION = (
    "Drive the user's real phone with phone-harness: their iPhone through the Mac's iPhone Mirroring "
    "window, an Android over adb, or a Phone Harness Cloud phone. `code` runs as Python with the helpers "
    "below pre-imported; print() whatever you need back. Each call is a fresh interpreter (variables do "
    "not persist); the phone keeps its state. Start a task with ensure_device() (alias "
    "ensure_mirroring()); if it raises about the iPhone being in use or timed out, stop and ask the user "
    "to lock their phone, then retry once.\n\n"
    "HELPERS: ensure_device(); connection_state(); screen_info(); screenshot() -> PNG path (look at it "
    "with vision_analyze); ocr() -> [{text, x, y, w, h}] with tap-ready centres; find_text(query); "
    "tap_text(query); ui(); tap_ui(query); tap(x, y) in screen points; "
    "tap_image_point(x, y, image_size=(w, h)) for screenshot pixels; long_press(x, y); "
    "drag(x1, y1, x2, y2); press(combo); type_text(text) (tap the field first); scroll(direction) moves "
    "content and is right for lists; swipe(direction) is a finger flick; scroll_until(done); "
    "scroll_collect(extract, key=...); home(); back() (Android only: on an iPhone tap the app's back "
    "arrow); app_switcher(); open_app(name); current_app(); list_apps(); wait_for_text(query); "
    "wait_for_app(app_id); wait_stable().\n\n"
    "Read the skill phone-harness:phone-harness before the first call. Ask the user before anything "
    "outward-facing or hard to undo (sending, posting, buying, deleting, changing settings). Never type "
    "passwords, PINs or 2FA codes. Scripts that reach past the phone (imports beyond standard data "
    "modules, file access, adb shell) wait for the user's approval."
)

SCHEMA = {
    "name": "phone_exec",
    "description": DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python using the pre-imported phone-harness helpers. print() what you need back.",
            },
            "timeout_s": {
                "type": "integer",
                "default": DEFAULT_TIMEOUT_S,
                "description": f"Max seconds for the script (default {DEFAULT_TIMEOUT_S}, max {MAX_TIMEOUT_S}).",
            },
        },
        "required": ["code"],
    },
}

# ---------------------------------------------------------------------------------------------
# Approval gate: what a phone script may do without asking.

# Modules a phone script can import without touching the computer beyond the phone.
SAFE_MODULES = frozenset({
    "phone_harness", "json", "re", "time", "math", "random", "string", "datetime", "itertools",
    "functools", "collections", "statistics", "textwrap", "difflib", "typing", "dataclasses", "enum",
    "decimal", "fractions", "pprint", "unicodedata", "operator", "heapq", "bisect", "copy",
})
# Builtins that read or write files, run arbitrary code, or wait on a terminal; shell() is
# phone-harness's adb shell.
RISKY_CALLS = frozenset({"open", "exec", "eval", "compile", "__import__", "breakpoint", "input", "shell"})
_DUNDER = re.compile(r"^__\w+__$")


def approval_reason(code: str) -> str | None:
    """Why this script needs the user's OK before it runs, or None when it only drives the phone.

    Phone helpers and plain data handling pass. Host access (imports outside SAFE_MODULES, file and
    eval builtins, dunder introspection) and raw adb shell commands do not. A heuristic, not a sandbox.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None  # it cannot run; the SyntaxError comes back as the result
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in SAFE_MODULES:
                    return f"imports {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "").split(".")[0] not in SAFE_MODULES:
                return f"imports from {node.module or '.'}"
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in RISKY_CALLS:
            return f"calls {node.func.id}()"
        elif isinstance(node, ast.Attribute) and _DUNDER.match(node.attr):
            return f"uses {node.attr}"
        elif isinstance(node, ast.Name) and _DUNDER.match(node.id):
            return f"uses {node.id}"
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _DUNDER.match(node.value):
            return f"references {node.value}"
    return None


def _on_pre_tool_call(tool_name: str = "", args: dict | None = None, task_id: str = "", **kwargs):
    if tool_name != "phone_exec":
        return None
    reason = approval_reason(str((args or {}).get("code") or ""))
    if reason is None:
        return None
    return {"action": "approve",
            "message": f"phone_exec wants to run a script that {reason}, which reaches past the phone "
                       "onto this computer."}

# ---------------------------------------------------------------------------------------------
# Settings and environment.


def _setting(key: str, default):
    if _CTX is None:
        return default
    try:
        value = _CTX.get_config(key, default=default)
    except Exception:
        return default
    return default if value is None else value


def _platform() -> str:
    value = str(_setting("platform", "auto") or "auto").strip().lower()
    return value if value in ("ios", "android") else "auto"


def _telemetry_opt_in() -> bool:
    value = _setting("telemetry", False)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _data_dir() -> Path:
    """Hermes's data folder for this plugin: follows the active profile, survives plugin updates."""
    try:
        from plugins.plugin_storage import plugin_data_dir
        return Path(plugin_data_dir(PLUGIN))
    except Exception:  # outside Hermes (unit tests)
        return Path.home() / ".hermes" / "plugin-data" / PLUGIN


def _workspace() -> Path:
    """PH_AGENT_WORKSPACE: where agent_helpers.py and notes live. The plugin setting, else an inherited
    PH_AGENT_WORKSPACE, else the agent-workspace of a phone-harness checkout at ~/.phone-harness (the
    documented install, which ships agent_helpers.py), else a folder in Hermes's plugin data."""
    for candidate in (str(_setting("agent_workspace", "") or "").strip(), os.environ.get("PH_AGENT_WORKSPACE", "")):
        if candidate:
            return Path(candidate).expanduser()
    checkout = Path.home() / ".phone-harness" / "agent-workspace"
    if checkout.is_dir():
        return checkout
    return _data_dir() / "agent-workspace"


def _module_site_dir() -> str | None:
    """Site dir of a phone_harness that Hermes's own interpreter can import, if any."""
    spec = importlib.util.find_spec("phone_harness")
    if spec is None or not spec.origin:
        return None
    return str(Path(spec.origin).resolve().parent.parent)


def _command() -> tuple[list[str], str | None] | None:
    """(argv, PYTHONPATH or None). The user's own ``phone-harness`` CLI when there is one (the version
    and config they use in their terminal), else the copy Hermes installed from this plugin's
    python_dependencies, run on Hermes's interpreter the way Hermes runs browser-harness."""
    exe = shutil.which("phone-harness")
    if not exe:
        fallback = Path.home() / ".local" / "bin" / "phone-harness"  # uv tool / pipx default
        if fallback.is_file() and os.access(fallback, os.X_OK):
            exe = str(fallback)
    if exe:
        return [exe], None
    site = _module_site_dir()
    if site:
        return [sys.executable, "-m", "phone_harness.run"], site
    return None


def _available() -> bool:
    return _command() is not None


def _env(site: str | None, workspace: Path) -> dict:
    env = dict(os.environ)
    # Hermes may run with its own PYTHONPATH/PYTHONHOME; never leak them into another interpreter.
    env.pop("PYTHONHOME", None)
    if site:
        env["PYTHONPATH"] = site
    else:
        env.pop("PYTHONPATH", None)
    env["PH_AGENT_WORKSPACE"] = str(workspace)
    env["PH_CLIENT"] = "hermes"
    platform = _platform()
    if platform != "auto":
        # Only an env-sourced platform says "the phone on this machine, on purpose": phone-harness
        # otherwise refuses to fall back to it after a rented cloud phone has gone.
        env["PHONE_HARNESS_PLATFORM"] = platform
    if not _telemetry_opt_in():
        env["PHONE_HARNESS_TELEMETRY"] = "0"
    return env

# ---------------------------------------------------------------------------------------------
# One script on the phone at a time.


def _lock_file(fh) -> None:
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(fh) -> None:
    with contextlib.suppress(OSError):
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class PhoneBusy(Exception):
    pass


@contextlib.contextmanager
def _phone_lock(wait_s: float):
    """Across threads (subagents) and processes (Desktop, CLI, cron). Raises PhoneBusy after wait_s."""
    deadline = time.monotonic() + wait_s
    if not _THREAD_LOCK.acquire(timeout=max(wait_s, 0.1)):
        raise PhoneBusy
    try:
        path = _data_dir() / "phone.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+b") as fh:
            while True:
                try:
                    _lock_file(fh)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise PhoneBusy from None
                    time.sleep(0.25)
            try:
                yield
            finally:
                _unlock_file(fh)
    finally:
        _THREAD_LOCK.release()

# ---------------------------------------------------------------------------------------------
# The tool.


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill the CLI and everything it spawned (it runs in its own process group)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


def _result(success: bool, **fields) -> str:
    return json.dumps({"success": success, **{k: v for k, v in fields.items() if v not in (None, "", [])}},
                      ensure_ascii=False)


def _timeout(value) -> int:
    try:
        return max(MIN_TIMEOUT_S, min(int(value or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S


_PNG = re.compile(r"(/[^\s'\"]+\.png)\b")


def phone_exec(args: dict, **kwargs) -> str:
    code = str((args or {}).get("code") or "")
    if not code.strip():
        return _result(False, error="No code provided. Start with: ensure_device(); print(screen_info())")
    launch = _command()
    if launch is None:
        return _result(False, error="phone-harness is not installed. Install it with "
                                    "`uv tool install phone-harness`, or reinstall this plugin so Hermes installs it.")
    cmd, site = launch
    timeout = _timeout((args or {}).get("timeout_s"))
    workspace = _workspace()
    group: dict[str, Any] = ({"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
                             if os.name == "nt" else {"start_new_session": True})
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        with _phone_lock(LOCK_WAIT_S):
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                env=_env(site, workspace), cwd=str(workspace), **group,
            )
            try:
                out, err = proc.communicate(input=code, timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.communicate(timeout=10)
                return _result(False, workspace=str(workspace),
                               error=f"phone_exec timed out after {timeout}s. Split the work into smaller "
                                     f"scripts or raise timeout_s (max {MAX_TIMEOUT_S}).")
    except PhoneBusy:
        return _result(False, error=f"The phone is busy with another phone_exec script (waited {LOCK_WAIT_S}s). "
                                    "Try again when it finishes.")
    except OSError as e:
        return _result(False, error=f"Failed to launch phone-harness: {e}")

    out = out or ""
    shots = [p for p in dict.fromkeys(_PNG.findall(out)) if os.path.isfile(p)]
    return _result(proc.returncode == 0, exit_code=proc.returncode, output=out[-OUTPUT_CAP:],
                   stderr=(err or "").strip()[-STDERR_CAP:], screenshots=shots, workspace=str(workspace))

# ---------------------------------------------------------------------------------------------
# Registration.


def _skill_path() -> Path | None:
    """phone-harness's own SKILL.md: shipped inside the package, else printed by `phone-harness skill`."""
    spec = importlib.util.find_spec("phone_harness")
    if spec is not None and spec.origin:
        packaged = Path(spec.origin).resolve().parent / "SKILL.md"
        if packaged.is_file():
            return packaged
    launch = _command()
    if launch is None:
        return None
    cached = _data_dir() / "skill" / "SKILL.md"
    try:
        text = subprocess.run([*launch[0], "skill"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", timeout=8, check=True, env=_env(launch[1], _workspace())).stdout
    except (OSError, subprocess.SubprocessError):
        return cached if cached.is_file() else None
    if not text.startswith("---"):
        return cached if cached.is_file() else None
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(text, encoding="utf-8")
    return cached


def register(ctx) -> None:
    global _CTX
    _CTX = ctx
    ctx.register_tool(name="phone_exec", toolset=PLUGIN, schema=SCHEMA, handler=phone_exec,
                      check_fn=_available, emoji="📱")
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    with contextlib.suppress(Exception):
        skill = _skill_path()
        if skill is not None:
            ctx.register_skill(PLUGIN, skill)
