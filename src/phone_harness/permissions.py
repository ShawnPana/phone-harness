"""macOS permission status and guided grant for iPhone Mirroring.

The flow follows agent-desktop (Apache-2.0):
https://github.com/vercel-labs/agent-desktop
Behavior adapted here, not the AppKit panel: preflight without prompting,
the grant charged to the nearest ancestor app (outermost bundle on that
path, else the Python binary), the versioned System Settings deep link,
and a fresh process for every poll. A long-lived process keeps the
Accessibility and Screen Recording answers from its first check.

phone-harness also remembers the code signature of a grant that worked.
When a later check fails and that signature changed, the grant is stale:
macOS still shows the old entry, and it no longer matches the binary.

iPhone Mirroring needs both Accessibility (taps and typing) and Screen
Recording (seeing the window). Automation is not used.
"""
import argparse
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

from . import config
from .term import Parser

# Same labels `doctor ios` already prints, so a check stays one line.
ACCESSIBILITY_LABEL = "Accessibility permission (taps & keystrokes)"
SCREEN_RECORDING_LABEL = "Screen Recording permission (seeing the phone)"

_KINDS = ("accessibility", "screen_recording")
_PANE = {"accessibility": "Privacy_Accessibility", "screen_recording": "Privacy_ScreenCapture"}
_MAX_ANCESTORS = 64
_GUIDE_TIMEOUT = 600
_GUIDE_INTERVAL = 1.0
_PROBE_TIMEOUT = 8

CLI_USAGE = """Usage:
  phone-harness permissions              status; does not prompt or open Settings
  phone-harness permissions --json       the same status as JSON
  phone-harness permissions --guide      open the missing pane and, in a terminal, wait
  phone-harness permissions --yes        never open Settings and never wait
  phone-harness permissions --request    show the system prompts, then report again

Accessibility and Screen Recording are granted to the app that launches
this command (Terminal, iTerm, Cursor, Hermes), not to the shell. If
System Settings lists the Python binary on its own, grant that too.
--yes and --non-interactive never open Settings and never wait. A pipe
opens the pane once and returns; it does not wait.
"""


def outermost_app(executable):
    """The outermost `.app` on one executable path.

    A helper such as `Code Helper (Plugin).app` sits inside the app TCC
    names. The nearest `.app` directory would be the helper; the last one
    walking upward is the app the user has to grant.
    """
    if not executable:
        return None
    apps = [parent for parent in Path(executable).parents if parent.suffix == ".app"]
    if not apps:
        return None
    return str(apps[-1])


def host_target(pid=None, parent_of=None, executable_of=None, self_exe=None):
    """The app macOS will charge this process to, or the Python binary.

    Walk starts at this process. The first ancestor whose executable lives
    in an app bundle wins, and the bundle reported is the outermost `.app`
    on that path. With no app (tmux, SSH, launchd), TCC asks about the
    binary itself.
    """
    parent_of = parent_of or default_parent
    executable_of = executable_of or default_executable
    start = os.getpid() if pid is None else pid
    seen = start
    for _ in range(_MAX_ANCESTORS):
        if seen is None or seen <= 1:
            break
        exe = executable_of(seen)
        app = outermost_app(exe)
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
    exe = self_exe or (executable_of(start) if start else None) or sys.executable
    exe = exe or sys.executable
    return {"kind": "binary", "name": Path(exe).name, "path": exe, "executable": exe}


def default_executable(pid):
    path = _proc_pidpath(pid)
    if path:
        return path
    _ppid, comm = _ps_parent_and_comm(pid)
    return comm or None


def default_parent(pid):
    ppid = _proc_ppid(pid)
    if ppid:
        return ppid
    ps_ppid, _comm = _ps_parent_and_comm(pid)
    return ps_ppid


def _proc_pidpath(pid):
    """Full executable path. `ps comm` is truncated and misses bundles."""
    if sys.platform != "darwin" or not pid or pid <= 0:
        return None
    try:
        import ctypes
        import ctypes.util
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        buf = ctypes.create_string_buffer(4 * 1024)
        n = libc.proc_pidpath(int(pid), buf, ctypes.sizeof(buf))
    except (OSError, AttributeError, ValueError):
        return None
    if n <= 0:
        return None
    try:
        return buf.value.decode()
    except UnicodeError:
        return None


def _proc_ppid(pid):
    """Parent pid via PROC_PIDT_SHORTBSDINFO.

    The full BSD proc info refuses a process owned by another user, so the
    walk used to stop at root-owned `/usr/bin/login` before it reached
    Terminal. The short record has no such check.
    """
    if sys.platform != "darwin" or not pid or pid <= 0:
        return None
    try:
        import ctypes
        import ctypes.util
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        # Matches proc_bsdshortinfo in xnu proc_info.h. MAXCOMLEN is 16,
        # uid_t is 32-bit. A size mismatch means this struct is wrong.
        class Info(ctypes.Structure):
            _fields_ = [
                ("pbsi_pid", ctypes.c_uint32),
                ("pbsi_ppid", ctypes.c_uint32),
                ("pbsi_pgid", ctypes.c_uint32),
                ("pbsi_status", ctypes.c_uint32),
                ("pbsi_comm", ctypes.c_char * 16),
                ("pbsi_flags", ctypes.c_uint32),
                ("pbsi_uid", ctypes.c_uint32),
                ("pbsi_gid", ctypes.c_uint32),
                ("pbsi_ruid", ctypes.c_uint32),
                ("pbsi_rgid", ctypes.c_uint32),
                ("pbsi_svuid", ctypes.c_uint32),
                ("pbsi_svgid", ctypes.c_uint32),
                ("pbsi_rfu", ctypes.c_uint32),
            ]
        info = Info()
        n = libc.proc_pidinfo(int(pid), 13, 0, ctypes.byref(info), ctypes.sizeof(info))
    except (OSError, AttributeError, ValueError):
        return None
    if n != ctypes.sizeof(info) or info.pbsi_ppid <= 0:
        return None
    return int(info.pbsi_ppid)


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


def settings_url(kind, version):
    """Deep link to one Privacy list.

    macOS 13 and later use the Settings extension anchor. Earlier releases
    use the old preference pane. iPhone Mirroring itself needs macOS 15,
    so the extension anchor is the one a mirroring setup opens.
    """
    pane = _PANE[kind]
    if tuple(version) >= (13, 0):
        anchor = "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?"
    else:
        anchor = "x-apple.systempreferences:com.apple.preference.security?"
    return anchor + pane


def list_name(kind, version):
    if kind == "accessibility" and tuple(version) >= (27, 0):
        return "Device Control and Data Access"
    if kind == "accessibility":
        return "Accessibility"
    return "Screen Recording"


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
    matches the app, or the Python binary when that binary is what TCC
    lists. A different launching app is a new grant, not a stale one.
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
        return "could not check (install pyobjc-framework-Quartz and pyobjc-framework-Cocoa)"
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
            f"If the toggle is already on, the grant is stale: remove the entry and add it again")


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
    stale signature from a toggle that was never on.
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


def live_preflight():
    """True, False, or None. Never prompts.

    None means the frameworks are missing, so the report says unknown
    instead of claiming the grant was refused.
    """
    accessibility = None
    screen = None
    try:
        from ApplicationServices import AXIsProcessTrusted
        accessibility = bool(AXIsProcessTrusted())
    except Exception:
        accessibility = None
    try:
        import Quartz
        fn = getattr(Quartz, "CGPreflightScreenCaptureAccess", None)
        screen = None if fn is None else bool(fn())
    except Exception:
        screen = None
    return {"accessibility": accessibility, "screen_recording": screen}


def _load_grants():
    data = config._read(config.paths()["permissions"], {})
    grants = data.get("grants")
    if not isinstance(grants, dict):
        return {}
    return {key: value for key, value in grants.items() if isinstance(value, dict)}


def _save_grants(grants):
    config._write(config.paths()["permissions"], {"grants": grants})


def collect(preflight=None, host=None, identify=None, version=None, grants=None, save=True):
    """Build the report. Pass `preflight` to avoid reading TCC in this process.

    A guide poll must not call the preflight APIs here: the first denial is
    sticky for the life of the process. The child probe is the reading.
    """
    if preflight is None and sys.platform != "darwin":
        return not_needed_report()
    if preflight is None:
        preflight = live_preflight()
    host = dict(host or host_target())
    identify = identify or code_identity
    if not host.get("cdhash"):
        bundle = ""
        if host.get("kind") == "app":
            bundle = (identify(host.get("path")) or {}).get("cdhash", "")
        host["cdhash"] = bundle or (identify(host.get("executable")) or {}).get("cdhash", "")
    binary_path = sys.executable
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


def explain(report):
    """Shared diagnosis under the two checks. Empty when nothing is wrong."""
    if not report or report.get("ok") or not report.get("needed"):
        return ""
    lines = []
    host = report.get("host") or {}
    binary = report.get("binary") or {}
    if host.get("kind") == "app":
        lines.append(f'Grant "{host.get("name")}" ({host.get("path")}).')
    else:
        lines.append(
            "No launching app was found (tmux, SSH, or a service). "
            f"Grant the Python binary ({host.get('path')}), or grant the terminal app that started this session."
        )
    if host.get("kind") == "app" and binary.get("path") and not binary.get("inside_host"):
        lines.append(
            "If macOS lists the Python binary separately, grant it too: "
            + binary["path"]
        )
    if report.get("note"):
        lines.append(report["note"])
    for kind in _KINDS:
        entry = report[kind]
        if entry["state"] == "granted":
            continue
        lines.append(f"{entry['list'] or kind}: {entry['summary']}")
        if entry.get("url"):
            lines.append("  " + entry["url"])
    lines.append("Open the pane and wait until it is granted: phone-harness permissions --guide")
    lines.append("Status only, no Settings window: phone-harness permissions --json")
    return "\n".join(lines)


def format_text(report, guide=None):
    if not report.get("needed"):
        text = ("phone-harness permissions\n\n"
                "macOS permissions are not required on this machine. "
                "iPhone Mirroring is a Mac feature; a cloud phone needs none of these grants.\n")
        if guide:
            text += f"\nguide: {guide}\n"
        return text
    rows = (
        (ACCESSIBILITY_LABEL, report["accessibility"]),
        (SCREEN_RECORDING_LABEL, report["screen_recording"]),
    )
    lines = ["phone-harness permissions", ""]
    for label, entry in rows:
        mark = "PASS" if entry["state"] == "granted" else "FAIL"
        line = f"  [{mark}] {label}"
        if mark == "FAIL" and entry.get("summary"):
            line += " - " + entry["summary"]
        lines.append(line)
    body = explain(report)
    if body:
        lines.append("")
        lines.append(body)
    if guide == "granted":
        lines.append("")
        lines.append("all clear")
    elif guide == "timeout":
        lines.append("")
        lines.append("Timed out waiting for the grant.")
    elif guide == "opened":
        lines.append("")
        lines.append("Opened System Settings. Grant the app named above, then run "
                     "phone-harness permissions again. This run did not wait.")
    elif guide == "skipped":
        lines.append("")
        lines.append("Non-interactive: System Settings was not opened and this run did not wait.")
    elif guide == "already_running":
        lines.append("")
        lines.append("A permission guide is already waiting in another terminal.")
    elif report.get("ok"):
        lines.append("")
        lines.append("all clear")
    else:
        lines.append("")
        lines.append("fix the FAILs above, then re-run")
    return "\n".join(lines) + "\n"


def format_json(report, guide=None):
    payload = {
        "ok": report["ok"],
        "needed": report["needed"],
        "platform": report["platform"],
        "host": report["host"],
        "binary": report["binary"],
        "accessibility": report["accessibility"],
        "screen_recording": report["screen_recording"],
        "note": report.get("note") or "",
    }
    if guide:
        payload["guide"] = guide
    return json.dumps(payload, indent=2) + "\n"


def doctor_lines(report):
    """(label, ok, hint) for the two checks, in doctor order."""
    lines = []
    for label, key in ((ACCESSIBILITY_LABEL, "accessibility"),
                       (SCREEN_RECORDING_LABEL, "screen_recording")):
        entry = report[key]
        ok = entry["state"] == "granted"
        lines.append((label, ok, "" if ok else entry.get("summary") or entry.get("suggestion") or ""))
    return lines


def emit_probe(kind):
    """One word for a fresh-process poll. Exit 0 only when granted."""
    if kind not in _KINDS:
        print("unknown", file=sys.stderr)
        return 2
    value = live_preflight().get(kind)
    if value is True:
        print("granted")
        return 0
    if value is False:
        print("denied")
        return 1
    print("unknown")
    return 1


def fresh_probe(kind):
    """Ask a new interpreter. This process must not be the one that reads TCC."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "phone_harness.run", "permissions", "--probe", kind],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    word = ""
    for line in reversed((proc.stdout or "").splitlines()):
        if line.strip():
            word = line.strip()
            break
    if word == "granted":
        return True
    if word == "denied":
        return False
    return None


def open_settings(url):
    if sys.platform != "darwin" or not url:
        return False
    try:
        subprocess.run(["open", url], check=False, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def acquire_guide_lock():
    """One waiting guide. The lock dies with the process that holds it."""
    try:
        import fcntl
    except ImportError:
        return None
    path = config.run_dir() / "permission-guide.lock"
    handle = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if handle is not None:
            handle.close()
        return None
    return handle


def _missing(report):
    return [kind for kind in _KINDS if report[kind]["state"] != "granted"]


def run_guide(wait, open_pane, timeout=_GUIDE_TIMEOUT, interval=_GUIDE_INTERVAL,
              opener=None, probe=None, sleep=None, clock=None, say=None,
              host=None, identify=None, version=None):
    """Open the missing pane and optionally poll until both are granted.

    `wait` is the terminal case. A pipe opens once and returns (`opened`)
    so an agent is not stuck for the timeout. `--yes` sets both false.
    Off macOS there is nothing to grant: iPhone Mirroring is the Mac path,
    and a cloud phone does not use these permissions.
    """
    if sys.platform != "darwin":
        return "not_needed", not_needed_report()
    opener = opener or open_settings
    probe = probe or fresh_probe
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    say = say or (lambda _message: None)

    def snapshot():
        return collect(
            preflight={kind: probe(kind) for kind in _KINDS},
            host=host, identify=identify, version=version,
        )

    report = snapshot()
    if not report["needed"] or report["ok"]:
        return "not_needed", report
    if not open_pane and not wait:
        return "skipped", report
    if not wait:
        kind = _missing(report)[0]
        opener(report[kind]["url"])
        return "opened", report

    lock = acquire_guide_lock()
    if lock is None:
        return "already_running", report
    try:
        deadline = clock() + max(0, timeout)
        opened = None
        while _missing(report) and clock() < deadline:
            kind = _missing(report)[0]
            entry = report[kind]
            if opened != kind:
                opener(entry["url"])
                opened = kind
                say(f'Waiting for {entry["list"]} (up to {int(timeout)}s).')
                say(f"Grant {_subject(report['host'])}.")
                if entry.get("url"):
                    say(entry["url"])
            if probe(kind) is True:
                say(f'{entry["list"]} granted.')
                report = snapshot()
                opened = None
                continue
            remaining = deadline - clock()
            if remaining <= 0:
                break
            before = clock()
            sleep(min(interval, remaining))
            if clock() <= before:
                break
        report = snapshot()
        if report["ok"]:
            return "granted", report
        return "timeout", report
    finally:
        lock.close()


def _prompt_accessibility():
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions
    except Exception:
        return
    options = {"AXTrustedCheckOptionPrompt": True}
    try:
        from ApplicationServices import kAXTrustedCheckOptionPrompt
        options = {kAXTrustedCheckOptionPrompt: True}
    except Exception:
        pass
    try:
        AXIsProcessTrustedWithOptions(options)
    except Exception:
        return


def _prompt_screen_recording():
    try:
        import Quartz
        fn = getattr(Quartz, "CGRequestScreenCaptureAccess", None)
        if fn:
            fn()
    except Exception:
        return


def request_prompts():
    """Show the system dialogs. They name whatever TCC decides, which is why
    `--guide` is the path that puts the right app in front of the user."""
    _prompt_accessibility()
    _prompt_screen_recording()


def _parser():
    parser = Parser(
        prog="phone-harness permissions",
        description=CLI_USAGE.strip("\n"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true", help="print the status as JSON")
    parser.add_argument("--guide", action="store_true",
                        help="open System Settings at the missing permission and wait in a terminal")
    parser.add_argument("--request", action="store_true",
                        help="show the system permission prompts, then report again")
    parser.add_argument("--yes", action="store_true",
                        help="do not open System Settings and do not wait")
    parser.add_argument("--non-interactive", action="store_true",
                        help="do not open System Settings and do not wait")
    parser.add_argument("--timeout", type=_timeout_value, default=_GUIDE_TIMEOUT,
                        help=f"seconds --guide waits in a terminal (default {_GUIDE_TIMEOUT})")
    parser.add_argument("--probe", choices=_KINDS, help=argparse.SUPPRESS)
    return parser


def _timeout_value(value):
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number of seconds") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("timeout must be >= 0")
    return number


def cli(argv):
    ns = _parser().parse_args(list(argv))
    if ns.probe:
        return emit_probe(ns.probe)
    noninteractive = bool(ns.yes or ns.non_interactive)
    guide = None
    if sys.platform != "darwin":
        report = not_needed_report()
        guide = "not_needed" if ns.guide else None
    else:
        if ns.request and not noninteractive:
            request_prompts()
        if ns.guide:
            tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
            wait = tty and not noninteractive
            say = None if ns.json or not wait else (lambda message: print(message, flush=True))
            guide, report = run_guide(
                wait=wait,
                open_pane=not noninteractive,
                timeout=ns.timeout,
                say=say,
            )
        elif ns.request:
            # The prompt APIs cache a denial in this process. Read the
            # answer from a fresh interpreter.
            report = collect(preflight={kind: fresh_probe(kind) for kind in _KINDS})
        else:
            report = collect()
    if ns.json:
        sys.stdout.write(format_json(report, guide))
    else:
        sys.stdout.write(format_text(report, guide))
    if guide == "already_running":
        return 0
    return 0 if report.get("ok") else 1
