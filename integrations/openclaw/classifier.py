"""Decide whether a phone_exec script only drives the phone.

The classifier behind the opt-in `classifierAutoRun` setting, the same one the Hermes plugin
uses. It reads a script on stdin, parses it without running it, and prints {"reason": ...}:
why the script reaches past the phone, or null when it does not. A heuristic, not a sandbox: it
can be bypassed, which is why the setting is off by default.

tests/test_openclaw_plugin.py keeps the logic below identical to integrations/hermes/__init__.py.
"""
from __future__ import annotations

import ast
import json
import re
import sys

# The only import the classifier treats as phone-only. Anything else asks.
SAFE_MODULES = frozenset({"phone_harness.helpers"})
# Builtins that read or write files, run arbitrary code, or wait on a terminal; shell() is
# phone-harness's adb shell.
RISKY_CALLS = frozenset({"open", "exec", "eval", "compile", "__import__", "breakpoint", "input", "shell"})
# Names helpers.py imports for itself. A script that pulls them in can reach this computer
# (importlib.import_module, Path.home().read_text, the transport module).
_HOST_NAMES = frozenset({
    "hashlib", "importlib", "time", "Path", "transport",
    "CORE_DIR", "REPO_ROOT", "AGENT_WORKSPACE",
})
_DUNDER = re.compile(r"^__\w+__$")


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


if __name__ == "__main__":
    print(json.dumps({"reason": approval_reason(sys.stdin.read())}))
