"""macOS Accessibility and Screen Recording: who to enable, and how to ask.

iPhone Mirroring needs both grants. macOS does not give them to phone-harness
itself. It gives them to the responsible app — Terminal or iTerm when a person
runs the command, and the host app (Cursor, VS Code, Grok Bot, …) when an
agent does. A terminal that already has the grants does not cover that host.

The pure helpers (which app, which sentence) take their inputs as arguments so
tests can run anywhere. The functions that talk to TCC are called only on
macOS, and only from the iOS doctor and the iOS backend.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import PurePosixPath

ACCESSIBILITY = "accessibility"
SCREEN_RECORDING = "screen_recording"

# A person at a terminal can toggle the switch while we watch. An agent has
# no terminal and must not block; a short recheck only catches a grant that
# landed before this process looked.
FIX_WAIT_S = 45.0
FIX_WAIT_NONINTERACTIVE_S = 2.0

_TITLES = {
    ACCESSIBILITY: "Accessibility",
    SCREEN_RECORDING: "Screen Recording",
}
_PANES = {
    ACCESSIBILITY: "Privacy_Accessibility",
    SCREEN_RECORDING: "Privacy_ScreenCapture",
}

# CGRequestScreenCaptureAccess blocks until the dialog is dismissed. It runs
# in a detached child so doctor and scripts return, and so the dialog is not
# torn down when they exit. TCC still attributes the choice to the responsible
# app of this process, which the child inherits.
_SCREEN_REQUEST = "import Quartz\nQuartz.CGRequestScreenCaptureAccess()\n"

_quiet = False
_warned = False
_checked = False
_ax_requested = False
_screen_requested = False
# None until the current process has confirmed proc_pidinfo's parent pid.
_procinfo_ok = None


@dataclass(frozen=True)
class AppInfo:
    """The app System Settings will list for this process.

    `name` is the outermost .app (the one a person recognises). Electron
    helpers are nested bundles inside that app; `helper_name` is set when the
    responsible binary is one of those, because the list sometimes shows the
    helper instead of the host.
    """
    name: str
    bundle_path: str
    helper_name: str | None = None
    helper_bundle_path: str | None = None


def app_bundles(path: str) -> list[tuple[str, str]]:
    """(bundle path, bundle name) for every .app component, outermost first.

    A component is a bundle only when the path segment itself ends in `.app`.
    A file named `notes.app.txt` is not one.
    """
    if not path:
        return []
    parts = PurePosixPath(path).parts
    found = []
    for i, part in enumerate(parts):
        if part.endswith(".app") and part != ".app":
            found.append((str(PurePosixPath(*parts[: i + 1])), part[:-4]))
    return found


def select_responsible_app(chain: list[str], responsible_path: str | None = None) -> AppInfo | None:
    """Pick the app macOS will grant, from a process chain.

    `chain` is executable paths from the current process toward launchd.
    `responsible_path` is the executable of the pid TCC names as responsible
    (responsibility_get_pid_responsible_for_pid), when that call works. It
    wins when it lives in an .app; otherwise the first ancestor that does.

    The name we show is the outermost bundle on that path. A helper at
    `Host.app/Contents/Frameworks/Host Helper.app/...` would otherwise be
    reported as "Host Helper", which is not the row people look for.
    """
    candidate = None
    if responsible_path and app_bundles(responsible_path):
        candidate = responsible_path
    else:
        for path in chain:
            if app_bundles(path):
                candidate = path
                break
    if not candidate:
        return None
    bundles = app_bundles(candidate)
    outer_path, outer_name = bundles[0]
    helper_name = None
    helper_path = None
    if len(bundles) > 1:
        helper_path, helper_name = bundles[-1]
        if helper_name == outer_name:
            helper_name = None
            helper_path = None
    return AppInfo(outer_name, outer_path, helper_name, helper_path)


def collect_executable_chain(pid, parent_of, path_of, limit=32) -> list[str]:
    """Executable paths from `pid` upward. Stops at launchd, a cycle, or `limit`."""
    chain = []
    seen = set()
    for _ in range(limit):
        if not pid or pid in seen:
            break
        seen.add(pid)
        chain.append(path_of(pid) or "")
        if pid == 1:
            break
        parent = parent_of(pid) or 0
        if parent == pid:
            break
        pid = parent
    return chain


def bundle_display_name(bundle_path: str) -> str:
    """CFBundleDisplayName, else CFBundleName, else the .app folder name."""
    stem = PurePosixPath(bundle_path).stem
    plist = PurePosixPath(bundle_path) / "Contents" / "Info.plist"
    try:
        import plistlib
        with open(plist, "rb") as fh:
            data = plistlib.load(fh)
        if not isinstance(data, dict):
            return stem
        return data.get("CFBundleDisplayName") or data.get("CFBundleName") or stem
    except (OSError, ValueError, TypeError):
        return stem


def with_display_names(app: AppInfo) -> AppInfo:
    name = bundle_display_name(app.bundle_path)
    helper = app.helper_name
    helper_path = app.helper_bundle_path
    if helper_path:
        helper = bundle_display_name(helper_path)
        if helper == name:
            helper = None
            helper_path = None
    return AppInfo(name, app.bundle_path, helper, helper_path)


def settings_url(kind: str) -> str:
    return "x-apple.systempreferences:com.apple.preference.security?" + _PANES[kind]


def app_phrase(app: AppInfo | None) -> str:
    """Who to enable, in the words a doctor line can carry."""
    if app is None:
        return ("the app that launched phone-harness "
                "(your terminal, or the agent app if you ran it from one)")
    loc = f'"{app.name}" ({app.bundle_path})'
    if app.helper_name and app.helper_name != app.name:
        return f'{loc} — if the list shows "{app.helper_name}" instead, enable that'
    return loc


def _who(app: AppInfo | None) -> str:
    return f'"{app.name}"' if app else "the app that launched phone-harness"


def permission_hint(kind: str, app: AppInfo | None) -> str:
    title = _TITLES[kind]
    return (f"System Settings > Privacy & Security > {title}: enable {app_phrase(app)}. "
            f'open "{settings_url(kind)}"')


def restart_hint(app: AppInfo | None, kind: str) -> str:
    """Both grants can stay invisible to this process until the app is relaunched.

    Accessibility has a second failure mode: the switch is on, AXIsProcessTrusted
    still returns false, and removing the row and adding it back is what clears
    it. Screen Recording does not need that sentence.
    """
    who = _who(app)
    text = (f"Quit {who} completely (Cmd-Q, not just the window) and reopen it "
            f"so macOS applies the permission, then re-run `phone-harness --doctor ios`.")
    if kind == ACCESSIBILITY:
        text += (f" If {who} is already enabled and the check still fails, remove it "
                 f"from the Accessibility list, add it again, then quit and reopen.")
    return text


def failure_hint(kind: str, app: AppInfo | None, *, fix: bool) -> str:
    parts = [permission_hint(kind, app), restart_hint(app, kind)]
    if not fix:
        parts.append("Guided setup: `phone-harness --doctor ios --fix`.")
    return " ".join(parts)


def capture_blank_hint(app: AppInfo | None, *, preflight_ok: bool) -> str:
    if preflight_ok:
        return ("capture failed or is blank — Screen Recording looks granted, but this "
                "process still cannot see the window. " + restart_hint(app, SCREEN_RECORDING))
    return "capture failed or is blank. " + failure_hint(SCREEN_RECORDING, app, fix=False)


def prompts_requested_message(kinds: list[str], app: AppInfo | None) -> str:
    titles = " and ".join(_TITLES[k] for k in kinds)
    return (f"  requested the macOS {titles} prompt for {app_phrase(app)}. "
            f"If you do not see a dialog, it may be behind other windows, or macOS "
            f"may have shown it once already.")


def fix_waiting_message(kind: str, app: AppInfo | None, interactive: bool, timeout_s: float) -> str:
    title = _TITLES[kind]
    who = app_phrase(app)
    url = settings_url(kind)
    if interactive:
        return (f"  {title} is not granted for {who}.\n"
                f"  Requested the system prompt and opened Settings ({url}).\n"
                f"  Waiting up to {timeout_s:.0f}s. Turn the toggle on. If it stays off, "
                f"quit the app completely (Cmd-Q) and reopen it — macOS often keeps the "
                f"old answer until that process starts again. Ctrl-C to stop.")
    return (f"  {title} is not granted for {who}.\n"
            f"  Requested the system prompt and opened Settings ({url}).\n"
            f"  Not a terminal, so not waiting. Turn the toggle on, quit the app "
            f"completely (Cmd-Q), reopen it, and re-run `phone-harness --doctor ios --fix`.")


def still_missing_message(kind: str, app: AppInfo | None) -> str:
    return "  still not granted. " + restart_hint(app, kind)


@dataclass(frozen=True)
class PermissionOutcome:
    accessibility: bool
    screen_recording: bool
    prompt_shown: bool
    granted_after_prompt: bool | None
    app: AppInfo | None
    prompted: tuple[str, ...] = ()


def poll_until(check, timeout_s, interval_s=0.4, sleep=time.sleep, clock=time.monotonic) -> bool:
    """Call `check` until it is true or `timeout_s` has elapsed. Never reads stdin."""
    start = clock()
    while True:
        if check():
            return True
        if clock() - start >= timeout_s:
            return False
        sleep(min(interval_s, max(0.0, timeout_s - (clock() - start))))


def resolve_permissions(fix, interactive, *, app, ax_trusted, screen_trusted,
                        request_ax, request_screen, open_pane, poll, echo) -> PermissionOutcome:
    """Request every missing grant, then optionally wait.

    Prompts are fired for both missing permissions before any wait, so a
    Screen Recording dialog is not stuck behind a 45s Accessibility poll.
    `granted_after_prompt` is None when nothing was missing. It is true only
    when every permission we asked for reads as granted afterwards — a restart
    is often still required, and this does not claim otherwise.
    """
    ax_ok = bool(ax_trusted())
    sc_ok = bool(screen_trusted())
    prompted: list[str] = []
    if not ax_ok:
        request_ax()
        prompted.append(ACCESSIBILITY)
    if not sc_ok:
        request_screen()
        prompted.append(SCREEN_RECORDING)

    if prompted and not fix:
        echo(prompts_requested_message(prompted, app))
        if ACCESSIBILITY in prompted:
            ax_ok = bool(ax_trusted())
        if SCREEN_RECORDING in prompted:
            sc_ok = bool(screen_trusted())
    elif fix and prompted:
        timeout = FIX_WAIT_S if interactive else FIX_WAIT_NONINTERACTIVE_S
        for kind, ok in ((ACCESSIBILITY, ax_ok), (SCREEN_RECORDING, sc_ok)):
            if kind not in prompted:
                continue
            echo(fix_waiting_message(kind, app, interactive, timeout))
            open_pane(kind)
            granted = bool(poll(ax_trusted if kind == ACCESSIBILITY else screen_trusted, timeout))
            if kind == ACCESSIBILITY:
                ax_ok = granted
            else:
                sc_ok = granted
            if not granted:
                echo(still_missing_message(kind, app))

    granted_after = all(
        ax_ok if kind == ACCESSIBILITY else sc_ok for kind in prompted
    ) if prompted else None
    return PermissionOutcome(
        accessibility=ax_ok,
        screen_recording=sc_ok,
        prompt_shown=bool(prompted),
        granted_after_prompt=granted_after,
        app=app,
        prompted=tuple(prompted),
    )


def op_needs_permission_prompt(op: str) -> bool:
    """Ops that see or touch the phone. `screen.bounds` is a window-list read."""
    if op == "screen.bounds":
        return False
    return op.startswith(("screen.", "input.", "nav.", "apps.", "focus.", "session."))


def reset_for_tests() -> None:
    global _quiet, _warned, _checked, _ax_requested, _screen_requested, _procinfo_ok
    _quiet = False
    _warned = False
    _checked = False
    _ax_requested = False
    _screen_requested = False
    _procinfo_ok = None


def set_quiet(quiet: bool) -> None:
    """Doctor prints its own lines. A script warning in the middle of them is noise."""
    global _quiet
    _quiet = quiet


def stdin_is_terminal() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except Exception:
        return False


# --- macOS TCC ---------------------------------------------------------------

def _libsystem():
    import ctypes
    lib = getattr(_libsystem, "_lib", None)
    if lib is None:
        lib = ctypes.CDLL("/usr/lib/libSystem.dylib", use_errno=True)
        _libsystem._lib = lib
    return lib


def _proc_pidpath(pid: int) -> str:
    if not pid or sys.platform != "darwin":
        return ""
    import ctypes
    lib = _libsystem()
    lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    lib.proc_pidpath.restype = ctypes.c_int
    buf = ctypes.create_string_buffer(4096)
    n = lib.proc_pidpath(int(pid), buf, 4096)
    if n <= 0:
        return ""
    return buf.value.decode(errors="surrogateescape")


def _parent_pid_ps(pid: int) -> int:
    try:
        out = subprocess.check_output(
            ["ps", "-o", "ppid=", "-p", str(int(pid))],
            text=True, stderr=subprocess.DEVNULL, timeout=2,
        )
        return int(out.strip() or "0")
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def _parent_pid_procinfo(pid: int) -> int:
    import ctypes

    class _BSDShortInfo(ctypes.Structure):
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

    lib = _libsystem()
    lib.proc_pidinfo.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    lib.proc_pidinfo.restype = ctypes.c_int
    info = _BSDShortInfo()
    n = lib.proc_pidinfo(int(pid), 13, 0, ctypes.byref(info), ctypes.sizeof(info))
    if n < ctypes.sizeof(info):
        return 0
    return int(info.pbsi_ppid)


def _parent_pid(pid: int) -> int:
    """Parent pid. proc_pidinfo's layout is checked once against os.getppid().

    A wrong struct would still return *a* number, and every ancestor walk
    would follow it. The first call is this process, which we can verify; if
    it disagrees, ps is used for the rest of the walk.
    """
    global _procinfo_ok
    if not pid or sys.platform != "darwin":
        return 0
    if _procinfo_ok is False:
        return _parent_pid_ps(pid)
    try:
        ppid = _parent_pid_procinfo(pid)
    except Exception:
        ppid = 0
    if _procinfo_ok is None and pid == os.getpid():
        _procinfo_ok = ppid > 0 and ppid == os.getppid()
        if not _procinfo_ok:
            return _parent_pid_ps(pid)
    if ppid <= 0:
        return _parent_pid_ps(pid)
    return ppid


def _responsible_pid(pid: int) -> int | None:
    """The pid TCC attributes this process's grants to, or None if the call fails.

    Private but stable libSystem symbol. A failure falls back to walking parents.
    """
    if sys.platform != "darwin":
        return None
    import ctypes
    try:
        fn = _libsystem().responsibility_get_pid_responsible_for_pid
    except (OSError, AttributeError):
        return None
    fn.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    fn.restype = ctypes.c_int
    out = ctypes.c_int(0)
    try:
        err = fn(int(pid), ctypes.byref(out))
    except Exception:
        return None
    if err != 0 or out.value <= 0:
        return None
    return int(out.value)


def responsible_app() -> AppInfo | None:
    """The app to name in guidance, or None when it cannot be determined."""
    if sys.platform != "darwin":
        return None
    try:
        chain = collect_executable_chain(os.getpid(), _parent_pid, _proc_pidpath)
        rpid = _responsible_pid(os.getpid())
        rpath = _proc_pidpath(rpid) if rpid else None
        app = select_responsible_app(chain, rpath)
        if app is None:
            return None
        return with_display_names(app)
    except Exception:
        return None


def accessibility_trusted() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        from ApplicationServices import AXIsProcessTrusted
        return bool(AXIsProcessTrusted())
    except Exception:
        return False


def screen_capture_access() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import Quartz
        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception:
        return False


def request_accessibility() -> bool:
    """Show the Accessibility prompt once per process. Returns the current grant.

    AXIsProcessTrustedWithOptions with AXTrustedCheckOptionPrompt posts the
    system dialog and returns immediately. It does not wait for the click,
    and it does not grant by itself.
    """
    global _ax_requested
    if _ax_requested:
        return accessibility_trusted()
    _ax_requested = True
    if sys.platform != "darwin":
        return False
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions
        try:
            from ApplicationServices import kAXTrustedCheckOptionPrompt
            key = kAXTrustedCheckOptionPrompt
        except ImportError:
            key = "AXTrustedCheckOptionPrompt"
        return bool(AXIsProcessTrustedWithOptions({key: True}))
    except Exception:
        return False


def request_screen_capture() -> None:
    """Ask macOS to show the Screen Recording prompt, without blocking."""
    global _screen_requested
    if _screen_requested:
        return
    _screen_requested = True
    if sys.platform != "darwin":
        return
    try:
        subprocess.Popen(
            [sys.executable, "-c", _SCREEN_REQUEST],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        return


def open_settings(kind: str) -> bool:
    if sys.platform != "darwin":
        return False
    try:
        r = subprocess.run(["open", settings_url(kind)], capture_output=True)
        return r.returncode == 0
    except Exception:
        return False


def script_warning(missing: list[str], app: AppInfo | None) -> str:
    who = _who(app)
    return (f"phone-harness: macOS {' and '.join(missing)} not granted for {who}. "
            f"A system prompt was requested. Run `phone-harness --doctor ios --fix`, "
            f"then quit and reopen {who}.")


def prompt_if_missing() -> None:
    """The first time a script hits a missing grant, ask macOS to show the prompt.

    Once per process, including when the grants are already there — later taps
    must not pay for a TCC check. Non-TTY safe: Accessibility returns
    immediately, Screen Recording is a detached child. Doctor sets quiet so it
    can print its own ladder instead of this one-line warning.
    """
    global _warned, _checked
    if _checked or sys.platform != "darwin":
        return
    _checked = True
    missing = []
    try:
        if not accessibility_trusted():
            request_accessibility()
            missing.append("Accessibility")
        if not screen_capture_access():
            request_screen_capture()
            missing.append("Screen Recording")
    except Exception:
        return
    if not missing or _quiet or _warned:
        return
    _warned = True
    print(script_warning(missing, responsible_app()), file=sys.stderr)


def explain_accessibility_failure(prefix: str) -> str:
    prompt_if_missing()
    return prefix + " " + failure_hint(ACCESSIBILITY, responsible_app(), fix=False)


def explain_capture_failure(detail: str) -> str:
    prompt_if_missing()
    app = responsible_app()
    try:
        granted = screen_capture_access()
    except Exception:
        granted = False
    if granted:
        why = ("Screen Recording looks granted, but this process still cannot see "
               "the window. " + restart_hint(app, SCREEN_RECORDING))
    else:
        why = failure_hint(SCREEN_RECORDING, app, fix=False)
    return f"{detail}. {why}"
