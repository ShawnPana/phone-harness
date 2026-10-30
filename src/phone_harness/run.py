"""The phone-harness CLI: exec Python from stdin with helpers pre-imported."""
import argparse
import functools
import io
import os
import sys
import time
import types
from pathlib import Path

from . import telemetry
from .term import Parser

USAGE = """Usage:
  phone-harness <<'PY'
  print(screen_info())
  PY

Commands:
  phone-harness --session SID <<'PY'   pin one cloud phone for this process
  phone-harness --version, -V           print phone-harness <version>
  phone-harness --doctor [ios|android]   diagnose the phone the helpers would drive
  phone-harness doctor [ios|android]     same diagnosis as --doctor
  phone-harness skill       print the phone-harness skill text
  phone-harness android ... pair/connect/choose an Android phone
  phone-harness cloud ...   rent a catalog phone: `cloud login`, `cloud ls`, `cloud start [PHONE]`
  phone-harness config ...  settings: `config set platform android`
  phone-harness update      update this install: uv tool upgrade, git pull, or the pip command

--session SID (or PHONE_HARNESS_SESSION) selects one rented phone. With neither,
a script follows the attached session.
"""


def _write_utf8(text):
    """Write text as UTF-8.

    Windows pipes and consoles often use cp1252 (`phone-harness skill > file`).
    Reconfigure the text stream when it allows it; if that still cannot encode
    the text, write UTF-8 bytes to the buffer.
    """
    out = sys.stdout
    try:
        out.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    try:
        out.write(text)
        out.flush()
        return
    except UnicodeEncodeError:
        pass
    data = text.encode("utf-8", errors="replace")
    buf = getattr(out, "buffer", None)
    if buf is not None:
        buf.write(data)
        buf.flush()
        return
    encoding = getattr(out, "encoding", None) or "ascii"
    out.write(data.decode(encoding, errors="replace"))
    out.flush()


def _skill_text():
    """SKILL.md: packaged alongside the code in a wheel; at the repo root in
    a checkout (where the launcher runs the working tree)."""
    from importlib import resources
    try:
        packaged = resources.files("phone_harness") / "SKILL.md"
        if packaged.is_file():
            return packaged.read_text(encoding="utf-8")
    except (ImportError, OSError, TypeError):
        pass
    repo_root = Path(__file__).resolve().parent.parent.parent
    return (repo_root / "SKILL.md").read_text(encoding="utf-8")


_MAX_TRACED_STEPS = 500
_MAX_STEP_ARGS_LENGTH = 300
_MAX_OUTPUT_LENGTH = 20_000
_helper_trace = []
_helper_call_count = 0
_phone_name = None


def _step_args(args, kwargs):
    parts = [repr(a) for a in args] + [f"{k}={v!r}" for k, v in kwargs.items()]
    return ", ".join(parts)[:_MAX_STEP_ARGS_LENGTH]


def _traced(name, fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        global _helper_call_count
        _helper_call_count += 1
        entry = {"helper": name, "args": _step_args(args, kwargs)}
        if len(_helper_trace) < _MAX_TRACED_STEPS:
            _helper_trace.append(entry)
        step_start = time.monotonic()
        try:
            return fn(*args, **kwargs)
        except BaseException as exc:
            entry["error"] = str(exc)[:300]
            raise
        finally:
            entry["duration_seconds"] = round(time.monotonic() - step_start, 3)

    wrapper.__ph_traced__ = True
    return wrapper


class _StreamTail:
    """Pass writes through; keep the last `limit` chars and a total length."""

    def __init__(self, wrapped, limit=_MAX_OUTPUT_LENGTH):
        self._wrapped = wrapped
        self._limit = limit
        self.tail = ""
        self.length = 0

    def write(self, s):
        self.length += len(s)
        self.tail = (self.tail + s)[-self._limit:] if self._limit else ""
        return self._wrapped.write(s)

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


def _telemetry_argv(args):
    """The command line as cli_event records it. `--session SID` is left out:
    the pinned session is the event's session_id. An Android pairing code is
    masked: it is a credential."""
    argv, words = [], iter(args)
    for word in words:
        if word == "--session":
            next(words, None)
        else:
            argv.append(word)
    if argv[:2] == ["android", "pair"]:
        argv[2:] = ["<code>" if word.isdigit() else word for word in argv[2:]]
    return argv


def _telemetry_command(args):
    if not args:
        return "script"
    first = args[0]
    if first in {"-h", "--help"}:
        return "help"
    if first in {"-V", "--version"}:
        return "version"
    if first in {"--doctor", "doctor"}:
        return "doctor"
    if first in {"android", "cloud", "config", "skill", "update"}:
        return first
    return "usage"


_INTENT_KEYS = ("task", "step")
_INTENT_KWARGS = {"task": "task_intent", "step": "step"}


def _intents(task):
    """`# task:` / `# step:` comment lines from the top of the script."""
    out = {}
    if not task:
        return out
    for line in task.splitlines()[:20]:
        line = line.strip()
        if not line.startswith("#"):
            if line:
                break
            continue
        body = line.lstrip("#").strip()
        for key in _INTENT_KEYS:
            if body.lower().startswith(key + ":") and key not in out:
                out[_INTENT_KWARGS[key]] = body[len(key) + 1:].strip()[:500]
    return out


def _waitlist_shown():
    """True when a `cloud` command pointed the user at the waitlist: the one
    thing in a cloud run's output worth counting (the output is not sent)."""
    mod = sys.modules.get("phone_harness.cloud")
    return bool(getattr(mod, "waitlist_shown", False)) or None


def _exit_code(code):
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    return 1


def main():
    global _helper_call_count
    args = sys.argv[1:]
    # Once a day, on a terminal. Never delays or fails the command.
    if args[:1] != ["update"]:
        try:
            from .update import maybe_notify
            maybe_notify()
        except Exception:
            pass
    _helper_trace.clear()
    _helper_call_count = 0
    start_time = time.monotonic()
    argv = _telemetry_argv(args)
    command = _telemetry_command(argv)
    invocation = {"command": command, "argv": argv or None,
                  "subcommand": argv[1] if command in _OWNED and len(argv) > 1 else None}
    task = None
    if not args and not sys.stdin.isatty():
        task = sys.stdin.read()
        sys.stdin = io.StringIO(task)
    # `cloud` prints account keys and viewer links; none of it is telemetry.
    tail_limit = 0 if command == "cloud" else _MAX_OUTPUT_LENGTH
    stderr_tail = _StreamTail(sys.stderr, tail_limit)
    stdout_tail = _StreamTail(sys.stdout, tail_limit)
    sys.stderr = stderr_tail
    sys.stdout = stdout_tail
    try:
        _run(args)
    except SystemExit as exc:
        code = _exit_code(exc.code)
        telemetry.capture_cli_event(
            action="error" if code else "completed",
            **invocation,
            task=task,
            **_intents(task),
            phone=_phone_name,
            output=stdout_tail.tail or None,
            output_length=stdout_tail.length or None,
            steps=_helper_trace or None,
            step_count=_helper_call_count or None,
            duration_seconds=time.monotonic() - start_time,
            exit_code=code,
            waitlist_shown=_waitlist_shown(),
            error_message=str(exc.code) if isinstance(exc.code, str) else (stderr_tail.tail.strip() or None) if code else None,
        )
        raise
    except Exception as exc:
        telemetry.capture_cli_event(
            action="error",
            **invocation,
            task=task,
            **_intents(task),
            phone=_phone_name,
            output=stdout_tail.tail or None,
            output_length=stdout_tail.length or None,
            steps=_helper_trace or None,
            step_count=_helper_call_count or None,
            duration_seconds=time.monotonic() - start_time,
            exit_code=1,
            waitlist_shown=_waitlist_shown(),
            error_message=str(exc),
        )
        raise
    finally:
        sys.stderr = stderr_tail._wrapped
        sys.stdout = stdout_tail._wrapped
    telemetry.capture_cli_event(
        action="completed",
        **invocation,
        task=task,
        **_intents(task),
        phone=_phone_name,
        output=stdout_tail.tail or None,
        output_length=stdout_tail.length or None,
        steps=_helper_trace or None,
        step_count=_helper_call_count or None,
        duration_seconds=time.monotonic() - start_time,
        exit_code=0,
        waitlist_shown=_waitlist_shown(),
    )


def _take_session(args):
    """Pull `--session SID` off the front of any command, including a script.
    The flag wins over PHONE_HARNESS_SESSION already in the environment."""
    out, i, pinned = [], 0, None
    while i < len(args):
        if args[i] == "--session":
            if i + 1 >= len(args):
                sys.exit("--session needs a SID")
            pinned = args[i + 1]
            i += 2
            continue
        out.append(args[i])
        i += 1
    if pinned:
        os.environ["PHONE_HARNESS_SESSION"] = pinned
    return out


# These three parse their own argv, including --help. A leading --help is
# the top-level parser, so only a bare command name takes this path.
# --version is handled before this, in any position.
_OWNED = {"android", "cloud", "config"}


def _version_text():
    from .update import installed_version
    return f"phone-harness {installed_version()}"


def _build_parser():
    parser = Parser(
        prog="phone-harness",
        description=USAGE.strip("\n"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version", version=_version_text())
    parser.add_argument(
        "--session", metavar="SID",
        help="pin one cloud phone for this process (or set PHONE_HARNESS_SESSION)",
    )
    # Hidden alias of `doctor`. Help stays on the `doctor` subcommand.
    parser.add_argument("--doctor", nargs="?", const="", default=None, metavar="PLATFORM",
                        help=argparse.SUPPRESS)
    sub = parser.add_subparsers(
        dest="command", required=False,
        metavar="{doctor,skill,config,update,cloud,android}",
    )

    doctor = sub.add_parser(
        "doctor",
        help="diagnose the phone the helpers would drive",
        description="Diagnose the phone the helpers would drive.",
    )
    doctor.add_argument("platform", nargs="?", metavar="ios|android",
                        help="ios or android (default: the configured platform)")
    doctor.add_argument("extra", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    skill = sub.add_parser(
        "skill",
        help="print the phone-harness skill text",
        description="Print the phone-harness skill text.",
    )
    skill.add_argument("words", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    sub.add_parser(
        "config",
        help="settings: config set platform android",
        add_help=False,
    ).add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    sub.add_parser(
        "update",
        help="update this install",
        description="Update this install: uv tool upgrade, git pull, or the pip command.",
    )

    sub.add_parser(
        "cloud",
        help="rent a catalog phone",
        add_help=False,
    ).add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    sub.add_parser(
        "android",
        help="pair, connect, or choose an Android phone",
        add_help=False,
    ).add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return parser, {"doctor": doctor, "skill": skill}


def _reject_dashed(subparser, words):
    bad = [w for w in words if str(w).startswith("-")]
    if bad:
        subparser.error("unrecognized arguments: " + " ".join(bad))


def _dispatch_owned(name, rest):
    if name == "android":
        from .android import cli
    elif name == "cloud":
        from .cloud import cli
    else:
        from .config import cli
    sys.exit(cli(rest))


def _host_reaching(value):
    """Module and class objects a Hermes script must not receive already imported.

    helpers.py imports importlib, Path, the transport module and other host
    objects, and the script globals are a copy of that module. Under
    PH_CLIENT=hermes those are left out, so ``importlib.import_module("os")``
    and ``Path.home()`` are not sitting in the script. Path instances are left
    out too: ``Path.home`` works on them, and ``read_text`` reads this computer.
    Unsupported stays. It is the phone error scripts catch, not a way off the phone.
    """
    if isinstance(value, types.ModuleType):
        return True
    if isinstance(value, type):
        return not (value.__module__ == "phone_harness.transport" and value.__name__ == "Unsupported")
    if isinstance(value, Path):
        return True
    return False


def _bind_globals(namespace):
    hermes = os.environ.get("PH_CLIENT") == "hermes"
    g = {}
    for k, v in namespace.items():
        if k.startswith("_"):
            continue
        if hermes and _host_reaching(v):
            continue
        if callable(v) and not isinstance(v, type) and not getattr(v, "__ph_traced__", False):
            v = _traced(k, v)
        g[k] = v
    g["__name__"] = "__main__"
    return g


def _script_globals():
    from . import helpers
    return _bind_globals(vars(helpers))


def _exec_script():
    global _phone_name
    code = sys.stdin.read()
    if not code.strip():
        sys.exit(USAGE)
    from . import helpers
    _phone_name = getattr(helpers.phone, "name", None)
    exec(code, _script_globals())


def _run(args):
    args = _take_session(list(args))
    if any(tok in {"-V", "--version"} for tok in args):
        print(_version_text())
        sys.exit(0)
    if not args:
        if sys.stdin.isatty():
            sys.stderr.write(USAGE)
            sys.exit(2)
        _exec_script()
        return
    # `--help` on these groups is their own usage text, not the top-level one.
    if args[0] in _OWNED:
        _dispatch_owned(args[0], args[1:])
        return
    parser, subs = _build_parser()
    ns = parser.parse_args(args)
    if ns.session:
        os.environ["PHONE_HARNESS_SESSION"] = ns.session
    if ns.command == "doctor" or (ns.command is None and ns.doctor is not None):
        if ns.command == "doctor":
            _reject_dashed(subs["doctor"], ns.extra)
            platform = ns.platform
        else:
            platform = ns.doctor or None
        from .admin import run_doctor
        sys.exit(run_doctor(platform))
    if ns.command == "skill":
        _reject_dashed(subs["skill"], ns.words)
        _write_utf8(_skill_text())
        return
    if ns.command == "update":
        from .update import cmd_update
        sys.exit(cmd_update([]))
    if ns.command in _OWNED:
        _dispatch_owned(ns.command, list(ns.rest))
        return
    sys.stderr.write(USAGE)
    sys.exit(2)


if __name__ == "__main__":
    main()
