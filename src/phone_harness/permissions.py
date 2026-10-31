"""macOS permission status and the guided grant for iPhone Mirroring.

Accessibility (taps and typing) and Screen Recording (seeing the window)
are recorded against the app that launched this process. A command running
under tmux, SSH, or launchd has no such app, so the Python binary is the
thing to grant. A running process can keep the answer it got first (Screen
Recording does), so every read runs in a new process (permission_probe).

When a check succeeds, the code hashes of that app and of Python are
saved. A later failure against a different hash means macOS still has the
old entry and it no longer matches.

`phone-harness doctor ios` reads the state. `--fix` acts on it: the panel in
permission_panel when someone is at the Mac in a terminal, text otherwise.
"""
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

from . import config, permission_panel
from .permission_probe import parse as parse_probe, preflight as local_preflight

# Same labels `doctor ios` already prints, so a check stays one line.
ACCESSIBILITY_LABEL = "Accessibility permission (taps & keystrokes)"
SCREEN_RECORDING_LABEL = "Screen Recording permission (seeing the phone)"

CHECK_COMMAND = "phone-harness doctor ios"
FIX_COMMAND = "phone-harness doctor ios --fix"

GUIDE_TIMEOUT = 600
_KINDS = ("accessibility", "screen_recording")
_PANE = {"accessibility": "Privacy_Accessibility", "screen_recording": "Privacy_ScreenCapture"}
_MAX_ANCESTORS = 64
_GUIDE_INTERVAL = 1.0
_PROBE_TIMEOUT = 8


def bundle_root(executable):
    """The outer `.app` directory that contains `executable`, if any.

    A nested helper is its own `.app` inside the one System Settings lists,
    so the first `.app` from the root is the answer. An `.app` inside a
    `.framework` (Python.app in a framework build of Python) is part of the
    interpreter, not an app anyone launched, so it does not count.
    """
    if not executable:
        return None
    built = None
    for part in Path(executable).parts:
        built = Path(part) if built is None else built / part
        if part.endswith(".framework"):
            return None
        if part.endswith(".app"):
            return str(built)
    return None


def host_target(pid=None, parent_of=None, executable_of=None, self_exe=None):
    """App bundle that owns this process, or the Python binary when there is none.

    Start at this process and walk parents. The first executable that lives
    inside an app bundle decides the result, and the path reported is that
    bundle's outer `.app`. No bundle on the chain means macOS will ask about
    the binary itself.
    """
    parent_of = parent_of or default_parent
    executable_of = executable_of or default_executable
    start = os.getpid() if pid is None else pid
    seen = start
    for _ in range(_MAX_ANCESTORS):
        if seen is None or seen <= 1:
            break
        exe = executable_of(seen)
        app = bundle_root(exe)
        if app:
            return {
                "kind": "app",
                "name": Path(app).name.removesuffix(".app"),
                "path": app,
                "executable": exe,
            }
        nxt = parent_of(seen)
        if nxt is None or nxt == seen:
            break
        seen = nxt
    exe = self_exe or _python_binary()
    return {"kind": "binary", "name": Path(exe).name, "path": exe, "executable": exe}


def default_executable(pid):
    path = _proc_pidpath(pid)
    if path:
        return path
    _ppid, comm = _ps_parent_and_comm(pid)
    return comm or None


def default_parent(pid):
    """Parent pid from `ps`. It can see a process owned by another user,
    which matters when a terminal's login helper sits between us and the app."""
    ps_ppid, _comm = _ps_parent_and_comm(pid)
    return ps_ppid


def _python_binary():
    """This interpreter's real path: the file macOS names when it lists Python."""
    return _proc_pidpath(os.getpid()) or os.path.realpath(sys.executable)


def _proc_pidpath(pid):
    """Full executable path. `ps` shortens the command and hides the bundle."""
    if sys.platform != "darwin" or not pid or pid <= 0:
        return None
    try:
        import ctypes
        import ctypes.util
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        buf = ctypes.create_string_buffer(4 * 1024)
        n = libc.proc_pidpath(int(pid), buf, ctypes.sizeof(buf))
    except (ImportError, OSError, AttributeError, ValueError):
        return None
    if n <= 0:
        return None
    try:
        return buf.value.decode()
    except UnicodeError:
        return None


def _ps_parent_and_comm(pid):
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        ppid, comm = out.split(None, 1)
        return int(ppid), comm.strip()
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, None


def macos_version():
    if sys.platform != "darwin":
        return (0, 0, 0)
    parts = []
    for piece in platform.mac_ver()[0].split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


# System Settings (macOS 13+) and the preference pane it replaced.
_PANE_ANCHOR = (
    ((13, 0), "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?"),
    ((0, 0), "x-apple.systempreferences:com.apple.preference.security?"),
)


def settings_url(kind, version):
    """URL that opens one Privacy & Security list in System Settings."""
    version = tuple(version)
    anchor = _PANE_ANCHOR[-1][1]
    for minimum, candidate in _PANE_ANCHOR:
        if version >= minimum:
            anchor = candidate
            break
    return anchor + _PANE[kind]


def list_name(kind, version):
    """Label System Settings shows for this list on that macOS version.
    The anchor in the URL stays the same when the label changes."""
    version = tuple(version)
    if kind == "accessibility":
        return "Device Control and Data Access" if version >= (27, 0) else "Accessibility"
    return "Screen & System Audio Recording" if version >= (15, 0) else "Screen Recording"


def parse_codesign(text):
    """CDHash and ad-hoc flag from `codesign -dvvv` (it writes to stderr)."""
    match = re.search(r"(?m)^CDHash=([0-9a-fA-F]+)\s*$", text or "")
    adhoc = bool(re.search(r"(?m)^Signature=adhoc\b", text or "")) or "flags=0x2(adhoc)" in (text or "")
    return {"cdhash": match.group(1).lower() if match else "", "adhoc": adhoc}


def code_identity(path, runner=None):
    if not path:
        return {"cdhash": "", "adhoc": False}
    runner = runner or subprocess.run
    try:
        result = runner(
            ["codesign", "-dvvv", path],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return {"cdhash": "", "adhoc": False}
    text = (getattr(result, "stderr", "") or "") + (getattr(result, "stdout", "") or "")
    return parse_codesign(text)


def _inside_host(binary, host):
    if host.get("kind") != "app" or not binary or not host.get("path"):
        return False
    try:
        return Path(binary).resolve().is_relative_to(Path(host["path"]).resolve())
    except (OSError, ValueError):
        return False


def _subject(host):
    if host.get("kind") == "app":
        return f'"{host.get("name") or "the app"}" ({host.get("path")})'
    path = host.get("path") or "the Python binary"
    return f"the Python binary ({path})"


def _classify(kind, granted, host, binary, prev):
    """granted/denied/stale/unknown for one permission.

    Stale only when this check failed and a grant we already saw no longer
    matches the app, or the Python binary when that binary is listed on
    its own. A different launching app is a new grant, not a stale one.
    """
    if granted is None:
        return "unknown", ""
    if granted:
        return "granted", ""
    if not prev:
        return "denied", ""
    same_host = prev.get("host_path") and prev.get("host_path") == host.get("path")
    # A different launching app needs its own grant. A binary move is stale
    # only for the same app, or when the binary itself is the subject.
    same_subject = bool(same_host or host.get("kind") == "binary")
    host_resigned = bool(
        same_host and prev.get("host_cdhash") and host.get("cdhash")
        and prev.get("host_cdhash") != host.get("cdhash")
    )
    binary_is_subject = host.get("kind") == "binary" or not binary.get("inside_host")
    binary_resigned = bool(
        same_subject and binary_is_subject and prev.get("binary_cdhash") and binary.get("cdhash")
        and prev.get("binary_path") == binary.get("path")
        and prev.get("binary_cdhash") != binary.get("cdhash")
    )
    binary_moved = bool(
        same_subject and binary_is_subject and prev.get("binary_path") and binary.get("path")
        and prev.get("binary_path") != binary.get("path")
    )
    if host_resigned:
        return "stale", "host_cdhash"
    if binary_moved:
        return "stale", "binary_moved"
    if binary_resigned:
        return "stale", "binary_cdhash"
    return "denied", ""


def _summary(kind, state, host, binary, version, reason, prev):
    name = list_name(kind, version)
    subject = _subject(host)
    if state == "granted":
        return ""
    if state == "unknown":
        return ("could not check (install pyobjc-framework-Quartz and "
                "pyobjc-framework-ApplicationServices)")
    if state == "stale" and reason == "binary_moved":
        return (f"stale grant: the Python binary moved from {prev.get('binary_path')} "
                f"to {binary.get('path')}. Remove the old entry and grant the new binary")
    if state == "stale" and reason == "binary_cdhash":
        return (f"stale grant: the Python binary was re-signed ({binary.get('path')}). "
                f"In {name}, remove the old entry and grant it again")
    if state == "stale":
        return (f"stale grant for {subject}: it was updated or re-signed; "
                f"in {name}, remove the old entry and add it again")
    where = f"System Settings > Privacy & Security > {name}"
    return (f"{where}: turn on {subject}. If it is not in the list, drag it in. "
            f"If the switch is already on, the grant is stale: remove the entry and add it again")


def _suggestion(kind, state, host, version, summary):
    if state == "granted":
        return ""
    url = settings_url(kind, version) if state != "unknown" else ""
    text = summary
    if url:
        text = f"{summary}. Open {url}"
    return text


def build_report(accessibility, screen_recording, host, binary, version, grants, platform_name="darwin"):
    """One status report. `accessibility` and `screen_recording` are True, False, or None."""
    grants = grants or {}
    host = dict(host or {})
    binary = dict(binary or {})
    binary["inside_host"] = _inside_host(binary.get("path"), host)
    raw = {"accessibility": accessibility, "screen_recording": screen_recording}
    entries = {}
    states = {}
    for kind in _KINDS:
        prev = grants.get(kind) or {}
        state, reason = _classify(kind, raw[kind], host, binary, prev)
        states[kind] = state
        summary = _summary(kind, state, host, binary, version, reason, prev)
        entries[kind] = {
            "state": state,
            "list": list_name(kind, version),
            "url": settings_url(kind, version) if state != "unknown" else "",
            "summary": summary,
            "suggestion": _suggestion(kind, state, host, version, summary),
            "reason": reason,
        }
    ok = all(states[kind] == "granted" for kind in _KINDS)
    report = {
        "ok": ok,
        "needed": True,
        "platform": platform_name,
        "host": {
            "kind": host.get("kind") or "binary",
            "name": host.get("name") or "",
            "path": host.get("path") or "",
            "executable": host.get("executable") or "",
            "cdhash": host.get("cdhash") or "",
        },
        "binary": {
            "path": binary.get("path") or "",
            "cdhash": binary.get("cdhash") or "",
            "adhoc": bool(binary.get("adhoc")),
            "inside_host": bool(binary.get("inside_host")),
        },
        "accessibility": entries["accessibility"],
        "screen_recording": entries["screen_recording"],
        "note": "",
    }
    extra = []
    for kind in _KINDS:
        prev = grants.get(kind) or {}
        if states[kind] == "granted":
            continue
        if prev.get("host_path") and prev.get("host_path") != host.get("path") and states[kind] != "stale":
            extra.append(
                f"The last {entries[kind]['list']} grant that worked was for {prev['host_path']}. "
                f"This process is {host.get('path')}, which needs its own grant."
            )
        if (states[kind] != "stale" and not binary.get("inside_host")
                and prev.get("binary_path") and binary.get("path")
                and prev.get("binary_path") != binary.get("path")):
            extra.append(
                f"The Python binary changed from {prev['binary_path']} to {binary['path']}. "
                "If System Settings lists it on its own, remove the old path and grant the new one."
            )
    if binary.get("adhoc") and not ok:
        extra.append(
            "This Python binary is ad-hoc signed, so a reinstall or re-sign gives it a new "
            "identity and leaves the previous grant stale."
        )
    # One copy of each sentence. Both permissions can notice the same move.
    seen = []
    for line in extra:
        if line not in seen:
            seen.append(line)
    report["note"] = " ".join(seen)
    return report


def not_needed_report():
    entry = {
        "state": "not_required",
        "list": "",
        "url": "",
        "summary": "",
        "suggestion": "iPhone Mirroring permissions are only needed on macOS.",
        "reason": "",
    }
    return {
        "ok": True,
        "needed": False,
        "platform": sys.platform,
        "host": {"kind": "none", "name": "", "path": "", "executable": "", "cdhash": ""},
        "binary": {"path": "", "cdhash": "", "adhoc": False, "inside_host": False},
        "accessibility": dict(entry),
        "screen_recording": dict(entry),
        "note": "",
    }


def updated_grants(grants, report):
    """Record signatures only for permissions that just read as granted.

    A failed check keeps the previous record so the next failure can tell a
    stale signature from a switch that was never on.
    """
    grants = {kind: dict(value) for kind, value in (grants or {}).items() if isinstance(value, dict)}
    if not report.get("needed"):
        return grants
    host = report["host"]
    binary = report["binary"]
    for kind in _KINDS:
        if report[kind]["state"] != "granted":
            continue
        previous = grants.get(kind) or {}
        entry = {
            "host_path": host.get("path") or "",
            "host_kind": host.get("kind") or "",
            "host_cdhash": host.get("cdhash") or previous.get("host_cdhash") or "",
            "binary_path": binary.get("path") or "",
            "binary_cdhash": binary.get("cdhash") or previous.get("binary_cdhash") or "",
        }
        if not entry["host_cdhash"] and previous.get("host_path") == entry["host_path"]:
            entry["host_cdhash"] = previous.get("host_cdhash") or ""
        if not entry["binary_cdhash"] and previous.get("binary_path") == entry["binary_path"]:
            entry["binary_cdhash"] = previous.get("binary_cdhash") or ""
        grants[kind] = entry
    return grants


def fresh_preflight(runner=None):
    """{kind: True/False/None}, read by a new interpreter. Never prompts.

    None means it could not be read, so the report says unknown instead of
    claiming the grant was refused. When the new process cannot run, this
    process reads it once itself.
    """
    runner = runner or subprocess.run
    try:
        proc = runner(
            [sys.executable, "-m", "phone_harness.permission_probe"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=_PROBE_TIMEOUT,
        )
        states = parse_probe(proc.stdout)
    except (OSError, subprocess.SubprocessError):
        states = None
    return states if states is not None else local_preflight()


def _load_grants():
    data = config._read(config.paths()["permissions"], {})
    grants = data.get("grants")
    if not isinstance(grants, dict):
        return {}
    return {key: value for key, value in grants.items() if isinstance(value, dict)}


def _save_grants(grants):
    config._write(config.paths()["permissions"], {"grants": grants})


def collect(preflight=None, host=None, identify=None, version=None, grants=None, save=True):
    """Build the report. Pass `preflight` to skip reading the grants."""
    if preflight is None and sys.platform != "darwin":
        return not_needed_report()
    if preflight is None:
        preflight = fresh_preflight()
    host = dict(host or host_target())
    identify = identify or code_identity
    if not host.get("cdhash"):
        bundle = ""
        if host.get("kind") == "app":
            bundle = (identify(host.get("path")) or {}).get("cdhash", "")
        host["cdhash"] = bundle or (identify(host.get("executable")) or {}).get("cdhash", "")
    binary_path = _python_binary()
    binary_id = identify(binary_path) or {"cdhash": "", "adhoc": False}
    binary = {
        "path": binary_path,
        "cdhash": binary_id.get("cdhash") or "",
        "adhoc": bool(binary_id.get("adhoc")),
    }
    version = macos_version() if version is None else tuple(version)
    if version == (0, 0, 0):
        # Simulating a Mac check off macOS (tests). Mirroring needs 15.
        version = (15, 0, 0)
    loaded = _load_grants() if grants is None else grants
    report = build_report(
        preflight.get("accessibility"), preflight.get("screen_recording"),
        host, binary, version, loaded,
        platform_name="darwin",
    )
    if save:
        fresh = updated_grants(loaded, report)
        if fresh != loaded:
            _save_grants(fresh)
    return report


def _missing(report):
    return [kind for kind in _KINDS if report[kind]["state"] != "granted"]


def python_row_note(report, full=False):
    """The extra row some setups show: the Python binary, apart from the app.
    Empty when the app is the only thing to grant."""
    host = report.get("host") or {}
    binary = report.get("binary") or {}
    if host.get("kind") != "app" or not binary.get("path") or binary.get("inside_host"):
        return ""
    if full:
        return f"If the list also shows the Python binary ({binary['path']}) on its own row, turn that on too."
    return f"If the list also shows {Path(binary['path']).name} on its own row, turn that on too."


def explain(report, hint=True):
    """Shared diagnosis under the two checks. Empty when nothing is wrong."""
    if not report or report.get("ok") or not report.get("needed"):
        return ""
    lines = []
    host = report.get("host") or {}
    if host.get("kind") == "app":
        lines.append(f'Grant "{host.get("name")}" ({host.get("path")}).')
    else:
        lines.append(
            "No launching app was found (tmux, SSH, or a service). "
            f"Grant the Python binary ({host.get('path')}), or grant the terminal app that started this session."
        )
    row = python_row_note(report, full=True)
    if row:
        lines.append(row)
    if report.get("note"):
        lines.append(report["note"])
    links = [report[kind] for kind in _missing(report) if report[kind].get("url")]
    for entry in links:
        lines.append(f"{entry['list']}: {entry['url']}")
    if hint and links:
        lines.append(f"Guided fix: {FIX_COMMAND}")
    return "\n".join(lines)


def doctor_lines(report):
    """(label, ok, hint) for the two checks, in doctor order."""
    lines = []
    for label, key in ((ACCESSIBILITY_LABEL, "accessibility"),
                       (SCREEN_RECORDING_LABEL, "screen_recording")):
        entry = report[key]
        ok = entry["state"] == "granted"
        lines.append((label, ok, "" if ok else entry.get("summary") or entry.get("suggestion") or ""))
    return lines


def report_payload(report):
    """The report as `doctor --json` prints it."""
    return {
        "ok": report["ok"],
        "needed": report["needed"],
        "platform": report["platform"],
        "host": report["host"],
        "binary": report["binary"],
        "accessibility": report["accessibility"],
        "screen_recording": report["screen_recording"],
        "note": report.get("note") or "",
    }


# --- doctor --fix -------------------------------------------------------------

def gui_session():
    """True when a window from this process would be seen at the Mac: the
    process is in a window server session and that session is on the
    console. SSH and launchd sessions have no window server session."""
    if sys.platform != "darwin":
        return False
    try:
        import Quartz
        info = Quartz.CGSessionCopyCurrentDictionary()
    except Exception:
        return False
    if not info:
        return False
    # The key's string differs from the constant's name.
    key = getattr(Quartz, "kCGSessionOnConsoleKey", "kCGSSessionOnConsoleKey")
    try:
        on_console = info.get(key)
    except Exception:
        on_console = None
    return on_console is None or bool(on_console)


def over_ssh(environ=None):
    environ = os.environ if environ is None else environ
    return any(environ.get(name) for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"))


def fix_mode(report, tty, noninteractive=False, gui=False, ssh=False):
    """How `doctor --fix` acts on this report.

    none           nothing is missing
    skip           --yes or --non-interactive: nothing opens, nothing waits
    manual         text only: the state could not be read, or a pipe with
                   nobody at the Mac
    open           a pipe at the Mac: open the first missing pane, return
    panel          a terminal at the Mac and an app to drag: the panel
    open_and_wait  a terminal at the Mac, no app (the Python binary): open
                   each pane and wait here
    wait           a terminal over SSH or without a display: wait here and
                   open nothing
    """
    if not report.get("needed") or report.get("ok"):
        return "none"
    if noninteractive:
        return "skip"
    if any(report[kind]["state"] == "unknown" for kind in _missing(report)):
        return "manual"
    desktop = bool(gui) and not ssh
    if not tty:
        return "open" if desktop else "manual"
    if not desktop:
        return "wait"
    if (report.get("host") or {}).get("kind") == "app" and report["host"].get("path"):
        return "panel"
    return "open_and_wait"


def guide_lock_path():
    return config.run_dir() / "permission-guide.lock"


def acquire_guide_lock():
    """One guide at a time, panel or text. The lock goes away with its process."""
    return permission_panel.take_lock(str(guide_lock_path()))


def open_settings(url):
    if sys.platform != "darwin" or not url:
        return False
    try:
        subprocess.run(["open", url], check=False, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def panel_spec(report, timeout=GUIDE_TIMEOUT):
    """What permission_panel reads on stdin."""
    host = report.get("host") or {}
    steps = [{"kind": kind, "list": report[kind]["list"], "url": report[kind]["url"],
              "state": report[kind]["state"]} for kind in _missing(report)]
    return {
        "host": {"name": host.get("name") or "", "path": host.get("path") or ""},
        "steps": steps,
        "note": python_row_note(report),
        "timeout": timeout,
        "lock": str(guide_lock_path()),
    }


def _panel_result(out):
    for line in reversed((out or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("outcome") in permission_panel.OUTCOMES:
            return data["outcome"], str(data.get("detail") or "")
    return None


def run_panel(report, timeout=GUIDE_TIMEOUT, popen=None):
    """Show the panel in its own process and wait for it. (outcome, detail)."""
    popen = popen or subprocess.Popen
    try:
        proc = popen(
            [sys.executable, "-m", "phone_harness.permission_panel"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return "error", f"could not start the panel ({exc})"
    spec = json.dumps(panel_spec(report, timeout)) + "\n"
    try:
        # The panel ends itself at its own timeout. This is the backstop.
        out, err = proc.communicate(spec, timeout=timeout + 60)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        return _panel_result(out) or ("timeout", "")
    except KeyboardInterrupt:
        # Ctrl-C reached the panel too. Make sure it is gone.
        proc.kill()
        proc.communicate()
        return "dismissed", "interrupted"
    result = _panel_result(out)
    if result is not None:
        return result
    last = [line for line in (err or "").splitlines() if line.strip()][-1:]
    return "error", last[0].strip() if last else f"the panel exited with status {proc.returncode}"


def _duration(seconds):
    seconds = int(seconds)
    if seconds >= 120 and seconds % 60 == 0:
        return f"{seconds // 60} minutes"
    return f"{seconds} seconds"


def wait_for_grant(report, open_pane, timeout=GUIDE_TIMEOUT, interval=_GUIDE_INTERVAL,
                   opener=None, probe=None, sleep=None, clock=None, say=None, lock=None):
    """Text guide: poll a new process every `interval` seconds until each
    missing grant lands, opening its pane first when `open_pane`. Returns
    granted, timeout, dismissed (Ctrl-C), or already_running."""
    opener = opener or open_settings
    probe = probe or fresh_preflight
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    say = say or (lambda _line: None)
    held = (lock or acquire_guide_lock)()
    if held is None:
        return "already_running"
    try:
        deadline = clock() + max(0.0, float(timeout))
        granted = set()
        for kind in _missing(report):
            if kind in granted:
                continue
            entry = report[kind]
            if open_pane:
                opener(entry["url"])
                say(f"Opened System Settings > Privacy & Security > {entry['list']}.")
            else:
                say("This session has no display at the Mac, so nothing was opened. At the Mac:")
            say(entry["summary"].rstrip(".") + ".")
            row = python_row_note(report)
            if row:
                say(row)
            say(f"Waiting up to {_duration(timeout)} for the grant. Ctrl-C stops.")
            while True:
                states = probe() or {}
                granted.update(name for name in _KINDS if states.get(name) is True)
                if kind in granted:
                    say(f"{entry['list']}: on.")
                    break
                remaining = deadline - clock()
                if remaining <= 0:
                    return "timeout"
                before = clock()
                sleep(min(interval, remaining))
                if clock() <= before:
                    return "timeout"
        return "granted"
    except KeyboardInterrupt:
        return "dismissed"
    finally:
        held.close()


def fix(report, tty, noninteractive=False, gui=None, ssh=None, timeout=GUIDE_TIMEOUT,
        panel=None, wait=None, opener=None, recollect=None, say=None):
    """Act on the missing grants the way this run allows (see fix_mode).

    Returns (result, report): result is {"mode", "outcome", "detail",
    "steps"}, and the report is read again when a grant may have landed.
    """
    say = say or (lambda _line: None)
    gui = gui_session() if gui is None else gui
    ssh = over_ssh() if ssh is None else ssh
    mode = fix_mode(report, tty, noninteractive, gui, ssh)
    steps = _missing(report) if report.get("needed") else []

    def result(outcome, detail=""):
        return {"mode": mode, "outcome": outcome, "detail": detail, "steps": steps}

    if mode == "none":
        return result("none"), report
    if mode == "skip":
        return result("skipped"), report
    if mode == "manual":
        unknown = any(report[kind]["state"] == "unknown" for kind in steps)
        return result("manual", "unknown" if unknown else "no_desktop"), report
    if mode == "open":
        if (opener or open_settings)(report[steps[0]]["url"]):
            return result("opened"), report
        return result("manual", "open_failed"), report

    outcome, detail = "error", ""
    if mode == "panel":
        name = report["host"].get("name") or "the app"
        say(f'Opening a panel beside System Settings. Drag "{name}" into the list, '
            "then confirm with Touch ID or your password. Not Now closes it.")
        try:
            outcome, detail = (panel or run_panel)(report, timeout)
        except Exception as exc:
            outcome, detail = "error", f"{type(exc).__name__}: {exc}"
        if outcome == "error":
            say(f"The panel could not run ({detail}). Guiding here instead.")
            mode = "open_and_wait"
    if mode in ("open_and_wait", "wait"):
        try:
            outcome = (wait or wait_for_grant)(
                report, open_pane=mode == "open_and_wait", timeout=timeout,
                opener=opener, say=say,
            )
        except Exception as exc:
            outcome, detail = "error", f"{type(exc).__name__}: {exc}"
    try:
        report = (recollect or (lambda: collect(host=report["host"])))()
    except Exception:
        pass
    if report.get("ok"):
        outcome = "granted"
    return result(outcome, detail), report


def outcome_line(result, report):
    """One line after the checks: what `--fix` did, and what is next."""
    if not result:
        return ""
    outcome = result.get("outcome")
    detail = result.get("detail") or ""
    host = report.get("host") or {}
    steps = result.get("steps") or []
    first = report.get(steps[0]) if steps and isinstance(report.get(steps[0]), dict) else {}
    where = first.get("list") or "the list"
    if outcome == "granted":
        line = "Granted."
        if host.get("kind") == "app" and "screen_recording" in steps:
            line += (f' If macOS asks to quit "{host.get("name")}", choose Later: '
                     "a new phone-harness process already sees the grant.")
        return line
    if outcome == "dismissed":
        return f"Not now. Run {FIX_COMMAND} when you are ready."
    if outcome == "timeout":
        return f"Timed out waiting for the grant. Run {FIX_COMMAND} to try again."
    if outcome == "opened":
        return (f"Opened System Settings > Privacy & Security > {where}. Turn on "
                f"{_subject(host)}, then run {CHECK_COMMAND} again. This run did not "
                "wait: its output is not a terminal.")
    if outcome == "skipped":
        return "Non-interactive: System Settings was not opened and this run did not wait."
    if outcome == "already_running":
        return (f"A permission guide is already open in another terminal. Finish or "
                f"close it, then run {CHECK_COMMAND} again.")
    if outcome == "manual" and detail == "unknown":
        return "Could not read the permission state, so nothing was opened."
    if outcome == "manual" and detail == "open_failed":
        return (f"Could not open System Settings. Open Privacy & Security > {where} "
                f"yourself, then run {CHECK_COMMAND} again.")
    if outcome == "manual":
        return (f"No desktop session here (SSH or no display), so nothing was opened. "
                f"Grant it at the Mac, then run {CHECK_COMMAND} again.")
    if outcome == "error":
        return f"The guide stopped ({detail}). Run {FIX_COMMAND} to try again."
    return ""
