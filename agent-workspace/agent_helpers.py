"""Agent-editable phone helpers.

Add task-specific primitives here. Core helpers from phone_harness.helpers
load this file at import time; anything defined here is available in
phone-harness scripts alongside the core helpers.
"""

from __future__ import annotations

import subprocess
import time


def tap_icon(label, index=0):
    """Tap a Home-Screen app icon by its label.

    Learned: tapping the label text itself does NOT launch the app in the
    mirrored Home Screen — the tappable icon is ~35 points above the label.
    Verified against Weather (label tap: no-op; icon tap: launches).

    Only works when OCR can read the label (often fails for Chinese names).
    For Chinese icons, screenshot + visual geometry tap instead.
    """
    from phone_harness.helpers import find_text, tap

    hits = find_text(label)
    if not hits:
        raise RuntimeError(f"no Home-Screen label matching {label!r}")
    h = hits[index]
    tap(h["x"], h["y"] - 35)
    return h


def is_home_edit_mode(items=None):
    """True if springboard jiggle/edit UI is visible (完成/编辑/⊖ style chrome)."""
    from phone_harness.helpers import ocr

    if items is None:
        items = ocr(min_confidence=0.2)
    texts = " ".join((it.get("text") or "") for it in items)
    # Chinese Done/Edit and common OCR mangling of those buttons
    markers = ("完成", "编辑", "Done", "Edit", "Remove", "移除")
    if any(m in texts for m in markers):
        # Avoid false positive on random in-app "Edit" alone — require
        # springboard-ish combo or Chinese 完成 with other chrome.
        if "完成" in texts or "编辑" in texts:
            return True
        if "Done" in texts and ("Edit" in texts or "Remove" in texts):
            return True
    return False


def exit_home_edit_mode():
    """Leave Home Screen jiggle/edit mode. NEVER tap red minus / delete.

    Prefer 完成/Done; fall back to top-right tap then home().
    """
    from phone_harness.helpers import (
        find_text,
        home,
        ocr,
        screen_info,
        screenshot,
        tap,
        wait,
    )

    items = ocr(min_confidence=0.2)
    for label in ("完成", "Done"):
        hits = find_text(label)
        if hits:
            tap(hits[0]["x"], hits[0]["y"])
            wait(0.8)
            return {"how": f"tap_text:{label}", "hit": hits[0]}

    # Top-right Done region (common layout in jiggle mode)
    w = screen_info()["window"]
    tap(w["x"] + w["w"] * 0.88, w["y"] + w["h"] * 0.08)
    wait(0.6)
    home()
    wait(0.8)
    path = screenshot()
    return {"how": "top_right_then_home", "screenshot": path}


def paste_text(text: str):
    """Paste arbitrary Unicode (incl. Chinese) into the focused iOS field.

    Sets the Mac clipboard then sends cmd+v through the mirroring keyboard
    path. Requires Continuity / paste into the mirrored field to work; always
    verify with screenshot/ocr after calling. Briefly steals focus (keyboard).
    """
    from phone_harness.helpers import press

    if text is None:
        raise ValueError("paste_text requires text")
    # pbcopy expects bytes on stdin
    p = subprocess.run(
        ["pbcopy"],
        input=text.encode("utf-8"),
        capture_output=True,
    )
    if p.returncode != 0:
        raise RuntimeError(
            f"pbcopy failed: {p.stderr.decode('utf-8', errors='replace')}"
        )
    time.sleep(0.15)
    press("cmd+v")
    time.sleep(0.35)
    return True


def open_app_latin(name: str):
    """Open an app via Spotlight using ASCII/Latin only.

    Same as open_app(), but fails fast if `name` is not typeable via keycodes
    (e.g. Chinese). Use brand Latin names: dianping, meituan, wechat, …
    """
    from phone_harness.helpers import open_app
    from phone_harness import mirror as mirror_mod

    # Validate against US-layout keycodes before focusing Spotlight.
    # Always use phone_harness.mirror (background module has no _keycode_for).
    for ch in name:
        if ch == "\n":
            continue
        code, _ = mirror_mod._keycode_for(ch)
        if code is None:
            raise ValueError(
                f"open_app_latin cannot type {ch!r} in {name!r}; "
                "use ASCII brand/pinyin (e.g. 'dianping') or screenshot+tap"
            )
    return open_app(name)


def mirror_looks_live():
    """Heuristic: screenshot is not a dead/black stream.

    Returns dict with ok bool and stats. Does not replace viewing the image
    for welcome screens — still screenshot+view after user says connected.
    Pure black captures are typically tiny (~30KB); live UI is much larger.
    """
    import os

    from phone_harness.helpers import screenshot

    try:
        path = screenshot()
    except Exception as e:
        return {"ok": False, "path": None, "bytes": 0, "reason": f"capture_failed:{e}"}

    size = os.path.getsize(path) if path and os.path.exists(path) else 0
    ok = size > 80000
    return {
        "ok": ok,
        "path": path,
        "bytes": size,
        "reason": None if ok else "screenshot_too_small_likely_black_or_empty",
    }
