"""Android backend behaviour that needs no phone: the commands it would send."""
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from phone_harness import android as android_mod  # noqa: E402
from phone_harness.android import Android, TreeUnavailable  # noqa: E402


class Recorded(Android):
    """An Android whose adb shell is a list."""

    def __init__(self, installed=(), fail_motionevent=False, dumps=()):
        super().__init__()
        self.sent, self.installed, self.fail_motionevent = [], list(installed), fail_motionevent
        self.dumps, self.dump_calls, self.shots = list(dumps), 0, 0
        self._bounds = {"x": 0, "y": 0, "w": 720, "h": 1280, "id": "fake"}
        self._resolved = True

    def _screen_require(self):
        return self._bounds

    def _adb(self, *args, binary=False, timeout=60):
        if "uiautomator" in args:
            self.dump_calls += 1
            return self.dumps.pop(0) if self.dumps else b"ERROR: could not get idle state."
        if "screencap" in args:
            self.shots += 1
            return b"\x89PNG-fake"
        raise AssertionError(args)

    def _pixel_text(self, min_confidence=0.3):
        self.shots += 1
        return [{"text": "Device name", "confidence": 0.9, "source": "pixels",
                 "x": 160, "y": 570, "w": 220, "h": 40}]

    def _gate(self):
        pass

    def _sh(self, cmd, timeout=60):
        self.sent.append(cmd)
        if self.fail_motionevent and "motionevent" in cmd:
            raise RuntimeError("Error: Unknown command: motionevent")
        if cmd.startswith("pm list packages"):
            return "\n".join(f"package:{p}" for p in self.installed)
        if cmd.startswith("cmd package resolve-activity"):
            return f"priority=0\n{cmd.split()[-1]}/.Main"
        return ""


class Scroll(unittest.TestCase):
    def test_a_scroll_holds_still_before_lifting_so_it_cannot_fling(self):
        phone = Recorded()
        phone._input_scroll(360, 640, dy=-768, dx=0, steps=10)
        (cmd,) = phone.sent
        self.assertTrue(cmd.startswith("input motionevent DOWN 360 1024"), cmd)
        self.assertIn("sleep 0.2 && input motionevent UP 360 256", cmd)
        self.assertNotIn("input swipe", cmd)

    def test_it_goes_sideways_and_stays_on_the_glass(self):
        phone = Recorded()
        phone._input_scroll(360, 640, dy=0, dx=-2000)
        (cmd,) = phone.sent
        self.assertTrue(cmd.startswith("input motionevent DOWN 676 640"), cmd)   # 94% of 720
        self.assertTrue(cmd.endswith("UP 43 640"), cmd)                          # 6% of 720

    def test_an_old_android_falls_back_to_a_slow_swipe_and_remembers(self):
        phone = Recorded(fail_motionevent=True)
        phone._input_scroll(360, 640, dy=-400)
        phone._input_scroll(360, 640, dy=-400)
        swipes = [c for c in phone.sent if c.startswith("input swipe")]
        self.assertEqual(len(swipes), 2)
        self.assertGreaterEqual(int(swipes[0].split()[-1]), 600)
        self.assertEqual(sum("motionevent" in c for c in phone.sent), 1)         # asked once


TREE = (b'<?xml version="1.0"?><hierarchy><node text="Apps" content-desc="" resource-id="" '
        b'class="android.widget.TextView" clickable="true" bounds="[100,900][300,960]"/></hierarchy>')


class Tree(unittest.TestCase):
    def test_a_never_idle_screen_is_asked_once_then_read_from_pixels(self):
        phone = Recorded()
        with unittest.mock.patch.object(android_mod, "_VISION", True):
            rows = phone._screen_text()
        self.assertEqual(phone.dump_calls, 1)                     # not five
        self.assertEqual([r["source"] for r in rows], ["pixels"])
        self.assertEqual(rows[0]["text"], "Device name")
        with unittest.mock.patch.object(android_mod, "_VISION", True):
            phone._screen_text()                                  # within the busy window:
        self.assertEqual(phone.dump_calls, 1)                     # ...no second dump at all

    def test_without_vision_it_fails_at_once_with_a_plain_message(self):
        phone = Recorded()
        with unittest.mock.patch.object(android_mod, "_VISION", False):
            with self.assertRaises(TreeUnavailable) as cm:
                phone._screen_text()
        self.assertEqual(phone.dump_calls, 1)
        self.assertIn("screenshot()", str(cm.exception))
        self.assertNotIn("animates", str(cm.exception))

    def test_a_good_tree_is_preferred_and_clears_the_busy_window(self):
        phone = Recorded(dumps=[TREE])
        phone._tree_busy_until = 0.0
        rows = phone._screen_text()
        self.assertEqual([(r["text"], r["source"], r["x"], r["y"]) for r in rows],
                         [("Apps", "tree", 200, 930)])
        self.assertEqual(phone.shots, 0)

    def test_a_transient_failure_is_retried_briefly(self):
        phone = Recorded(dumps=[b"garbage", TREE])
        with unittest.mock.patch.object(android_mod.time, "sleep"):
            rows = phone._screen_text()
        self.assertEqual(phone.dump_calls, 2)
        self.assertEqual(rows[0]["text"], "Apps")


class Launch(unittest.TestCase):
    INSTALLED = ["com.android.chrome", "com.android.chrome.helper", "com.facebook.orca",
                 "com.facebook.katana", "com.zhiliaoapp.musically", "com.whatsapp"]

    def launched(self, name):
        phone = Recorded(self.INSTALLED)
        return phone._apps_launch(name), phone.sent[-1]

    def test_names_people_say(self):
        for name, pkg in [("TikTok", "com.zhiliaoapp.musically"), ("tik tok", "com.zhiliaoapp.musically"),
                          ("Facebook", "com.facebook.katana"), ("Messenger", "com.facebook.orca"),
                          ("chrome", "com.android.chrome"), ("WhatsApp", "com.whatsapp"),
                          ("com.whatsapp", "com.whatsapp")]:
            with self.subTest(name=name):
                got, last = self.launched(name)
                self.assertEqual(got, pkg)
                self.assertEqual(last, f"am start -W -n {pkg}/.Main")


if __name__ == "__main__":
    unittest.main()
