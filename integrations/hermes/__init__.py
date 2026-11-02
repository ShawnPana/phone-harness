"""phone-harness for Hermes Agent: one ``phone_exec`` tool.

The model writes a Python script. The plugin pipes it on stdin to the phone-harness Hermes
installed from this plugin's python_dependencies, which runs it with the phone helpers
pre-imported against the user's phone and hands stdout back as the tool result. A screenshot
saved during the call is attached to that result when the model can view images. Each call is
a fresh interpreter; the phone and the agent workspace persist.

Safety, in order of what it protects:
- Every ``phone_exec`` call asks the user first. The ``pre_tool_call`` hook returns an approve
  directive with ``rule_key`` ``phone_exec``, so one session or always answer covers later calls.
- Classifier-only auto-run is an explicit opt-in (plugin setting ``classifier_auto_run`` or
  ``PHONE_HARNESS_CLASSIFIER_AUTO_RUN``). That check is a heuristic and can be bypassed.
- The child environment comes from Hermes's scrubbed subprocess env when that is importable,
  otherwise the process env minus ``*_API_KEY`` / ``*_TOKEN`` / ``*_SECRET``.
- One script drives the phone at a time, across subagents in this process.
"""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("phone_harness.hermes")

PLUGIN = "phone-harness"
DEFAULT_TIMEOUT_S, MIN_TIMEOUT_S, MAX_TIMEOUT_S = 300, 5, 1800
LOCK_WAIT_S = 120
OUTPUT_CAP, STDERR_CAP = 50_000, 4_000

_THREAD_LOCK = threading.Lock()   # subagents in this process take turns on the phone

DESCRIPTION = (
    "Drive the user's real phone with phone-harness: their iPhone through the Mac's iPhone Mirroring "
    "window, an Android over adb, or a Phone Harness Cloud phone. `code` runs as Python with the phone "
    "helpers pre-imported; print() whatever you need back. Each call is a fresh interpreter (variables "
    "do not persist); the phone keeps its state. Start with ensure_device(); it covers iPhone "
    "Mirroring, Android, and a cloud phone. Read what is on screen with screen(). screenshot() prints "
    "the image path it saved, and that image is attached to this result when you can view images. "
    "Ask the user before anything outward-facing or hard to undo (sending, posting, buying, deleting, "
    "changing settings). "
    "Never type passwords, PINs or 2FA codes. Read the skill phone-harness:phone-harness for the helpers."
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
# Approval gate.

# Opt-in for skipping the prompt when a classifier thinks the script only drives the phone.
# The check is a heuristic and can be bypassed. Off unless the user turns it on.
CLASSIFIER_AUTO_RUN = "classifier_auto_run"
CLASSIFIER_AUTO_RUN_ENV = "PHONE_HARNESS_CLASSIFIER_AUTO_RUN"

# The only import the classifier treats as phone-only. Anything else asks.
SAFE_MODULES = frozenset({"phone_harness.helpers"})
# Builtins that read or write files, run arbitrary code, or wait on a terminal; shell() is
# phone-harness's adb shell; push_file() sends a file from this computer to a cloud phone.
RISKY_CALLS = frozenset({"open", "exec", "eval", "compile", "__import__", "breakpoint", "input", "shell",
                         "push_file"})
# Names helpers.py imports for itself. A script that pulls them in can reach this computer
# (importlib.import_module, Path.home().read_text, the transport module).
_HOST_NAMES = frozenset({
    "hashlib", "importlib", "time", "Path", "transport",
    "CORE_DIR", "REPO_ROOT", "AGENT_WORKSPACE",
})
_DUNDER = re.compile(r"^__\w+__$")
_FM_END = re.compile(r"\n---\s*(?:\n|$)")
_SUMMARY_LIMIT = 140

_plugin_ctx = None


def _helpers_import(module: str) -> bool:
    return module == "phone_harness.helpers"


def _assign_bindings(targets, value) -> list[tuple[list[str], ast.AST]]:
    """Simple name bindings in one assignment, including ``a, b = open, eval``."""
    found = []
    for target in targets:
        if isinstance(target, ast.Name):
            found.append(([target.id], value))
            continue
        if not isinstance(target, (ast.Tuple, ast.List)):
            continue
        if not all(isinstance(elt, ast.Name) for elt in target.elts):
            continue
        names = [elt.id for elt in target.elts]
        if isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(names):
            for name, elt in zip(names, value.elts):
                found.append(([name], elt))
    return found


def _name_kinds(tree: ast.AST) -> tuple[set[str], set[str], set[str]]:
    """Risky-call names, send names, and names bound to the string ``raw``.

    Covers ``f = open``, ``s = send``, ``op = "raw"``, chains of those, and
    ``from phone_harness.helpers import shell as sh``.
    """
    risky = set(RISKY_CALLS)
    send_names = {"send"}
    raw_names: set[str] = set()
    pending: list[tuple[list[str], ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and not node.level and node.module:
            if node.module == "phone_harness":
                imported = [alias for alias in node.names if alias.name == "helpers"]
            elif _helpers_import(node.module):
                imported = [alias for alias in node.names if alias.name != "*"]
            else:
                imported = []
            for alias in imported:
                if alias.name in RISKY_CALLS or alias.name == "send":
                    bound = alias.asname or alias.name
                    pending.append(([bound], ast.Name(id=alias.name, ctx=ast.Load())))
        elif isinstance(node, ast.Assign):
            pending.extend(_assign_bindings(node.targets, node.value))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            pending.append(([node.target.id], node.value))
        elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            pending.append(([node.target.id], node.value))

    def kind_of(value: ast.AST) -> str | None:
        # A name that is already a risky call stays risky even if it is also
        # called send. Otherwise ``send = open`` would hide later aliases.
        if isinstance(value, ast.Name):
            if value.id in risky:
                return "risky"
            if value.id in send_names:
                return "send"
            if value.id in raw_names:
                return "raw"
            return None
        if isinstance(value, ast.Constant) and value.value == "raw":
            return "raw"
        if isinstance(value, ast.Attribute):
            if value.attr in RISKY_CALLS:
                return "risky"
            if value.attr == "send":
                return "send"
        return None

    changed = True
    while changed:
        changed = False
        for names, value in pending:
            kind = kind_of(value)
            if kind == "raw":
                bucket = raw_names
            elif kind == "send":
                bucket = send_names
            elif kind == "risky":
                bucket = risky
            else:
                continue
            for name in names:
                if name not in bucket:
                    bucket.add(name)
                    changed = True
    return risky, send_names, raw_names


def _risky_callee(func: ast.AST, risky: set[str]) -> str | None:
    if isinstance(func, ast.Name) and func.id in risky:
        return func.id
    if isinstance(func, ast.Attribute) and func.attr in RISKY_CALLS:
        return func.attr
    if isinstance(func, ast.NamedExpr):
        return _risky_callee(func.value, risky)
    return None


def _is_send_call(func: ast.AST, send_names: set[str]) -> bool:
    if isinstance(func, ast.Name):
        return func.id in send_names
    if isinstance(func, ast.Attribute):
        return func.attr == "send"
    if isinstance(func, ast.NamedExpr):
        return _is_send_call(func.value, send_names)
    return False


def _passes_raw(node: ast.Call, raw_names: set[str]) -> bool:
    """True when the op argument is the string ``raw`` or a name bound to it."""
    candidates = [node.args[0]] if node.args else []
    candidates.extend(kw.value for kw in node.keywords if kw.arg == "op")
    for arg in candidates:
        if isinstance(arg, ast.Constant) and arg.value == "raw":
            return True
        if isinstance(arg, ast.Name) and arg.id in raw_names:
            return True
    return False


def approval_reason(code: str) -> str | None:
    """Why this script reaches past the phone, or None when it only drives the phone.

    Only ``phone_harness.helpers`` imports pass. Host objects that module carries
    (``Path``, ``importlib``, the transport), file and eval builtins and aliases of
    those calls, ``send("raw", ...)``, and dunder introspection do not. A heuristic,
    not a sandbox: it can be bypassed.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None  # it cannot run; the SyntaxError comes back as the result
    risky, send_names, raw_names = _name_kinds(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _helpers_import(alias.name):
                    return f"imports {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                return f"imports from {'.' * (node.level or 1)}{node.module or ''}"
            if node.module == "phone_harness":
                for alias in node.names:
                    if alias.name != "helpers":
                        return f"imports {alias.name} from phone_harness"
                continue
            if not _helpers_import(node.module):
                return f"imports from {node.module}"
            for alias in node.names:
                if alias.name == "*" or alias.name in _HOST_NAMES:
                    return f"imports {alias.name} from {node.module}"
        elif isinstance(node, ast.Call):
            callee = _risky_callee(node.func, risky)
            if callee is not None:
                if callee in RISKY_CALLS:
                    return f"calls {callee}()"
                return f"calls {callee}(), an alias of a risky call"
            if _is_send_call(node.func, send_names) and _passes_raw(node, raw_names):
                return 'calls send("raw", ...)'
        elif isinstance(node, ast.Attribute) and (_DUNDER.match(node.attr) or node.attr in _HOST_NAMES):
            return f"uses {node.attr}"
        elif isinstance(node, ast.Name) and _DUNDER.match(node.id):
            return f"uses {node.id}"
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _DUNDER.match(node.value):
            return f"references {node.value}"
    return None


def _script_summary(code: str) -> str:
    """Short text Hermes can show on the approval prompt."""
    compact = " ".join(code.split())
    if not compact:
        compact = "(empty)"
    elif len(compact) > _SUMMARY_LIMIT:
        compact = compact[:_SUMMARY_LIMIT - 3].rstrip() + "..."
    return f"phone_exec: {compact}"


def _classifier_auto_run() -> bool:
    """True only when the user opted in to classifier-only auto-run.

    ``PHONE_HARNESS_CLASSIFIER_AUTO_RUN``, when set, wins over the plugin setting
    so one process can force the gate on or off. The default is off.
    """
    raw = os.environ.get(CLASSIFIER_AUTO_RUN_ENV)
    if raw is not None:
        return _truthy(raw)
    ctx = _plugin_ctx
    if ctx is None:
        return False
    try:
        value = ctx.get_config(CLASSIFIER_AUTO_RUN, False)
    except Exception:
        return False
    return _truthy(value)


def _approve(message: str) -> dict:
    return {"action": "approve", "message": message, "rule_key": "phone_exec"}


def _on_pre_tool_call(tool_name: str = "", args: dict | None = None, task_id: str = "", **kwargs):
    if tool_name != "phone_exec":
        return None
    code = str((args or {}).get("code") or "")
    if _classifier_auto_run():
        reason = approval_reason(code)
        if reason is None:
            return None
        return _approve(
            f"phone_exec wants to run a script that {reason}, which reaches past the phone "
            "onto this computer. Classifier auto-run is a heuristic and can be bypassed."
        )
    return _approve(_script_summary(code))

# ---------------------------------------------------------------------------------------------
# The copy Hermes installed, and where its scripts keep notes.


def _data_dir() -> Path:
    """Hermes's data folder for this plugin: follows the active profile, survives plugin updates."""
    try:
        from plugins.plugin_storage import plugin_data_dir
        return Path(plugin_data_dir(PLUGIN))
    except Exception:  # outside Hermes (unit tests)
        return Path.home() / ".hermes" / "plugin-data" / PLUGIN


def _package_dir() -> Path | None:
    """Directory of the phone_harness Hermes installed, or None when it cannot be imported."""
    spec = importlib.util.find_spec("phone_harness")
    if spec is None or not spec.origin:
        return None
    return Path(spec.origin).parent


def _command() -> list[str] | None:
    """argv for the phone-harness Hermes installed, on Hermes's own interpreter."""
    if _package_dir() is None:
        return None
    return [sys.executable, "-m", "phone_harness.run"]


def _available() -> bool:
    return _command() is not None


def _workspace() -> Path:
    """Where notes live. An inherited PH_AGENT_WORKSPACE, else one folder in Hermes's plugin data."""
    inherited = os.environ.get("PH_AGENT_WORKSPACE", "").strip()
    if inherited:
        return Path(inherited).expanduser()
    return _data_dir() / "agent-workspace"


def _seed_workspace(workspace: Path) -> None:
    """Copy the packaged agent_helpers.py in when that file is missing.

    Hermes's plugin-data folder is created up front. An empty directory
    makes phone-harness skip its first-run seed, so the workspace can end
    up with no helpers. Existing files are never overwritten.
    """
    try:
        from phone_harness.workspace import seed_missing_files
    except ImportError as exc:
        log.warning("phone-harness: could not seed the agent workspace: %s", exc)
        workspace.mkdir(parents=True, exist_ok=True)
        return
    seed_missing_files(workspace)
    if not workspace.is_dir():
        workspace.mkdir(parents=True, exist_ok=True)


# Same enabling spellings as phone_harness.config's bool env override.
_TRUTHY = ("1", "true", "yes", "on")


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY
    return bool(value)


def _config_json() -> Path:
    """config.json, using the same directory rules as phone_harness.config.config_dir.

    Inlined on purpose: importing phone_harness.config loads .env files and can seed an
    agent workspace in this process. Hermes only needs the stored telemetry flag.
    """
    home = os.environ.get("PHONE_HARNESS_HOME")
    if home:
        root = Path(home) / "config"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
        root = Path(base) / "phone-harness"
    return root / "config.json"


def _telemetry_opted_in() -> bool:
    """True only for an explicit opt-in.

    Opt-in is ``phone-harness config set telemetry true`` (a value stored in config.json)
    or PHONE_HARNESS_TELEMETRY set to an enabling value. The built-in default is on for
    the CLI and is not an opt-in. An explicit false stays off.
    """
    raw = os.environ.get("PHONE_HARNESS_TELEMETRY")
    if raw is not None:
        return _truthy(raw)
    try:
        data = json.loads(_config_json().read_text())
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or "telemetry" not in data:
        return False
    return _truthy(data["telemetry"])


_SECRET_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET")


def _scrub_secrets(base: dict) -> dict:
    """Copy ``base`` without ``*_API_KEY`` / ``*_TOKEN`` / ``*_SECRET``.

    PATH, HOME, ANDROID_* and everything else stay, so adb, iPhone Mirroring
    and a cloud phone still find the tools and accounts they need.
    """
    scrubbed = {}
    for key, value in base.items():
        if key.upper().endswith(_SECRET_SUFFIXES):
            continue
        scrubbed[key] = value
    return scrubbed


def _base_child_env() -> dict:
    """Hermes's scrubbed subprocess env, or the suffix scrub when that is not importable.

    A failure inside Hermes's helper is raised: the phone tool should not silently
    fall back to an environment full of provider keys.
    """
    try:
        from tools.environments.local import hermes_subprocess_env
    except ImportError:
        return _scrub_secrets(dict(os.environ))
    env = hermes_subprocess_env()
    if not isinstance(env, dict):
        raise TypeError("hermes_subprocess_env() did not return a dict")
    return dict(env)


def _env(workspace: Path) -> dict:
    """Environment for the phone-harness child.

    Starts from Hermes's scrubbed subprocess env when that module imports, otherwise
    from the process env minus ``*_API_KEY`` / ``*_TOKEN`` / ``*_SECRET``. Then sets
    the workspace and client, and puts back any ``PHONE_HARNESS_*`` the user exported,
    and ``DO_NOT_TRACK``.

    Telemetry stays off unless the user explicitly opted in. The child is told the answer
    either way: Hermes can start it under another HOME, away from the config.json read here.
    """
    env = _base_child_env()
    for key, value in os.environ.items():
        if key.startswith("PHONE_HARNESS_") or key == "DO_NOT_TRACK":
            env[key] = value
    # A standard Hermes runs on a bare Python, attaches its packages at boot and strips them
    # from every child's PYTHONPATH. This child is that same Python, so it gets back the one
    # folder it needs to import the phone_harness Hermes installed.
    package = _package_dir()
    if package is not None:
        env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(package.parent), env.get("PYTHONPATH"))))
    env["PH_AGENT_WORKSPACE"] = str(workspace)
    env["PH_CLIENT"] = "hermes"
    env["PHONE_HARNESS_TELEMETRY"] = "1" if _telemetry_opted_in() else "0"
    return env

# ---------------------------------------------------------------------------------------------
# One script on the phone at a time (subagents in this process).


class PhoneBusy(Exception):
    pass


@contextlib.contextmanager
def _phone_lock(wait_s: float):
    """Subagents take turns. Raises PhoneBusy after wait_s."""
    if not _THREAD_LOCK.acquire(timeout=max(wait_s, 0.1)):
        raise PhoneBusy
    try:
        yield
    finally:
        _THREAD_LOCK.release()

# ---------------------------------------------------------------------------------------------
# The tool.


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill the runner and everything it spawned (it runs in its own process group)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


def _payload(success: bool, **fields) -> dict:
    return {"success": success, **{k: v for k, v in fields.items() if v not in (None, "", [])}}


def _result(success: bool, **fields) -> str:
    return json.dumps(_payload(success, **fields), ensure_ascii=False)


def _timeout(value) -> int:
    try:
        return max(MIN_TIMEOUT_S, min(int(value or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S


# Absolute PNG / JPEG / WebP paths. A relative path, or a suffix of one, does not count.
_IMAGE_PATH = re.compile(
    r"(?<![\w.])((?:[A-Za-z]:[\\/]|/)[^\s\"']+?\.(?:png|jpe?g|webp))(?![\w.])",
    re.IGNORECASE,
)
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _absolute_image(path: str) -> bool:
    if path.startswith(("/", "\\")):
        return True
    return len(path) >= 3 and path[0].isalpha() and path[1] == ":" and path[2] in "/\\"


def _find_screenshot(stdout: str, since: float) -> str | None:
    """Last absolute image path in stdout that exists and was modified during this call.

    A one-second slack covers filesystems that store timestamps in whole seconds.
    """
    for path in reversed(_IMAGE_PATH.findall(stdout or "")):
        if not _absolute_image(path):
            continue
        try:
            if os.path.isfile(path) and os.path.getmtime(path) >= since - 1:
                return path
        except OSError:
            continue
    return None


def _attach_screenshot(result: dict, path: str) -> dict | None:
    """Image tool result for a model that can view it, or None to keep the text result.

    A large image is reduced before it is attached. Any failure keeps the text result.
    """
    try:
        from tools.vision_tools import (
            _EMBED_MAX_DIMENSION,
            _resize_image_for_vision,
            _should_use_native_vision_fast_path,
        )
        from tools.vision_tools_history_budget import resolve_embed_target_bytes
        if not _should_use_native_vision_fast_path():
            return None
        mime = _IMAGE_MIME.get(Path(path).suffix.lower(), "image/png")
        data_url = _resize_image_for_vision(
            Path(path),
            mime_type=mime,
            max_base64_bytes=resolve_embed_target_bytes(),
            max_dimension=_EMBED_MAX_DIMENSION,
            force_jpeg=True,
        )
        if not isinstance(data_url, str) or not data_url.startswith("data:"):
            return None
        text = json.dumps(result, ensure_ascii=False)
        attached = text + "\n\nThe screenshot from this call is attached — inspect it with your native vision."
        return {
            "_multimodal": True,
            "text_summary": text,
            "meta": {"screenshot_path": path, "native_vision": True},
            "content": [
                {"type": "text", "text": attached},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    except Exception as exc:
        log.debug("screenshot attach failed, returning text: %s", exc)
        return None


def phone_exec(args: dict, **kwargs) -> str | dict:
    code = str((args or {}).get("code") or "")
    if not code.strip():
        return _result(False, error="No code provided. Start with: ensure_device(); print(screen_info())")
    cmd = _command()
    if cmd is None:
        return _result(False, error="phone-harness is not installed in Hermes's environment. "
                                    "Reinstall this plugin so Hermes installs it.")
    timeout = _timeout((args or {}).get("timeout_s"))
    workspace = _workspace()
    group: dict[str, Any] = ({"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
                             if os.name == "nt" else {"start_new_session": True})
    try:
        _seed_workspace(workspace)
        with _phone_lock(LOCK_WAIT_S):
            started = time.time()
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                env=_env(workspace), cwd=str(workspace), **group,
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
    shot = _find_screenshot(out, started)
    payload = _payload(proc.returncode == 0, exit_code=proc.returncode, output=out[-OUTPUT_CAP:],
                       stderr=(err or "").strip()[-STDERR_CAP:],
                       screenshots=[shot] if shot else [], workspace=str(workspace))
    if shot:
        attached = _attach_screenshot(payload, shot)
        if attached is not None:
            return attached
    return json.dumps(payload, ensure_ascii=False)

# ---------------------------------------------------------------------------------------------
# Registration.


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Flat YAML scalars at the top of a SKILL.md, or {} when there is no frontmatter.

    Hermes stores the description and frontmatter ``register_skill`` is given and does not
    re-read the file for the skill list, so leaving them off shows an empty skill.
    """
    text = text.lstrip("\ufeff")
    if not text.startswith("---"):
        return {}
    end = _FM_END.search(text[3:])
    if not end:
        return {}
    data: dict[str, str] = {}
    for line in text[3:end.start() + 3].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        data[key.strip()] = value
    return data


def _packaged_skill() -> tuple[Path, str, dict[str, str]] | None:
    """SKILL.md inside the installed phone_harness package, read once. None if it does not ship one."""
    root = _package_dir()
    if root is None:
        return None
    path = root / "SKILL.md"
    if not path.is_file():
        return None
    frontmatter = _parse_frontmatter(path.read_text(encoding="utf-8"))
    return path, frontmatter.get("description", ""), frontmatter


def _register_skill(ctx) -> None:
    skill = _packaged_skill()
    if skill is None:
        return
    path, description, frontmatter = skill
    try:
        ctx.register_skill(PLUGIN, path, description=description, frontmatter=frontmatter)
    except TypeError:  # Hermes builds that take a path and a description, and not frontmatter
        ctx.register_skill(PLUGIN, path, description=description)


def register(ctx) -> None:
    global _plugin_ctx
    _plugin_ctx = ctx
    ctx.register_tool(name="phone_exec", toolset=PLUGIN, schema=SCHEMA, handler=phone_exec,
                      check_fn=_available, emoji="📱")
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    try:
        _register_skill(ctx)
    except Exception as exc:
        log.warning("phone-harness: could not register the skill: %s", exc)
