"""The phone-harness CLI: exec Python from stdin with helpers pre-imported."""
import sys
from pathlib import Path

USAGE = """Usage:
  phone-harness <<'PY'
  print(screen_info())
  PY

Commands:
  phone-harness --doctor    diagnose permissions, app, and session state
  phone-harness skill       print the phone-harness skill text
  phone-harness cloud ...   rent/list/release cloud phone sessions
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


def main():
    args = sys.argv[1:]
    if args and args[0] in {"-h", "--help"}:
        print(USAGE)
        return
    if args and args[0] in {"--doctor", "doctor"}:
        from .admin import run_doctor
        sys.exit(run_doctor())
    if args and args[0] == "cloud":
        from .cloud import cli
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
    g = {k: v for k, v in vars(helpers).items() if not k.startswith("_")}
    g["__name__"] = "__main__"
    exec(code, g)


if __name__ == "__main__":
    main()
