"""The agent-facing targets/marks helpers, with a fake phone behind them."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# helpers.py connects at import time. Point it at the adb backend so the test
# does not need an iPhone or a real phone; no op is sent unless it is mocked.
_old_platform = os.environ.get("PHONE_HARNESS_PLATFORM")
os.environ["PHONE_HARNESS_PLATFORM"] = "android"
try:
    import phone_harness.helpers as h  # noqa: E402
    from phone_harness.transport import TreeUnavailable  # noqa: E402
finally:
    if _old_platform is None:
        del os.environ["PHONE_HARNESS_PLATFORM"]
    else:
        os.environ["PHONE_HARNESS_PLATFORM"] = _old_platform

from PIL import Image  # noqa: E402

WINDOW = {"x": 0, "y": 0, "w": 1000, "h": 2000}


def target(target_id, label, kind="button", x=100, y=100, w=100, h=40,
           enabled=True, source="tree"):
    return {
        "id": target_id, "label": label, "label_source": "text",
        "kind": kind, "role": "x", "x": x, "y": y, "w": w, "h": h,
        "source": source, "enabled": enabled, "long_clickable": False,
        "scrollable": False, "checked": None, "selected": False,
    }


def mark_set(items, **kw):
    mark = {
        "schema": 1, "mark_id": "m12345", "path": "/tmp/not-used.png",
        "targets": items, "img_px": [1000, 2000], "window": dict(WINDOW),
        "app": None, "orientation": "portrait", "coverage": "tree",
        "complete": True, "omitted": 0, "created_at": time.time(),
    }
    mark.update(kw)
    return mark


class Targets(unittest.TestCase):
    def test_filters_keep_the_original_ids(self):
        items = [target(1, "Search", "icon"), target(2, "Settings")]
        with mock.patch.object(h, "_read_targets", return_value=items):
            self.assertEqual([t["id"] for t in h.targets(kind="icon")], [1])
            self.assertEqual([t["id"] for t in h.targets("set")], [2])
            self.assertEqual(
                [t["id"] for t in h.targets("Search", exact=True)], [1])

    def test_a_tree_failure_falls_back_to_text(self):
        boxes = [{"text": "Sign in", "x": 500, "y": 500, "w": 100,
                  "h": 30, "source": "pixels"}]
        with mock.patch.object(h, "find_window", return_value=WINDOW), \
                mock.patch.object(h, "supports", return_value=True), \
                mock.patch.object(h, "ui",
                                  side_effect=TreeUnavailable("busy")), \
                mock.patch.object(h, "screen", return_value=boxes):
            items = h._read_targets()
        self.assertEqual([t["label"] for t in items], ["Sign in"])
        self.assertEqual(items[0]["source"], "pixels")


class TapTarget(unittest.TestCase):
    def test_a_unique_match_is_tapped(self):
        items = [target(1, "Search", "icon"), target(2, "Settings")]
        with mock.patch.object(h, "_read_targets", return_value=items), \
                mock.patch.object(h, "tap") as tap:
            hit = h.tap_target("Search")
        self.assertEqual(hit["id"], 1)
        tap.assert_called_once_with(100, 100)

    def test_ambiguity_is_refused(self):
        items = [target(1, "Search", "icon"),
                 target(2, "Search", x=300)]
        with mock.patch.object(h, "_read_targets", return_value=items):
            with self.assertRaises(h.AmbiguousTarget) as cm:
                h.tap_target("Search")
        self.assertEqual(len(cm.exception.candidates), 2)

    def test_index_chooses_an_ambiguous_match(self):
        items = [target(1, "Search", "icon"),
                 target(2, "Search", x=300)]
        with mock.patch.object(h, "_read_targets", return_value=items), \
                mock.patch.object(h, "tap") as tap:
            hit = h.tap_target("Search", index=1)
        self.assertEqual(hit["id"], 2)
        tap.assert_called_once_with(300, 100)

    def test_a_disabled_target_is_refused(self):
        items = [target(1, "Save", enabled=False)]
        with mock.patch.object(h, "_read_targets", return_value=items):
            with self.assertRaises(h.TargetDisabled):
                h.tap_target("Save")


class TapMark(unittest.TestCase):
    def test_it_taps_the_saved_center(self):
        marks = mark_set([target(1, "Search", "icon", x=200, y=300)])
        with mock.patch.object(h, "find_window",
                               return_value=dict(WINDOW, id="fake")), \
                mock.patch.object(h, "tap") as tap:
            hit = h.tap_mark(marks, 1)
        self.assertEqual(hit["id"], 1)
        tap.assert_called_once_with(200, 300)

    def test_an_old_mark_set_is_stale(self):
        marks = mark_set([target(1, "Search")],
                         created_at=time.time() - 120)
        with self.assertRaises(h.StaleMark):
            h.tap_mark(marks, 1)

    def test_a_changed_window_is_stale(self):
        marks = mark_set([target(1, "Search")])
        with mock.patch.object(h, "find_window",
                               return_value={"x": 0, "y": 0,
                                             "w": 900, "h": 2000}):
            with self.assertRaises(h.StaleMark):
                h.tap_mark(marks, 1)

    def test_a_changed_app_is_stale(self):
        marks = mark_set([target(1, "Search")], app="com.example")
        with mock.patch.object(h, "find_window", return_value=WINDOW), \
                mock.patch.object(h, "current_app",
                                  return_value="com.other"):
            with self.assertRaises(h.StaleMark):
                h.tap_mark(marks, 1)

    def test_a_disabled_target_is_refused(self):
        marks = mark_set([target(1, "Save", enabled=False)])
        with self.assertRaises(h.TargetDisabled):
            h.tap_mark(marks, 1)

    def test_an_unknown_id_is_refused(self):
        marks = mark_set([target(1, "Save")])
        with self.assertRaises(RuntimeError):
            h.tap_mark(marks, 99)


class MarkScreen(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.image = str(Path(self.tmp.name) / "screen.png")
        Image.new("RGBA", (200, 400), (255, 255, 255, 255)).save(self.image)
        self.window = {"x": 0, "y": 0, "w": 200, "h": 400, "id": "fake"}

    def clean_mark(self, marks):
        for path in (marks["path"], marks["path"] + ".json"):
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_it_returns_a_mark_set_and_writes_the_image(self):
        items = [target(1, "Search", "icon", x=100, y=100, w=40, h=40)]
        with mock.patch.object(h, "_read_targets", return_value=items), \
                mock.patch.object(h, "send",
                                  return_value=(self.image, self.window)), \
                mock.patch.object(h, "current_app",
                                  return_value="com.example"), \
                mock.patch.object(h.config, "run_dir",
                                  return_value=Path(self.tmp.name) / "run"):
            marks = h.mark_screen()
        self.addCleanup(self.clean_mark, marks)
        self.assertEqual(marks["schema"], 1)
        self.assertEqual(marks["targets"], items)
        self.assertEqual(marks["img_px"], [200, 400])
        self.assertEqual(marks["window"],
                         {"x": 0, "y": 0, "w": 200, "h": 400})
        self.assertEqual(marks["app"], "com.example")
        self.assertEqual(marks["coverage"], "tree")
        self.assertTrue(marks["complete"])
        self.assertEqual(marks["omitted"], 0)
        self.assertTrue(Path(marks["path"]).is_file())

    def test_a_text_only_read_is_marked_incomplete_on_android(self):
        items = [target(1, "Sign in", "text", source="pixels")]
        with mock.patch.object(h, "_read_targets", return_value=items), \
                mock.patch.object(h, "supports", return_value=True), \
                mock.patch.object(h, "send",
                                  return_value=(self.image, self.window)), \
                mock.patch.object(h, "current_app", return_value=None), \
                mock.patch.object(h.config, "run_dir",
                                  return_value=Path(self.tmp.name) / "run"):
            marks = h.mark_screen()
        self.addCleanup(self.clean_mark, marks)
        self.assertEqual(marks["coverage"], "text")
        self.assertFalse(marks["complete"])

    def test_iphone_text_is_complete_because_it_is_the_native_source(self):
        items = [target(1, "Sign in", "text", source="pixels")]
        with mock.patch.object(h, "_read_targets", return_value=items), \
                mock.patch.object(h, "supports", return_value=False), \
                mock.patch.object(h, "send",
                                  return_value=(self.image, self.window)), \
                mock.patch.object(h, "current_app", return_value=None), \
                mock.patch.object(h.config, "run_dir",
                                  return_value=Path(self.tmp.name) / "run"):
            marks = h.mark_screen()
        self.addCleanup(self.clean_mark, marks)
        self.assertEqual(marks["coverage"], "text")
        self.assertTrue(marks["complete"])

    def test_a_later_script_can_load_the_mark_set_by_path(self):
        items = [target(1, "Search", "icon", x=100, y=100, w=40, h=40)]
        with mock.patch.object(h, "_read_targets", return_value=items), \
                mock.patch.object(h, "send",
                                  return_value=(self.image, self.window)), \
                mock.patch.object(h, "current_app",
                                  return_value="com.example"), \
                mock.patch.object(h.config, "run_dir",
                                  return_value=Path(self.tmp.name) / "run"):
            marks = h.mark_screen()
        self.addCleanup(self.clean_mark, marks)
        with mock.patch.object(h, "find_window",
                               return_value=dict(self.window)), \
                mock.patch.object(h, "current_app",
                                  return_value="com.example"), \
                mock.patch.object(h, "tap") as tap:
            hit = h.tap_mark(marks["path"], 1)
        self.assertEqual(hit["id"], 1)
        tap.assert_called_once_with(100, 100)

    def test_a_later_script_can_load_the_mark_set_by_id(self):
        items = [target(1, "Search", "icon", x=100, y=100, w=40, h=40)]
        with mock.patch.object(h, "_read_targets", return_value=items), \
                mock.patch.object(h, "send",
                                  return_value=(self.image, self.window)), \
                mock.patch.object(h, "current_app",
                                  return_value="com.example"), \
                mock.patch.object(h.config, "run_dir",
                                  return_value=Path(self.tmp.name) / "run"):
            marks = h.mark_screen()
        self.addCleanup(self.clean_mark, marks)
        with mock.patch.object(h, "find_window",
                               return_value=dict(self.window)), \
                mock.patch.object(h, "current_app",
                                  return_value="com.example"), \
                mock.patch.object(h.config, "run_dir",
                                  return_value=Path(self.tmp.name) / "run"), \
                mock.patch.object(h, "tap") as tap:
            hit = h.tap_mark(marks["mark_id"], 1)
        self.assertEqual(hit["id"], 1)
        tap.assert_called_once_with(100, 100)


if __name__ == "__main__":
    unittest.main()
