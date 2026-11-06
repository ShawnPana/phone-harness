import sys
import unittest
from pathlib import Path
from unittest import mock

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from phone_harness import helpers  # noqa: E402


class SwipeValidationTest(unittest.TestCase):
    def test_invalid_direction_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            helpers.swipe("diagonal")
        self.assertIn("direction must be one of", str(ctx.exception))
        self.assertIn("'diagonal'", str(ctx.exception))

    def test_non_positive_distance_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            helpers.swipe("up", distance=0)
        self.assertIn("distance must be positive", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            helpers.swipe("left", distance=-0.5)
        self.assertIn("distance must be positive", str(ctx.exception))

    @mock.patch("phone_harness.helpers.drag")
    @mock.patch("phone_harness.helpers._win")
    def test_valid_swipe_calls_drag(self, mock_win, mock_drag):
        mock_win.return_value = {"x": 100, "y": 200, "w": 400, "h": 800}
        helpers.swipe("up", distance=0.25)
        mock_drag.assert_called_once()
        args, kwargs = mock_drag.call_args
        # center is (100 + 200, 200 + 400) = (300, 600)
        # dy = -1 * 800 * 0.25 = -200
        # y1 = 600 - (-100) = 700, y2 = 600 + (-100) = 500
        self.assertAlmostEqual(args[0], 300)
        self.assertAlmostEqual(args[1], 700)
        self.assertAlmostEqual(args[2], 300)
        self.assertAlmostEqual(args[3], 500)


if __name__ == "__main__":
    unittest.main()
