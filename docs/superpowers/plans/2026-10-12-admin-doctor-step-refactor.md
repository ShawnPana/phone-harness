# Doctor Step-Ladder Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `src/phone_harness/admin.py`'s imperative `_check()` + manual-`return` ladder with a small declarative `Step`/`run_steps()` runner, fixing GitHub issue #4 (an unguarded import crashes mid-ladder; non-fatal checks print `[FAIL]` instead of a softer tag) as a structural side effect rather than a one-off patch.

**Architecture:** A `Step` dataclass (a check function + `fatal` + `stop_if_failed`) and a `run_steps(steps)` runner that: (1) calls each step's function inside a `try/except Exception`, turning any crash — including today's unguarded `ApplicationServices` import — into a reported `FAIL` instead of an uncaught traceback; (2) derives the printed tag from `(ok, fatal)` in exactly one place, so a `fatal=False` step always prints `WARN`, never `FAIL`; (3) supports a step returning `None` to mean "doesn't apply right now, print nothing" (e.g. OCR after a failed capture); (4) supports a step's function returning a 4th `fatal` override, for the one case (Android's accessibility-tree check) where severity depends on *how* the check failed, not just that it did. `_doctor_ios()` and `_doctor_android()` become a list of small check functions (closures over a local `ctx` dict for the handful of values later steps need, e.g. the mirroring window or the adb-connected phone object) fed to `run_steps()`.

**Tech Stack:** Python 3.10+, stdlib only (`dataclasses`, `unittest`, `unittest.mock`) — matches the rest of `phone_harness`.

**Spec:** No separate spec doc. This plan is derived from GitHub issue #4 (ShawnPana/phone-harness#4) and the refactor design agreed in conversation; both travel with this plan file.

## Global Constraints

- Preserve `run_doctor(platform=None) -> int` as the only public contract `run.py` relies on (`src/phone_harness/run.py:410-411`) — its signature and exit-code behavior (0 = all clear, 1 = a fatal check failed) must not change.
- Preserve every existing printed check's wording, ordering, and PASS/FAIL/WARN semantics exactly, **except** the two issue #4 fixes (fold `ApplicationServices` into the guarded pyobjc import; print `WARN` instead of `FAIL` for non-fatal checks).
- No new third-party dependencies.
- `tests/test_cli.py`'s existing doctor-dispatch tests (`test_doctor_alias_still_dispatches_and_help_does_not`, around line 401) must keep passing unmodified — they mock `phone_harness.admin.run_doctor` at the boundary and must not need changes.
- Full suite (`python -m unittest discover tests`) must stay green throughout — this is what CI runs (`.github/workflows/ci.yml`).

---

## File Structure

- **Modify:** `src/phone_harness/admin.py` — add `Step`/`run_steps`; rewrite `_doctor_ios()` and `_doctor_android()` to build a step list each; update `run_doctor()` to use the returned failure list; delete the old `_check()` function and the module-level `_failures` list once nothing references them.
- **Create:** `tests/test_admin.py` — new test file (none exists yet for this module). Follows the project's existing test convention: `unittest.TestCase`, `unittest.mock`, `sys.path` adjusted to `src/` at import time (see `tests/test_cli.py:1-15` for the exact pattern to copy).

No other files change. `admin.py` stays a single file (222 lines today) — small enough that splitting it would be unilateral restructuring the codebase doesn't otherwise do (the biggest module, `cloud.py`, is ~1,900 lines in one file).

---

### Task 1: `Step` dataclass and `run_steps()` runner

**Files:**
- Modify: `src/phone_harness/admin.py` (add near the top, after the module docstring and imports)
- Test: `tests/test_admin.py` (new file)

**Interfaces:**
- Produces:
  - `class Step: fn: Callable[[], object]; fatal: bool = True; stop_if_failed: bool = False` — a dataclass. `fn` takes no arguments and returns one of:
    - `None` — this step doesn't apply right now; print nothing, don't affect failures.
    - `(label: str, ok: bool, hint: str)` — tag derived from `step.fatal`.
    - `(label: str, ok: bool, hint: str, fatal_override: bool)` — tag derived from `fatal_override` instead of `step.fatal`.
  - `run_steps(steps: list[Step]) -> list[str]` — runs each step in order, prints one `[PASS]`/`[FAIL]`/`[WARN]` line per non-`None` result, and returns the labels of every step that failed with `fatal` true (the override from a 4-tuple result, else `step.fatal`). A step whose `fn` raises is treated as `(fn.__name__, False, f"unexpected error: {e}")` — this is what turns today's unguarded-import crash into a reported failure.

This task does not touch `_doctor_ios`/`_doctor_android` yet — it only adds the new primitive, unused, so it is safe to land on its own.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_admin.py`:

```python
"""Step/run_steps: the declarative check-ladder runner behind `--doctor`."""
import sys
import unittest
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from phone_harness.admin import Step, run_steps  # noqa: E402


def _lines(capsys_value):
    return [l for l in capsys_value.splitlines() if l.strip()]


class RunStepsTagging(unittest.TestCase):
    def test_a_passing_step_prints_pass_and_reports_no_failure(self):
        out = []
        with unittest.mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            failures = run_steps([Step(lambda: ("thing ok", True, ""))])
        self.assertEqual(failures, [])
        self.assertIn("[PASS] thing ok", out[0])

    def test_a_fatal_failing_step_prints_fail_and_is_reported(self):
        out = []
        with unittest.mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            failures = run_steps([Step(lambda: ("thing bad", False, "do X"), fatal=True)])
        self.assertEqual(failures, ["thing bad"])
        self.assertIn("[FAIL] thing bad", out[0])
        self.assertIn("do X", out[0])

    def test_a_non_fatal_failing_step_prints_warn_not_fail_and_is_not_reported(self):
        """This is GitHub issue #4's second bug: a non-fatal check must not
        print [FAIL], and must not count toward the overall verdict."""
        out = []
        with unittest.mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            failures = run_steps([Step(lambda: ("thing bad", False, "ignore"), fatal=False)])
        self.assertEqual(failures, [])
        self.assertIn("[WARN] thing bad", out[0])
        self.assertNotIn("[FAIL]", out[0])


class RunStepsControlFlow(unittest.TestCase):
    def test_stop_if_failed_skips_the_remaining_steps(self):
        calls = []
        def second():
            calls.append("second")
            return ("second", True, "")
        with unittest.mock.patch("builtins.print"):
            run_steps([
                Step(lambda: ("first", False, ""), stop_if_failed=True),
                Step(second),
            ])
        self.assertEqual(calls, [])

    def test_a_step_returning_none_prints_nothing_and_is_skipped(self):
        out = []
        with unittest.mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            failures = run_steps([Step(lambda: None), Step(lambda: ("after", True, ""))])
        self.assertEqual(len(out), 1)
        self.assertIn("after", out[0])
        self.assertEqual(failures, [])

    def test_a_four_tuple_result_overrides_the_steps_own_fatal_flag(self):
        """Android's accessibility-tree check needs this: the exception
        branch is non-fatal even though the step's default is fatal=True."""
        out = []
        with unittest.mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            failures = run_steps([Step(lambda: ("tree", False, "known limitation", False),
                                        fatal=True)])
        self.assertEqual(failures, [])
        self.assertIn("[WARN] tree", out[0])

    def test_a_raising_step_is_reported_as_a_failure_instead_of_crashing(self):
        """This is GitHub issue #4's first bug, generalized: any step whose
        body raises (not just the ApplicationServices import) must become
        a clean FAIL line instead of an uncaught traceback."""
        def boom():
            raise ModuleNotFoundError("No module named 'ApplicationServices'")
        out = []
        with unittest.mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            failures = run_steps([Step(boom, fatal=True)])
        self.assertEqual(failures, ["boom"])
        self.assertIn("[FAIL] boom", out[0])
        self.assertIn("ApplicationServices", out[0])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_admin -v`
Expected: `ImportError: cannot import name 'Step' from 'phone_harness.admin'` (neither `Step` nor `run_steps` exist yet).

- [ ] **Step 3: Implement `Step` and `run_steps`**

In `src/phone_harness/admin.py`, add after the existing imports (keep the module docstring and `import os/shutil/subprocess/sys/tempfile/Path` as they are):

```python
from dataclasses import dataclass
from typing import Callable


@dataclass
class Step:
    """One `--doctor` check.

    `fn` takes no arguments and returns `None` (this step doesn't apply
    right now — print nothing), a 3-tuple `(label, ok, hint)`, or a
    4-tuple `(label, ok, hint, fatal)` that overrides this Step's own
    `fatal` for this particular result.
    """
    fn: Callable[[], object]
    fatal: bool = True
    stop_if_failed: bool = False


def run_steps(steps):
    """Run a `--doctor` check ladder in order.

    Args:
        steps: The `Step`s to run, in the order they should be checked.

    Returns:
        The labels of every step that failed with `fatal` true — this is
        what the overall doctor verdict is based on.
    """
    failures = []
    for step in steps:
        try:
            result = step.fn()
        except Exception as e:
            # A step whose body crashes (a bad import, an unguarded
            # attribute access) is exactly what a doctor check exists to
            # report — never let it become an uncaught traceback.
            result = (getattr(step.fn, "__name__", "check"), False,
                       f"unexpected error: {e}")
        if result is None:
            continue
        label, ok, hint, *rest = result
        fatal = rest[0] if rest else step.fatal
        mark = "PASS" if ok else ("FAIL" if fatal else "WARN")
        print(f"  [{mark}] {label}" + (f" — {hint}" if not ok and hint else ""))
        if not ok and fatal:
            failures.append(label)
        if not ok and step.stop_if_failed:
            break
    return failures
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_admin -v`
Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/phone_harness/admin.py tests/test_admin.py
git commit -m "admin: add Step/run_steps, a declarative --doctor check runner

Unused by _doctor_ios/_doctor_android yet (next tasks). Exceptions inside
a step's fn are caught and reported as a FAIL instead of propagating,
and a step's tag is derived from (ok, fatal) in one place so a
fatal=False step can never print [FAIL]. Both fix the general class of
bug behind issue #4, ahead of rewiring the ios/android ladders onto it.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: Rewrite `_doctor_ios()` onto `run_steps()`

**Files:**
- Modify: `src/phone_harness/admin.py:70-150` (the whole current `_doctor_ios` body)
- Test: `tests/test_admin.py` (append)

**Interfaces:**
- Consumes: `Step`, `run_steps` from Task 1.
- Produces: `_doctor_ios() -> list[str]` — now returns the failures list instead of relying on the module-level `_failures` global (Task 4 wires this into `run_doctor`). Signature change is internal-only; nothing outside `admin.py` calls `_doctor_ios` (confirmed via `grep -rn "_doctor_ios" src/`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_admin.py` (add `from unittest import mock` to the top-level imports alongside `import unittest`, and `import phone_harness.admin as admin` instead of only importing `Step, run_steps`):

```python
from unittest import mock

import phone_harness.admin as admin
from phone_harness.admin import Step, run_steps  # noqa: E402


class DoctorIosPyobjcGuard(unittest.TestCase):
    """GitHub issue #4, bug #1: a missing ApplicationServices import must
    report a FAIL, not crash _doctor_ios before the PASS line even makes
    sense."""

    def test_a_missing_pyobjc_module_is_a_single_fail_not_a_crash(self):
        real_import = __import__
        def fake_import(name, *a, **kw):
            if name == "ApplicationServices":
                raise ModuleNotFoundError("No module named 'ApplicationServices'")
            return real_import(name, *a, **kw)
        out = []
        with mock.patch("builtins.__import__", side_effect=fake_import), \
             mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            failures = admin._doctor_ios()
        self.assertEqual(len(failures), 1)
        self.assertIn("pyobjc frameworks", failures[0])
        self.assertTrue(any("[FAIL] pyobjc frameworks" in l for l in out))
        # And nothing past the pyobjc check ran:
        self.assertEqual(len(out), 1)


class DoctorIosNonFatalTag(unittest.TestCase):
    """GitHub issue #4, bug #2: a non-fatal check (Mirroring not running
    yet) must print WARN, never FAIL, and must not block "all clear"."""

    def test_mirroring_not_running_prints_warn_and_is_non_fatal(self):
        out = []
        with mock.patch.object(admin, "Quartz", create=True), \
             mock.patch("Quartz.CGPreflightScreenCaptureAccess", return_value=True, create=True), \
             mock.patch("ApplicationServices.AXIsProcessTrusted", return_value=True, create=True), \
             mock.patch("phone_harness.mirror.APP_PATH", "/Applications/Mirroring.app"), \
             mock.patch("phone_harness.mirror.APP_NAME", "Mirroring"), \
             mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("phone_harness.mirror.running_app", return_value=None), \
             mock.patch("phone_harness.mirror.find_window", return_value=None), \
             mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            admin._doctor_ios()
        self.assertTrue(any("[WARN]" in l and "running" in l for l in out))
        self.assertFalse(any("[FAIL]" in l and "running" in l for l in out))


class DoctorIosLadder(unittest.TestCase):
    """The capture->OCR->session-state tail, exercised with every real
    side effect mocked so it runs the same on any OS."""

    def _patched(self, **extra):
        win = {"id": 42}
        patches = {
            "phone_harness.mirror.APP_PATH": "/Applications/Mirroring.app",
            "phone_harness.mirror.APP_NAME": "Mirroring",
        }
        patches.update(extra)
        return win, patches

    def test_a_good_capture_runs_ocr_then_checks_session_state(self):
        win = {"id": 42}
        out = []
        with mock.patch("ApplicationServices.AXIsProcessTrusted", return_value=True, create=True), \
             mock.patch("Quartz.CGPreflightScreenCaptureAccess", return_value=True, create=True), \
             mock.patch("phone_harness.mirror.APP_PATH", "/Applications/Mirroring.app"), \
             mock.patch("phone_harness.mirror.APP_NAME", "Mirroring"), \
             mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("phone_harness.mirror.running_app", return_value=object()), \
             mock.patch("phone_harness.mirror.find_window", return_value=win), \
             mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)), \
             mock.patch("os.path.getsize", return_value=50_000), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch("os.unlink"), \
             mock.patch("phone_harness.ocr.recognize", return_value=[1, 2, 3]), \
             mock.patch("phone_harness.ios.IPhone") as iphone_cls, \
             mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            iphone_cls.return_value.send.return_value = "ready"
            failures = admin._doctor_ios()
        self.assertEqual(failures, [])
        self.assertTrue(any("Vision OCR works (3 text boxes)" in l for l in out))
        self.assertTrue(any("session state: ready" in l for l in out))

    def test_a_bad_capture_skips_ocr_but_still_checks_session_state(self):
        win = {"id": 42}
        out = []
        with mock.patch("ApplicationServices.AXIsProcessTrusted", return_value=True, create=True), \
             mock.patch("Quartz.CGPreflightScreenCaptureAccess", return_value=True, create=True), \
             mock.patch("phone_harness.mirror.APP_PATH", "/Applications/Mirroring.app"), \
             mock.patch("phone_harness.mirror.APP_NAME", "Mirroring"), \
             mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("phone_harness.mirror.running_app", return_value=object()), \
             mock.patch("phone_harness.mirror.find_window", return_value=win), \
             mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)), \
             mock.patch("os.path.getsize", return_value=10), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch("os.unlink"), \
             mock.patch("phone_harness.ocr.recognize") as ocr_recognize, \
             mock.patch("phone_harness.ios.IPhone") as iphone_cls, \
             mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            iphone_cls.return_value.send.return_value = "ready"
            admin._doctor_ios()
        ocr_recognize.assert_not_called()
        self.assertFalse(any("Vision OCR" in l for l in out))
        self.assertTrue(any("session state: ready" in l for l in out))

    def test_no_window_stops_before_capture_and_session_state(self):
        out = []
        with mock.patch("ApplicationServices.AXIsProcessTrusted", return_value=True, create=True), \
             mock.patch("Quartz.CGPreflightScreenCaptureAccess", return_value=True, create=True), \
             mock.patch("phone_harness.mirror.APP_PATH", "/Applications/Mirroring.app"), \
             mock.patch("phone_harness.mirror.APP_NAME", "Mirroring"), \
             mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("phone_harness.mirror.running_app", return_value=object()), \
             mock.patch("phone_harness.mirror.find_window", return_value=None), \
             mock.patch("subprocess.run") as run, \
             mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            failures = admin._doctor_ios()
        run.assert_not_called()
        self.assertIn("mirroring window found", failures)
        self.assertFalse(any("session state" in l for l in out))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_admin -v`
Expected: `DoctorIosPyobjcGuard`, `DoctorIosNonFatalTag`, and `DoctorIosLadder` tests FAIL (the current `_doctor_ios` still imports `ApplicationServices` unguarded, still routes through the old `_check`/`_failures` global, and returns `None` instead of a list).

- [ ] **Step 3: Rewrite `_doctor_ios`**

Replace the entire current body of `_doctor_ios()` (`src/phone_harness/admin.py:70-150`) with:

```python
def _doctor_ios():
    """Walk the iPhone check ladder, stopping early once a step blocks the
    next one from meaning anything (e.g. no window -> nothing to capture).

    Ladder: pyobjc installed -> Accessibility + Screen Recording permissions
    -> Mirroring app installed/running -> mirroring window found -> a test
    window capture -> OCR on that capture -> iPhone session state.

    Returns:
        The labels of every fatal check that failed.
    """
    ctx = {}

    def check_pyobjc():
        try:
            # ApplicationServices lives here, in the same guarded import as
            # the other three frameworks, so a missing module is a FAIL
            # line instead of an uncaught crash on the next statement
            # (GitHub issue #4).
            import Quartz, Vision, AppKit, ApplicationServices  # noqa: F401
        except ImportError as e:
            return ("pyobjc frameworks", False,
                    f"pip install pyobjc-framework-Quartz pyobjc-framework-Vision "
                    f"pyobjc-framework-Cocoa ({e})")
        return ("pyobjc frameworks (Quartz, Vision, AppKit, ApplicationServices)",
                 True, "")

    def check_accessibility():
        from ApplicationServices import AXIsProcessTrusted
        return ("Accessibility permission (taps & keystrokes)", AXIsProcessTrusted(),
                "System Settings > Privacy & Security > Accessibility: enable your terminal")

    def check_screen_recording():
        import Quartz as Q
        return ("Screen Recording permission (seeing the phone)",
                bool(Q.CGPreflightScreenCaptureAccess()),
                "System Settings > Privacy & Security > Screen Recording: enable your terminal")

    def check_mirroring_installed():
        from . import mirror
        return (f"{mirror.APP_NAME} installed", Path(mirror.APP_PATH).exists(),
                "requires macOS Sequoia+ with a paired iPhone")

    def check_mirroring_running():
        from . import mirror
        # Not fatal: the app auto-launches on first real use.
        return (f"{mirror.APP_NAME} running", mirror.running_app() is not None,
                "will auto-launch on first use — not fatal")

    def check_window():
        from . import mirror
        win = mirror.find_window()
        ctx["win"] = win
        return ("mirroring window found", win is not None,
                "open iPhone Mirroring once manually to pair the phone")

    def check_capture():
        win = ctx["win"]
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
        # Never crash here: a missing Screen Recording grant is exactly
        # what this check exists to report.
        r = subprocess.run(["screencapture", "-x", "-o", "-l", str(win["id"]), path],
                           capture_output=True)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        # A capture with no Screen Recording grant still "succeeds" but
        # writes a near-empty/blank image, so size is the real signal.
        good = r.returncode == 0 and size > 20_000
        if good:
            ctx["capture_path"] = path  # handed to check_ocr; cleaned up there
        elif os.path.exists(path):
            os.unlink(path)
        return (f"window capture works ({size} bytes)", good,
                "capture failed or is blank — Screen Recording permission "
                "needs a terminal restart to take effect")

    def check_ocr():
        path = ctx.get("capture_path")
        if not path:
            # Nothing to OCR — print nothing rather than a misleading FAIL.
            return None
        win = ctx["win"]
        try:
            from . import ocr
            n = len(ocr.recognize(path, win))
            return (f"Vision OCR works ({n} text boxes)", True, "")
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def check_session_state():
        from . import ios
        state = ios.IPhone().send("session.state")
        # Not fatal: an interstitial (locked Mac, "iPhone in Use", etc.) is
        # a transient state the user clears themselves, not a setup defect.
        return (f"session state: {state}", state == "ready",
                "an interstitial is up (iPhone in Use / Connect / Mac Locked) — "
                "clear it on the Mac; lock the iPhone if it says in use")

    return run_steps([
        Step(check_pyobjc, stop_if_failed=True),
        Step(check_accessibility),
        Step(check_screen_recording),
        Step(check_mirroring_installed),
        Step(check_mirroring_running, fatal=False),
        Step(check_window, stop_if_failed=True),
        Step(check_capture),
        Step(check_ocr),
        Step(check_session_state, fatal=False),
    ])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_admin -v`
Expected: all tests PASS, including the Task 1 tests (no regression).

- [ ] **Step 5: Commit**

```bash
git add src/phone_harness/admin.py tests/test_admin.py
git commit -m "admin: rewrite _doctor_ios onto run_steps, fixing issue #4

- ApplicationServices now imports inside the same guarded block as
  Quartz/Vision/AppKit, so a missing module is a FAIL line instead of
  an uncaught ModuleNotFoundError right after the pyobjc PASS line.
- Non-fatal checks (Mirroring running, session state) now print WARN,
  never FAIL, so 'all clear' can't appear under a visible FAIL line.
- _doctor_ios now returns its failures list instead of mutating a
  module-level global (wired into run_doctor in the next commit).

Fixes ShawnPana/phone-harness#4.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: Rewrite `_doctor_android()` onto `run_steps()`

**Files:**
- Modify: `src/phone_harness/admin.py:107-147` (current `_doctor_android` body — line numbers shift after Task 2's edit; locate by the `def _doctor_android():` header)
- Test: `tests/test_admin.py` (append)

**Interfaces:**
- Consumes: `Step`, `run_steps` from Task 1.
- Produces: `_doctor_android() -> list[str]` — same shape change as `_doctor_ios` in Task 2. Nothing outside `admin.py` calls it (confirmed via `grep -rn "_doctor_android" src/`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_admin.py`:

```python
class DoctorAndroidLadder(unittest.TestCase):
    def test_adb_missing_stops_before_anything_else_runs(self):
        out = []
        with mock.patch("phone_harness.config.get", return_value="adb"), \
             mock.patch("shutil.which", return_value=None), \
             mock.patch("phone_harness.android.Android") as android_cls, \
             mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            failures = admin._doctor_android()
        android_cls.assert_not_called()
        self.assertEqual(failures, ["adb found (adb)"])
        self.assertEqual(len(out), 1)

    def test_a_ready_phone_runs_the_full_ladder(self):
        out = []
        phone = mock.Mock()
        phone.send.side_effect = lambda op: {
            "session.state": "ready",
            "screen.bounds": {"id": "dev1", "w": 1080, "h": 2400},
            "tree": [1, 2, 3],
        }[op]
        with mock.patch("phone_harness.config.get", return_value="adb"), \
             mock.patch("shutil.which", side_effect=lambda b: "/usr/bin/adb" if b == "adb" else None), \
             mock.patch("phone_harness.android.Android", return_value=phone), \
             mock.patch("phone_harness.android._phone_label", return_value="Pixel"), \
             mock.patch("phone_harness.config.devices_of",
                        return_value={"phones": ["Pixel"], "primary": "Pixel"}), \
             mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            failures = admin._doctor_android()
        self.assertEqual(failures, [])
        self.assertTrue(any("talking to Pixel (dev1)" in l for l in out))
        self.assertTrue(any("accessibility tree readable (3 nodes)" in l for l in out))
        self.assertTrue(any("remembered phones: Pixel; primary: Pixel" in l for l in out))

    def test_a_locked_phone_warns_and_skips_bounds_and_tree(self):
        out = []
        phone = mock.Mock()
        phone.send.return_value = "locked"
        with mock.patch("phone_harness.config.get", return_value="adb"), \
             mock.patch("shutil.which", side_effect=lambda b: "/usr/bin/adb" if b == "adb" else None), \
             mock.patch("phone_harness.android.Android", return_value=phone), \
             mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            failures = admin._doctor_android()
        self.assertTrue(any("[WARN] phone unlocked" in l for l in out))
        self.assertFalse(any("talking to" in l for l in out))
        self.assertEqual(failures, [])  # "locked" counts as reachable; WARN isn't fatal

    def test_a_tree_read_error_is_reported_as_warn_not_fail(self):
        out = []
        phone = mock.Mock()
        def send(op):
            if op == "session.state":
                return "ready"
            if op == "screen.bounds":
                return {"id": "dev1", "w": 1080, "h": 2400}
            raise RuntimeError("uiautomator refused: screen never settled")
        phone.send.side_effect = send
        with mock.patch("phone_harness.config.get", return_value="adb"), \
             mock.patch("shutil.which", side_effect=lambda b: "/usr/bin/adb" if b == "adb" else None), \
             mock.patch("phone_harness.android.Android", return_value=phone), \
             mock.patch("phone_harness.android._phone_label", return_value="Pixel"), \
             mock.patch("phone_harness.config.devices_of",
                        return_value={"phones": ["Pixel"], "primary": "Pixel"}), \
             mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
            failures = admin._doctor_android()
        self.assertTrue(any("[WARN] accessibility tree readable" in l for l in out))
        self.assertNotIn("accessibility tree readable", failures)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_admin -v`
Expected: `DoctorAndroidLadder` tests FAIL (current `_doctor_android` still uses the old `_check`/global and returns `None`).

- [ ] **Step 3: Rewrite `_doctor_android`**

Replace the entire current body of `_doctor_android()` with:

```python
def _doctor_android():
    """Walk the Android check ladder, stopping early once a step blocks the
    next one from meaning anything (e.g. no adb -> no phone to talk to).

    Ladder: adb found -> (optional) scrcpy found -> a phone reachable and
    in a ready/locked state -> screen bounds readable -> accessibility
    tree readable -> list of remembered phones.

    Returns:
        The labels of every fatal check that failed.
    """
    ctx = {}

    def check_adb():
        from . import config
        adb = str(config.get("android.adb"))
        found = shutil.which(adb)
        if not found:
            return (f"adb found ({adb})", False,
                    _ADB_INSTALL + ", or set android.adb to the binary")
        return (f"adb found ({found})", True, "")

    def check_scrcpy():
        return ("scrcpy found (optional: live mirror during `android awake`)",
                bool(shutil.which("scrcpy")), _SCRCPY_INSTALL + " — not required")

    def check_reachable():
        from . import android
        phone = android.Android()
        ctx["phone"] = phone
        state = phone.send("session.state")
        ctx["state"] = state
        # Human-readable fix for each adb/device state the backend reports.
        hints = {
            "no-device": "plug in with USB debugging on and tap Allow, or "
                         "Wireless debugging + `phone-harness android pair CODE`",
            "unauthorized": "tap Allow on the phone's 'Allow USB debugging?' prompt",
            "offline": "unplug/replug, or toggle Wireless debugging off and on",
            "locked": "unlock the phone (`phone-harness android awake` keeps it so)",
            "no-adb": "adb did not answer",
        }
        ctx["hints"] = hints
        # "locked" counts as reachable (adb still works on a locked phone);
        # check_unlocked below reports that case on its own line.
        return (f"a phone is reachable and ready (state: {state})",
                state in ("ready", "locked"), hints.get(state, ""))

    def check_unlocked():
        if ctx.get("state") != "locked":
            return None  # only relevant when the phone is actually locked
        return ("phone unlocked", False, ctx["hints"]["locked"], False)  # non-fatal

    def check_screen_bounds():
        from . import android
        b = ctx["phone"].send("screen.bounds")
        ctx["bounds"] = b
        label = android._phone_label(b["id"]) if b else "?"
        return (f"talking to {label} ({b['id']}), screen {b['w']}x{b['h']}", bool(b), "")

    def check_tree():
        try:
            n = len(ctx["phone"].send("tree"))
            return (f"accessibility tree readable ({n} nodes)", n > 0, "")
        except RuntimeError as e:
            # Some screens (constantly animating) make uiautomator refuse
            # the tree; that's a known limitation, not a setup problem.
            return ("accessibility tree readable", False, str(e)[:120], False)

    def check_remembered_phones():
        from . import config
        reg = config.devices_of("android")
        return (f"remembered phones: {', '.join(reg['phones']) or 'none'}; primary: "
                f"{reg['primary'] or 'none'}", True, "")

    failures = run_steps([
        Step(check_adb, stop_if_failed=True),
        Step(check_scrcpy, fatal=False),
        Step(check_reachable),
        Step(check_unlocked, fatal=False),
    ])
    if ctx.get("state") != "ready":
        # Anything other than fully "ready" (locked, unauthorized, etc.)
        # means screen/tree calls below would just fail too.
        return failures
    return failures + run_steps([
        Step(check_screen_bounds),
        Step(check_tree),
        Step(check_remembered_phones),
    ])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_admin -v`
Expected: all tests PASS, including Tasks 1-2 (no regression).

- [ ] **Step 5: Commit**

```bash
git add src/phone_harness/admin.py tests/test_admin.py
git commit -m "admin: rewrite _doctor_android onto run_steps

Same shape as the ios rewrite: returns its failures list instead of a
module-level global, and the locked-phone / tree-read-error warnings
now print WARN via run_steps' tagging instead of a hand-rolled fatal=False
_check() call.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4: Wire `run_doctor` to the returned failures; delete the old `_check`/`_failures`

**Files:**
- Modify: `src/phone_harness/admin.py` (the `run_doctor` function, and delete the now-unused `_check` function + `_failures` global)
- Test: none new — this task's correctness is proven by the full existing suite (`tests/test_cli.py`'s doctor-dispatch tests) plus Tasks 1-3's tests still passing.

**Interfaces:**
- Consumes: `_doctor_ios() -> list[str]`, `_doctor_android() -> list[str]` from Tasks 2-3.
- Produces: `run_doctor(platform=None) -> int` — same public contract as before, body changed to use the returned list instead of the `_failures` global.

- [ ] **Step 1: Confirm the current failing state**

Run: `python -m unittest discover tests -v 2>&1 | tail -20`
Expected: `tests/test_admin.py` all green (from Tasks 1-3); `tests/test_cli.py`'s doctor tests still green too, since they mock `run_doctor` itself and never touch `_check`/`_failures` — but `run_doctor` is currently dead code with respect to `_failures` (the global is set by the *old* `_check`, which no longer exists after Tasks 2-3 rewired `_doctor_ios`/`_doctor_android` to stop calling it). This step is a checkpoint, not a new failing test: `grep -n "_failures\|def _check" src/phone_harness/admin.py` should show the leftover dead code to remove next.

- [ ] **Step 2: Update `run_doctor` and delete the dead code**

In `src/phone_harness/admin.py`, replace:

```python
def run_doctor(platform=None):
    """Entry point for `phone-harness --doctor [ios|android]`.

    Runs the ios or android check ladder and prints a final verdict line.

    Args:
        platform: `"ios"` or `"android"` to check a specific phone type;
            `None` to check whichever the config default is set to.

    Returns:
        Process exit code: 0 if every fatal check passed, 1 otherwise.
    """
    from . import config
    platform = (platform or config.get("platform")).lower()
    # Note when the caller asked for a platform other than the configured
    # default, so the output is clear about which one is "normal" for them.
    print(f"phone-harness doctor — {platform}"
          f"{'' if platform == config.get('platform') else '  (default is ' + config.get('platform') + ')'}\n")
    _failures.clear()
    if platform == "android":
        _doctor_android()
    else:
        _doctor_ios()
    ok = not _failures
    print("\nall clear" if ok else "\nfix the FAILs above, then re-run")
    return 0 if ok else 1
```

with:

```python
def run_doctor(platform=None):
    """Entry point for `phone-harness --doctor [ios|android]`.

    Runs the ios or android check ladder and prints a final verdict line.

    Args:
        platform: `"ios"` or `"android"` to check a specific phone type;
            `None` to check whichever the config default is set to.

    Returns:
        Process exit code: 0 if every fatal check passed, 1 otherwise.
    """
    from . import config
    platform = (platform or config.get("platform")).lower()
    # Note when the caller asked for a platform other than the configured
    # default, so the output is clear about which one is "normal" for them.
    print(f"phone-harness doctor — {platform}"
          f"{'' if platform == config.get('platform') else '  (default is ' + config.get('platform') + ')'}\n")
    failures = _doctor_android() if platform == "android" else _doctor_ios()
    ok = not failures
    print("\nall clear" if ok else "\nfix the FAILs above, then re-run")
    return 0 if ok else 1
```

Then delete the now-unused `_check` function and the `_failures = []` global entirely (both sit between `run_doctor` and the `# --- iPhone ---` comment in the current file — confirm nothing else references them first with `grep -rn "_check\b\|_failures" src/phone_harness/admin.py`, which should only show the definitions themselves after Tasks 2-3).

- [ ] **Step 3: Run the full suite to verify no regression**

Run: `python -m unittest discover tests -v`
Expected: every test passes, including `tests/test_cli.py`'s `test_doctor_alias_still_dispatches_and_help_does_not` (unchanged — it mocks `run_doctor` at the boundary) and all of `tests/test_admin.py` from Tasks 1-3.

- [ ] **Step 4: Sanity-check the public contract by hand**

Run: `python -c "import sys; sys.path.insert(0, 'src'); from phone_harness.admin import run_doctor; import inspect; print(inspect.signature(run_doctor))"`
Expected: `(platform=None)` — unchanged from before the refactor.

- [ ] **Step 5: Commit**

```bash
git add src/phone_harness/admin.py
git commit -m "admin: wire run_doctor to the returned failures list

_doctor_ios/_doctor_android now return their own failures instead of
mutating a module-level global; run_doctor just sums what it's handed.
Deletes the old _check() and _failures global, both now unused.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage** (against GitHub issue #4 and the conversation's design):
- Bug #1 (unguarded `ApplicationServices` import crashes past a misleading PASS) → Task 2's `check_pyobjc`, plus the generic `try/except` in `run_steps` (Task 1) as a structural backstop for *any* future unguarded step body.
- Bug #2 (`[FAIL]` printed for non-fatal checks, contradicting "all clear") → Task 1's single-source `mark = "PASS" if ok else ("FAIL" if fatal else "WARN")`.
- Global mutable `_failures` state → removed in Task 4; each doctor function now returns its own list.
- Declarative, testable ladder for both platforms → Tasks 2-3.
- `run_doctor`'s public contract preserved → Task 4, verified by the existing `tests/test_cli.py` doctor-dispatch tests plus a direct signature check.

**Placeholder scan:** no `TBD`/`implement later`/`add appropriate handling` language anywhere above; every step has runnable code, not a description of code.

**Type/name consistency:** `Step(fn, fatal=True, stop_if_failed=False)` and `run_steps(steps) -> list[str]` are defined once in Task 1 and used with those exact names and shapes in Tasks 2-3; `_doctor_ios()`/`_doctor_android()` both return `list[str]` consistently, consumed identically by `run_doctor` in Task 4.

---

## Execution Handoff

Two execution options:

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — execute tasks in this session using `executing-plans`, batch execution with checkpoints for review.

Which approach?
