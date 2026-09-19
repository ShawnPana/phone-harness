"""Android backend behaviour that needs no phone: the commands it would send."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from phone_harness.android import Android  # noqa: E402


class Recorded(Android):
    """An Android whose adb shell is a list."""

    def __init__(self, installed=(), fail_motionevent=False):
        super().__init__()
        self.sent, self.installed, self.fail_motionevent = [], list(installed), fail_motionevent
        self._bounds = {"x": 0, "y": 0, "w": 720, "h": 1280, "id": "fake"}

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
