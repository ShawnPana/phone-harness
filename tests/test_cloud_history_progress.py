"""Pagination may advance through empty pages, but cannot follow cursor cycles."""
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from phone_harness import cloud


class HistoryProgress(unittest.TestCase):
    def run_pages(self, pages, limit=20):
        calls = []
        def api(method, path):
            calls.append(path)
            if len(calls) > len(pages):
                raise AssertionError("pagination repeated an already visited cursor")
            return pages[len(calls) - 1]
        output = io.StringIO()
        with patch.object(cloud, "_api", side_effect=api), redirect_stdout(output):
            result = cloud._history(["--json", "-n", str(limit)])
        return result, calls, json.loads(output.getvalue())

    def test_self_cycle_raises_without_requesting_page_again(self):
        with self.assertRaisesRegex(SystemExit, "cursor"):
            self.run_pages([{"items": [], "next_cursor": "a"}, {"items": [], "next_cursor": "a"}])

    def test_multi_cursor_cycle_raises(self):
        with self.assertRaisesRegex(SystemExit, "cursor"):
            self.run_pages([{"items": [], "next_cursor": "a"}, {"items": [], "next_cursor": "b"}, {"items": [], "next_cursor": "a"}])

    def test_advancing_empty_pages_can_reach_records(self):
        pages = [{"items": [], "next_cursor": str(i)} for i in range(25)]
        pages.append({"items": [{"sid": "record"}], "next_cursor": None})
        result, calls, items = self.run_pages(pages)
        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 26)
        self.assertEqual(items, [{"sid": "record"}])

    def test_normal_pages_and_requested_limit_are_preserved(self):
        result, calls, items = self.run_pages([{"items": [{"sid": "one"}], "next_cursor": "a"},
                                              {"items": [{"sid": "two"}], "next_cursor": "b"}], limit=2)
        self.assertEqual(items, [{"sid": "one"}, {"sid": "two"}])
        self.assertEqual(len(calls), 2)
        self.assertIn("limit=1", calls[1])

    def test_empty_final_page_terminates(self):
        result, calls, items = self.run_pages([{"items": [], "next_cursor": None}])
        self.assertEqual((result, len(calls), items), (0, 1, []))

    def test_satisfied_limit_does_not_need_to_follow_next_cursor(self):
        result, calls, items = self.run_pages([
            {"items": [], "next_cursor": "a"},
            {"items": [{"sid": "record"}], "next_cursor": "a"},
        ], limit=1)
        self.assertEqual((result, len(calls), items), (0, 2, [{"sid": "record"}]))


if __name__ == "__main__":
    unittest.main()
