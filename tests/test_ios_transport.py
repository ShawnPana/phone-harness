import importlib
import os
import unittest
from unittest import mock

from phone_harness import ios


class IOSTransportSelectionTests(unittest.TestCase):
    def test_background_import_failure_does_not_select_focused_backend(self):
        failure = OSError("missing private symbol")

        with mock.patch.dict(os.environ, {"PHONE_HARNESS_BACKGROUND": "1"}), \
                mock.patch.object(importlib, "import_module", side_effect=failure):
            with self.assertRaisesRegex(RuntimeError, "focus-free iPhone input"):
                ios._load_transport()

    def test_classic_backend_remains_an_explicit_opt_in(self):
        classic = object()

        with mock.patch.dict(os.environ, {"PHONE_HARNESS_BACKGROUND": "0"}), \
                mock.patch.object(importlib, "import_module", return_value=classic) as load:
            self.assertEqual((classic, False), ios._load_transport())

        load.assert_called_once_with(".mirror", ios.__package__)


if __name__ == "__main__":
    unittest.main()
