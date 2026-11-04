"""Target extraction: tree nodes in, numbered tap targets out."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from phone_harness import targets as t  # noqa: E402

SCREEN = {"x": 0, "y": 0, "w": 1080, "h": 2400}


def node(cls="android.view.View", **kw):
    base = {
        "text": "", "desc": "", "id": "", "class": cls,
        "package": "com.example", "clickable": False,
        "long_clickable": False, "scrollable": False, "enabled": True,
        "selected": False, "checkable": False, "checked": None,
        "password": False, "depth": 0, "parent": None,
        "x": 100, "y": 100, "w": 100, "h": 40,
    }
    base.update(kw)
    return base


class TreeTargets(unittest.TestCase):
    def test_unlabeled_imagebutton_is_an_icon(self):
        items = t.from_tree(
            [node("android.widget.ImageButton", clickable=True)], screen=SCREEN)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "icon")
        self.assertIsNone(items[0]["label"])
        self.assertIsNone(items[0]["label_source"])

    def test_label_comes_from_desc(self):
        items = t.from_tree(
            [node("android.widget.ImageButton", clickable=True, desc="Search")],
            screen=SCREEN)
        self.assertEqual(items[0]["label"], "Search")
        self.assertEqual(items[0]["label_source"], "desc")

    def test_label_comes_from_resource_id(self):
        items = t.from_tree(
            [node("android.widget.ImageView", clickable=True,
                  id="com.example:id/btn_search")], screen=SCREEN)
        self.assertEqual(items[0]["label"], "search")
        self.assertEqual(items[0]["label_source"], "id_hint")
        self.assertEqual(items[0]["id_hint"], "search")

    def test_a_row_aggregates_child_text(self):
        nodes = [
            node("android.widget.LinearLayout", clickable=True,
                 x=300, y=300, w=600, h=100),
            node("android.widget.TextView", text="Title",
                 x=300, y=280, w=200, h=30, depth=1, parent=0),
            node("android.widget.TextView", text="Subtitle",
                 x=300, y=330, w=200, h=30, depth=1, parent=0),
        ]
        items = t.from_tree(nodes, screen=SCREEN)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["label"], "Title Subtitle")
        self.assertEqual(items[0]["label_source"], "text")

    def test_a_row_skips_clickable_child_text(self):
        nodes = [
            node("android.widget.LinearLayout", clickable=True,
                 x=300, y=300, w=600, h=100),
            node("android.widget.TextView", text="Title",
                 x=300, y=280, w=200, h=30, depth=1, parent=0),
            node("android.widget.ImageButton", clickable=True, desc="Favorite",
                 x=800, y=300, w=48, h=48, depth=1, parent=0),
        ]
        items = t.from_tree(nodes, screen=SCREEN)
        self.assertEqual([i["label"] for i in items], ["Title", "Favorite"])

    def test_disabled_targets_are_listed(self):
        items = t.from_tree(
            [node("android.widget.Button", text="Save", clickable=True,
                  enabled=False)], screen=SCREEN)
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0]["enabled"])

    def test_zero_size_and_off_screen_nodes_are_skipped(self):
        nodes = [
            node("android.widget.Button", text="Zero", clickable=True, w=0),
            node("android.widget.Button", text="Off", clickable=True,
                 x=2000),
            node("android.widget.Button", text="On", clickable=True),
        ]
        items = t.from_tree(nodes, screen=SCREEN)
        self.assertEqual([i["label"] for i in items], ["On"])

    def test_a_near_identical_wrapper_is_deduped(self):
        nodes = [
            node("android.widget.FrameLayout", clickable=True,
                 x=200, y=200, w=400, h=100),
            node("android.widget.TextView", text="Open", clickable=True,
                 x=200, y=200, w=400, h=100, depth=1, parent=0),
        ]
        items = t.from_tree(nodes, screen=SCREEN)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["label"], "Open")
        self.assertEqual(items[0]["role"], "android.widget.TextView")

    def test_reading_order_is_row_major(self):
        nodes = [
            node("android.widget.Button", text="A", clickable=True,
                 x=500, y=100),
            node("android.widget.Button", text="B", clickable=True,
                 x=100, y=100),
            node("android.widget.Button", text="C", clickable=True,
                 x=100, y=300),
        ]
        items = t.from_tree(nodes, screen=SCREEN)
        self.assertEqual([i["label"] for i in items], ["B", "A", "C"])
        self.assertEqual([i["id"] for i in items], [1, 2, 3])

    def test_system_ui_is_optional(self):
        system = node("android.widget.Button", text="Back", clickable=True,
                      package="com.android.systemui")
        self.assertEqual(t.from_tree([system], screen=SCREEN), [])
        self.assertEqual(
            len(t.from_tree([system], include_system=True, screen=SCREEN)), 1)

    def test_scroll_containers_are_not_tap_targets(self):
        full = node("android.widget.ScrollView", scrollable=True,
                    x=540, y=1200, w=1080, h=2400)
        self.assertEqual(t.from_tree([full], screen=SCREEN), [])
        inner = node("android.widget.ScrollView", scrollable=True,
                     x=540, y=1200, w=400, h=400)
        self.assertEqual(t.from_tree([inner], screen=SCREEN), [])

    def test_a_thin_full_width_unlabeled_divider_is_skipped(self):
        divider = node("android.widget.FrameLayout", clickable=True,
                       x=540, y=1000, w=1080, h=88)
        self.assertEqual(t.from_tree([divider], screen=SCREEN), [])

    def test_an_app_bar_and_its_title_wrapper_are_skipped(self):
        nodes = [
            node("android.widget.FrameLayout", clickable=True,
                 id="com.example:id/action_bar_container",
                 x=540, y=200, w=1080, h=401),
            node("android.widget.LinearLayout", clickable=True,
                 x=230, y=315, w=319, h=117, depth=1, parent=0),
            node("android.widget.TextView", text="Settings",
                 x=230, y=315, w=319, h=117, depth=2, parent=1),
            node("android.widget.FrameLayout", clickable=True,
                 id="com.example:id/header_view",
                 x=540, y=478, w=1080, h=154),
            node("android.widget.TextView", text="Search settings",
                 x=600, y=478, w=800, h=60, depth=1, parent=3),
        ]
        items = t.from_tree(nodes, screen=SCREEN)
        self.assertEqual([i["label"] for i in items], ["Search settings"])


class TextTargets(unittest.TestCase):
    def test_ocr_boxes_become_text_targets(self):
        items = t.from_text(
            [{"text": "Sign in", "x": 500, "y": 500, "w": 100, "h": 30,
              "source": "pixels"}], screen=SCREEN)
        self.assertEqual(items[0]["kind"], "text")
        self.assertEqual(items[0]["label"], "Sign in")
        self.assertEqual(items[0]["label_source"], "ocr")
        self.assertEqual(items[0]["source"], "pixels")

    def test_status_and_home_bands_are_optional(self):
        screen = {"x": 0, "y": 0, "w": 1080, "h": 1000}
        boxes = [
            {"text": "clock", "x": 500, "y": 10, "w": 100, "h": 20},
            {"text": "content", "x": 500, "y": 500, "w": 100, "h": 20},
            {"text": "home", "x": 500, "y": 990, "w": 100, "h": 20},
        ]
        self.assertEqual([i["label"] for i in t.from_text(boxes, screen=screen)],
                         ["content"])
        self.assertEqual(
            len(t.from_text(boxes, include_system=True, screen=screen)), 3)


class Fingerprints(unittest.TestCase):
    def test_mark_id_is_stable_until_a_target_moves(self):
        items = t.from_tree(
            [node("android.widget.Button", text="Save", clickable=True)],
            screen=SCREEN)
        first = t.mark_id("com.example", "portrait", (1080, 2400),
                          t.coverage(items), items)
        again = t.mark_id("com.example", "portrait", (1080, 2400),
                          t.coverage(items), items)
        self.assertEqual(first, again)
        items[0]["x"] += 1
        moved = t.mark_id("com.example", "portrait", (1080, 2400),
                          t.coverage(items), items)
        self.assertNotEqual(first, moved)


if __name__ == "__main__":
    unittest.main()
