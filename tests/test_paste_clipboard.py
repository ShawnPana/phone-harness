"""paste_with must not leave typed text (passwords, tokens) on the Mac clipboard.

pbcopy/pbpaste only exist on macOS, so subprocess.run is replaced with a fake
clipboard. What is checked is the sequence the real clipboard would see.
"""
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

import importlib.machinery
import types

# mirror.py imports macOS frameworks at module level; stub them off-Mac.
for _name in ("Quartz", "AppKit", "ApplicationServices", "Foundation", "objc"):
    if _name not in sys.modules:
        _mod = types.ModuleType(_name)
        _mod.__spec__ = importlib.machinery.ModuleSpec(_name, loader=None)
        _mod.__getattr__ = lambda name, _n=_name: type(name, (), {})
        sys.modules[_name] = _mod

from phone_harness import config, mirror


class FakeClipboard:
    def __init__(self, contents=b""):
        self.contents = contents
        self.broken = False

    def run(self, cmd, input=None, capture_output=False, **kw):
        if cmd == ["pbpaste"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=self.contents)
        if cmd == ["pbcopy"]:
            if not self.broken:
                self.contents = input
            return subprocess.CompletedProcess(cmd, 0)
        raise AssertionError(f"unexpected command {cmd}")


class PasteWith(unittest.TestCase):
    def paste(self, clip, text="hunter2", settings=None, during_paste=None):
        settings = {"ios.paste_settle": 0, **(settings or {})}
        presses = []

        def press(key):
            presses.append((key, clip.contents))
            if during_paste:
                during_paste()

        def get(key, default=None):
            return settings.get(key, _default(key))

        with mock.patch.object(mirror.subprocess, "run", clip.run), \
             mock.patch.object(mirror.time, "sleep"), \
             mock.patch.object(config, "get", get):
            mirror.paste_with(press, text)
        return presses

    def test_text_is_on_clipboard_when_cmd_v_is_pressed(self):
        clip = FakeClipboard(b"old")
        presses = self.paste(clip)
        self.assertEqual(presses, [("cmd+v", b"hunter2")])

    def test_secret_does_not_remain_on_clipboard_by_default(self):
        clip = FakeClipboard(b"old")
        self.paste(clip)
        self.assertEqual(clip.contents, b"old")

    def test_empty_prior_clipboard_is_restored_empty(self):
        clip = FakeClipboard(b"")
        self.paste(clip)
        self.assertNotIn(b"hunter2", clip.contents)

    def test_opt_out_keeps_old_behaviour(self):
        clip = FakeClipboard(b"old")
        self.paste(clip, settings={"ios.restore_clipboard": False})
        self.assertEqual(clip.contents, b"hunter2")

    def test_does_not_clobber_something_the_user_copied_meanwhile(self):
        clip = FakeClipboard(b"old")
        self.paste(clip, during_paste=lambda: setattr(clip, "contents", b"user copy"))
        self.assertEqual(clip.contents, b"user copy")

    def test_restored_even_if_press_raises(self):
        clip = FakeClipboard(b"old")

        def boom(key):
            raise RuntimeError("press failed")

        with mock.patch.object(mirror.subprocess, "run", clip.run), \
             mock.patch.object(mirror.time, "sleep"), \
             mock.patch.object(config, "get", lambda k, d=None: 0 if k == "ios.paste_settle" else _default(k)):
            with self.assertRaises(RuntimeError):
                mirror.paste_with(boom, "hunter2")
        self.assertEqual(clip.contents, b"old")

    def test_failed_copy_raises_and_never_presses(self):
        clip = FakeClipboard(b"old")
        clip.broken = True
        with self.assertRaises(RuntimeError):
            self.paste(clip)


def _default(key):
    section, name = key.split(".")
    return config.DEFAULTS[section][name]


if __name__ == "__main__":
    unittest.main()
