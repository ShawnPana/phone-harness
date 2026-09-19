"""Every op the helpers send must be accepted by every backend.

helpers.py is shared; each backend implements the ops on its own. Nothing else
ties the two together, so a keyword added for one phone (`dx` for a sideways
scroll, `keystrokes` for typing) used to break the other with a TypeError the
first time an agent called it. This reads the calls out of helpers.py and
checks each backend's signature against them. No phone needed.

    python -m unittest discover tests
"""
import ast
import importlib
import inspect
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))


def sent_by_helpers():
    """{op: set of keyword names} for every send("op", kw=...) in helpers.py."""
    ops = {}
    for node in ast.walk(ast.parse((SRC / "phone_harness" / "helpers.py").read_text())):
        if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "send"
                and node.args and isinstance(node.args[0], ast.Constant)):
            ops.setdefault(node.args[0].value, set()).update(
                k.arg for k in node.keywords if k.arg)
    return ops


def backends():
    from phone_harness.android import Android
    found = {"android": Android}
    if sys.platform == "darwin":
        try:
            found["ios"] = importlib.import_module("phone_harness.ios").IPhone
        except ImportError:
            pass                                  # no pyobjc here: Android is still checked
    return found


class BackendContract(unittest.TestCase):
    def test_helpers_send_something(self):
        ops = sent_by_helpers()
        self.assertIn("input.scroll", ops)
        self.assertIn("dx", ops["input.scroll"])

    def test_every_backend_accepts_what_the_helpers_send(self):
        problems = []
        for name, cls in backends().items():
            for op, kwargs in sorted(sent_by_helpers().items()):
                method = getattr(cls, "_" + op.replace(".", "_"), None)
                if method is None:
                    continue                      # Unsupported is an honest answer
                params = inspect.signature(method).parameters
                if any(p.kind is p.VAR_KEYWORD for p in params.values()):
                    continue
                missing = kwargs - set(params)
                if missing:
                    problems.append(f"{name}: {op} is sent {sorted(missing)} "
                                    f"but {method.__qualname__} does not take it")
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
