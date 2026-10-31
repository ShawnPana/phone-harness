import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from phone_harness import config, telemetry


class ConfigSetBooleanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_dir = Path(self.tmp.name)
        self.config_file = self.config_dir / "config.json"
        patcher = patch.object(config, "config_dir", return_value=self.config_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_set_telemetry_off_coerces_to_boolean_false(self):
        for off_val in ("off", "no", "false", "0"):
            with self.subTest(value=off_val):
                res = config.set("telemetry", off_val)
                self.assertIs(res, False)
                self.assertIs(config.get("telemetry"), False)
                self.assertFalse(telemetry.is_enabled())
                data = json.loads(self.config_file.read_text(encoding="utf-8"))
                self.assertIs(data["telemetry"], False)

    def test_set_telemetry_on_coerces_to_boolean_true(self):
        for on_val in ("on", "yes", "true", "1"):
            with self.subTest(value=on_val):
                res = config.set("telemetry", on_val)
                self.assertIs(res, True)
                self.assertIs(config.get("telemetry"), True)
                self.assertTrue(telemetry.is_enabled())
                data = json.loads(self.config_file.read_text(encoding="utf-8"))
                self.assertIs(data["telemetry"], True)

    def test_lookup_coerces_stale_string_in_config_file(self):
        self.config_file.write_text(json.dumps({"telemetry": "off"}), encoding="utf-8")
        val, source = config.lookup("telemetry")
        self.assertIs(val, False)
        self.assertEqual(source, "file")
        self.assertFalse(telemetry.is_enabled())

    def test_cli_set_telemetry_prints_boolean(self):
        out = []
        with patch("builtins.print", side_effect=out.append):
            code = config.cli(["set", "telemetry", "off"])
        self.assertEqual(code, 0)
        self.assertEqual(out, ["telemetry = false"])
        self.assertIs(config.get("telemetry"), False)


if __name__ == "__main__":
    unittest.main()
