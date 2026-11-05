"""Mark rendering: badges land on the target boxes and the cap is reported."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from PIL import Image  # noqa: E402
from phone_harness import marks  # noqa: E402


class Marks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "screen.png")
        Image.new("RGBA", (200, 400), (255, 255, 255, 255)).save(self.path)
        self.window = {"x": 0, "y": 0, "w": 200, "h": 400}
        self.targets = [
            {"id": 1, "x": 100, "y": 100, "w": 40, "h": 40,
             "kind": "icon", "label": "Search"},
            {"id": 2, "x": 100, "y": 300, "w": 40, "h": 40,
             "kind": "text", "label": "Other"},
        ]

    def test_it_writes_a_png_and_reports_the_size(self):
        out, size, omitted = marks.draw_marks(
            self.path, self.targets, self.window)
        self.addCleanup(os.unlink, out)
        self.assertTrue(Path(out).is_file())
        self.assertEqual(size, (200, 400))
        self.assertEqual(omitted, 0)
        pixel = Image.open(out).convert("RGB").getpixel((75, 75))
        self.assertGreater(pixel[0], 150)
        self.assertLess(pixel[1], 100)

    def test_the_grid_is_optional(self):
        plain, _, _ = marks.draw_marks(self.path, self.targets, self.window)
        grid, _, _ = marks.draw_marks(
            self.path, self.targets, self.window, grid=True)
        self.addCleanup(os.unlink, plain)
        self.addCleanup(os.unlink, grid)
        self.assertNotEqual(Path(plain).read_bytes(), Path(grid).read_bytes())
        pixel = Image.open(grid).convert("RGB").getpixel((50, 200))
        self.assertGreater(pixel[2], pixel[0])

    def test_max_marks_caps_drawn_badges(self):
        out, _, omitted = marks.draw_marks(
            self.path, self.targets, self.window, max_marks=1)
        self.addCleanup(os.unlink, out)
        self.assertEqual(omitted, 1)

    def test_an_explicit_output_path_is_used(self):
        out_path = str(Path(self.tmp.name) / "marked.png")
        out, _, _ = marks.draw_marks(
            self.path, self.targets, self.window, out_path=out_path)
        self.assertEqual(out, out_path)
        self.assertTrue(Path(out_path).is_file())


if __name__ == "__main__":
    unittest.main()
