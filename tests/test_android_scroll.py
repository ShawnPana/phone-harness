import os
import unittest
from contextlib import ExitStack
from unittest.mock import patch

with patch.dict(os.environ, {"PHONE_HARNESS_PLATFORM": "android"}):
    from phone_harness import helpers
from phone_harness.android import Android


class AndroidScrollTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.phone = Android()
        bounds = {"x": 0, "y": 0, "w": 1000, "h": 2000, "id": "test-device"}
        stack.enter_context(patch.object(helpers, "phone", self.phone))
        stack.enter_context(patch.object(self.phone, "_screen_require", return_value=bounds))
        stack.enter_context(patch.object(self.phone, "_gate"))
        self.shell = stack.enter_context(patch.object(self.phone, "_sh"))
        self.text = stack.enter_context(patch.object(self.phone, "_screen_text"))
        stack.enter_context(patch.object(helpers.time, "sleep"))

    def test_public_scroll_sends_a_drag_in_each_direction(self):
        expected = {
            "down": "input swipe 500 1000 500 500 300",
            "up": "input swipe 500 1000 500 1500 300",
            "right": "input swipe 500 1000 250 1000 300",
            "left": "input swipe 500 1000 750 1000 300",
        }
        for direction, command in expected.items():
            with self.subTest(direction=direction):
                self.shell.reset_mock()
                helpers.scroll(direction, amount=0.25)
                self.shell.assert_called_once_with(command)

    def test_scroll_screen_sends_each_direction_and_preserves_text_sets(self):
        before = [{"text": "First", "y": 500, "confidence": 1.0}]
        after = [{"text": "Second", "y": 500, "confidence": 1.0}]
        expected = {
            "down": "input swipe 500 1000 500 500 500",
            "up": "input swipe 500 1000 500 1500 500",
            "right": "input swipe 500 1000 250 1000 500",
            "left": "input swipe 500 1000 750 1000 500",
        }
        for direction, command in expected.items():
            for settled in (after, before):
                with self.subTest(direction=direction, settled=settled):
                    self.shell.reset_mock()
                    self.text.side_effect = [before, settled]
                    result = helpers.scroll_screen(direction, amount=0.25, settle=0)
                    self.shell.assert_called_once_with(command)
                    self.assertEqual(result, {
                        "before": frozenset({"First"}),
                        "after": frozenset(box["text"] for box in settled),
                        "boxes": settled,
                    })

    def test_scroll_until_finds_content_after_android_scroll(self):
        before = [{"text": "First", "y": 500, "confidence": 1.0}]
        after = [{"text": "Target", "y": 500, "confidence": 1.0}]
        self.text.side_effect = [before, before, after]
        result = helpers.scroll_until(
            lambda boxes: next((b for b in boxes if b["text"] == "Target"), None),
            amount=0.25, max_scrolls=2, settle=0,
        )
        self.assertEqual(result, after[0])
        self.shell.assert_called_once_with("input swipe 500 1000 500 500 500")

    def test_scroll_collect_deduplicates_and_stops_at_end(self):
        first = {"text": "First", "y": 500, "confidence": 1.0}
        second = {"text": "Second", "y": 700, "confidence": 1.0}
        self.text.side_effect = [[first], [first], [first, second],
                                 [first, second], [first, second]]
        result = helpers.scroll_collect(amount=0.25, max_scrolls=3,
                                        end_after=1, settle=0)
        self.assertEqual(result, {"items": ["First", "Second"],
                                  "stop": "reached-end", "scrolls": 2})
        self.assertEqual(self.shell.call_count, 2)
        for call in self.shell.call_args_list:
            self.assertEqual(call.args, ("input swipe 500 1000 500 500 500",))

    def test_transport_scroll_accepts_full_vocabulary_and_optional_dx(self):
        for deltas, command in (
            ({"dx": -40, "dy": 30}, "input swipe 100 200 60 230 600"),
            ({"dy": 30}, "input swipe 100 200 100 230 600"),
        ):
            with self.subTest(deltas=deltas):
                self.shell.reset_mock()
                self.phone.send("input.scroll", x=100, y=200, steps=12, **deltas)
                self.shell.assert_called_once_with(command)

    def test_raw_vertical_scroll_keeps_existing_positional_arguments(self):
        self.phone._input_scroll(100, 200, 30, 2)
        self.shell.assert_called_once_with("input swipe 100 200 100 230 150")


if __name__ == "__main__":
    unittest.main()
