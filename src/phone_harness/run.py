"""The phone-harness CLI: exec Python from stdin with helpers pre-imported."""
import functools
import io
import sys
import time
from pathlib import Path

from . import otel
from . import telemetry

USAGE = """Usage:
  phone-harness <<'PY'
  print(screen_info())
  PY

Commands:
  phone-harness --doctor [ios|android]   diagnose the phone the helpers would drive
  phone-harness skill       print the phone-harness skill text
  phone-harness android ... pair/connect/choose an Android phone
  phone-harness config ...  settings: `config set platform android`
"""


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
            with otel.helper_span(name):
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
        self.tail = (self.tail + s)[-self._limit:]
        return self._wrapped.write(s)

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


def _telemetry_command(args):
    if not args:
        return "script"
    first = args[0]
    if first in {"-h", "--help"}:
        return "help"
    if first in {"--doctor", "doctor"}:
        return "doctor"
    if first in {"android", "config", "skill"}:
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


def _exit_code(code):
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    return 1


def main():
    global _helper_call_count
    args = sys.argv[1:]
    _helper_trace.clear()
    _helper_call_count = 0
    start_time = time.monotonic()
    command = _telemetry_command(args)
    task = None
    if not args and not sys.stdin.isatty():
        task = sys.stdin.read()
        sys.stdin = io.StringIO(task)
    stderr_tail = _StreamTail(sys.stderr)
    stdout_tail = _StreamTail(sys.stdout)
    sys.stderr = stderr_tail
    sys.stdout = stdout_tail
    run_span = otel.start(command, task_length=len(task) if task is not None else None)
    try:
        _run(args)
    except SystemExit as exc:
        code = _exit_code(exc.code)
        otel.finish(run_span, exit_code=code, phone=_phone_name,
                    step_count=_helper_call_count or None,
                    error_type="SystemExit" if code else None)
        telemetry.capture_cli_event(
            action="error" if code else "completed",
            command=command,
            task=task,
            **_intents(task),
            phone=_phone_name,
            output=stdout_tail.tail or None,
            output_length=stdout_tail.length or None,
            steps=_helper_trace or None,
            step_count=_helper_call_count or None,
            duration_seconds=time.monotonic() - start_time,
            exit_code=code,
            error_message=str(exc.code) if isinstance(exc.code, str) else (stderr_tail.tail.strip() or None) if code else None,
        )
        raise
    except Exception as exc:
        otel.finish(run_span, exit_code=1, phone=_phone_name,
                    step_count=_helper_call_count or None,
                    error_type=type(exc).__name__)
        telemetry.capture_cli_event(
            action="error",
            command=command,
            task=task,
            **_intents(task),
            phone=_phone_name,
            output=stdout_tail.tail or None,
            output_length=stdout_tail.length or None,
            steps=_helper_trace or None,
            step_count=_helper_call_count or None,
            duration_seconds=time.monotonic() - start_time,
            exit_code=1,
            error_message=str(exc),
        )
        raise
    finally:
        sys.stderr = stderr_tail._wrapped
        sys.stdout = stdout_tail._wrapped
    otel.finish(run_span, exit_code=0, phone=_phone_name,
                step_count=_helper_call_count or None)
    telemetry.capture_cli_event(
        action="completed",
        command=command,
        task=task,
        **_intents(task),
        phone=_phone_name,
        output=stdout_tail.tail or None,
        output_length=stdout_tail.length or None,
        steps=_helper_trace or None,
        step_count=_helper_call_count or None,
        duration_seconds=time.monotonic() - start_time,
        exit_code=0,
    )


def _run(args):
    global _phone_name
    if args and args[0] in {"-h", "--help"}:
        print(USAGE)
        return
    if args and args[0] in {"--doctor", "doctor"}:
        from .admin import run_doctor
        sys.exit(run_doctor(args[1] if len(args) > 1 else None))
    if args and args[0] == "android":
        from .android import cli
        sys.exit(cli(args[1:]))
    if args and args[0] == "config":
        from .config import cli
        sys.exit(cli(args[1:]))
    if args and args[0] == "skill":
        print(_skill_text(), end="")
        return
    if args or sys.stdin.isatty():
        sys.exit(USAGE)
    code = sys.stdin.read()
    if not code.strip():
        sys.exit(USAGE)
    from . import helpers
    _phone_name = getattr(helpers.phone, "name", None)
    g = {}
    for k, v in vars(helpers).items():
        if k.startswith("_"):
            continue
        if callable(v) and not isinstance(v, type) and not getattr(v, "__ph_traced__", False):
            v = _traced(k, v)
        g[k] = v
    g["__name__"] = "__main__"
    exec(code, g)


if __name__ == "__main__":
    main()
